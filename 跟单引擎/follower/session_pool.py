from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

import requests

from .executor import close_member_session, make_member_session


def build_member_session_fingerprint(member: Dict[str, Any]) -> str:
    payload = {
        "member_id": str((member or {}).get("id") or "").strip(),
        "base_url": str((member or {}).get("base_url") or "").strip(),
        "proxy": str((member or {}).get("proxy") or "").strip(),
        "cookie": str((member or {}).get("cookie") or "").strip(),
        "uid": str((member or {}).get("uid") or "").strip(),
        "ver": str((member or {}).get("ver") or "").strip(),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class SessionEntry:
    session: requests.Session
    member_id: str
    fingerprint: str
    created_at: float
    last_used_at: float
    invalid_reason: str = ""


class MemberSessionPool:
    def __init__(
        self,
        *,
        session_factory: Optional[Callable[[Dict[str, Any]], requests.Session]] = None,
        now_func: Optional[Callable[[], float]] = None,
        invalidate_callback: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self._session_factory = session_factory or self._default_session_factory
        self._now = now_func or time.time
        self._invalidate_callback = invalidate_callback
        self._entries: Dict[str, SessionEntry] = {}
        self._lock = threading.RLock()

    def _default_session_factory(self, member: Dict[str, Any]) -> requests.Session:
        return make_member_session(
            base_url=str((member or {}).get("base_url") or "").strip(),
            proxy=str((member or {}).get("proxy") or "").strip(),
            cookie=str((member or {}).get("cookie") or "").strip(),
        )

    def size(self) -> int:
        with self._lock:
            return len(self._entries)

    def get_or_create(self, member: Dict[str, Any]) -> requests.Session:
        member_id = str((member or {}).get("id") or "").strip()
        if not member_id:
            raise ValueError("member.id is required for session pool")
        fingerprint = build_member_session_fingerprint(member)
        now_s = float(self._now())

        with self._lock:
            entry = self._entries.get(member_id)
            if entry and entry.fingerprint == fingerprint:
                entry.last_used_at = now_s
                return entry.session
            if entry:
                self._close_entry(entry, reason="fingerprint_changed")
            session = self._session_factory(member)
            self._entries[member_id] = SessionEntry(
                session=session,
                member_id=member_id,
                fingerprint=fingerprint,
                created_at=now_s,
                last_used_at=now_s,
            )
            return session

    def invalidate(self, member_id: str, reason: str) -> bool:
        mid = str(member_id or "").strip()
        if not mid:
            return False
        with self._lock:
            entry = self._entries.pop(mid, None)
            if not entry:
                return False
            self._close_entry(entry, reason=reason)
            return True

    def prune_idle(self, max_idle_s: float) -> int:
        max_idle = float(max_idle_s or 0.0)
        if max_idle <= 0.0:
            return 0
        now_s = float(self._now())
        removed = 0
        with self._lock:
            for member_id, entry in list(self._entries.items()):
                idle_s = now_s - float(entry.last_used_at or 0.0)
                if idle_s < max_idle:
                    continue
                self._entries.pop(member_id, None)
                self._close_entry(entry, reason="idle_timeout")
                removed += 1
        return removed

    def close_all(self) -> None:
        with self._lock:
            for member_id, entry in list(self._entries.items()):
                self._entries.pop(member_id, None)
                self._close_entry(entry, reason="close_all")

    def _close_entry(self, entry: SessionEntry, *, reason: str) -> None:
        entry.invalid_reason = str(reason or "").strip()
        close_member_session(entry.session)
        if self._invalidate_callback:
            try:
                self._invalidate_callback(entry.member_id, entry.invalid_reason)
            except Exception:
                pass
