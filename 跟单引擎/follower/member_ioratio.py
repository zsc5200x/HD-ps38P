from __future__ import annotations

import copy
import json
import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from hdzz.common.config import atomic_write_json, read_json

from .market_rows import MarketRow, extract_market_rows, iter_rows_with_ioratio, market_row_fingerprint


MEMBER_IORATIO_MISSING = "MEMBER_IORATIO_MISSING"
MEMBER_IORATIO_STALE = "MEMBER_IORATIO_STALE"
MEMBER_IORATIO_INVALID = "MEMBER_IORATIO_INVALID"
MEMBER_IORATIO_VALID = "VALID"

MEMBER_IORATIO_CACHE_VERSION = 1
MEMBER_IORATIO_CACHE_MAX_QUOTES = 8192
_CACHE_LOCK = threading.RLock()
_PRIORITY_REQUEST_LOCK = threading.RLock()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _line_key(value: Any) -> str:
    if value in (None, ""):
        return "-"
    try:
        number = float(value)
    except Exception:
        return _text(value)
    return f"{number:.6f}".rstrip("0").rstrip(".") or "0"


def member_ioratio_cache_key(
    *,
    member_id: Any,
    mode: Any,
    gid: Any,
    market_type: Any,
    period: Any,
    side: Any,
    line: Any,
) -> str:
    return "|".join(
        (
            _text(member_id),
            _text(mode).lower() or "live",
            _text(gid),
            _text(market_type).upper(),
            _text(period).upper(),
            _text(side).upper(),
            _line_key(line),
        )
    )


def _valid_ioratio(value: Any) -> bool:
    try:
        return math.isfinite(float(_text(value)))
    except Exception:
        return False


def _read_document(path: Path) -> Dict[str, Any]:
    doc = read_json(Path(path))
    quotes = doc.get("quotes") if isinstance(doc.get("quotes"), dict) else {}
    return {"version": MEMBER_IORATIO_CACHE_VERSION, "quotes": quotes}


def request_member_ioratio_refresh(
    data_dir: Path,
    *,
    member_id: str,
    market_row: MarketRow,
    mode: str = "live",
) -> Dict[str, Any]:
    """Queue a kitchen-side refresh for one member and one exact MarketRow.

    The follow engine and odds watcher are separate processes.  This small
    JSONL inbox keeps the refresh request out of ``MemberExecutor`` while
    allowing the watcher to perform the network read with its pooled sessions.
    """

    request = {
        "request_id": f"{_text(member_id)}:{market_row.market_row_fingerprint}:{time.time_ns()}",
        "member_id": _text(member_id),
        "mode": _text(mode).lower() or "live",
        "created_at": float(time.time()),
        "market_row": market_row.to_dict(),
        "priority_refresh": True,
        # The exact row owns the child gid, while the kitchen may need the
        # source/master gid to resolve the board before selecting that row.
        "source_gid": _text((market_row.raw or {}).get("gid") or (market_row.raw or {}).get("requested_gid") or market_row.gid),
    }
    path = Path(data_dir) / "member_ioratio_refresh_requests.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n"
    with _PRIORITY_REQUEST_LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
    return copy.deepcopy(request)


@dataclass(frozen=True)
class MemberIoratio:
    member_id: str
    member_label: str
    gid: str
    market_type: str
    period: str
    side: str
    line: Optional[float]
    ioratio: str
    fetched_at: float
    age_ms: int
    public_snapshot_revision: int
    wtype: str = ""
    rtype: str = ""
    chose_team: str = ""
    con: str = ""
    ratio: str = ""
    market_row_fingerprint: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "member_id": self.member_id,
            "member_label": self.member_label,
            "gid": self.gid,
            "market_type": self.market_type,
            "period": self.period,
            "side": self.side,
            "line": self.line,
            "ioratio": self.ioratio,
            "fetched_at": self.fetched_at,
            "age_ms": self.age_ms,
            "public_snapshot_revision": self.public_snapshot_revision,
            "wtype": self.wtype,
            "rtype": self.rtype,
            "chose_team": self.chose_team,
            "con": self.con,
            "ratio": self.ratio,
            "market_row_fingerprint": self.market_row_fingerprint,
        }


@dataclass(frozen=True)
class MemberIoratioLookup:
    status: str
    quote: Optional[MemberIoratio] = None
    message: str = ""


def store_member_snapshot_ioratios(
    path: Path,
    *,
    member: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    mode: str,
    requested_gid: str,
    public_snapshot_revision: int,
    fetched_at: Optional[float] = None,
) -> Dict[str, Any]:
    member_id = _text(member.get("id"))
    if not member_id:
        raise ValueError("member_id_missing")
    fetched = float(time.time() if fetched_at is None else fetched_at)
    member_label = _text(member.get("label") or member.get("username") or member_id)
    rows = list(
        iter_rows_with_ioratio(
            extract_market_rows(
                snapshot,
                mode=mode,
                requested_gid=requested_gid,
                include_ioratio=True,
            )
        )
    )
    stored_keys = []
    with _CACHE_LOCK:
        doc = _read_document(Path(path))
        quotes = doc["quotes"]
        for row in rows:
            key = member_ioratio_cache_key(
                member_id=member_id,
                mode=mode,
                gid=row.get("gid"),
                market_type=row.get("market_type"),
                period=row.get("period"),
                side=row.get("side"),
                line=row.get("line"),
            )
            record = dict(row)
            record["market_row_fingerprint"] = str(
                record.get("market_row_fingerprint")
                or record.get("fingerprint")
                or market_row_fingerprint(record)
            )
            record.update(
                {
                    "member_id": member_id,
                    "member_label": member_label,
                    "mode": _text(mode).lower() or "live",
                    "fetched_at": fetched,
                    "public_snapshot_revision": int(public_snapshot_revision or 0),
                }
            )
            quotes[key] = record
            stored_keys.append(key)

        if len(quotes) > MEMBER_IORATIO_CACHE_MAX_QUOTES:
            keep = {
                key
                for key, _ in sorted(
                    quotes.items(),
                    key=lambda item: float((item[1] or {}).get("fetched_at") or 0.0),
                    reverse=True,
                )[:MEMBER_IORATIO_CACHE_MAX_QUOTES]
            }
            quotes = {key: value for key, value in quotes.items() if key in keep}
        atomic_write_json(
            Path(path),
            {"version": MEMBER_IORATIO_CACHE_VERSION, "quotes": quotes},
        )
    return {"member_id": member_id, "stored_count": len(stored_keys), "keys": stored_keys}


def store_member_ioratio(
    path: Path,
    *,
    member_id: str,
    member_label: str,
    mode: str,
    market_row: MarketRow,
    ioratio: str,
    fetched_at: Optional[float] = None,
) -> Dict[str, Any]:
    """Store one exact row quote; primarily useful for deterministic callers/tests."""

    record = market_row.to_dict()
    record["market_row_fingerprint"] = market_row.market_row_fingerprint
    record["ioratio"] = _text(ioratio)
    member = {"id": member_id, "label": member_label}
    fetched = float(time.time() if fetched_at is None else fetched_at)
    key = member_ioratio_cache_key(
        member_id=member_id,
        mode=mode,
        gid=market_row.gid,
        market_type=market_row.market_type,
        period=market_row.period,
        side=market_row.side,
        line=market_row.line,
    )
    with _CACHE_LOCK:
        doc = _read_document(Path(path))
        record.update(
            {
                "member_id": _text(member.get("id")),
                "member_label": _text(member.get("label")),
                "mode": _text(mode).lower() or "live",
                "fetched_at": fetched,
                "public_snapshot_revision": int(market_row.revision or 0),
            }
        )
        doc["quotes"][key] = record
        atomic_write_json(Path(path), doc)
    return copy.deepcopy(record)


class MemberIoratioService:
    def __init__(self, cache_path: Path, *, clock: Any = time.time) -> None:
        self.cache_path = Path(cache_path)
        self.clock = clock

    def lookup(
        self,
        member_id: str,
        market_row: MarketRow,
        *,
        max_age_ms: int,
        now: Optional[float] = None,
    ) -> MemberIoratioLookup:
        now_value = float(self.clock() if now is None else now)
        mode = _text((market_row.raw or {}).get("mode") or "live").lower() or "live"
        key = member_ioratio_cache_key(
            member_id=member_id,
            mode=mode,
            gid=market_row.gid,
            market_type=market_row.market_type,
            period=market_row.period,
            side=market_row.side,
            line=market_row.line,
        )
        with _CACHE_LOCK:
            record = copy.deepcopy(_read_document(self.cache_path)["quotes"].get(key))
        if not isinstance(record, dict):
            return MemberIoratioLookup(
                MEMBER_IORATIO_MISSING,
                message=(
                    f"member ioratio missing member_id={member_id} row_gid={market_row.gid} "
                    f"line={market_row.line}"
                ),
            )
        if _text(record.get("member_id")) != _text(member_id):
            return MemberIoratioLookup(
                MEMBER_IORATIO_INVALID,
                message="member quote cache member_id mismatch",
            )
        for field_name in ("gid", "wtype", "rtype", "chose_team", "con", "ratio"):
            if _text(record.get(field_name)) != _text(getattr(market_row, field_name)):
                return MemberIoratioLookup(
                    MEMBER_IORATIO_INVALID,
                    message=f"member quote row mismatch field={field_name}",
                )
        if _line_key(record.get("line")) != _line_key(market_row.line):
            return MemberIoratioLookup(MEMBER_IORATIO_INVALID, message="member quote row mismatch field=line")
        quote_fingerprint = _text(
            record.get("market_row_fingerprint")
            or record.get("fingerprint")
            or market_row_fingerprint(record)
        )
        current_fingerprint = _text(market_row.market_row_fingerprint)
        if not quote_fingerprint or quote_fingerprint != current_fingerprint:
            return MemberIoratioLookup(
                MEMBER_IORATIO_STALE,
                message=(
                    "member ioratio market row fingerprint mismatch "
                    f"quote={quote_fingerprint or '-'} public={current_fingerprint or '-'}"
                ),
            )
        if not _valid_ioratio(record.get("ioratio")):
            return MemberIoratioLookup(MEMBER_IORATIO_INVALID, message="member ioratio invalid")
        try:
            fetched_at = float(record.get("fetched_at") or 0.0)
        except Exception:
            fetched_at = 0.0
        age_ms = max(0, int(round((now_value - fetched_at) * 1000.0)))
        if fetched_at <= 0 or age_ms > max(0, int(max_age_ms or 0)):
            return MemberIoratioLookup(
                MEMBER_IORATIO_STALE,
                message=f"member ioratio stale age_ms={age_ms}",
            )
        quote = MemberIoratio(
            member_id=_text(record.get("member_id")),
            member_label=_text(record.get("member_label")),
            gid=_text(record.get("gid")),
            market_type=_text(record.get("market_type")).upper(),
            period=_text(record.get("period")).upper(),
            side=_text(record.get("side")).upper(),
            line=None if record.get("line") in (None, "") else float(record.get("line")),
            ioratio=_text(record.get("ioratio")),
            fetched_at=fetched_at,
            age_ms=age_ms,
            public_snapshot_revision=int(record.get("public_snapshot_revision") or 0),
            wtype=_text(record.get("wtype")),
            rtype=_text(record.get("rtype")),
            chose_team=_text(record.get("chose_team")),
            con=_text(record.get("con")),
            ratio=_text(record.get("ratio")),
            market_row_fingerprint=quote_fingerprint,
        )
        return MemberIoratioLookup(MEMBER_IORATIO_VALID, quote=quote)


__all__ = [
    "MEMBER_IORATIO_INVALID",
    "MEMBER_IORATIO_MISSING",
    "MEMBER_IORATIO_STALE",
    "MEMBER_IORATIO_VALID",
    "MemberIoratio",
    "MemberIoratioLookup",
    "MemberIoratioService",
    "member_ioratio_cache_key",
    "request_member_ioratio_refresh",
    "store_member_ioratio",
    "store_member_snapshot_ioratios",
]
