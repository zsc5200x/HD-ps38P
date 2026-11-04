"""Telemetry writer for the Market Snapshot watcher."""

from __future__ import annotations

import json
import math
import threading
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence


SNAPSHOT_WATCH_EVENTS_FILE = "snapshot_watch_metrics.jsonl"
SNAPSHOT_PUBLISH_EVENTS_FILE = "snapshot_publish_events.jsonl"
_WRITE_LOCK = threading.RLock()
_PUBLISH_EVENT_SEQ: Dict[str, int] = {}


def _float(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except Exception:
        return None
    return parsed if math.isfinite(parsed) else None


def _percentile(values: Sequence[float], fraction: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    pos = (len(ordered) - 1) * float(fraction)
    low = int(math.floor(pos))
    high = int(math.ceil(pos))
    if low == high:
        return round(ordered[low], 3)
    return round(ordered[low] * (high - pos) + ordered[high] * (pos - low), 3)


def _distribution(values: Iterable[Any]) -> Dict[str, Any]:
    parsed = [value for value in (_float(item) for item in values) if value is not None]
    return {
        "count": len(parsed),
        "avg": round(sum(parsed) / len(parsed), 3) if parsed else None,
        "p50": _percentile(parsed, 0.50),
        "p95": _percentile(parsed, 0.95),
        "p99": _percentile(parsed, 0.99),
        "min": round(min(parsed), 3) if parsed else None,
        "max": round(max(parsed), 3) if parsed else None,
    }


def summarise_snapshot_watch_events(events: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    all_rows = [dict(row) for row in events]
    member_quote_rows = [
        row for row in all_rows
        if str(row.get("event") or "").strip() == "member_ioratio_refresh"
    ]
    rows = [
        row for row in all_rows
        if str(row.get("event") or "").strip() != "member_ioratio_refresh"
    ]
    successes = [row for row in rows if bool(row.get("ok"))]
    failures = [row for row in rows if not bool(row.get("ok"))]
    by_gid: Dict[str, List[float]] = defaultdict(list)
    for row in successes:
        gid = str(row.get("gid") or "").strip()
        fetched_at = _float(row.get("snapshot_fetched_at") or row.get("finished_at"))
        if gid and fetched_at is not None:
            by_gid[gid].append(fetched_at)
    periods: List[float] = []
    for values in by_gid.values():
        values.sort()
        periods.extend((right - left) * 1000.0 for left, right in zip(values, values[1:]) if right >= left)
    errors = Counter(str(row.get("error_class") or row.get("error") or "") for row in failures)
    return {
        "total_refresh": len(rows),
        "success_count": len(successes),
        "failure_count": len(failures),
        "success_rate": round(len(successes) / len(rows), 6) if rows else None,
        "active_match_count": _distribution(row.get("active_match_count") for row in rows),
        "refresh_elapsed_ms": _distribution(row.get("refresh_elapsed_ms") for row in rows),
        "per_match_update_period_ms": _distribution(periods),
        "active_gid_count": len(by_gid),
        "error_counts": dict(errors),
        "events_available": bool(rows),
        "member_ioratio_refresh": {
            "total": len(member_quote_rows),
            "success_count": sum(1 for row in member_quote_rows if bool(row.get("ok"))),
            "failure_count": sum(1 for row in member_quote_rows if not bool(row.get("ok"))),
            "elapsed_ms": _distribution(row.get("elapsed_ms") for row in member_quote_rows),
        },
    }


def record_snapshot_watch_event(data_dir: Path, event: Mapping[str, Any]) -> None:
    path = Path(data_dir) / SNAPSHOT_WATCH_EVENTS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(dict(event), ensure_ascii=False, separators=(",", ":"))
    with _WRITE_LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def record_snapshot_publish_event(data_dir: Path, event: Mapping[str, Any]) -> None:
    """Append a small cross-process wake event for active follow jobs."""
    path = Path(data_dir) / SNAPSHOT_PUBLISH_EVENTS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with _WRITE_LOCK:
        key = str(path.resolve())
        seq = int(_PUBLISH_EVENT_SEQ.get(key) or 0)
        if seq <= 0 and path.exists():
            # Continue the sequence after a watcher restart.  Only a bounded
            # tail is read so a long-running event log does not turn publish
            # into an O(n) operation.
            try:
                with path.open("rb") as handle:
                    handle.seek(0, 2)
                    size = handle.tell()
                    handle.seek(max(0, size - 65536))
                    tail = handle.read().decode("utf-8", errors="ignore")
                for raw_line in reversed(tail.splitlines()):
                    try:
                        previous = json.loads(raw_line)
                    except Exception:
                        continue
                    try:
                        seq = int(previous.get("event_seq") or 0)
                    except Exception:
                        seq = 0
                    if seq > 0:
                        break
            except Exception:
                seq = 0
        seq += 1
        _PUBLISH_EVENT_SEQ[key] = seq
        payload = dict(event)
        payload["event_seq"] = seq
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


__all__ = [
    "SNAPSHOT_WATCH_EVENTS_FILE",
    "SNAPSHOT_PUBLISH_EVENTS_FILE",
    "record_snapshot_watch_event",
    "record_snapshot_publish_event",
    "summarise_snapshot_watch_events",
]
