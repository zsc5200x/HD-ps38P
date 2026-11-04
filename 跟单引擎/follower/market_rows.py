from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .strategy import decode_handicap_line


def _text(value: Any) -> str:
    return str(value or "").strip()


def _parse_line(value: Any, *, signed: bool = False) -> Optional[float]:
    text = re.sub(r"<[^>]+>", " ", _text(value))
    split = re.search(
        r"(?P<sign>[+-])?\s*(?P<a>\d+(?:\.\d+)?)\s*(?:/|至|～|~)\s*(?P<b>\d+(?:\.\d+)?)",
        text,
    )
    if split:
        result = (float(split.group("a")) + float(split.group("b"))) / 2.0
        return -result if signed and split.group("sign") == "-" else result
    match = re.search(r"(?P<sign>[+-])?\s*(?P<n>\d+(?:\.\d+)?)", text)
    if not match:
        return None
    result = float(match.group("n"))
    return -result if signed and match.group("sign") == "-" else result


def _valid_ioratio(value: Any) -> bool:
    try:
        return math.isfinite(float(_text(value)))
    except Exception:
        return False


_MARKET_ROW_FINGERPRINT_FIELDS = (
    "gid",
    "gtype",
    "wtype",
    "rtype",
    "chose_team",
    "market_type",
    "period",
    "side",
    "line",
    "con",
    "ratio",
)


def market_row_fingerprint(row: Mapping[str, Any]) -> str:
    """Return a stable identity for one exact market line.

    The public snapshot revision describes the whole fetched board and can
    advance because another line changed.  Quote validity must instead be
    tied to this exact row identity.
    """

    values: Dict[str, Any] = {}
    for field_name in _MARKET_ROW_FINGERPRINT_FIELDS:
        value = row.get(field_name)
        if field_name == "line":
            value = None if value in (None, "") else _line_key(value)
        else:
            value = _text(value)
        values[field_name] = value
    encoded = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _line_key(value: Any) -> Any:
    try:
        number = float(value)
    except Exception:
        return _text(value)
    return number


def _expected_wtype(*, mode: str, market_type: str, period: str) -> str:
    live = _text(mode).lower() == "live"
    half = _text(period).upper() == "HALF_TIME"
    if market_type == "TOTAL":
        return "HROU" if half and live else "HOU" if half else "ROU" if live else "OU"
    if market_type == "HANDICAP":
        return "HRE" if half else "RE" if live else "R"
    if market_type == "MONEYLINE":
        return "HRM" if half and live else "HM" if half else "RM" if live else "M"
    return ""


def _row_line(
    *,
    market_type: str,
    line_obj: Mapping[str, Any],
    side_obj: Mapping[str, Any],
    params: Mapping[str, Any],
) -> Optional[float]:
    if market_type == "TOTAL":
        return _parse_line(
            line_obj.get("rawLine") or line_obj.get("line") or side_obj.get("line"),
            signed=False,
        )
    if market_type == "HANDICAP":
        decoded = decode_handicap_line(params.get("con"), params.get("ratio"))
        if decoded is not None:
            return float(decoded)
        return _parse_line(side_obj.get("line") or line_obj.get("rawLine") or line_obj.get("line"), signed=True)
    return None


@dataclass(frozen=True)
class MarketRow:
    """One immutable, line-specific public market structure."""

    gid: str
    gtype: str
    wtype: str
    rtype: str
    chose_team: str
    market_type: str
    period: str
    side: str
    line: Optional[float]
    con: str
    ratio: str
    revision: int
    fetched_at: float
    age_ms: int = 0
    fingerprint: str = ""
    raw: Dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not self.fingerprint:
            object.__setattr__(
                self,
                "fingerprint",
                market_row_fingerprint(
                    {
                        field_name: getattr(self, field_name)
                        for field_name in _MARKET_ROW_FINGERPRINT_FIELDS
                    }
                ),
            )

    @property
    def market_row_fingerprint(self) -> str:
        """Named alias used by member quote synchronization diagnostics."""

        return self.fingerprint or market_row_fingerprint(self.to_dict())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "gid": self.gid,
            "gtype": self.gtype,
            "wtype": self.wtype,
            "rtype": self.rtype,
            "chose_team": self.chose_team,
            "market_type": self.market_type,
            "period": self.period,
            "side": self.side,
            "line": self.line,
            "con": self.con,
            "ratio": self.ratio,
            "revision": self.revision,
            "fetched_at": self.fetched_at,
            "age_ms": self.age_ms,
            "fingerprint": self.fingerprint,
            "market_row_fingerprint": self.market_row_fingerprint,
        }


def extract_market_rows(
    snapshot: Mapping[str, Any],
    *,
    mode: str,
    requested_gid: str = "",
    include_ioratio: bool = False,
) -> List[Dict[str, Any]]:
    """Flatten parsed boards into atomic rows without combining fields across lines."""

    simple = snapshot.get("simple") if isinstance(snapshot.get("simple"), Mapping) else {}
    meta = snapshot.get("meta") if isinstance(snapshot.get("meta"), Mapping) else {}
    default_gid = _text(meta.get("masterGid") or requested_gid)
    gtype = _text(meta.get("gtype") or snapshot.get("gtype") or "FT") or "FT"
    rows: List[Dict[str, Any]] = []

    sections = (
        ("handicap", "HANDICAP", "FULL_TIME", ("home", "away")),
        ("halfHandicap", "HANDICAP", "HALF_TIME", ("home", "away")),
        ("overUnder", "TOTAL", "FULL_TIME", ("over", "under")),
        ("halfOverUnder", "TOTAL", "HALF_TIME", ("over", "under")),
    )
    side_names = {"home": "HOME", "away": "AWAY", "over": "OVER", "under": "UNDER"}
    chose_names = {"home": "H", "away": "C", "over": "C", "under": "H"}

    for section_name, market_type, period, side_keys in sections:
        section = simple.get(section_name) if isinstance(simple.get(section_name), Mapping) else {}
        lines = section.get("lines") if isinstance(section.get("lines"), list) else []
        for line_obj in lines:
            if not isinstance(line_obj, Mapping):
                continue
            for side_key in side_keys:
                side_obj = line_obj.get(side_key) if isinstance(line_obj.get(side_key), Mapping) else None
                if side_obj is None:
                    continue
                params = side_obj.get("params") if isinstance(side_obj.get("params"), Mapping) else {}
                wtype = _text(
                    params.get("wtype")
                    or line_obj.get("wtype")
                    or _expected_wtype(mode=mode, market_type=market_type, period=period)
                )
                chose_team = _text(params.get("chose_team") or chose_names[side_key])
                row: Dict[str, Any] = {
                    # Never borrow the match/master gid for a line. If neither
                    # the side params nor this line owns a gid, validation must
                    # reject the row instead of submitting a different market.
                    "gid": _text(params.get("gid") or line_obj.get("gid")),
                    "gtype": gtype,
                    "wtype": wtype,
                    "rtype": _text(params.get("rtype") or (f"{wtype}{chose_team}" if wtype else "")),
                    "chose_team": chose_team,
                    "market_type": market_type,
                    "period": period,
                    "side": side_names[side_key],
                    "line": _row_line(
                        market_type=market_type,
                        line_obj=line_obj,
                        side_obj=side_obj,
                        params=params,
                    ),
                    "con": _text(params.get("con")),
                    "ratio": _text(params.get("ratio")),
                }
                row["market_row_fingerprint"] = market_row_fingerprint(row)
                # Keep the historical key for existing cache readers.
                row["fingerprint"] = row["market_row_fingerprint"]
                odd = params.get("ioratio") if params.get("ioratio") not in (None, "") else side_obj.get("odd")
                if include_ioratio and _valid_ioratio(odd):
                    row["ioratio"] = _text(odd)
                rows.append(row)

    money_sections = [("moneyLine", "FULL_TIME")]
    if isinstance(simple.get("halfMoneyLine"), Mapping):
        money_sections.append(("halfMoneyLine", "HALF_TIME"))
    elif isinstance(simple.get("moneyLine"), Mapping) and _text(simple["moneyLine"].get("period")).upper() == "HALF_TIME":
        # Backward compatibility for a short-lived cache shape that stored
        # the half-time object under moneyLine.
        money_sections = [("moneyLine", "HALF_TIME")]

    for money_key, default_period in money_sections:
        money = simple.get(money_key) if isinstance(simple.get(money_key), Mapping) else {}
        if not money or bool(money.get("empty", False)):
            continue
        money_period = _text(money.get("period") or default_period).upper() or default_period
        wtype_default = _expected_wtype(mode=mode, market_type="MONEYLINE", period=money_period)
        for side_key, side, chose_team in (
            ("home", "HOME", "H"),
            ("away", "AWAY", "C"),
            ("draw", "DRAW", "N"),
        ):
            side_obj = money.get(side_key) if isinstance(money.get(side_key), Mapping) else None
            if side_obj is None:
                continue
            params = side_obj.get("params") if isinstance(side_obj.get("params"), Mapping) else {}
            wtype = _text(params.get("wtype") or money.get("wtype") or wtype_default)
            chose = _text(params.get("chose_team") or chose_team)
            row = {
                "gid": _text(params.get("gid") or money.get("gid") or default_gid),
                "gtype": gtype,
                "wtype": wtype,
                "rtype": _text(params.get("rtype") or (f"{wtype}{chose}" if wtype else "")),
                "chose_team": chose,
                "market_type": "MONEYLINE",
                "period": money_period,
                "side": side,
                "line": None,
                "con": _text(params.get("con") or "0"),
                "ratio": _text(params.get("ratio") or "0"),
            }
            row["market_row_fingerprint"] = market_row_fingerprint(row)
            row["fingerprint"] = row["market_row_fingerprint"]
            odd = params.get("ioratio") if params.get("ioratio") not in (None, "") else side_obj.get("odd")
            if include_ioratio and _valid_ioratio(odd):
                row["ioratio"] = _text(odd)
            rows.append(row)
    return rows


def _without_member_odds(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _without_member_odds(item)
            for key, item in value.items()
            if str(key).strip().lower() not in {"ioratio", "odd"}
        }
    if isinstance(value, list):
        return [_without_member_odds(item) for item in value]
    return copy.deepcopy(value)


def public_snapshot_payload(snapshot: Mapping[str, Any], *, mode: str, requested_gid: str) -> Dict[str, Any]:
    """Return a public snapshot that contains structure only, never shared odds."""

    payload = _without_member_odds(snapshot)
    if not isinstance(payload, dict):
        payload = {}
    payload["market_rows"] = extract_market_rows(
        snapshot,
        mode=mode,
        requested_gid=requested_gid,
        include_ioratio=False,
    )
    return payload


def iter_rows_with_ioratio(rows: Iterable[Mapping[str, Any]]) -> Iterable[Dict[str, Any]]:
    for row in rows:
        if not isinstance(row, Mapping) or not _valid_ioratio(row.get("ioratio")):
            continue
        yield dict(row)


__all__ = [
    "MarketRow",
    "extract_market_rows",
    "iter_rows_with_ioratio",
    "market_row_fingerprint",
    "public_snapshot_payload",
]
