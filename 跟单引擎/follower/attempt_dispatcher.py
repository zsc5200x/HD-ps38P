from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple


CompletedAttempt = Tuple[str, str, Future]


class AttemptDispatcher:
    """Bounded executor with a configurable per-member concurrency limit."""

    def __init__(self, max_workers: int = 3, per_member_limit: int = 2) -> None:
        workers = max(1, int(max_workers or 1))
        self.max_workers = workers
        self.per_member_limit = max(1, int(per_member_limit or 1))
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="follow-attempt")
        self._lock = threading.RLock()
        self._busy_members: Dict[str, int] = {}
        self._member_active_counts = self._busy_members
        self._active: Dict[Future, Tuple[str, str]] = {}
        self._keys: set = set()

    def set_per_member_limit(self, limit: int) -> None:
        with self._lock:
            self.per_member_limit = max(1, int(limit or 1))

    def submit(
        self,
        key: str,
        member_id: str,
        fn: Callable[..., Any],
        *args: Any,
        member_parallel_limit: Optional[int] = None,
        **kwargs: Any,
    ) -> Optional[Future]:
        job_key = str(key or "").strip()
        mid = str(member_id or "").strip()
        if not (job_key and mid):
            return None

        with self._lock:
            per_member_limit = self.per_member_limit if member_parallel_limit is None else max(1, int(member_parallel_limit or 1))
            if job_key in self._keys:
                return None
            if len(self._active) >= self.max_workers:
                return None
            if int(self._member_active_counts.get(mid, 0)) >= per_member_limit:
                return None
            self._keys.add(job_key)
            self._busy_members[mid] = self._busy_members.get(mid, 0) + 1

            def run() -> Any:
                return fn(*args, **kwargs)

            future = self._executor.submit(run)
            self._active[future] = (job_key, mid)
            return future

    def poll_completed(self) -> List[CompletedAttempt]:
        completed: List[CompletedAttempt] = []
        with self._lock:
            for future, (job_key, mid) in list(self._active.items()):
                if not future.done():
                    continue
                self._active.pop(future, None)
                self._keys.discard(job_key)
                self._release_member(mid)
                completed.append((job_key, mid, future))
        return completed

    def wait(self, future: Future, timeout: Optional[float] = None) -> Any:
        try:
            return future.result(timeout=timeout)
        finally:
            with self._lock:
                rec = self._active.pop(future, None)
                if rec:
                    job_key, mid = rec
                    self._keys.discard(job_key)
                    self._release_member(mid)

    def _release_member(self, member_id: str) -> None:
        count = self._busy_members.get(member_id, 0) - 1
        if count > 0:
            self._busy_members[member_id] = count
        else:
            self._busy_members.pop(member_id, None)

    def is_member_busy(self, member_id: str) -> bool:
        with self._lock:
            return self._busy_members.get(str(member_id or "").strip(), 0) > 0

    def busy_member_ids(self, member_parallel_limit: Optional[int] = None) -> set:
        """Return members with at least one active network attempt.

        Members below the limit remain eligible, which lets separate fresh HTTP
        sessions use the same logged-in account concurrently.
        """
        limit = 1 if member_parallel_limit is None else max(1, int(member_parallel_limit or 1))
        with self._lock:
            return {
                member_id
                for member_id, count in self._member_active_counts.items()
                if int(count) >= limit
            }

    def saturated_member_ids(self) -> set:
        """Return members that cannot accept another attempt."""
        with self._lock:
            return {mid for mid, count in self._busy_members.items() if count >= self.per_member_limit}

    def is_key_active(self, key: str) -> bool:
        with self._lock:
            return str(key or "").strip() in self._keys

    def available_slots(self) -> int:
        with self._lock:
            return max(0, self.max_workers - len(self._active))

    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    def shutdown(self, wait: bool = True) -> None:
        self._executor.shutdown(wait=bool(wait))
