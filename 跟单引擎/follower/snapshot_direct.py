"""Snapshot Direct 唯一生产执行链的领域对象与纯 FT_bet 执行器。"""

from __future__ import annotations

import math
import re
import time
import hashlib
import json
from urllib.parse import urlsplit
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from hdzz.common.parse import parse_xml_flat
from hdzz.build_info import current_build_info

from .executor import (
    close_member_session,
    make_member_session,
    member_post_session,
    member_post_session_once,
)
from .market_rows import MarketRow, market_row_fingerprint
from .member_ioratio import MemberIoratio
from .odds_snapshot import load_snapshot
from .strategy import (
    decode_handicap_line,
    decode_total_line,
    encode_handicap_line,
    encode_total_line,
    format_handicap_line_value,
)


SNAPSHOT_MISSING = "SNAPSHOT_MISSING"
SNAPSHOT_STALE = "SNAPSHOT_STALE"
TARGET_LINE_MISSING = "TARGET_LINE_MISSING"
SNAPSHOT_INVALID = "SNAPSHOT_INVALID"
SNAPSHOT_VALID = "VALID"

SUCCESS = "SUCCESS"
MEMBER_ERROR = "MEMBER_ERROR"
MARKET_ERROR = "MARKET_ERROR"
UNKNOWN_RESULT = "UNKNOWN_RESULT"
MARKET_SUBMIT_REJECTED = "MARKET_SUBMIT_REJECTED"
BET_CONTEXT_MISMATCH = "BET_CONTEXT_MISMATCH"
MARKET_CHANGED_DURING_CONTEXT = "MARKET_CHANGED_DURING_CONTEXT"
CONTEXT_RETRYABLE = "CONTEXT_RETRYABLE"
SOURCE_ORDER_DIRECT = "source_order_direct"
SOURCE_ORDER_READY = "SOURCE_ORDER_READY"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _sha256_text(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _safe_url_identity(value: Any) -> Dict[str, Any]:
    parsed = urlsplit(_text(value))
    host = _text(parsed.hostname).lower()
    return {
        "scheme": _text(parsed.scheme).lower(),
        "host_sha256": _sha256_text(host) if host else "",
        "port": parsed.port,
    }


def _safe_error_value(value: Any) -> Any:
    text = _text(value)
    if not text:
        return ""
    if re.fullmatch(r"[0-9A-Za-z_:-]{1,96}", text):
        return text
    return {"sha256": _sha256_text(text), "length": len(text)}


def _cookie_hash_set(value_hashes: Mapping[str, str]) -> str:
    canonical = json.dumps(dict(sorted(value_hashes.items())), ensure_ascii=True, separators=(",", ":"))
    return _sha256_text(canonical)


def _request_fingerprint(
    *,
    member_id: str,
    uid: str,
    base_url: str,
    ver: str,
    form: Mapping[str, Any],
    session_meta: Mapping[str, Any],
    build_info: Mapping[str, Any],
) -> Dict[str, Any]:
    return {
        "build": {
            "version": _text(build_info.get("version")),
            "git_commit": _text(build_info.get("git_commit")),
            "build_id": _text(build_info.get("build_id")),
        },
        "member_id_sha256": _sha256_text(member_id),
        "uid_sha256": _sha256_text(uid),
        "cookie_names": list(session_meta.get("cookie_names") or []),
        "cookie_value_hash_set": dict(session_meta.get("cookie_value_hash") or {}),
        "cookie_value_hash_set_sha256": str(session_meta.get("cookie_value_hash_set_sha256") or ""),
        "cookie_count": int(session_meta.get("cookie_count") or 0),
        "base_url": _safe_url_identity(base_url),
        "final_url_host": _safe_url_identity(f"{_text(base_url).rstrip('/')}/transform.php?ver={ver}"),
        "ver_sha256": _sha256_text(ver),
        "ver_length": len(ver),
        "network": {
            "trust_env": bool(session_meta.get("trust_env")),
            "proxy_enabled": bool(session_meta.get("proxy_enabled")),
        },
        "headers": dict(session_meta.get("request_headers") or {}),
        "payload": {
            key: form.get(key)
            for key in (
                "gid",
                "gtype",
                "wtype",
                "rtype",
                "chose_team",
                "con",
                "ratio",
                "ioratio",
                "isRB",
                "f",
                "golds",
                "odd_f_type",
            )
        },
    }


def _response_fingerprint(response_meta: Mapping[str, Any], data: Mapping[str, Any], raw: str) -> Dict[str, Any]:
    body = str(raw or "")
    return {
        "status": response_meta.get("http_status"),
        "content_type": _text(response_meta.get("response_content_type")),
        "server": _text(response_meta.get("response_server")),
        "via": _text(response_meta.get("response_via")),
        "header_names": list(response_meta.get("response_header_names") or []),
        "body_length": int(response_meta.get("response_body_length") or len(body.encode("utf-8"))),
        "body_sha256": _text(response_meta.get("response_body_sha256")) or _sha256_text(body),
        "xml_code": _text(data.get("code")),
        "xml_errormsg": _safe_error_value(data.get("errormsg")),
        "ticket_present": bool(_text(data.get("ticket_id") or data.get("ticketId"))),
    }


def _strip_markup(value: Any) -> str:
    text = _text(value)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _is_live(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = _text(value).lower()
    return text in {"1", "true", "yes", "live", "滚球", "rb"} or "滚球" in text or "live" in text


def _parse_line(value: Any, *, signed: bool = False) -> Optional[float]:
    """Parse the handicap/total at the end of the order text.

    Team names are user/provider supplied and may contain digits (for example
    ``波特兰木材2队``).  Searching for the first number therefore turns a team
    name into a fake handicap.  The line is the trailing numeric token; an
    optional ``@odds`` suffix is removed before parsing.
    """
    text = _strip_markup(value)
    if not text:
        return None
    # Odds are sometimes included in the raw order text. They are not part of
    # the requested market line and must never become the target line.
    text = re.sub(r"\s*@\s*[+-]?\d+(?:\.\d+)?\s*$", "", text).strip()
    split = re.search(
        r"(?P<sign>[+-])?\s*(?P<a>\d+(?:\.\d+)?)\s*(?:/|至|～|~)\s*(?P<b>\d+(?:\.\d+)?)\s*$",
        text,
    )
    if split:
        avg = (float(split.group("a")) + float(split.group("b"))) / 2.0
        if signed and split.group("sign") == "-":
            avg = -avg
        return avg
    match = re.search(r"(?P<sign>[+-])?\s*(?P<n>\d+(?:\.\d+)?)\s*$", text)
    if not match:
        return None
    value0 = float(match.group("n"))
    if signed and match.group("sign") == "-":
        value0 = -value0
    return value0


def _team_names(order: Mapping[str, Any], raw: Mapping[str, Any]) -> Tuple[str, str]:
    home = _text(raw.get("TEAM_H") or order.get("team_h") or order.get("home_team"))
    away = _text(raw.get("TEAM_C") or order.get("team_c") or order.get("away_team"))
    if home and away:
        return home, away
    team_text = _strip_markup(raw.get("SHOWTEXT_TEAM") or raw.get("SHOWTEXT_TEAM_NUM"))
    if " v " in team_text:
        left, right = team_text.split(" v ", 1)
        home = home or _strip_markup(left).split("(", 1)[0].strip()
        away = away or _strip_markup(right).split("(", 1)[0].strip()
    return home, away


def _resolve_side(order: Mapping[str, Any], raw: Mapping[str, Any], market_type: str) -> str:
    if market_type == "TOTAL":
        text = " ".join(
            _text(raw.get(key) or order.get(key))
            for key in ("ORDER_TYPE", "SHOWTEXT_ORDER_TYPE_IORATIO", "line")
        )
        return "OVER" if "大" in text and "小" not in text else "UNDER"
    pick = _text(raw.get("ORDER_TYPE") or order.get("order_type") or order.get("line"))
    if any(word in pick for word in ("和局", "平局", "和", "平")):
        return "DRAW"
    home, away = _team_names(order, raw)
    if home and home in pick:
        return "HOME"
    if away and away in pick:
        return "AWAY"
    explicit = _text(raw.get("chose_team") or order.get("chose_team")).upper()
    return {"H": "HOME", "C": "AWAY", "N": "DRAW"}.get(explicit, "")


@dataclass(frozen=True)
class MarketIntent:
    order_id: str
    target: str
    gid: str
    gtype: str
    is_live: bool
    period: str
    market_type: str
    side: str
    target_line: Optional[float]
    source_odds: str
    split_amount: float = 0.0
    created_at: float = 0.0
    # Crown displays some first-half corner totals as an integer while the
    # CN/HROU feed exposes the corresponding half-line (for example, 5 ->
    # 4.5).  Keep this marker on the intent so that compatibility stays
    # limited to corner totals and cannot affect ordinary football markets.
    is_corner: bool = False

    @classmethod
    def from_order(
        cls,
        order: Mapping[str, Any],
        *,
        split_amount: float = 0.0,
        created_at: Optional[float] = None,
    ) -> "MarketIntent":
        raw = order.get("raw") if isinstance(order.get("raw"), Mapping) else {}
        wager = _text(raw.get("WAGERSTYPE") or order.get("betType") or order.get("wagerType"))
        order_type = _text(raw.get("ORDER_TYPE") or order.get("order_type"))
        line_text = _text(order.get("line") or raw.get("SHOWTEXT_ORDER_TYPE_IORATIO") or order_type)
        combined = f"{wager} {order_type} {line_text}"
        is_corner = "角球" in combined or "corner" in combined.lower()
        if "独赢" in wager or "獨贏" in wager:
            market_type = "MONEYLINE"
        elif "让球" in wager:
            market_type = "HANDICAP"
        elif any(word in combined for word in ("大 / 小", "大小", "大球", "小球", "大", "小")):
            market_type = "TOTAL"
        else:
            market_type = "OTHER"
        is_live = _is_live(order.get("live")) or _is_live(raw.get("MARKET")) or "滚球" in wager
        period = "HALF_TIME" if any(word in combined for word in ("上半", "半场", "半場")) else "FULL_TIME"
        signed = market_type == "HANDICAP"
        target_line = None if market_type == "MONEYLINE" else _parse_line(line_text, signed=signed)
        gid = _text(
            raw.get("_EVENT")
            or order.get("gid")
            or order.get("game_gid")
            or raw.get("GID")
        )
        return cls(
            order_id=_text(order.get("orderId") or raw.get("TID")),
            target=_text(order.get("target")),
            gid=gid,
            gtype=_text(raw.get("GTYPE") or order.get("gtype") or "FT") or "FT",
            is_live=is_live,
            period=period,
            market_type=market_type,
            side=_resolve_side(order, raw, market_type),
            target_line=target_line,
            source_odds=_text(order.get("odds") or raw.get("ODDS") or raw.get("IORATIO")),
            split_amount=float(split_amount or 0.0),
            created_at=float(time.time() if created_at is None else created_at),
            is_corner=is_corner,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "order_id": self.order_id,
            "target": self.target,
            "gid": self.gid,
            "gtype": self.gtype,
            "is_live": self.is_live,
            "period": self.period,
            "market_type": self.market_type,
            "side": self.side,
            "target_line": self.target_line,
            "source_odds": self.source_odds,
            "split_amount": self.split_amount,
            "created_at": self.created_at,
            "is_corner": self.is_corner,
        }


# Compatibility name for callers that still refer to the resolved public row as a snapshot.
MarketSnapshot = MarketRow


@dataclass(frozen=True)
class SubmitTemplate:
    gid: str
    gtype: str
    wtype: str
    rtype: str
    chose_team: str
    con: str
    ratio: str
    ioratio: str
    is_rb: str
    f: str
    snapshot_revision: int
    snapshot_fetched_at: float
    snapshot_age_ms: int = 0
    line: Optional[float] = None
    snapshot_fingerprint: str = ""
    market_row_ratio: str = ""

    quote_source_member: str = ""
    execution_member: str = ""
    member_ioratio_source_member: str = ""
    member_ioratio_fetched_at: float = 0.0
    member_ioratio_age_ms: int = 0

    @property
    def market_row_fingerprint(self) -> str:
        return self.snapshot_fingerprint

    @classmethod
    def from_market_row(
        cls,
        market_row: MarketRow,
        member_quote: Optional[MemberIoratio],
        *,
        execution_member: str,
        ioratio_override: Any = None,
        ratio_override: Any = None,
        ioratio_source: str = "member_ioratio_cache",
    ) -> "SubmitTemplate":
        raw = market_row.raw if isinstance(market_row.raw, dict) else {}
        source = raw.get("source_member") if isinstance(raw.get("source_member"), dict) else {}
        mode = _text(raw.get("mode") or "live").lower()
        return cls(
            gid=market_row.gid,
            gtype=market_row.gtype,
            wtype=market_row.wtype,
            rtype=market_row.rtype,
            chose_team=market_row.chose_team,
            con=market_row.con,
            ratio=_text(ratio_override) if ratio_override is not None else market_row.ratio,
            ioratio=_text(ioratio_override) if ioratio_override is not None else _text(member_quote.ioratio if member_quote else ""),
            is_rb="Y" if mode == "live" else "N",
            f="1R" if mode == "live" else "1M",
            snapshot_revision=market_row.revision,
            snapshot_fetched_at=market_row.fetched_at,
            snapshot_age_ms=market_row.age_ms,
            line=market_row.line,
            snapshot_fingerprint=market_row.fingerprint,
            market_row_ratio=market_row.ratio,
            quote_source_member=_text(source.get("id") or source.get("label")),
            execution_member=_text(execution_member),
            member_ioratio_source_member=(
                _text(member_quote.member_id) if member_quote else _text(ioratio_source)
            ),
            member_ioratio_fetched_at=(member_quote.fetched_at if member_quote else 0.0),
            member_ioratio_age_ms=(member_quote.age_ms if member_quote else 0),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "gid": self.gid,
            "gtype": self.gtype,
            "wtype": self.wtype,
            "rtype": self.rtype,
            "chose_team": self.chose_team,
            "con": self.con,
            "ratio": self.ratio,
            "ioratio": self.ioratio,
            "isRB": self.is_rb,
            "f": self.f,
            "snapshot_revision": self.snapshot_revision,
            "snapshot_fetched_at": self.snapshot_fetched_at,
            "snapshot_age_ms": self.snapshot_age_ms,
            "line": self.line,
            "snapshot_fingerprint": self.snapshot_fingerprint,
            "quote_source_member": self.quote_source_member,
            "execution_member": self.execution_member,
            "member_ioratio_source_member": self.member_ioratio_source_member,
            "public_snapshot_revision": self.snapshot_revision,
            "public_line": self.line,
            "public_con": self.con,
            "public_ratio": self.market_row_ratio or self.ratio,
            "member_ioratio": self.ioratio,
            "member_ioratio_fetched_at": self.member_ioratio_fetched_at,
            "member_ioratio_age_ms": self.member_ioratio_age_ms,
            "market_row_gid": self.gid,
            "market_row_line": self.line,
            "market_row_con": self.con,
            "market_row_ratio": self.market_row_ratio or self.ratio,
            "market_row_revision": self.snapshot_revision,
            "market_row_fingerprint": self.snapshot_fingerprint,
        }


def build_source_order_submit_template(
    intent: MarketIntent,
    *,
    execution_member: str,
) -> SubmitTemplate:
    """Build FT_bet protocol fields from the immutable source order only.

    This path deliberately does not consult Snapshot, member quote caches, or
    FT_order_view.  It therefore accepts only protocol mappings that can be
    reconstructed and round-trip validated from the source order itself.
    """

    gid = _text(intent.gid)
    gtype = _text(intent.gtype).upper()
    market_type = _text(intent.market_type).upper()
    period = _text(intent.period).upper()
    side = _text(intent.side).upper()
    source_odds = _text(intent.source_odds)

    if not gid:
        raise ValueError("source order has no gid")
    if gtype != "FT":
        raise ValueError(f"source-order direct supports FT only: gtype={gtype or 'missing'}")
    if not _valid_positive_ioratio(source_odds):
        raise ValueError("source order ioratio is missing or invalid")
    if intent.is_corner:
        raise ValueError("corner source line requires platform-specific normalization")
    if period not in {"FULL_TIME", "HALF_TIME"}:
        raise ValueError(f"unsupported source order period: {period or 'missing'}")

    is_half = period == "HALF_TIME"
    if market_type == "HANDICAP":
        if side not in {"HOME", "AWAY"}:
            raise ValueError(f"unsupported handicap side: {side or 'missing'}")
        if intent.target_line is None:
            raise ValueError("source handicap line is missing")
        line = float(intent.target_line)
        if not math.isfinite(line) or not math.isclose(line * 4.0, round(line * 4.0), abs_tol=1e-7):
            raise ValueError(f"source handicap line is not a quarter line: {line}")
        wtype = "HRE" if is_half else "RE" if intent.is_live else "R"
        chose_team = "H" if side == "HOME" else "C"
        con, ratio = encode_handicap_line(line)
        decoded = decode_handicap_line(con, ratio)
        if decoded is None or not math.isclose(float(decoded), line, abs_tol=1e-7):
            raise ValueError(
                f"source handicap protocol round-trip mismatch: line={line} con={con} ratio={ratio}"
            )
    elif market_type == "TOTAL":
        if side not in {"OVER", "UNDER"}:
            raise ValueError(f"unsupported total side: {side or 'missing'}")
        if intent.target_line is None:
            raise ValueError("source total line is missing")
        line = abs(float(intent.target_line))
        if (
            not math.isfinite(line)
            or line <= 0.0
            or not math.isclose(line * 4.0, round(line * 4.0), abs_tol=1e-7)
        ):
            raise ValueError(f"source total line is invalid: {line}")
        wtype = "HROU" if is_half and intent.is_live else "HOU" if is_half else "ROU" if intent.is_live else "OU"
        chose_team = "C" if side == "OVER" else "H"
        con, ratio = encode_total_line(line, is_over=side == "OVER")
        decoded = decode_total_line(con, ratio)
        expected_decoded = line if side == "OVER" else -line
        if decoded is None or not math.isclose(float(decoded), expected_decoded, abs_tol=1e-7):
            raise ValueError(
                f"source total protocol round-trip mismatch: line={line} con={con} ratio={ratio}"
            )
    elif market_type == "MONEYLINE":
        raise ValueError("moneyline requires the platform's dynamic submit ratio")
    else:
        raise ValueError(f"unsupported source order market: {market_type or 'missing'}")

    rtype = f"{wtype}{chose_team}"
    source_fingerprint = _sha256_text(
        json.dumps(
            {
                "order_id": intent.order_id,
                "gid": gid,
                "gtype": gtype,
                "wtype": wtype,
                "rtype": rtype,
                "chose_team": chose_team,
                "line": line,
                "con": con,
                "ratio": ratio,
                "ioratio": source_odds,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return SubmitTemplate(
        gid=gid,
        gtype=gtype,
        wtype=wtype,
        rtype=rtype,
        chose_team=chose_team,
        con=con,
        ratio=ratio,
        ioratio=source_odds,
        is_rb="Y" if intent.is_live else "N",
        f="1R" if intent.is_live else "1M",
        snapshot_revision=0,
        snapshot_fetched_at=0.0,
        snapshot_age_ms=0,
        line=line,
        snapshot_fingerprint=source_fingerprint,
        market_row_ratio=ratio,
        quote_source_member="source_order",
        execution_member=_text(execution_member),
        member_ioratio_source_member="source_order",
    )


@dataclass(frozen=True)
class SnapshotLookup:
    status: str
    snapshot: Optional[MarketSnapshot] = None
    message: str = ""


class SnapshotUnavailable(RuntimeError):
    def __init__(self, status: str, message: str = "") -> None:
        self.status = _text(status) or SNAPSHOT_MISSING
        self.message = _text(message) or self.status
        super().__init__(self.message)


def _mode_for_intent(intent: MarketIntent) -> str:
    return "live" if intent.is_live else "today"


def _market_key(intent: MarketIntent) -> str:
    if intent.market_type == "TOTAL":
        return "halfOverUnder" if intent.period == "HALF_TIME" else "overUnder"
    if intent.market_type == "HANDICAP":
        return "halfHandicap" if intent.period == "HALF_TIME" else "handicap"
    if intent.market_type == "MONEYLINE":
        return "moneyLine"
    return ""


def _base_wtype(value: Any) -> str:
    text = _text(value).upper()
    if len(text) > 1 and text[0] in "ABCDEF":
        return text[1:]
    return text


def _expected_wtype(intent: MarketIntent) -> str:
    live = intent.is_live
    half = intent.period == "HALF_TIME"
    if intent.market_type == "TOTAL":
        return "HROU" if half and live else "HOU" if half else "ROU" if live else "OU"
    if intent.market_type == "HANDICAP":
        return "HRE" if half and live else "HRE" if half else "RE" if live else "R"
    if intent.market_type == "MONEYLINE":
        return "HRM" if half and live else "HM" if half else "RM" if live else "M"
    return ""


def intent_market_fields(intent: MarketIntent) -> Dict[str, Any]:
    """Return the market identity requested from the Snapshot kitchen."""
    wtype = _expected_wtype(intent)
    chose_team = {
        "OVER": "C",
        "UNDER": "H",
        "HOME": "H",
        "AWAY": "C",
        "DRAW": "N",
    }.get(intent.side, "")
    target_line = intent.target_line
    return {
        "gid": intent.gid,
        "gtype": intent.gtype,
        "market_type": intent.market_type,
        "period": intent.period,
        "is_corner": intent.is_corner,
        "side": intent.side,
        "target_line": target_line,
        "target_line_val": (
            abs(float(target_line))
            if target_line is not None and intent.market_type == "TOTAL"
            else target_line
        ),
        "target_line_txt": (
            f"{abs(float(target_line)):g}"
            if target_line is not None and intent.market_type == "TOTAL"
            else format_handicap_line_value(float(target_line))
            if target_line is not None and intent.market_type == "HANDICAP"
            else ""
        ),
        "is_over": intent.side == "OVER" if intent.market_type == "TOTAL" else None,
        "wtype": wtype,
        "rtype": f"{wtype}{chose_team}" if wtype and chose_team else "",
        "chose_team": chose_team,
        "mode": _mode_for_intent(intent),
    }


def line_matches(
    target_line: Optional[float],
    line_value: Optional[float],
    *,
    market_type: str = "",
    period: str = "",
    is_corner: bool = False,
) -> bool:
    """Match an order display line to a normalized public MarketRow.

    The only non-exact alias is Crown's first-half corner-total convention:
    an order displayed as an integer (``小 5``) is represented by the CN
    ``HROU`` feed as ``4.5``.  This alias is deliberately opt-in and does not
    alter the MarketRow's real line or its con/ratio encoding.
    """
    if target_line is None:
        return True
    if line_value is None:
        return False
    target = float(target_line)
    current = float(line_value)
    if str(market_type or "").upper() == "TOTAL":
        if abs(abs(current) - abs(target)) < 1e-6:
            return True
        if (
            bool(is_corner)
            and str(period or "").upper() == "HALF_TIME"
            and target >= 1.0
            and abs(target - round(target)) < 1e-6
            and abs(abs(current) - (abs(target) - 0.5)) < 1e-6
        ):
            return True
        return False
    return abs(current - target) < 1e-6


def _line_matches(intent: MarketIntent, line_value: Optional[float]) -> bool:
    return line_matches(
        intent.target_line,
        line_value,
        market_type=intent.market_type,
        period=intent.period,
        is_corner=intent.is_corner,
    )


def _valid_odd(value: Any) -> bool:
    try:
        return math.isfinite(float(_text(value)))
    except Exception:
        return False


def _valid_positive_ioratio(value: Any) -> bool:
    """Crown's context quote must be a finite, strictly positive odd."""
    try:
        return math.isfinite(float(_text(value))) and float(_text(value)) > 0.0
    except Exception:
        return False


def _valid_context_ratio(value: Any) -> bool:
    """Validate Crown's encoded submit-time ratio before using it in FT_bet."""
    try:
        return math.isfinite(float(_text(value)))
    except Exception:
        return False


class MarketSnapshotService:
    """Read and normalize the latest local snapshot for one MarketIntent."""

    def __init__(self, cache_path: Path, *, clock: Any = time.time) -> None:
        self.cache_path = Path(cache_path)
        self.clock = clock

    def lookup(
        self,
        intent: MarketIntent,
        *,
        max_age_ms: Optional[int] = 500,
        now: Optional[float] = None,
    ) -> SnapshotLookup:
        now_value = float(self.clock() if now is None else now)
        # ``None`` is an explicit read-current mode used only after the
        # server-side BetContext has validated the submit-time structure.  It
        # still requires a cached exact MarketRow; it merely avoids treating
        # elapsed cache age as a second independent rejection reason.
        max_age_s = (
            float("inf")
            if max_age_ms is None
            else max(0.0, float(max_age_ms or 0) / 1000.0)
        )
        mode = _mode_for_intent(intent)
        fresh = load_snapshot(self.cache_path, gid=intent.gid, mode=mode, now=now_value, max_age_s=max_age_s)
        if fresh is None:
            any_age = load_snapshot(self.cache_path, gid=intent.gid, mode=mode, now=now_value, max_age_s=float("inf"))
            if any_age is None:
                return SnapshotLookup(SNAPSHOT_MISSING, message=f"snapshot missing gid={intent.gid}")
            return SnapshotLookup(SNAPSHOT_STALE, message=f"snapshot stale age_ms={any_age.get('age_ms', 0)}")

        rows = fresh.get("market_rows") if isinstance(fresh.get("market_rows"), list) else []
        expected = _expected_wtype(intent)
        for row in rows:
            if not isinstance(row, dict):
                continue
            # A corner order carries the exact CN full/half gid.  Ordinary FT
            # and CN boards can share HROU/ROU plus the same display line, so
            # market type/period/line alone is not a sufficient binding.
            if intent.is_corner and _text(row.get("gid")) != _text(intent.gid):
                continue
            if _text(row.get("market_type")).upper() != intent.market_type:
                continue
            if _text(row.get("period")).upper() != intent.period:
                continue
            if _text(row.get("side")).upper() != intent.side:
                continue
            if expected and _base_wtype(row.get("wtype")) != _base_wtype(expected):
                continue
            line_value = None if row.get("line") in (None, "") else float(row.get("line"))
            if not _line_matches(intent, line_value):
                continue
            record = dict(fresh)
            record["mode"] = mode
            selected_row = dict(row)
            selected_row["market_row_fingerprint"] = _text(
                selected_row.get("market_row_fingerprint")
                or selected_row.get("fingerprint")
                or market_row_fingerprint(selected_row)
            )
            selected_row["fingerprint"] = selected_row["market_row_fingerprint"]
            record["selected_market_row"] = selected_row
            snapshot = MarketSnapshot(
                gid=_text(row.get("gid")),
                gtype=_text(row.get("gtype") or intent.gtype),
                wtype=_text(row.get("wtype")),
                rtype=_text(row.get("rtype")),
                chose_team=_text(row.get("chose_team")),
                market_type=_text(row.get("market_type")).upper(),
                period=_text(row.get("period")).upper(),
                side=_text(row.get("side")).upper(),
                line=line_value,
                con=_text(row.get("con")),
                ratio=_text(row.get("ratio")),
                revision=int(row.get("revision") or fresh.get("revision") or 0),
                fingerprint=selected_row["market_row_fingerprint"],
                fetched_at=_normalized_timestamp(row.get("fetched_at") or fresh.get("fetched_at")),
                age_ms=int(fresh.get("age_ms") or 0),
                raw=record,
            )
            return SnapshotLookup(SNAPSHOT_VALID, snapshot=snapshot)
        return SnapshotLookup(
            TARGET_LINE_MISSING,
            message=(
                f"target market row missing source_gid={intent.gid} "
                f"market={intent.market_type} side={intent.side} line={intent.target_line}"
            ),
        )

    def get_latest(
        self,
        intent: MarketIntent,
        *,
        max_age_ms: Optional[int] = 500,
        now: Optional[float] = None,
    ) -> MarketSnapshot:
        result = self.lookup(intent, max_age_ms=max_age_ms, now=now)
        if result.snapshot is None:
            raise SnapshotUnavailable(result.status, result.message)
        return result.snapshot

def _normalized_timestamp(value: Any) -> float:
    try:
        timestamp = float(value or 0.0)
    except Exception:
        return 0.0
    return timestamp / 1000.0 if timestamp > 100_000_000_000 else timestamp


class SnapshotValidator:
    """Validate a resolved snapshot against its source intent."""

    @staticmethod
    def _gid_matches(intent: MarketIntent, snapshot: MarketSnapshot) -> bool:
        expected = _text(intent.gid)
        actual = _text(snapshot.gid)
        if expected == actual:
            return True
        raw = snapshot.raw if isinstance(snapshot.raw, dict) else {}
        if expected == _text(raw.get("master_gid") or raw.get("gid")):
            return True
        for item in raw.get("ftGids") or []:
            if isinstance(item, dict) and expected == _text(item.get("gid")):
                return True
        return False

    def validate(
        self,
        intent: MarketIntent,
        snapshot: Optional[MarketSnapshot],
        *,
        max_age_ms: Optional[int] = 500,
        now: Optional[float] = None,
    ) -> SnapshotLookup:
        if snapshot is None:
            return SnapshotLookup(SNAPSHOT_MISSING, message="snapshot is None")
        if not self._gid_matches(intent, snapshot):
            return SnapshotLookup(SNAPSHOT_INVALID, message="gid mismatch")
        if snapshot.market_type != intent.market_type:
            return SnapshotLookup(SNAPSHOT_INVALID, message="market_type mismatch")
        if snapshot.period != intent.period:
            return SnapshotLookup(SNAPSHOT_INVALID, message="period mismatch")
        if snapshot.side != intent.side:
            return SnapshotLookup(SNAPSHOT_INVALID, message="side mismatch")
        if not _line_matches(intent, snapshot.line):
            return SnapshotLookup(TARGET_LINE_MISSING, message="target line mismatch")
        now_value = float(time.time() if now is None else now)
        age_ms = max(0, int(round((now_value - _normalized_timestamp(snapshot.fetched_at)) * 1000.0)))
        if max_age_ms is not None and age_ms > max(0, int(max_age_ms or 0)):
            return SnapshotLookup(SNAPSHOT_STALE, message=f"snapshot stale age_ms={age_ms}")
        for field_name in ("gid", "wtype", "rtype", "chose_team", "con", "ratio"):
            if not _text(getattr(snapshot, field_name)):
                return SnapshotLookup(SNAPSHOT_INVALID, message=f"market row {field_name} missing")
        selected = snapshot.raw.get("selected_market_row") if isinstance(snapshot.raw, dict) else None
        if not isinstance(selected, dict):
            return SnapshotLookup(SNAPSHOT_INVALID, message="selected market row missing")
        for field_name in ("gid", "wtype", "rtype", "chose_team", "con", "ratio"):
            if _text(selected.get(field_name)) != _text(getattr(snapshot, field_name)):
                return SnapshotLookup(SNAPSHOT_INVALID, message=f"market row atomic binding failed field={field_name}")
        selected_line = selected.get("line")
        selected_line_value = None if selected_line in (None, "") else float(selected_line)
        if selected_line_value != snapshot.line:
            return SnapshotLookup(SNAPSHOT_INVALID, message="market row atomic binding failed field=line")
        selected_fingerprint = _text(
            selected.get("market_row_fingerprint")
            or selected.get("fingerprint")
            or market_row_fingerprint(selected)
        )
        if selected_fingerprint != snapshot.market_row_fingerprint:
            return SnapshotLookup(SNAPSHOT_INVALID, message="market row atomic binding failed field=fingerprint")
        return SnapshotLookup(SNAPSHOT_VALID, snapshot=snapshot)


def build_submit_template(
    market_row: MarketSnapshot,
    member_quote: Optional[MemberIoratio],
    *,
    execution_member: str,
    ioratio_override: Any = None,
    ratio_override: Any = None,
    ioratio_source: str = "member_ioratio_cache",
) -> SubmitTemplate:
    if member_quote is not None and _text(member_quote.member_id) != _text(execution_member):
        raise ValueError("member ioratio does not belong to execution member")
    if member_quote is not None and _text(member_quote.gid) != _text(market_row.gid):
        raise ValueError("member ioratio gid does not match selected market row")
    if member_quote is not None and member_quote.line != market_row.line:
        raise ValueError("member ioratio line does not match selected market row")
    quote_fingerprint = _text(member_quote.market_row_fingerprint or market_row_fingerprint(member_quote.to_dict())) if member_quote is not None else ""
    row_fingerprint = _text(market_row.market_row_fingerprint)
    if member_quote is not None and (not row_fingerprint or quote_fingerprint != row_fingerprint):
        raise ValueError("member ioratio market row fingerprint does not match selected market row")
    if ioratio_override is not None and not _valid_positive_ioratio(ioratio_override):
        raise ValueError("context ioratio is not a valid positive odd")
    if ratio_override is not None and _text(market_row.market_type).upper() != "MONEYLINE":
        raise ValueError("dynamic context ratio is only valid for MONEYLINE")
    if ratio_override is not None and not _text(ratio_override):
        raise ValueError("context ratio is empty")
    return SubmitTemplate.from_market_row(
        market_row,
        member_quote,
        execution_member=execution_member,
        ioratio_override=ioratio_override,
        ratio_override=ratio_override,
        ioratio_source=ioratio_source,
    )


@dataclass(frozen=True)
class ExecutionResult:
    status: str
    member_id: str
    ticket_id: str = ""
    error_code: str = ""
    error_message: str = ""
    response: Dict[str, Any] = field(default_factory=dict, compare=False, repr=False)
    request: Dict[str, Any] = field(default_factory=dict, compare=False, repr=False)
    http_status: Optional[int] = None
    raw_response: str = field(default="", compare=False, repr=False)
    xml_code: str = ""
    xml_errormsg: str = ""
    session_meta: Dict[str, Any] = field(default_factory=dict, compare=False, repr=False)
    ft_bet_started_at: float = 0.0
    ft_bet_finished_at: float = 0.0
    ft_bet_elapsed_ms: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "member_id": self.member_id,
            "ticket_id": self.ticket_id,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "response": dict(self.response),
            "request": dict(self.request),
            "http_status": self.http_status,
            "raw_response": self.raw_response,
            "xml_code": self.xml_code,
            "xml_errormsg": self.xml_errormsg,
            "session_meta": dict(self.session_meta),
            "ft_bet_started_at": self.ft_bet_started_at,
            "ft_bet_finished_at": self.ft_bet_finished_at,
            "ft_bet_elapsed_ms": self.ft_bet_elapsed_ms,
        }


@dataclass(frozen=True)
class BetContextResult:
    """One optional, diagnostic-only server context preparation result."""

    request: Dict[str, Any] = field(default_factory=dict, compare=False, repr=False)
    response: Dict[str, Any] = field(default_factory=dict, compare=False, repr=False)
    raw_response: str = field(default="", compare=False, repr=False)
    http_status: Optional[int] = None
    elapsed_ms: int = 0
    error: str = ""
    matches_market_row: bool = False


class BetContext:
    """Prepare the smallest possible server context for a diagnostic A/B run.

    This class deliberately knows nothing about market selection.  The caller
    supplies the already validated MarketRow and the request only echoes its
    immutable gid/wtype/chose_team.  The response is checked against the same
    row; a mismatch is a hard stop and never becomes a FT_bet.
    """

    @staticmethod
    def prepare(
        *,
        session: Any,
        member: Mapping[str, Any],
        market_row: MarketSnapshot,
        expected_ioratio: Any = None,
        timeout_s: int = 8,
    ) -> BetContextResult:
        base_url = _text(member.get("base_url"))
        ver = _text(member.get("ver"))
        uid = _text(member.get("uid"))
        request = {
            "p": "FT_order_view",
            "uid": "[REDACTED]",
            "ver": "[REDACTED]",
            "langx": "zh-cn",
            "odd_f_type": "H",
            "gid": market_row.gid,
            "gtype": market_row.gtype,
            "wtype": market_row.wtype,
            "chose_team": market_row.chose_team,
        }
        wire_form = dict(request)
        wire_form["ver"] = ver
        wire_form["uid"] = uid
        started = time.time()

        # The request is the authoritative binding for fields that the Crown
        # response does not echo (notably gid and chose_team).  Reject before
        # any network call if the request cannot be traced to this MarketRow.
        request_binding = {
            "gid": _text(request.get("gid")),
            "gtype": _text(request.get("gtype")),
            "wtype": _text(request.get("wtype")),
            "chose_team": _text(request.get("chose_team")),
        }
        expected_binding = {
            "gid": _text(market_row.gid),
            "gtype": _text(market_row.gtype),
            "wtype": _text(market_row.wtype),
            "chose_team": _text(market_row.chose_team),
        }
        if request_binding != expected_binding:
            return BetContextResult(
                request=request,
                response={
                    "request_binding": request_binding,
                    "request_binding_expected": expected_binding,
                },
                elapsed_ms=0,
                error=BET_CONTEXT_MISMATCH,
                matches_market_row=False,
            )
        response_meta: Dict[str, Any] = {}
        try:
            raw = member_post_session_once(
                session=session,
                base_url=base_url,
                ver=ver,
                form=wire_form,
                timeout_s=max(2, int(timeout_s or 0)),
                response_meta=response_meta,
            )
            response = parse_xml_flat(str(raw or ""))
        except BaseException as exc:
            elapsed_ms = int(max(0.0, time.time() - started) * 1000.0)
            # FT_order_view is a pre-submit context request.  A transport or
            # upstream gateway failure happens before FT_bet exists, so it is
            # safe to retry with a fresh member/session.  Keep this separate
            # from UNKNOWN_RESULT, which is reserved for an ambiguous FT_bet.
            http_status = response_meta.get("http_status")
            try:
                status_code = int(http_status or 0)
            except Exception:
                status_code = 0
            error_text = f"{type(exc).__name__}: {exc}".lower()
            retryable_transport = status_code in {502, 503, 504} or any(
                token in error_text
                for token in (
                    "connectionerror",
                    "remote end closed",
                    "remotedisconnected",
                    "readtimeout",
                    "read timed out",
                    "connection aborted",
                    "connection reset",
                    "chunkedencodingerror",
                    "timeout",
                )
            )
            return BetContextResult(
                request=request,
                response=dict(response_meta),
                raw_response=_text(response_meta.get("response_body")),
                http_status=http_status,
                elapsed_ms=elapsed_ms,
                error=CONTEXT_RETRYABLE if retryable_transport else f"{type(exc).__name__}: {exc}",
                matches_market_row=False,
            )

        elapsed_ms = int(max(0.0, time.time() - started) * 1000.0)
        code = _text(response.get("code"))
        if code != "501":
            return BetContextResult(
                request=request,
                response=response,
                raw_response=str(raw or ""),
                http_status=response_meta.get("http_status", 200),
                elapsed_ms=elapsed_ms,
                error=f"context_code_not_ok:{code or 'EMPTY'}",
                matches_market_row=False,
            )

        # FT_order_view code=501 does not normally echo gid/chose_team.  Only
        # validate those optional fields when the server actually sends them;
        # the request_binding above is what proves their identity.
        observed = {
            "gid": _text(response.get("gid") or response.get("game_gid")),
            "rtype": _text(response.get("rtype") or response.get("fast_check")),
            "chose_team": _text(response.get("chose_team") or response.get("choseTeam")),
            "con": _text(response.get("con") or response.get("concede")),
            "ratio": _text(response.get("ratio")),
            "ioratio": _text(response.get("ioratio")),
        }
        optional_observed = {}
        if response.get("gid") not in (None, "") or response.get("game_gid") not in (None, ""):
            optional_observed["gid"] = _text(response.get("gid") or response.get("game_gid"))
        if response.get("chose_team") not in (None, "") or response.get("choseTeam") not in (None, ""):
            optional_observed["chose_team"] = _text(response.get("chose_team") or response.get("choseTeam"))
        expected = {
            "rtype": _text(market_row.rtype),
            "con": _text(market_row.con),
            "ratio": _text(market_row.ratio),
        }

        def _numeric_equal(left: Any, right: Any) -> bool:
            if _text(left) == _text(right):
                return True
            try:
                return math.isfinite(float(left)) and math.isfinite(float(right)) and abs(float(left) - float(right)) <= 1e-9
            except Exception:
                return False

        matches = bool(observed["rtype"] and observed["rtype"] == expected["rtype"])
        matches = matches and _text(observed["con"]) == _text(expected["con"])
        # MONEYLINE Context responses carry the server's current encoded
        # price ratio (for example 1630), while the public row uses the
        # structural placeholder ratio=0.  Require a present response value,
        # but do not compare those two different protocol meanings.  Totals
        # and handicaps retain the strict MarketRow ratio binding.
        if _text(market_row.market_type).upper() == "MONEYLINE":
            matches = matches and _valid_context_ratio(observed["ratio"])
        else:
            matches = matches and _text(observed["ratio"]) == _text(expected["ratio"])
        # The public/member cache is only a candidate/diagnostic quote.  The
        # server's code=501 response is the authoritative quote immediately
        # before submit, so a cache/context difference is never a structural
        # mismatch.  A missing, non-numeric, or non-positive context quote is
        # still unsafe and remains a hard failure.
        context_ioratio_valid = _valid_positive_ioratio(observed["ioratio"])
        matches = matches and context_ioratio_valid
        for field_name, value in optional_observed.items():
            if _text(value) != _text(expected_binding[field_name]):
                matches = False
        error = "" if matches else "BET_CONTEXT_MISMATCH"
        response = dict(response)
        response["request_binding"] = request_binding
        response["request_binding_expected"] = expected_binding
        response["context_observed"] = observed
        response["context_expected"] = expected
        response["cached_ioratio"] = _text(expected_ioratio)
        response["context_ioratio"] = observed["ioratio"]
        response["context_optional_fields"] = optional_observed
        response["context_ioratio_match"] = bool(context_ioratio_valid)
        response["context_spread"] = _text(response.get("spread"))
        return BetContextResult(
            request=request,
            response=response,
            raw_response=str(raw or ""),
            http_status=response_meta.get("http_status", 200),
            elapsed_ms=elapsed_ms,
            error=error,
            matches_market_row=matches,
        )


def _session_cookie_diagnostics(session: Any) -> Dict[str, Any]:
    """Return cookie metadata without persisting cookie values."""
    names = []
    domain_path = []
    value_hashes: Dict[str, str] = {}
    jar = getattr(session, "cookies", None)
    try:
        for cookie in jar or []:
            name = _text(getattr(cookie, "name", ""))
            if not name:
                continue
            value = str(getattr(cookie, "value", "") or "")
            names.append(name)
            value_hashes[name] = hashlib.sha256(value.encode("utf-8")).hexdigest()
            domain_path.append(
                {
                    "name": name,
                    "domain_sha256": _sha256_text(getattr(cookie, "domain", "") or ""),
                    "path": str(getattr(cookie, "path", "") or ""),
                }
            )
    except Exception:
        pass
    return {
        "cookie_names": sorted(set(names)),
        "cookie_domain_path": domain_path,
        "cookie_value_hash": value_hashes,
        "cookie_value_hash_set_sha256": _cookie_hash_set(value_hashes),
        "cookie_count": len(set(names)),
    }


def _safe_proxy(value: Any) -> str:
    """Record only whether a proxy was configured; never persist its address."""
    text = _text(value)
    return "configured" if text else ""


class MemberExecutor:
    """Pure FT_bet submitter; it never queries or decides a market."""

    def __init__(self, *, session_factory: Any = make_member_session, post: Any = member_post_session) -> None:
        self._session_factory = session_factory
        self._post = post

    def submit(
        self,
        member: Mapping[str, Any],
        submit_template: SubmitTemplate,
        amount: float,
        *,
        session: Any = None,
        timeout_s: int = 8,
    ) -> ExecutionResult:
        member_id = _text(member.get("id"))
        base_url = _text(member.get("base_url"))
        ver = _text(member.get("ver"))
        uid = _text(member.get("uid"))
        cookie = _text(member.get("cookie"))
        if not (base_url and ver and uid and cookie):
            return ExecutionResult(MEMBER_ERROR, member_id, error_code="SESSION_MISSING", error_message="member session fields missing")
        own_session = session is None
        session_created_at = float(time.time()) if own_session else 0.0
        sess = session or self._session_factory(base_url=base_url, proxy=_text(member.get("proxy")), cookie=cookie)
        try:
            _telemetry_meta = getattr(sess, "_hdzz_telemetry_meta", {})
            _telemetry_meta = dict(_telemetry_meta) if isinstance(_telemetry_meta, dict) else {}
            _telemetry_meta.update({"member_hash": _sha256_text(member_id), "member_id_hash": _sha256_text(member_id)})
            setattr(sess, "_hdzz_telemetry_meta", _telemetry_meta)
        except Exception:
            pass
        build_info = current_build_info()
        session_meta: Dict[str, Any] = {
            "session_mode": "fresh_per_attempt" if own_session else "provided",
            "session_created_at": session_created_at,
            "session_object_id": str(id(sess)),
            "proxy": _safe_proxy(member.get("proxy")),
            "build": {
                "version": _text(build_info.get("version")),
                "git_commit": _text(build_info.get("git_commit")),
                "build_id": _text(build_info.get("build_id")),
            },
            "build_version": _text(build_info.get("version")),
            "git_commit": _text(build_info.get("git_commit")),
            "build_id": _text(build_info.get("build_id")),
            "member_id_sha256": _sha256_text(member_id),
            "member_id_hash": _sha256_text(member_id),
            "uid_sha256": _sha256_text(uid),
            "uid_hash": _sha256_text(uid),
            "base_url": _safe_url_identity(base_url),
            "ver_sha256": _sha256_text(ver),
            "ver_length": len(ver),
        }
        session_meta.update(_session_cookie_diagnostics(sess))
        session_meta["trust_env"] = bool(getattr(sess, "trust_env", True))
        session_meta["proxy_enabled"] = bool(_text(member.get("proxy")))
        form = {
            "ver": ver,
            "p": "FT_bet",
            "uid": uid,
            "langx": "zh-cn",
            "odd_f_type": "H",
            "golds": str(int(float(amount))),
            "gid": submit_template.gid,
            "gtype": submit_template.gtype,
            "wtype": submit_template.wtype,
            "rtype": submit_template.rtype,
            "chose_team": submit_template.chose_team,
            "ioratio": submit_template.ioratio,
            "con": submit_template.con,
            "ratio": submit_template.ratio,
            "autoOdd": "Y",
            "timestamp": str(int(time.time() * 1000)),
            "timestamp2": "",
            "isRB": submit_template.is_rb,
            "imp": "N",
            "ptype": "",
            "isYesterday": "N",
            "f": submit_template.f,
        }
        diagnostic_request = dict(form)
        diagnostic_request["uid"] = "[REDACTED]"
        diagnostic_request["ver"] = "[REDACTED]"
        session_meta["ft_bet_request_fingerprint"] = _request_fingerprint(
            member_id=member_id,
            uid=uid,
            base_url=base_url,
            ver=ver,
            form=form,
            session_meta=session_meta,
            build_info=build_info,
        )
        response_meta: Dict[str, Any] = {}
        ft_bet_started_at = float(time.time())
        ft_bet_finished_at = ft_bet_started_at
        try:
            post_kwargs = {
                "session": sess,
                "base_url": base_url,
                "ver": ver,
                "form": form,
                "timeout_s": max(2, int(timeout_s or 0)),
            }
            if self._post is member_post_session:
                post_kwargs["response_meta"] = response_meta
            text = self._post(**post_kwargs)
            ft_bet_finished_at = float(time.time())
            data = parse_xml_flat(str(text or ""))
            request_fingerprint = dict(session_meta.get("ft_bet_request_fingerprint") or {})
            request_fingerprint["headers"] = dict(response_meta.get("request_headers") or {})
            session_meta["ft_bet_request_fingerprint"] = request_fingerprint
            session_meta["ft_bet_response_fingerprint"] = _response_fingerprint(response_meta, data, str(text or ""))
        except BaseException as exc:
            ft_bet_finished_at = float(time.time())
            request_fingerprint = dict(session_meta.get("ft_bet_request_fingerprint") or {})
            request_fingerprint["headers"] = dict(response_meta.get("request_headers") or {})
            session_meta["ft_bet_request_fingerprint"] = request_fingerprint
            partial_raw = _text(response_meta.get("response_body"))
            try:
                partial_data = parse_xml_flat(partial_raw)
            except Exception:
                partial_data = {}
            session_meta["ft_bet_response_fingerprint"] = _response_fingerprint(response_meta, partial_data, partial_raw)
            try:
                return ExecutionResult(
                    UNKNOWN_RESULT,
                    member_id,
                    error_code=type(exc).__name__,
                    error_message=str(exc),
                    request=diagnostic_request,
                    http_status=response_meta.get("http_status"),
                    raw_response=_text(response_meta.get("response_body")),
                    session_meta=session_meta,
                    ft_bet_started_at=ft_bet_started_at,
                    ft_bet_finished_at=ft_bet_finished_at,
                    ft_bet_elapsed_ms=int(max(0.0, ft_bet_finished_at - ft_bet_started_at) * 1000.0),
                )
            finally:
                if own_session:
                    close_member_session(sess)
        code = _text(data.get("code"))
        error = _text(data.get("errormsg"))
        message = _text(data.get("msg"))
        ticket = _text(data.get("ticket_id") or data.get("ticketId"))
        common = {
            "response": data,
            "request": diagnostic_request,
            "http_status": response_meta.get("http_status", 200 if text is not None else None),
            "raw_response": str(text or ""),
            "xml_code": code,
            "xml_errormsg": error,
        }
        if code == "560" and ticket:
            result = ExecutionResult(SUCCESS, member_id, ticket_id=ticket, **common)
        elif code.lower() == "error" and message.lower() == "doublelogin":
            result = ExecutionResult(MEMBER_ERROR, member_id, error_code="doubleLogin", error_message="会员登录状态异常", **common)
        elif error in {"1X001", "1X003", "1X015", "1X016"} or error.startswith("betError"):
            result = ExecutionResult(MARKET_ERROR, member_id, error_code=error or code, error_message=message or error, **common)
        elif error or code == "555":
            result = ExecutionResult(MEMBER_ERROR, member_id, error_code=error or code, error_message=message or error, **common)
        else:
            result = ExecutionResult(UNKNOWN_RESULT, member_id, error_code=code, error_message=message or error, **common)
        try:
            return ExecutionResult(
                result.status,
                result.member_id,
                ticket_id=result.ticket_id,
                error_code=result.error_code,
                error_message=result.error_message,
                response=result.response,
                request=result.request,
                http_status=result.http_status,
                raw_response=result.raw_response,
                xml_code=result.xml_code,
                xml_errormsg=result.xml_errormsg,
                session_meta=session_meta,
                ft_bet_started_at=ft_bet_started_at,
                ft_bet_finished_at=ft_bet_finished_at,
                ft_bet_elapsed_ms=int(max(0.0, ft_bet_finished_at - ft_bet_started_at) * 1000.0),
            )
        finally:
            if own_session:
                close_member_session(sess)


__all__ = [
    "BET_CONTEXT_MISMATCH",
    "CONTEXT_RETRYABLE",
    "MARKET_CHANGED_DURING_CONTEXT",
    "BetContext",
    "BetContextResult",
    "ExecutionResult",
    "MARKET_ERROR",
    "MARKET_SUBMIT_REJECTED",
    "MEMBER_ERROR",
    "MarketIntent",
    "MarketSnapshot",
    "MarketSnapshotService",
    "SNAPSHOT_INVALID",
    "SNAPSHOT_MISSING",
    "SNAPSHOT_STALE",
    "SNAPSHOT_VALID",
    "SOURCE_ORDER_DIRECT",
    "SOURCE_ORDER_READY",
    "SnapshotLookup",
    "SnapshotUnavailable",
    "SnapshotValidator",
    "SubmitTemplate",
    "TARGET_LINE_MISSING",
    "UNKNOWN_RESULT",
    "SUCCESS",
    "build_source_order_submit_template",
    "build_submit_template",
    "intent_market_fields",
    "line_matches",
]
