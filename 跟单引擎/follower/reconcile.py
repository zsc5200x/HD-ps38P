from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Optional


def _text(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
        if abs(number) > 100_000_000_000:
            number /= 1000.0
        return number
    except (TypeError, ValueError):
        text = _text(value)
        if not text:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(text[:19], fmt).timestamp()
            except ValueError:
                continue
        return None


def _first(record: Dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        value = record.get(key)
        if value not in (None, ""):
            return value
    return None


def extract_wagers(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("wagers", "WAGERS", "rows", "list", "records", "dataList", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    data = payload.get("data")
    if data is not payload:
        found = extract_wagers(data)
        if found:
            return found
    for value in payload.values():
        if isinstance(value, dict):
            found = extract_wagers(value)
            if found:
                return found
        elif isinstance(value, list) and any(isinstance(item, dict) for item in value):
            found = [item for item in value if isinstance(item, dict)]
            if found:
                return found
    return []


def match_recent_wager(
    wagers: Iterable[Dict[str, Any]],
    *,
    member_id: str,
    gid: str,
    stake: float,
    submitted_at: float,
    window_seconds: float = 90.0,
) -> Optional[Dict[str, Any]]:
    expected_member = _text(member_id)
    expected_gid = _text(gid)
    expected_stake = float(stake or 0.0)
    submitted = float(submitted_at or 0.0)
    window = max(1.0, float(window_seconds or 0.0))
    matches = []

    for raw in wagers:
        if not isinstance(raw, dict):
            continue
        ticket = _text(_first(raw, ("ticket_id", "ticketId", "w_id", "wId", "tid", "wager_id", "wagerId", "bet_id", "betId")))
        if not ticket:
            continue
        actual_member = _text(_first(raw, ("member_id", "memberId", "member", "username", "uid", "UID")))
        if expected_member and actual_member != expected_member:
            continue
        actual_gid = _text(_first(raw, ("gid", "GID", "game_id", "gameId", "event_id", "eventId")))
        if expected_gid and actual_gid != expected_gid:
            continue
        actual_stake = _number(_first(raw, ("gold", "golds", "GOLD", "GOLDS", "stake", "amount", "bet_amount")))
        if actual_stake is None or abs(actual_stake - expected_stake) > 0.01:
            continue
        wager_ts = _number(_first(raw, ("ts", "bet_ts", "created_at", "create_time", "timestamp", "wager_time", "wagerTime", "bet_time", "betTime")))
        if wager_ts is None or submitted <= 0 or abs(wager_ts - submitted) > window:
            continue
        rec = dict(raw)
        rec["ticket_id"] = ticket
        matches.append((abs(wager_ts - submitted), rec))

    if not matches:
        return None
    matches.sort(key=lambda item: item[0])
    return matches[0][1]


def reconcile_uncertain_bet(
    fetch_payload: Callable[[], Any],
    *,
    member_id: str,
    gid: str,
    stake: float,
    submitted_at: float,
    window_seconds: float = 90.0,
) -> Dict[str, Any]:
    try:
        payload = fetch_payload()
    except Exception as exc:
        return {"status": "unavailable", "error": f"{type(exc).__name__}: {exc}"}
    match = match_recent_wager(
        extract_wagers(payload),
        member_id=member_id,
        gid=gid,
        stake=stake,
        submitted_at=submitted_at,
        window_seconds=window_seconds,
    )
    if match:
        return {"status": "matched", "ticket_id": _text(match.get("ticket_id")), "wager": match}
    return {"status": "not_found"}
