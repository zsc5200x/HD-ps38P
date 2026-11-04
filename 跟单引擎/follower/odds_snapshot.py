from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Set

from hdzz.common.config import atomic_write_json, read_json

from .market_rows import public_snapshot_payload


SNAPSHOT_CACHE_VERSION = 1
SNAPSHOT_CACHE_MAX_MATCHES = 512
_CACHE_LOCK = threading.RLock()
_KEY_LOCKS_LOCK = threading.Lock()
_KEY_LOCKS: Dict[str, threading.Lock] = {}


def _clean_mode(value: Any) -> str:
    mode = str(value or "live").strip().lower()
    return mode if mode in {"live", "today", "hot", "early"} else "live"


def _clean_gid(value: Any) -> str:
    return str(value or "").strip()


def snapshot_cache_key(gid: Any, mode: Any) -> str:
    gid_text = _clean_gid(gid)
    if not gid_text:
        return ""
    return f"{_clean_mode(mode)}|{gid_text}"


def _key_lock(gid: Any, mode: Any) -> threading.Lock:
    key = snapshot_cache_key(gid, mode) or "__invalid__"
    with _KEY_LOCKS_LOCK:
        lock = _KEY_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _KEY_LOCKS[key] = lock
        return lock


def _market_fingerprint(snapshot: Dict[str, Any]) -> str:
    meta = snapshot.get("meta") if isinstance(snapshot.get("meta"), dict) else {}
    payload = {
        "simple": snapshot.get("simple") if isinstance(snapshot.get("simple"), dict) else {},
        "ftGids": snapshot.get("ftGids") if isinstance(snapshot.get("ftGids"), list) else [],
        "score": meta.get("score"),
        "masterGid": meta.get("masterGid"),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _canonical_gid(snapshot: Dict[str, Any], requested_gid: str) -> str:
    meta = snapshot.get("meta") if isinstance(snapshot.get("meta"), dict) else {}
    master_gid = _clean_gid(meta.get("masterGid"))
    if master_gid:
        return master_gid
    for item in snapshot.get("ftGids") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("master") or "").strip().upper() == "Y":
            gid = _clean_gid(item.get("gid"))
            if gid:
                return gid
    return requested_gid


def _snapshot_gids(snapshot: Dict[str, Any], requested_gid: str, canonical_gid: str) -> Set[str]:
    out = {requested_gid, canonical_gid}
    meta = snapshot.get("meta") if isinstance(snapshot.get("meta"), dict) else {}
    for value in (meta.get("requestedGid"), meta.get("masterGid")):
        gid = _clean_gid(value)
        if gid:
            out.add(gid)
    for item in snapshot.get("ftGids") or []:
        if not isinstance(item, dict):
            continue
        gid = _clean_gid(item.get("gid"))
        if gid:
            out.add(gid)
    return {gid for gid in out if gid}


def _read_document(path: Path) -> Dict[str, Any]:
    doc = read_json(Path(path))
    matches = doc.get("matches") if isinstance(doc.get("matches"), dict) else {}
    aliases = doc.get("aliases") if isinstance(doc.get("aliases"), dict) else {}
    return {"version": SNAPSHOT_CACHE_VERSION, "matches": matches, "aliases": aliases}


def store_snapshot(
    path: Path,
    snapshot: Dict[str, Any],
    *,
    gid: Any,
    mode: Any,
    now: Optional[float] = None,
    source_member: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    requested_gid = _clean_gid(gid)
    if not requested_gid:
        raise ValueError("missing_gid")
    if not isinstance(snapshot, dict):
        raise ValueError("snapshot_must_be_dict")

    mode_text = _clean_mode(mode)
    public_snapshot = public_snapshot_payload(
        snapshot,
        mode=mode_text,
        requested_gid=requested_gid,
    )
    fetched_at = float(time.time() if now is None else now)
    canonical_gid = _canonical_gid(public_snapshot, requested_gid)
    canonical_key = snapshot_cache_key(canonical_gid, mode_text)
    requested_key = snapshot_cache_key(requested_gid, mode_text)
    fingerprint = _market_fingerprint(public_snapshot)

    with _CACHE_LOCK:
        doc = _read_document(Path(path))
        matches = doc["matches"]
        aliases = doc["aliases"]
        previous_key = str(aliases.get(requested_key) or canonical_key)
        previous = matches.get(previous_key) if isinstance(matches.get(previous_key), dict) else {}
        previous_revision = int(previous.get("revision") or 0)
        revision = max(1, previous_revision + (0 if previous.get("fingerprint") == fingerprint else 1))

        record = copy.deepcopy(public_snapshot)
        record.update(
            {
                "gid": requested_gid,
                "master_gid": canonical_gid,
                "mode": mode_text,
                "fetched_at": fetched_at,
                "revision": revision,
                "status": "ok",
                "fingerprint": fingerprint,
            }
        )
        if source_member:
            record["source_member"] = {
                "id": str(source_member.get("id") or ""),
                "label": str(source_member.get("label") or source_member.get("username") or ""),
            }

        rows = record.get("market_rows") if isinstance(record.get("market_rows"), list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            row["revision"] = revision
            row["fetched_at"] = fetched_at

        matches[canonical_key] = record
        if previous_key != canonical_key:
            matches.pop(previous_key, None)
        for alias_gid in _snapshot_gids(public_snapshot, requested_gid, canonical_gid):
            aliases[snapshot_cache_key(alias_gid, mode_text)] = canonical_key

        if len(matches) > SNAPSHOT_CACHE_MAX_MATCHES:
            keep_keys = {
                key
                for key, _ in sorted(
                    matches.items(),
                    key=lambda item: float((item[1] or {}).get("fetched_at") or 0.0),
                    reverse=True,
                )[:SNAPSHOT_CACHE_MAX_MATCHES]
            }
            matches = {key: value for key, value in matches.items() if key in keep_keys}
            aliases = {key: value for key, value in aliases.items() if value in keep_keys}

        atomic_write_json(
            Path(path),
            {"version": SNAPSHOT_CACHE_VERSION, "matches": matches, "aliases": aliases},
        )

    result = copy.deepcopy(record)
    result.update({"cached": False, "stale": False, "age_ms": 0})
    return result


def load_snapshot(
    path: Path,
    *,
    gid: Any,
    mode: Any,
    now: Optional[float] = None,
    max_age_s: float = 2.5,
    allow_stale_s: float = 0.0,
) -> Optional[Dict[str, Any]]:
    requested_key = snapshot_cache_key(gid, mode)
    if not requested_key:
        return None
    now_value = float(time.time() if now is None else now)

    with _CACHE_LOCK:
        doc = _read_document(Path(path))
    canonical_key = str(doc["aliases"].get(requested_key) or requested_key)
    record = doc["matches"].get(canonical_key)
    if not isinstance(record, dict):
        return None
    try:
        fetched_at = float(record.get("fetched_at") or 0.0)
    except Exception:
        return None
    if fetched_at <= 0:
        return None

    age_s = max(0.0, now_value - fetched_at)
    fresh_limit = max(0.0, float(max_age_s))
    stale_limit = fresh_limit + max(0.0, float(allow_stale_s))
    if age_s > stale_limit:
        return None

    result = copy.deepcopy(record)
    result.update(
        {
            "cached": True,
            "stale": age_s > fresh_limit,
            "age_ms": int(round(age_s * 1000.0)),
        }
    )
    return result


def load_or_refresh_snapshot(
    path: Path,
    *,
    gid: Any,
    mode: Any,
    fetcher: Callable[[], Dict[str, Any]],
    now: Optional[float] = None,
    max_age_s: float = 2.5,
    allow_stale_s: float = 10.0,
    force_refresh: bool = False,
    source_member: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    now_value = float(time.time() if now is None else now)
    with _key_lock(gid, mode):
        if not force_refresh:
            fresh = load_snapshot(
                Path(path),
                gid=gid,
                mode=mode,
                now=now_value,
                max_age_s=max_age_s,
            )
            if fresh is not None:
                return fresh

        try:
            snapshot = fetcher()
            if not isinstance(snapshot, dict):
                raise RuntimeError("snapshot_fetcher_returned_non_dict")
            stored_at = float(time.time() if now is None else now_value)
            return store_snapshot(
                Path(path),
                snapshot,
                gid=gid,
                mode=mode,
                now=stored_at,
                source_member=source_member,
            )
        except Exception as exc:
            stale = load_snapshot(
                Path(path),
                gid=gid,
                mode=mode,
                now=now_value,
                max_age_s=max_age_s,
                allow_stale_s=allow_stale_s,
            )
            if stale is None:
                raise
            stale["status"] = "stale"
            stale["warning"] = f"snapshot_refresh_failed: {exc}"
            return stale
