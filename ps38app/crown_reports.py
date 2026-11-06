"""皇冠会员历史注单报表入库。

只读皇冠会员接口，把每笔历史注单按票号写入本地 SQLite：
- crown_wagers：逐单明细（本金、水位、输赢、赛果、比分）
- crown_sync：每个会员最近一次同步状态

重复同步按 (account_id, ticket) upsert，内容不变不重写。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .config import resolve_path
from .follow.crown import CrownAdapter
from .follow.crown_markets import xml_root

BEIJING = timezone(timedelta(hours=8))
DEFAULT_DAYS = 8

SCHEMA = """
CREATE TABLE IF NOT EXISTS crown_wagers (
    account_id TEXT NOT NULL,
    ticket TEXT NOT NULL,
    settle_date TEXT,
    bet_time TEXT,
    league TEXT,
    home TEXT,
    away TEXT,
    bet_type TEXT,
    odds TEXT,
    stake TEXT,
    win_gold TEXT,
    result TEXT,
    result_data TEXT,
    score TEXT,
    settled INTEGER NOT NULL DEFAULT 0,
    raw_hash TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (account_id, ticket)
);
CREATE INDEX IF NOT EXISTS crown_wagers_date
    ON crown_wagers(settle_date, account_id);
CREATE INDEX IF NOT EXISTS crown_wagers_settled
    ON crown_wagers(settled, settle_date);
CREATE TABLE IF NOT EXISTS crown_sync (
    account_id TEXT PRIMARY KEY,
    last_sync_at TEXT,
    dates TEXT,
    inserted INTEGER NOT NULL DEFAULT 0,
    updated INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
"""


def report_db_path(config: Dict[str, Any]) -> Path:
    configured = str(config.get("crown_report_db_path") or "").strip()
    if configured:
        return resolve_path(configured, "data/crown_reports.sqlite3")
    return Path(config["db_path"]).with_name("crown_reports.sqlite3")


def _now() -> str:
    return datetime.now(BEIJING).isoformat(timespec="seconds")


def _digits(value: Any) -> str:
    return "".join(char for char in str(value or "") if char.isdigit())


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


class CrownReportStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect(write=True) as connection:
            connection.executescript(SCHEMA)

    def connect(self, *, write: bool = False):
        class _Context:
            def __enter__(inner):
                inner.connection = sqlite3.connect(self.path, timeout=10)
                inner.connection.row_factory = sqlite3.Row
                return inner.connection

            def __exit__(inner, exc_type, exc, tb):
                try:
                    if exc_type is None and write:
                        inner.connection.commit()
                    else:
                        inner.connection.rollback()
                finally:
                    inner.connection.close()
                return False

        return _Context()

    @staticmethod
    def _hash(fields: Dict[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(fields, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def upsert(self, connection, account_id: str, fields: Dict[str, Any]) -> str:
        now = _now()
        raw_hash = self._hash(fields)
        existing = connection.execute(
            "SELECT raw_hash FROM crown_wagers WHERE account_id=? AND ticket=?",
            (account_id, fields["ticket"]),
        ).fetchone()
        if existing is None:
            connection.execute(
                """INSERT INTO crown_wagers(
                    account_id,ticket,settle_date,bet_time,league,home,away,
                    bet_type,odds,stake,win_gold,result,result_data,score,
                    settled,raw_hash,first_seen,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    account_id,
                    fields["ticket"],
                    fields.get("settle_date", ""),
                    fields.get("bet_time", ""),
                    fields.get("league", ""),
                    fields.get("home", ""),
                    fields.get("away", ""),
                    fields.get("bet_type", ""),
                    fields.get("odds", ""),
                    fields.get("stake", ""),
                    fields.get("win_gold", ""),
                    fields.get("result", ""),
                    fields.get("result_data", ""),
                    fields.get("score", ""),
                    1 if fields.get("win_gold") not in (None, "") else 0,
                    raw_hash,
                    now,
                    now,
                ),
            )
            return "inserted"
        if existing["raw_hash"] == raw_hash:
            return "skipped"
        connection.execute(
            """UPDATE crown_wagers SET
                settle_date=?, bet_time=?, league=?, home=?, away=?,
                bet_type=?, odds=?, stake=?, win_gold=?, result=?,
                result_data=?, score=?, settled=?, raw_hash=?, updated_at=?
               WHERE account_id=? AND ticket=?""",
            (
                fields.get("settle_date", ""),
                fields.get("bet_time", ""),
                fields.get("league", ""),
                fields.get("home", ""),
                fields.get("away", ""),
                fields.get("bet_type", ""),
                fields.get("odds", ""),
                fields.get("stake", ""),
                fields.get("win_gold", ""),
                fields.get("result", ""),
                fields.get("result_data", ""),
                fields.get("score", ""),
                1 if fields.get("win_gold") not in (None, "") else 0,
                raw_hash,
                now,
                account_id,
                fields["ticket"],
            ),
        )
        return "updated"

    def record_sync(
        self,
        connection,
        account_id: str,
        *,
        dates: List[str],
        inserted: int,
        updated: int,
        skipped: int,
        error: str = "",
    ) -> None:
        connection.execute(
            """INSERT INTO crown_sync(
                account_id,last_sync_at,dates,inserted,updated,skipped,last_error
            ) VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(account_id) DO UPDATE SET
                last_sync_at=excluded.last_sync_at, dates=excluded.dates,
                inserted=excluded.inserted, updated=excluded.updated,
                skipped=excluded.skipped, last_error=excluded.last_error""",
            (
                account_id,
                _now(),
                ",".join(dates),
                inserted,
                updated,
                skipped,
                error[:240],
            ),
        )

    def summary(self, account_id: Optional[str] = None) -> List[Dict[str, Any]]:
        query = (
            "SELECT account_id, settle_date, COUNT(*) AS orders, "
            "COALESCE(SUM(CAST(REPLACE(stake, ',', '') AS REAL)), 0) AS stake, "
            "COALESCE(SUM(CAST(REPLACE(win_gold, ',', '') AS REAL)), 0) AS win_loss "
            "FROM crown_wagers WHERE settled=1"
        )
        params: List[Any] = []
        if account_id:
            query += " AND account_id=?"
            params.append(account_id)
        query += " GROUP BY account_id, settle_date ORDER BY settle_date DESC, account_id"
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(query, params)]

    def crown_today(self) -> str:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT dates FROM crown_sync WHERE dates<>'' ORDER BY last_sync_at DESC LIMIT 1"
            ).fetchone()
        if row and row["dates"]:
            first = str(row["dates"]).split(",")[0].strip()
            if first:
                return first
        return datetime.now(BEIJING).strftime("%Y-%m-%d")

    def totals(self, date_from: str, date_to: str) -> Dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN settled=1 THEN 1 ELSE 0 END) AS settled,
                    SUM(CASE WHEN settled=0 THEN 1 ELSE 0 END) AS pending,
                    SUM(CASE WHEN settled=1 AND CAST(REPLACE(win_gold, ',', '') AS REAL) > 0
                        THEN 1 ELSE 0 END) AS wins,
                    SUM(CASE WHEN settled=1 AND CAST(REPLACE(win_gold, ',', '') AS REAL) < 0
                        THEN 1 ELSE 0 END) AS losses,
                    SUM(CASE WHEN settled=1 AND CAST(REPLACE(win_gold, ',', '') AS REAL) = 0
                        THEN 1 ELSE 0 END) AS pushes,
                    COALESCE(SUM(CASE WHEN settled=1
                        THEN CAST(REPLACE(win_gold, ',', '') AS REAL) ELSE 0 END), 0) AS pnl
                FROM crown_wagers WHERE settle_date BETWEEN ? AND ?""",
                (date_from, date_to),
            ).fetchone()
        data = dict(row) if row else {}
        for key in ("total", "settled", "pending", "wins", "losses", "pushes"):
            data[key] = int(data.get(key) or 0)
        data["pnl"] = float(data.get("pnl") or 0)
        return data

    def rows(
        self,
        *,
        account_id: str = "",
        date_from: str = "",
        date_to: str = "",
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM crown_wagers WHERE 1=1"
        params: List[Any] = []
        if account_id:
            query += " AND account_id=?"
            params.append(account_id)
        if date_from:
            query += " AND settle_date>=?"
            params.append(date_from)
        if date_to:
            query += " AND settle_date<=?"
            params.append(date_to)
        query += " ORDER BY settle_date DESC, bet_time DESC LIMIT ?"
        params.append(max(1, min(5000, int(limit or 500))))
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(query, params)]

    def sync_states(self) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM crown_sync")]


def wager_fields(day: str, wager) -> Optional[Dict[str, str]]:
    ticket = _digits(wager.findtext("w_id") or wager.get("tid") or "")
    if not ticket:
        return None
    return {
        "ticket": ticket,
        "settle_date": day,
        "bet_time": _text(wager.findtext("addtime")),
        "league": _text(wager.findtext("league")),
        "home": _text(wager.findtext("team_h_show")),
        "away": _text(wager.findtext("team_c_show")),
        "bet_type": _text(wager.findtext("wtype")),
        "odds": _text(wager.findtext("ioratio")),
        "stake": _text(wager.findtext("gold")),
        "win_gold": _text(wager.findtext("win_gold")),
        "result": _text(wager.findtext("result")),
        "result_data": _text(wager.findtext("result_data")),
        "score": _text(wager.findtext("score")),
    }


def json_wager_fields(wager: Dict[str, Any], default_day: str) -> Optional[Dict[str, str]]:
    ticket = _digits(wager.get("w_id") or wager.get("ticket_id") or "")
    if not ticket:
        return None
    # get_today_wagers 里的 win_gold 多数是“预计可赢”，不是已结算。
    # 只有带最终比分 result_data 的才按已结算处理，其余一律记为未结。
    final_score = _text(wager.get("result_data"))
    win_gold = _text(wager.get("win_gold")) if final_score else ""
    settle_date = _text(wager.get("adddate"))[:10] or default_day
    return {
        "ticket": ticket,
        "settle_date": settle_date,
        "bet_time": _text(wager.get("addtime")),
        "league": _text(wager.get("league")),
        "home": _text(wager.get("team_h_show")),
        "away": _text(wager.get("team_c_show")),
        "bet_type": _text(wager.get("wtype")),
        "odds": _text(wager.get("ioratio")),
        "stake": _text(wager.get("gold")),
        "win_gold": win_gold,
        "result": _text(wager.get("result")),
        "result_data": final_score,
        "score": _text(wager.get("score")),
    }


def history_dates(adapter: CrownAdapter, session: Dict[str, Any], opener) -> List[str]:
    body = adapter._post(
        session,
        {
            "p": "get_history_data",
            "gtype": "ALL",
            "isAll": "Y",
            "startdate": "",
            "enddate": "",
            "filter": "N",
        },
        opener,
        deadline=time.monotonic() + 15,
    )
    root = xml_root(body)
    dates = [_text(node.text) for node in root.iter("date") if _text(node.text)]
    if not dates:
        dates = [
            _text(item) for item in (root.findtext("allDateValue") or "").split("|") if _text(item)
        ]
    return list(dict.fromkeys(dates))


def sync_account(
    adapter: CrownAdapter,
    store: CrownReportStore,
    account_id: str,
    *,
    days: int = DEFAULT_DAYS,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    stats = {
        "account_id": account_id,
        "inserted": 0,
        "updated": 0,
        "skipped": 0,
        "dates": [],
        "error": "",
    }

    def check_stopped() -> None:
        if should_stop is not None and should_stop():
            raise RuntimeError("报表同步已停止")

    try:
        check_stopped()
        session = adapter._load_session(account_id)
        opener = adapter._new_opener(session)
        dates = history_dates(adapter, session, opener)[: max(1, int(days))]
        stats["dates"] = dates
        records = []
        # 网络读取在写事务前完成，避免慢速报表接口持续占用 SQLite 写锁。
        try:
            check_stopped()
            today_body = adapter._post(
                session,
                {
                    "p": "get_today_wagers",
                    "LS": "g",
                    "selGtype": "ALL",
                    "chk_cw": "N",
                    "ts": str(int(time.time() * 1000)),
                    "format": "json",
                    "db_slow": "N",
                },
                opener,
                deadline=time.monotonic() + 20,
            )
            today_data = json.loads(today_body.decode("utf-8", "replace"))
            default_day = dates[0] if dates else datetime.now(BEIJING).strftime("%Y-%m-%d")
            for wager in today_data.get("wagers") or []:
                if isinstance(wager, dict):
                    fields = json_wager_fields(wager, default_day)
                    if fields is not None:
                        records.append(fields)
        except Exception:  # noqa: BLE001 - 今日接口失败时仍同步历史
            check_stopped()
        for day in dates:
            check_stopped()
            body = adapter._post(
                session,
                {
                    "p": "history_switch",
                    "LS": "c",
                    "today_gmt": day,
                    "gtype": "ALL",
                    "tmp_flag": "Y",
                },
                opener,
                deadline=time.monotonic() + 25,
            )
            root = xml_root(body)
            for wager in root.iter("wagers"):
                fields = wager_fields(day, wager)
                if fields is not None:
                    records.append(fields)
        check_stopped()
        with store.connect(write=True) as connection:
            for fields in records:
                outcome = store.upsert(connection, account_id, fields)
                stats[outcome] += 1
            store.record_sync(
                connection,
                account_id,
                dates=dates,
                inserted=stats["inserted"],
                updated=stats["updated"],
                skipped=stats["skipped"],
            )
    except Exception as exc:  # noqa: BLE001 - 单账号失败不影响其他会员
        stats.update(inserted=0, updated=0, skipped=0)
        stats["error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
        try:
            with store.connect(write=True) as connection:
                store.record_sync(
                    connection,
                    account_id,
                    dates=stats["dates"],
                    inserted=0,
                    updated=0,
                    skipped=0,
                    error=stats["error"],
                )
        except sqlite3.Error:
            pass
    return stats


def _default_report_path(config: Dict[str, Any]) -> Path:
    return report_db_path(config)


class ReportSyncService:
    """每 N 秒扫描账号库里的皇冠会员并增量同步历史注单。"""

    def __init__(
        self,
        config: Dict[str, Any],
        *,
        store: Optional[CrownReportStore] = None,
        adapter: Optional[CrownAdapter] = None,
        account_provider=None,
        interval: Optional[int] = None,
    ):
        self.config = dict(config)
        self.store = store or CrownReportStore(_default_report_path(config))
        self._stop = threading.Event()
        self.adapter = adapter or CrownAdapter(config, should_stop=self._stop.is_set)
        self.interval = max(
            60, int(interval or config.get("crown_report_sync_interval_seconds") or 300)
        )
        self.days = max(1, min(30, int(config.get("crown_report_sync_days") or DEFAULT_DAYS)))
        self._account_provider = account_provider
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._known: set[str] = set()
        self._state: Dict[str, Any] = {
            "running": False,
            "last_run_at": "",
            "next_run_at": "",
            "results": [],
            "error": "",
        }

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="crown-report-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread and self._thread.join(timeout=5)

    def _accounts(self) -> List[Dict[str, Any]]:
        if callable(self._account_provider):
            return list(self._account_provider() or [])
        database = Path(self.config["pool_db_path"])
        try:
            with closing(
                sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
            ) as connection:
                connection.row_factory = sqlite3.Row
                return [
                    dict(row)
                    for row in connection.execute(
                        "SELECT id,name,status FROM accounts "
                        "WHERE platform='crown_member' AND enabled=1 ORDER BY name"
                    )
                ]
        except sqlite3.Error:
            return []

    def run_once(self) -> Dict[str, Any]:
        results = []
        for account in self._accounts():
            if self._stop.is_set():
                break
            account_id = str(account.get("id") or "")
            if not account_id:
                continue
            if str(account.get("status") or "") != "online":
                results.append(
                    {
                        "account_id": account_id,
                        "name": account.get("name", ""),
                        "error": "账号不在线",
                    }
                )
                continue
            days = self.days if account_id not in self._known else min(2, self.days)
            stats = sync_account(
                self.adapter, self.store, account_id, days=days, should_stop=self._stop.is_set
            )
            if not stats["error"]:
                with self._lock:
                    self._known.add(account_id)
            results.append(
                {
                    "account_id": account_id,
                    "name": account.get("name", ""),
                    "days": days,
                    "inserted": stats["inserted"],
                    "updated": stats["updated"],
                    "skipped": stats["skipped"],
                    "error": stats["error"],
                }
            )
        with self._lock:
            self._state.update(
                last_run_at=_now(),
                next_run_at=datetime.now(BEIJING).timestamp() + self.interval,
                results=results,
                error="",
            )
        return {"results": results}

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 - 报表同步失败不影响主程序
                with self._lock:
                    self._state["error"] = f"{type(exc).__name__}: {exc}"
            self._stop.wait(self.interval)

    def status(self) -> Dict[str, Any]:
        with self._lock:
            state = dict(self._state)
            state["known_accounts"] = sorted(self._known)
        state["running"] = bool(self._thread and self._thread.is_alive())
        state["interval_seconds"] = self.interval
        state["days"] = self.days
        state["database"] = str(self.store.path)
        return state
