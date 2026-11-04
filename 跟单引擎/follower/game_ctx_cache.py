from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple


def _normalize_showtype_hint(st: Any) -> str:
    s = str(st or "").strip().lower()
    if s in {"live", "today", "hot", "early"}:
        return s
    return ""


def _looks_like_xml_serverresponse(text: str) -> bool:
    t = str(text or "").lstrip()
    if not t:
        return False
    head = t[:160].lower()
    if head.startswith("<?xml"):
        return True
    if head.startswith("<serverresponse"):
        return True
    if "<serverresponse" in head[:80]:
        return True
    return False


class GameContextCache:
    """
    Cache lid/ecid context for game_more requests.

    Notes:
    - key keeps base_url + gid to support multi-site runs
    - uid is accepted for backward-compatible legacy-key migration
    """

    def __init__(self, ttl_seconds: float = 300.0):
        self.ttl_seconds = float(ttl_seconds)
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def build_key(*, base_url: str, uid: str, gid: str) -> str:
        _ = str(uid or "")
        return f"{str(base_url or '').strip().rstrip('/')}|{str(gid or '').strip()}"

    @staticmethod
    def _legacy_key(*, base_url: str, uid: str, gid: str) -> str:
        return (
            f"{str(base_url or '').strip().rstrip('/')}|"
            f"{str(uid or '').strip()}|{str(gid or '').strip()}"
        )

    def get(
        self,
        *,
        base_url: str,
        uid: str,
        gid: str,
        max_age_seconds: Optional[float] = None,
    ) -> Tuple[str, str, str]:
        key = self.build_key(base_url=base_url, uid=uid, gid=gid)
        now = float(time.time())
        with self._lock:
            rec = self._cache.get(key) or {}
            if not rec:
                legacy = self._cache.get(
                    self._legacy_key(base_url=base_url, uid=uid, gid=gid)
                ) or {}
                if legacy:
                    try:
                        self._cache[key] = dict(legacy)
                    except Exception:
                        pass
                    rec = legacy
        lid = str(rec.get("lid") or "").strip()
        ecid = str(rec.get("ecid") or "").strip()
        st_hint = _normalize_showtype_hint(rec.get("showtype"))
        ts = float(rec.get("ts") or 0.0)
        if not (lid and ecid and ts > 0):
            return "", "", ""
        ttl = float(max_age_seconds if max_age_seconds is not None else self.ttl_seconds)
        if ttl <= 0:
            return "", "", ""
        if (now - ts) > ttl:
            return "", "", ""
        return lid, ecid, st_hint

    def set(
        self,
        *,
        base_url: str,
        uid: str,
        gid: str,
        lid: str,
        ecid: str,
        showtype: str = "",
    ) -> None:
        lid2 = str(lid or "").strip()
        ecid2 = str(ecid or "").strip()
        if not (lid2 and ecid2):
            return
        rec: Dict[str, Any] = {"lid": lid2, "ecid": ecid2, "ts": float(time.time())}
        st2 = _normalize_showtype_hint(showtype)
        if st2:
            rec["showtype"] = st2
        with self._lock:
            self._cache[self.build_key(base_url=base_url, uid=uid, gid=gid)] = rec
            self._cache[self._legacy_key(base_url=base_url, uid=uid, gid=gid)] = rec

    def clear_expired(self) -> None:
        ttl = float(self.ttl_seconds)
        if ttl <= 0:
            return
        now = float(time.time())
        with self._lock:
            keys = list(self._cache.keys())
            for key in keys:
                rec = self._cache.get(key) or {}
                ts = float(rec.get("ts") or 0.0) if isinstance(rec, dict) else 0.0
                if ts <= 0 or (now - ts) > ttl:
                    self._cache.pop(key, None)


class XmlResponseCache:
    """Very short TTL XML cache for get_game_list/get_game_more responses."""

    def __init__(self, ttl_seconds: float = 0.8, max_items: int = 256):
        self.ttl_seconds = float(ttl_seconds)
        self.max_items = int(max(1, int(max_items)))
        self._cache: Dict[str, Tuple[float, str]] = {}
        self._lock = threading.Lock()
        self._flights: Dict[str, threading.Event] = {}

    def get(self, key: str, ttl_s: Optional[float] = None) -> str:
        k = str(key or "").strip()
        if not k:
            return ""
        ttl = float(self.ttl_seconds if ttl_s is None else ttl_s)
        if ttl <= 0:
            return ""
        now = float(time.time())
        with self._lock:
            rec = self._cache.get(k)
        if not rec:
            return ""
        ts, text = rec
        try:
            if (now - float(ts)) > ttl:
                return ""
        except Exception:
            return ""
        return str(text or "")

    def set(self, key: str, xml_text: str) -> None:
        k = str(key or "").strip()
        if not k:
            return
        v = str(xml_text or "")
        if not v:
            return
        if not _looks_like_xml_serverresponse(v):
            return
        now = float(time.time())
        with self._lock:
            self._cache[k] = (now, v)
            if len(self._cache) > self.max_items:
                self._cache.clear()
                self._cache[k] = (now, v)

    def get_or_fetch(self, key: str, ttl_s: float, fetcher: Callable[[], str]) -> str:
        cached = self.get(key, ttl_s)
        if cached:
            return cached

        k = str(key or "").strip()
        if not k:
            return str(fetcher() or "")
        leader = False
        with self._lock:
            event = self._flights.get(k)
            if event is None:
                event = threading.Event()
                self._flights[k] = event
                leader = True

        if not leader:
            event.wait(timeout=max(1.0, float(ttl_s or 0.0) + 30.0))
            cached = self.get(k, ttl_s)
            if cached:
                return cached
            return str(fetcher() or "")

        try:
            value = str(fetcher() or "")
            self.set(k, value)
            return value
        finally:
            with self._lock:
                current = self._flights.pop(k, None)
                if current:
                    current.set()

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
