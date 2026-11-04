"""Partner order parsing, validation, SQLite storage and read queries.

Only the read-only partner reporting API is used. The schema is intentionally
identical to the original PS3838 order store, so an existing
``partner_orders.sqlite3`` can be imported without a migration.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

SOURCE = "ps38ag"
BEIJING = timezone(timedelta(hours=8))
EVENT_LIMIT = 5000


class PollError(Exception):
    """Base error for an unusable upstream response."""


class IncompletePage(PollError):
    """The upstream page cannot be trusted; the round must not be committed."""


class SessionExpired(PollError):
    """The browser session is not authenticated any more."""


def now_iso() -> str:
    return datetime.now(BEIJING).isoformat(timespec="milliseconds")


def beijing_iso(epoch_ms: Optional[int]) -> Optional[str]:
    if epoch_ms is None:
        return None
    return datetime.fromtimestamp(epoch_ms / 1000, BEIJING).isoformat(timespec="milliseconds")


def _number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ── market metadata ────────────────────────────────────────────────────────

def is_corner_order(row: Dict[str, Any]) -> bool:
    raw = row.get("raw_json") or row
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = {}
    choices = raw.get("choices") if isinstance(raw, dict) else None
    choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    for value in (row.get("is_corner"), row.get("corner_event_unit"),
                  row.get("cornerEventUnit"), choice.get("cornerEventUnit")):
        if value is True or str(value).strip().lower() in {"1", "true"}:
            return True
    labels = [row.get(key) for key in ("units", "market", "league", "teams", "betType")]
    labels.extend(choice.get(key) for key in ("units", "leagueName", "eventName"))
    return bool(re.search(r"角球|\bcorners?\b", " ".join(str(value or "") for value in labels), re.I))


def with_market_metadata(row: Dict[str, Any]) -> Dict[str, Any]:
    corner = is_corner_order(row)
    return {**row, "is_corner": corner, "market_scope": "CORNER" if corner else "GOALS"}


# ── upstream row parsing ───────────────────────────────────────────────────

def _member_identity(row: Dict[str, Any]) -> Tuple[str, str]:
    code = next((row.get(key) for key in
                 ("userCode", "playerUserCode", "memberUserCode") if row.get(key)), None)
    if code:
        return f"code:{code}", str(code)
    user_id = next((row.get(key) for key in
                    ("playerUserId", "memberUserId") if row.get(key)), None)
    if user_id is None:
        raise IncompletePage("有效订单缺少会员身份，拒绝猜测")
    return f"id:{user_id}", str(user_id)


def order_identity(row: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    order_id = next((row.get(key) for key in
                     ("playerWagerId", "orderNumber", "originalWagerId")
                     if row.get(key) not in (None, "", 0, "0")), None)
    if order_id is None:
        return None  # Aggregate footer rows carry no order id.
    member_identity, _ = _member_identity(row)
    return member_identity, str(order_id)


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or value is None or str(value) != str(int(value)):
        raise IncompletePage(f"分页字段 {field} 无效")
    return int(value)


def parse_page(response: Dict[str, Any], page: int, page_size: int,
               status: str) -> Tuple[List[Dict[str, Any]], Tuple[int, int, int]]:
    """Validate one upstream page and return its usable rows plus paging meta."""
    if response.get("httpStatus") in (401, 403):
        raise SessionExpired(f"后台会话已失效（HTTP {response.get('httpStatus')}）")
    if response.get("httpStatus") != 200:
        raise PollError(f"订单接口 HTTP {response.get('httpStatus')}")
    payload = response.get("json") or {}
    body = payload.get("body") if isinstance(payload, dict) else None
    if not isinstance(body, dict):
        raise IncompletePage("注单响应缺少 body；本轮不提交")
    data, paging = body.get("data"), body.get("paging")
    if not isinstance(data, list) or not isinstance(paging, dict):
        raise IncompletePage("注单响应缺少 data/paging；本轮不提交")

    total = _integer(paging.get("totalItems"), "totalItems")
    pages = _integer(paging.get("totalPages"), "totalPages")
    size = _integer(paging.get("rowsPerPage"), "rowsPerPage")
    begin = _integer(paging.get("beginIndex"), "beginIndex")
    pages_valid = pages in (0, 1) if total == 0 else pages == math.ceil(total / size)
    if (total < 0 or size != page_size or pages < 0 or pages > 1000
            or not pages_valid or begin != (page - 1) * size):
        raise IncompletePage("分页总数、页码或页大小不一致；本轮不提交")
    if "pageNumber" in paging and paging["pageNumber"] != page:
        raise IncompletePage("返回页码与请求不一致；本轮不提交")

    valid: List[Dict[str, Any]] = []
    seen = set()
    for row in data:
        if not isinstance(row, dict):
            raise IncompletePage("注单行格式错误")
        identity = order_identity(row)
        if identity is None:
            continue
        if identity in seen:
            raise IncompletePage("分页存在重复订单；本轮不提交")
        seen.add(identity)
        actual = str(row.get("status") or status).upper()
        if actual not in ("OPEN", "SETTLED"):
            raise IncompletePage("订单状态无法识别")
        valid.append(dict(row, status=actual))

    expected = min(size, max(0, total - (page - 1) * size))
    if len(valid) != expected:
        raise IncompletePage("订单有效行数与分页总数不一致；本轮不提交")
    return valid, (total, pages, size)


SELECTION_LABELS = {
    "OVER": "大", "UNDER": "小", "HOME": "主", "AWAY": "客", "DRAW": "和",
    "YES": "是", "NO": "否", "ODD": "单", "EVEN": "双",
}

BET_TYPE_LABELS = {
    "OU": "大小", "AH": "让球", "HDP": "让球", "1X2": "独赢", "EH": "欧赔",
    "DC": "双重机会", "HT_OU": "半场大小", "HT_AH": "半场让球", "CS": "正确比分",
    "TG": "总进球", "OR": "其他",
}

RESULT_ALIASES = {
    "WIN": "WIN", "LOSE": "LOSE", "WIN_HALF": "WIN_HALF", "LOSE_HALF": "LOSE_HALF",
    "DRAW": "DRAW", "PUSH": "DRAW", "REFUNDED": "REFUNDED", "VOID": "REFUNDED",
}


def _selection_label(selection: str, name: str) -> str:
    if name:
        return name
    upper = selection.upper()
    return SELECTION_LABELS.get(upper, selection)


def _bet_type_label(value: str) -> str:
    upper = value.upper()
    return BET_TYPE_LABELS.get(upper, value)


def result_of(row: Dict[str, Any]) -> str:
    """Normalise the upstream settlement result to a stable enum."""
    english = str(row.get("wagerStatus") or "").strip().upper()
    if english in RESULT_ALIASES:
        return RESULT_ALIASES[english]
    text = str(row.get("wagerStatusTranslated") or "")
    if "贏半" in text or "赢半" in text:
        return "WIN_HALF"
    if "輸半" in text or "输半" in text:
        return "LOSE_HALF"
    if "退还" in text or "退回" in text:
        return "REFUNDED"
    if "胜" in text:
        return "WIN"
    if "负" in text:
        return "LOSE"
    if "平" in text:
        return "DRAW"
    return "OPEN" if str(row.get("status") or "").upper() == "OPEN" else ""


def win_loss_of(row: Dict[str, Any], result: str) -> Optional[float]:
    """Net profit/loss in the order currency, derived from upstream amounts."""
    stake = _number(row.get("stake")) or 0.0
    to_win = _number(row.get("toWin")) or 0.0
    if result == "WIN":
        return to_win
    if result == "LOSE":
        return -stake
    if result == "WIN_HALF":
        return to_win / 2
    if result == "LOSE_HALF":
        return -stake / 2
    if result in ("DRAW", "REFUNDED"):
        return 0.0
    return None


def parse_order(row: Dict[str, Any], agent_code: str) -> Dict[str, Any]:
    member_identity, member_code = _member_identity(row)
    order_id = order_identity(row)[1]
    choices = row.get("choices")
    first = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}

    handicap = str(first.get("handicap") or "").strip()
    bet_type = _bet_type_label(str(first.get("btName") or "").strip())
    market = " ".join(part for part in (bet_type, handicap) if part)
    selection = _selection_label(str(first.get("selection") or "").strip(),
                                 str(first.get("selectionName") or "").strip())

    created_ms = row.get("createdDate")
    created_ms = int(created_ms) if isinstance(created_ms, (int, float)) and not isinstance(created_ms, bool) else None
    settled_ms = row.get("settlementDateTime")
    settled_ms = int(settled_ms) if isinstance(settled_ms, (int, float)) and not isinstance(settled_ms, bool) else None

    result = result_of(row)
    win_loss = win_loss_of(row, result)

    raw = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "source": SOURCE,
        "agent_code": agent_code,
        "member_identity": member_identity,
        "member_user_code": member_code,
        "member_name": str(row.get("firstName") or row.get("name") or row.get("nickname") or "").strip(),
        "order_id": order_id,
        "status": str(row.get("status") or "").upper(),
        "inplay": 1 if first.get("inplay") else 0,
        "sport": str(first.get("sportName") or ""),
        "league": str(first.get("leagueName") or ""),
        "teams": str(first.get("eventName") or ""),
        "market": market,
        "selection": selection,
        "handicap": handicap,
        "odds": _number(row.get("odds") if row.get("odds") is not None else first.get("odds")),
        "stake": _number(row.get("stake")),
        "to_win": _number(row.get("toWin")),
        "currency": str(row.get("currencyCode") or ""),
        "wager_status": str(row.get("wagerStatusTranslated") or ""),
        "result": result,
        "win_loss": win_loss,
        "created_ms": created_ms,
        "created_at": beijing_iso(created_ms),
        "settled_at": beijing_iso(settled_ms),
        "content_hash": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "raw_json": raw,
        "is_corner": is_corner_order({"raw_json": raw}),
    }


ORDER_COLUMNS = (
    "source", "agent_code", "member_identity", "member_user_code", "member_name",
    "order_id", "status", "inplay", "sport", "league", "teams", "market", "selection",
    "handicap", "odds", "stake", "to_win", "currency", "wager_status", "result", "win_loss",
    "created_ms", "created_at", "settled_at", "content_hash", "raw_json",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS partner_orders (
    source TEXT NOT NULL,
    agent_code TEXT NOT NULL,
    member_identity TEXT NOT NULL,
    member_user_code TEXT,
    member_name TEXT,
    order_id TEXT NOT NULL,
    status TEXT NOT NULL,
    inplay INTEGER NOT NULL DEFAULT 0,
    sport TEXT,
    league TEXT,
    teams TEXT,
    market TEXT,
    selection TEXT,
    handicap TEXT,
    odds REAL,
    stake REAL,
    to_win REAL,
    currency TEXT,
    wager_status TEXT,
    result TEXT,
    win_loss REAL,
    created_ms INTEGER,
    created_at TEXT,
    settled_at TEXT,
    content_hash TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    status_changed_at TEXT NOT NULL,
    PRIMARY KEY (source, member_identity, order_id)
);
CREATE INDEX IF NOT EXISTS partner_orders_status ON partner_orders(status, created_ms);
CREATE INDEX IF NOT EXISTS partner_orders_agent ON partner_orders(agent_code, status);
CREATE INDEX IF NOT EXISTS partner_orders_created ON partner_orders(created_ms);
CREATE INDEX IF NOT EXISTS partner_orders_member ON partner_orders(member_user_code);

CREATE TABLE IF NOT EXISTS partner_order_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    agent_code TEXT NOT NULL,
    member_identity TEXT NOT NULL,
    order_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    UNIQUE(source, member_identity, order_id)
);
CREATE INDEX IF NOT EXISTS partner_events_cursor ON partner_order_events(event_id);

CREATE TABLE IF NOT EXISTS partner_poll_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    agent_code TEXT NOT NULL,
    snapshot_type TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    elapsed_seconds REAL NOT NULL,
    request_count INTEGER NOT NULL,
    record_count INTEGER NOT NULL,
    new_count INTEGER NOT NULL,
    changed_count INTEGER NOT NULL,
    event_count INTEGER NOT NULL,
    baseline INTEGER NOT NULL,
    complete INTEGER NOT NULL,
    error_code TEXT
);

CREATE TABLE IF NOT EXISTS partner_poll_state (
    source TEXT NOT NULL,
    agent_code TEXT NOT NULL,
    snapshot_type TEXT NOT NULL,
    last_success_at TEXT NOT NULL,
    record_count INTEGER NOT NULL,
    pruned_event_id INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source, agent_code, snapshot_type)
);

CREATE TABLE IF NOT EXISTS partner_agents (
    code TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    level TEXT,
    name TEXT,
    resolved_at TEXT NOT NULL
);
"""

ORDER_SELECT = (
    "agent_code, member_user_code, member_name, order_id, status, inplay, "
    "sport, league, teams, market, selection, handicap, odds, stake, to_win, "
    "currency, wager_status, result, win_loss, created_ms, created_at, settled_at"
)

PERIOD_COLUMNS = (
    "json_extract(raw_json,'$.choices[0].periodName') AS period_name, "
    "json_extract(raw_json,'$.choices[0].periodType') AS period_type, "
    "json_extract(raw_json,'$.choices[0].cornerEventUnit') AS corner_event_unit, "
    "json_extract(raw_json,'$.choices[0].units') AS units"
)


def query_orders(conn: sqlite3.Connection, status: Optional[str] = "OPEN",
                 agent: Optional[str] = None, search: str = "",
                 limit: int = 50, offset: int = 0,
                 from_ms: Optional[int] = None,
                 to_ms: Optional[int] = None,
                 basis: str = "created",
                 from_utc: Optional[str] = None,
                 to_utc: Optional[str] = None) -> Dict[str, Any]:
    clauses = ["source=?"]
    params: List[Any] = [SOURCE]
    if status and str(status).upper() != "ALL":
        clauses.append("status=?")
        params.append(str(status).upper())
    if agent:
        clauses.append("agent_code=?")
        params.append(agent)
    if basis == "settled":
        clauses.append("settled_at IS NOT NULL AND settled_at<>''")
        if from_utc:
            clauses.append("datetime(settled_at)>=?")
            params.append(str(from_utc))
        if to_utc:
            clauses.append("datetime(settled_at)<=?")
            params.append(str(to_utc))
        order_by = "settled_at DESC, created_ms DESC"
    else:
        if from_ms is not None:
            clauses.append("created_ms>=?")
            params.append(int(from_ms))
        if to_ms is not None:
            clauses.append("created_ms<=?")
            params.append(int(to_ms))
        order_by = "created_ms DESC"
    if search:
        like = f"%{search}%"
        clauses.append("(member_user_code LIKE ? OR member_name LIKE ? OR teams LIKE ? "
                       "OR league LIKE ? OR market LIKE ? OR order_id LIKE ? OR agent_code LIKE ?)")
        params.extend([like] * 7)
    where = " AND ".join(clauses)
    total = conn.execute(f"SELECT COUNT(*) FROM partner_orders WHERE {where}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT {ORDER_SELECT}, {PERIOD_COLUMNS} "
        f"FROM partner_orders WHERE {where} "
        f"ORDER BY {order_by} LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    return {"total": total, "rows": [with_market_metadata(dict(row)) for row in rows]}


def query_events(conn: sqlite3.Connection, cursor: Optional[int],
                 limit: int = 50) -> Dict[str, Any]:
    min_id = conn.execute(
        "SELECT COALESCE(MIN(event_id), 0) FROM partner_order_events"
    ).fetchone()[0]
    max_id = conn.execute(
        "SELECT COALESCE(MAX(event_id), 0) FROM partner_order_events"
    ).fetchone()[0]
    if cursor is None:
        return {"cursor": max_id, "reset": False, "events": []}
    if cursor < 0 or cursor > max_id:
        return {"cursor": max_id, "reset": True, "events": []}
    if min_id and cursor < int(min_id) - 1:
        return {
            "cursor": max_id,
            "reset": True,
            "gap": True,
            "requested_after": int(cursor),
            "pruned_through": int(min_id) - 1,
            "events": [],
        }
    rows = conn.execute(
        f"""SELECT e.event_id, e.agent_code, e.observed_at, e.member_identity, e.order_id,
                  o.member_user_code, o.member_name, o.status, o.league, o.teams, o.market,
                  o.selection, o.handicap, o.odds, o.stake, o.currency, o.wager_status,
                  o.created_ms, o.created_at, {PERIOD_COLUMNS}
            FROM partner_order_events e
            LEFT JOIN partner_orders o
              ON o.source=e.source AND o.member_identity=e.member_identity
             AND o.order_id=e.order_id
            WHERE e.event_id>? ORDER BY e.event_id LIMIT ?""",
        (cursor, limit),
    ).fetchall()
    events = [with_market_metadata(dict(row)) for row in rows]
    return {"cursor": events[-1]["event_id"] if events else cursor,
            "reset": False, "events": events}


def query_summary(conn: sqlite3.Connection, agent: Optional[str] = None,
                  from_ms: Optional[int] = None,
                  to_ms: Optional[int] = None,
                  basis: str = "created",
                  from_utc: Optional[str] = None,
                  to_utc: Optional[str] = None) -> Dict[str, Any]:
    """Aggregate stake and settlement outcome over the same filter scope."""
    clauses = ["source=?"]
    params: List[Any] = [SOURCE]
    if agent:
        clauses.append("agent_code=?")
        params.append(agent)
    if basis == "settled":
        clauses.append("settled_at IS NOT NULL AND settled_at<>''")
        if from_utc:
            clauses.append("datetime(settled_at)>=?")
            params.append(str(from_utc))
        if to_utc:
            clauses.append("datetime(settled_at)<=?")
            params.append(str(to_utc))
    else:
        if from_ms is not None:
            clauses.append("created_ms>=?")
            params.append(int(from_ms))
        if to_ms is not None:
            clauses.append("created_ms<=?")
            params.append(int(to_ms))
    where = " AND ".join(clauses)

    def column(condition: str) -> str:
        return f"COALESCE(SUM(CASE WHEN {condition} THEN 1 ELSE 0 END),0)"

    totals = conn.execute(
        f"""SELECT COUNT(*) AS orders,
                   COALESCE(SUM(stake),0) AS total_stake,
                   COALESCE(SUM(CASE WHEN status='OPEN' THEN stake ELSE 0 END),0) AS open_stake,
                   COALESCE(SUM(CASE WHEN status='SETTLED' THEN stake ELSE 0 END),0) AS settled_stake,
                   COALESCE(SUM(CASE WHEN win_loss>0 THEN win_loss ELSE 0 END),0) AS win_amount,
                   COALESCE(SUM(CASE WHEN win_loss<0 THEN -win_loss ELSE 0 END),0) AS lose_amount,
                   COALESCE(SUM(win_loss),0) AS net,
                   {column("status='OPEN'")} AS open_orders,
                   {column("status='SETTLED'")} AS settled_orders,
                   {column("result='WIN'")} AS win_count,
                   {column("result='LOSE'")} AS lose_count,
                   {column("result='WIN_HALF'")} AS win_half_count,
                   {column("result='LOSE_HALF'")} AS lose_half_count,
                   {column("result='DRAW'")} AS draw_count,
                   {column("result='REFUNDED'")} AS refunded_count
            FROM partner_orders WHERE {where}""",
        params,
    ).fetchone()

    agents = conn.execute(
        f"""SELECT agent_code, COUNT(*) AS orders,
                   COALESCE(SUM(stake),0) AS stake,
                   COALESCE(SUM(CASE WHEN status='OPEN' THEN stake ELSE 0 END),0) AS open_stake,
                   COALESCE(SUM(win_loss),0) AS net,
                   {column("result='WIN'")} AS win_count,
                   {column("result='LOSE'")} AS lose_count
            FROM partner_orders WHERE {where}
            GROUP BY agent_code ORDER BY agent_code""",
        params,
    ).fetchall()

    currency_row = conn.execute(
        f"SELECT currency FROM partner_orders WHERE {where} AND currency<>'' "
        "GROUP BY currency ORDER BY COUNT(*) DESC LIMIT 1",
        params,
    ).fetchone()
    states = conn.execute(
        "SELECT agent_code, snapshot_type, last_success_at, record_count "
        "FROM partner_poll_state ORDER BY agent_code, snapshot_type"
    ).fetchall()

    payload = {key: totals[key] for key in totals.keys()}
    payload["currency"] = currency_row[0] if currency_row else ""
    payload["open"] = payload.pop("open_orders")
    payload["settled"] = payload.pop("settled_orders")
    payload["agents"] = [dict(row) for row in agents]
    payload["states"] = [dict(row) for row in states]
    return payload


# Fixture identity is the event start time (kickoff), so a rematch between the
# same two teams on another date is not merged into the same row.
FIXTURE_EVENT_MS = (
    "COALESCE(CAST(json_extract(raw_json,'$.choices[0].eventDate') AS INTEGER), created_ms)"
)

FIXTURE_LINE_KEY = """
    CASE
        WHEN market LIKE 'HDP%' OR market LIKE '让球%'
            THEN 'HDP|' || selection || '|' || CAST(CAST(handicap AS REAL) AS TEXT)
        WHEN market LIKE '大小%' OR market LIKE 'OU%'
            THEN 'OU|' || selection || '|' || CAST(CAST(handicap AS REAL) AS TEXT)
        ELSE market || '|' || selection || '|' || COALESCE(CAST(CAST(handicap AS REAL) AS TEXT), '')
    END || '|' || COALESCE(CAST(period_type AS TEXT), '') || '|' ||
    CASE WHEN corner_event_unit=1 OR units IN ('角球','Corner','Corners','corner','corners') THEN 'CORNER' ELSE 'GOALS' END
"""


def count_live_fixtures(conn: sqlite3.Connection) -> int:
    """Distinct in-play fixtures that still have an open order."""
    try:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS live FROM (
                SELECT teams, {FIXTURE_EVENT_MS} AS ev
                FROM partner_orders
                WHERE source=? AND status='OPEN' AND inplay=1
                GROUP BY teams, ev
            )
            """,
            (SOURCE,),
        ).fetchone()
    except sqlite3.Error:
        return 0
    return int((row["live"] if row else 0) or 0)


def _order_scope(status, agent, search, from_ms, to_ms, basis,
                 from_utc, to_utc) -> Tuple[str, List[Any]]:
    """Shared WHERE clause for order-level and fixture-level queries."""
    clauses = ["source=?"]
    params: List[Any] = [SOURCE]
    if status and str(status).upper() != "ALL":
        clauses.append("status=?")
        params.append(str(status).upper())
    if agent:
        clauses.append("agent_code=?")
        params.append(agent)
    if search:
        like = f"%{search}%"
        clauses.append("(member_user_code LIKE ? OR member_name LIKE ? OR teams LIKE ? "
                       "OR league LIKE ? OR market LIKE ? OR order_id LIKE ? OR agent_code LIKE ?)")
        params.extend([like] * 7)
    if basis == "settled":
        clauses.append("settled_at IS NOT NULL AND settled_at<>''")
        if from_utc:
            clauses.append("datetime(settled_at)>=?")
            params.append(str(from_utc))
        if to_utc:
            clauses.append("datetime(settled_at)<=?")
            params.append(str(to_utc))
    else:
        if from_ms is not None:
            clauses.append("created_ms>=?")
            params.append(int(from_ms))
        if to_ms is not None:
            clauses.append("created_ms<=?")
            params.append(int(to_ms))
    return " AND ".join(clauses), params


def query_fixtures(conn: sqlite3.Connection, status: Optional[str] = "ALL",
                   agent: Optional[str] = None, search: str = "",
                   limit: int = 50, offset: int = 0,
                   from_ms: Optional[int] = None,
                   to_ms: Optional[int] = None,
                   basis: str = "created",
                   from_utc: Optional[str] = None,
                   to_utc: Optional[str] = None) -> Dict[str, Any]:
    """Aggregate orders per fixture: ticket count, members, lines, stake."""
    where, params = _order_scope(status, agent, search, from_ms, to_ms, basis,
                                 from_utc, to_utc)
    rows = conn.execute(
        f"""
        WITH o AS (
            SELECT teams, league, market, selection, handicap, odds, stake,
                   created_ms, member_identity, status,
                   json_extract(raw_json,'$.choices[0].periodType') AS period_type,
                   json_extract(raw_json,'$.choices[0].cornerEventUnit') AS corner_event_unit,
                   json_extract(raw_json,'$.choices[0].units') AS units,
                   {FIXTURE_EVENT_MS} AS ev
            FROM partner_orders WHERE {where}
        )
        SELECT teams, ev, MAX(league) AS league,
               COUNT(*) AS orders,
               COUNT(DISTINCT member_identity) AS members,
               COUNT(DISTINCT {FIXTURE_LINE_KEY}) AS lines,
               COALESCE(SUM(stake),0) AS stake,
               COALESCE(SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END),0) AS open_orders,
               COALESCE(SUM(CASE WHEN status='SETTLED' THEN 1 ELSE 0 END),0) AS settled_orders,
               MIN(created_ms) AS first_ms,
               MAX(created_ms) AS last_ms
        FROM o GROUP BY teams, ev
        """,
        params,
    ).fetchall()

    fixtures: List[Dict[str, Any]] = []
    for row in rows:
        fixtures.append({
            "teams": row["teams"],
            "event_ms": row["ev"],
            "league": row["league"] or "",
            "orders": row["orders"],
            "members": row["members"],
            "lines": row["lines"],
            "stake": row["stake"],
            "open_orders": row["open_orders"],
            "settled_orders": row["settled_orders"],
            "first_ms": row["first_ms"],
            "last_ms": row["last_ms"],
        })

    fixtures.sort(key=lambda item: (-(item["last_ms"] or 0), -item["orders"]))
    total = len(fixtures)
    return {
        "total": total,
        "rows": fixtures[offset:offset + max(1, limit)],
    }


COPY_MERGE_WINDOW_MS = 20000
COPY_SCAN_LIMIT = 20000


def _copy_pick_key(row) -> tuple:
    """(market, selection, handicap, period) — the \"same line\" part of the key."""
    try:
        handicap = round(float(row["handicap"] or 0), 4)
    except (TypeError, ValueError):
        handicap = 0.0
    period = row["period_type"]
    try:
        period_key = int(period) if period not in (None, "") else -1
    except (TypeError, ValueError):
        period_key = -1
    return (str(row["market"] or ""), str(row["selection"] or ""), handicap, period_key)


def _copy_group_key(base, start_ms) -> str:
    """合并批次的稳定身份：同一个 20 秒窗口永远得到同一个键。"""
    teams, event_ms, pick, score = base
    payload = [teams, int(event_ms), list(pick), score, int(start_ms)]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def _copy_member(row, score, *, include_internal=False) -> Dict[str, Any]:
    """展开行里展示的一笔源单。"""
    try:
        odds = float(row["odds"]) if row["odds"] is not None else None
    except (TypeError, ValueError):
        odds = None
    member = {
        "order_id": str(row["order_id"] or ""),
        "league": row["league"] or "",
        "teams": row["teams"] or "",
        "score": score,
        "market": row["market"] or "",
        "selection": row["selection"] or "",
        "handicap": row["handicap"],
        "period_type": row["period_type"],
        "units": row["units"],
        "corner_event_unit": row["corner_event_unit"],
        "odds": odds,
        "stake": float(row["stake"] or 0),
        "agent": row["agent_code"] or "",
        "member": str(
            row["member_user_code"] or row["member_name"] or row["member_identity"] or ""
        ),
        "created_ms": int(row["created_ms"] or 0),
        "status": str(row["status"] or ""),
    }
    if include_internal:
        member["_source_row"] = dict(row)
    return member


def query_copy_groups(conn: sqlite3.Connection, status: Optional[str] = "ALL",
                      agent: Optional[str] = None, search: str = "",
                      limit: int = 50, offset: int = 0,
                      from_ms: Optional[int] = None,
                      to_ms: Optional[int] = None,
                      basis: str = "created",
                      from_utc: Optional[str] = None,
                      to_utc: Optional[str] = None,
                      window_ms: int = COPY_MERGE_WINDOW_MS,
                      scan_limit: int = COPY_SCAN_LIMIT,
                      include_internal: bool = False) -> Dict[str, Any]:
    """Merge orders exactly like the Telegram push groups them.

    Base key = teams + kickoff + pick (market/side/handicap/period) + score.
    A new order joins the newest group of the same key only while it arrives
    within ``window_ms`` (20 s) of that group's first order, so a group never
    spans more than 20 seconds.
    """
    where, params = _order_scope(status, agent, search, from_ms, to_ms, basis,
                                 from_utc, to_utc)
    rows = conn.execute(
        f"""
        SELECT * FROM (
            SELECT source, order_id, agent_code, teams, league, market, selection, handicap,
                   odds, stake, status, member_identity, member_user_code, member_name,
                   created_ms, inplay, raw_json, content_hash, currency, sport,
                   json_extract(raw_json,'$.choices[0].score') AS score,
                   {FIXTURE_EVENT_MS} AS event_ms,
                   json_extract(raw_json,'$.choices[0].periodType') AS period_type,
                   json_extract(raw_json,'$.choices[0].cornerEventUnit') AS corner_event_unit,
                   json_extract(raw_json,'$.choices[0].units') AS units
            FROM partner_orders WHERE {where}
            ORDER BY created_ms DESC
            LIMIT ?
        )
        ORDER BY created_ms ASC
        """,
        params + [int(scan_limit)],
    ).fetchall()

    groups: List[Dict[str, Any]] = []
    active: Dict[tuple, Dict[str, Any]] = {}
    for row in rows:
        created = int(row["created_ms"] or 0)
        score = str(row["score"] or "").strip()
        base = (
            str(row["teams"] or ""),
            int(row["event_ms"] or 0),
            _copy_pick_key(row),
            score,
        )
        group = active.get(base)
        if group is None or created - group["start_ms"] > window_ms:
            group = {
                "group_key": _copy_group_key(base, created),
                "sources": [],
                "teams": row["teams"], "league": row["league"] or "",
                "market": row["market"] or "", "selection": row["selection"] or "",
                "handicap": row["handicap"], "period_type": row["period_type"],
                "units": row["units"], "corner_event_unit": row["corner_event_unit"],
                "event_ms": int(row["event_ms"] or 0),
                "score_first": score, "score_last": score,
                "start_ms": created, "end_ms": created,
                "orders": 0, "open_orders": 0, "settled_orders": 0,
                "members": set(), "member_sample": [], "stake": 0.0, "odds": [],
            }
            groups.append(group)
            active[base] = group
        group["sources"].append(_copy_member(row, score, include_internal=include_internal))
        group["end_ms"] = max(group["end_ms"], created)
        group["orders"] += 1
        if str(row["status"] or "").upper() == "OPEN":
            group["open_orders"] += 1
        else:
            group["settled_orders"] += 1
        group["stake"] += float(row["stake"] or 0)
        if row["odds"] is not None:
            try:
                group["odds"].append(float(row["odds"]))
            except (TypeError, ValueError):
                pass
        member = str(
            row["member_user_code"] or row["member_name"] or row["member_identity"] or ""
        )
        if member and member not in group["members"]:
            group["members"].add(member)
            if len(group["member_sample"]) < 4:
                group["member_sample"].append(member)
        if score:
            group["score_last"] = score

    merged: List[Dict[str, Any]] = []
    for group in groups:
        odds = group["odds"]
        merged.append({
            "group_key": group["group_key"],
            "sources": group["sources"],
            "teams": group["teams"], "league": group["league"],
            "market": group["market"], "selection": group["selection"],
            "handicap": group["handicap"], "period_type": group["period_type"],
            "units": group["units"], "corner_event_unit": group["corner_event_unit"],
            "event_ms": group["event_ms"],
            "score_first": group["score_first"], "score_last": group["score_last"],
            "start_ms": group["start_ms"], "end_ms": group["end_ms"],
            "duration_ms": max(0, group["end_ms"] - group["start_ms"]),
            "orders": group["orders"],
            "open_orders": group["open_orders"], "settled_orders": group["settled_orders"],
            "members": len(group["members"]), "member_sample": group["member_sample"],
            "stake": round(group["stake"], 2),
            "odds_min": min(odds) if odds else None,
            "odds_max": max(odds) if odds else None,
        })
    merged.sort(key=lambda item: -(item["end_ms"] or 0))
    total = len(merged)
    start = max(0, int(offset))
    end = start + max(1, int(limit))
    # 展开明细只随当前页下发，避免一次把上万笔源单塞进响应。
    for index, item in enumerate(merged):
        if not start <= index < end:
            item.pop("sources", None)
    return {
        "total": total,
        "rows": merged[start:end],
        "window_ms": window_ms,
        "scanned": len(rows),
    }


def query_fixture_orders(conn: sqlite3.Connection, teams: str, event_ms: int,
                         status: Optional[str] = "ALL", agent: Optional[str] = None,
                         search: str = "", limit: int = 500,
                         from_ms: Optional[int] = None,
                         to_ms: Optional[int] = None,
                         basis: str = "created",
                         from_utc: Optional[str] = None,
                         to_utc: Optional[str] = None) -> Dict[str, Any]:
    """Order lines for one fixture, oldest first."""
    where, params = _order_scope(status, agent, search, from_ms, to_ms, basis,
                                 from_utc, to_utc)
    where += f" AND teams=? AND {FIXTURE_EVENT_MS}=?"
    params = params + [teams, int(event_ms)]
    total = conn.execute(
        f"SELECT COUNT(*) FROM partner_orders WHERE {where}", params
    ).fetchone()[0]
    rows = conn.execute(
        f"SELECT {ORDER_SELECT}, "
        "json_extract(raw_json,'$.choices[0].score') AS score, "
        f"{PERIOD_COLUMNS} "
        f"FROM partner_orders WHERE {where} "
        "ORDER BY created_ms ASC LIMIT ?",
        params + [max(1, limit)],
    ).fetchall()
    return {
        "teams": teams,
        "event_ms": int(event_ms),
        "total": total,
        "rows": [with_market_metadata(dict(row)) for row in rows],
    }


class OrderStore:
    """Writable order store owned by the poller thread."""

    def __init__(self, path: Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self._run_counter = 0
        self._migrate_schema()

    def _migrate_schema(self) -> None:
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(partner_orders)")}
        if "result" not in columns:
            self.db.execute("ALTER TABLE partner_orders ADD COLUMN result TEXT")
        if "win_loss" not in columns:
            self.db.execute("ALTER TABLE partner_orders ADD COLUMN win_loss REAL")
        pending = self.db.execute(
            "SELECT COUNT(*) FROM partner_orders WHERE result IS NULL"
        ).fetchone()[0]
        if not pending:
            return
        rows = self.db.execute(
            "SELECT rowid, raw_json FROM partner_orders WHERE result IS NULL"
        ).fetchall()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            for row in rows:
                data = json.loads(row["raw_json"])
                result = result_of(data)
                self.db.execute(
                    "UPDATE partner_orders SET result=?, win_loss=? WHERE rowid=?",
                    (result, win_loss_of(data, result), row["rowid"]),
                )
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def close(self) -> None:
        self.db.close()

    def has_orders(self) -> bool:
        return self.db.execute("SELECT 1 FROM partner_orders LIMIT 1").fetchone() is not None

    def has_poll_state(self) -> bool:
        return self.db.execute("SELECT 1 FROM partner_poll_state LIMIT 1").fetchone() is not None

    def save_agents(self, agents: Sequence[Dict[str, Any]]) -> None:
        now = now_iso()
        with self.db:
            for agent in agents:
                self.db.execute(
                    """INSERT INTO partner_agents (code, user_id, level, name, resolved_at)
                       VALUES (?,?,?,?,?)
                       ON CONFLICT(code) DO UPDATE SET
                           user_id=excluded.user_id, level=excluded.level,
                           name=excluded.name, resolved_at=excluded.resolved_at""",
                    (agent["code"], int(agent["user_id"]), str(agent.get("level") or ""),
                     str(agent.get("name") or ""), now),
                )

    def saved_agents(self) -> List[Dict[str, Any]]:
        rows = self.db.execute(
            "SELECT code, user_id, level, name FROM partner_agents ORDER BY code"
        ).fetchall()
        return [dict(row) for row in rows]

    def _insert_order(self, fields: Dict[str, Any], now: str) -> None:
        columns = ORDER_COLUMNS + ("first_seen_at", "last_seen_at", "changed_at", "status_changed_at")
        values = [fields[column] for column in ORDER_COLUMNS] + [now, now, now, now]
        placeholders = ",".join("?" for _ in columns)
        self.db.execute(
            f"INSERT INTO partner_orders ({','.join(columns)}) VALUES ({placeholders})",
            values,
        )

    def _update_order(self, fields: Dict[str, Any], now: str, changed: bool,
                      status_changed: bool) -> None:
        update_columns = [column for column in ORDER_COLUMNS
                          if column not in ("source", "member_identity", "order_id")]
        assignments = ", ".join(f"{column}=?" for column in update_columns)
        values = [fields[column] for column in update_columns]
        self.db.execute(
            f"""UPDATE partner_orders SET {assignments},
                last_seen_at=?, changed_at=COALESCE(?, changed_at),
                status_changed_at=COALESCE(?, status_changed_at)
                WHERE source=? AND member_identity=? AND order_id=?""",
            values + [now, now if changed else None, now if status_changed else None,
                      SOURCE, fields["member_identity"], fields["order_id"]],
        )

    def commit_snapshot(self, agent_code: str, snapshot_type: str,
                        rows: List[Dict[str, Any]], stats: Dict[str, Any]) -> Dict[str, Any]:
        """Atomically commit one complete OPEN or SETTLED snapshot."""
        snapshot_type = snapshot_type.upper()
        if snapshot_type not in ("OPEN", "SETTLED"):
            raise ValueError("快照类型必须是 OPEN 或 SETTLED")

        merged: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for row in rows:
            key = order_identity(row)
            if key is None:
                continue
            previous = merged.get(key)
            if previous is None or str(row.get("status") or "").upper() == "SETTLED":
                merged[key] = row

        now = now_iso()
        new = changed = events = 0
        self.db.execute("BEGIN IMMEDIATE")
        try:
            baseline = self.db.execute(
                "SELECT 1 FROM partner_poll_state WHERE source=? AND agent_code=? AND snapshot_type=?",
                (SOURCE, agent_code, snapshot_type),
            ).fetchone() is None
            for (member_identity, order_id), row in merged.items():
                fields = parse_order(row, agent_code)
                old = self.db.execute(
                    "SELECT status, content_hash FROM partner_orders "
                    "WHERE source=? AND member_identity=? AND order_id=?",
                    (SOURCE, member_identity, order_id),
                ).fetchone()
                if old is None:
                    self._insert_order(fields, now)
                    new += 1
                    if snapshot_type == "OPEN" and not baseline and fields["status"] == "OPEN":
                        cursor = self.db.execute(
                            """INSERT OR IGNORE INTO partner_order_events
                               (source, agent_code, member_identity, order_id, observed_at)
                               VALUES (?,?,?,?,?)""",
                            (SOURCE, agent_code, member_identity, order_id, now),
                        )
                        events += cursor.rowcount
                    continue
                if old["status"] == "SETTLED" and fields["status"] == "OPEN":
                    continue  # A stale OPEN response must never undo a settlement.
                different = old["content_hash"] != fields["content_hash"]
                self._update_order(fields, now, different, old["status"] != fields["status"])
                changed += int(different)

            self.db.execute(
                """INSERT INTO partner_poll_state
                   (source, agent_code, snapshot_type, last_success_at, record_count)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(source, agent_code, snapshot_type) DO UPDATE SET
                       last_success_at=excluded.last_success_at,
                       record_count=excluded.record_count""",
                (SOURCE, agent_code, snapshot_type, now, len(merged)),
            )
            self._prune_events()
            self._insert_run(agent_code, snapshot_type, stats, len(merged), new, changed, events, baseline, 1, None)
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return {"new": new, "changed": changed, "events": events,
                "baseline": int(baseline), "records": len(merged)}

    def _prune_events(self) -> None:
        cutoff = self.db.execute(
            "SELECT event_id FROM partner_order_events ORDER BY event_id DESC LIMIT 1 OFFSET ?",
            (EVENT_LIMIT,),
        ).fetchone()
        if cutoff is not None:
            self.db.execute("DELETE FROM partner_order_events WHERE event_id<=?", (int(cutoff[0]),))

    def _insert_run(self, agent_code: str, snapshot_type: str, stats: Dict[str, Any],
                    record_count: int, new: int, changed: int, events: int,
                    baseline: bool, complete: int, error_code: Optional[str]) -> None:
        self.db.execute(
            """INSERT INTO partner_poll_runs
               (source, agent_code, snapshot_type, started_at, finished_at,
                elapsed_seconds, request_count, record_count, new_count,
                changed_count, event_count, baseline, complete, error_code)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (SOURCE, agent_code, snapshot_type, stats["started_at"], stats["finished_at"],
             float(stats.get("elapsed_seconds", 0)), int(stats.get("request_count", 0)),
             record_count, new, changed, events, int(baseline), complete, error_code),
        )
        self._run_counter += 1
        if self._run_counter % 500 == 0:
            self.db.execute(
                "DELETE FROM partner_poll_runs WHERE id <= "
                "(SELECT id FROM partner_poll_runs ORDER BY id DESC LIMIT 1 OFFSET 50000)"
            )

    def record_failure(self, agent_code: str, snapshot_type: str, stats: Dict[str, Any],
                       error_code: str) -> None:
        with self.db:
            self._insert_run(agent_code, snapshot_type, stats, 0, 0, 0, 0, False, 0, error_code)

    def counts(self) -> Dict[str, int]:
        row = self.db.execute(
            "SELECT COUNT(*) AS total, "
            "COALESCE(SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END),0) AS open, "
            "COALESCE(SUM(CASE WHEN status='SETTLED' THEN 1 ELSE 0 END),0) AS settled "
            "FROM partner_orders WHERE source=?",
            (SOURCE,),
        ).fetchone()
        return {"total": row["total"], "open": row["open"], "settled": row["settled"]}


def open_readonly(path: Path) -> Optional[sqlite3.Connection]:
    """Open the order DB read-only for HTTP requests; never create it."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection
    except sqlite3.Error:
        return None
