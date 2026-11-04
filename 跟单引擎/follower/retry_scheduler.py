from __future__ import annotations

import copy

from typing import Any, Dict, List, Optional, Tuple

from .models import RetryJob


_WAIT_POLICY_BY_ERR: Dict[str, Dict[str, str]] = {
    "1X001": {
        "started_field": "market_closed_wait_started_at",
        "deadline_field": "market_closed_wait_deadline_at",
        "timeout_reason": "盘口封盘等待超时，已停止该订单跟单。",
    },
    "LINE_NOT_FOUND": {
        "started_field": "line_not_found_wait_started_at",
        "deadline_field": "line_not_found_wait_deadline_at",
        "timeout_reason": "目标盘口线等待超时，已停止该订单跟单。",
    },
}

CONTEXT_RETRYABLE = "CONTEXT_RETRYABLE"
UNKNOWN_RESULT = "UNKNOWN_RESULT"
CONTEXT_RETRY_BACKOFF_SECONDS = (0.15, 0.30, 0.50)
CONTEXT_RETRY_MAX_DELAY_SECONDS = 30.0

for _snapshot_err in ("SNAPSHOT_MISSING", "SNAPSHOT_STALE", "TARGET_LINE_MISSING", "SNAPSHOT_INVALID", "MARKET_ERROR"):
    _WAIT_POLICY_BY_ERR[_snapshot_err] = {
        "started_field": "line_not_found_wait_started_at",
        "deadline_field": "line_not_found_wait_deadline_at",
        "timeout_reason": "等待最新盘口超时，已停止该订单跟单。",
    }


def _wait_policy_for_err(err: str) -> Optional[Dict[str, str]]:
    key = str(err or "").strip().upper()
    return _WAIT_POLICY_BY_ERR.get(key)


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def retry_job_to_dict(job: RetryJob) -> Dict[str, Any]:
    return {
        "key": job.key,
        "parent_key": job.parent_key,
        "independent_split": bool(job.independent_split),
        "order_id": job.order_id,
        "target": job.target,
        "stake": job.stake,
        "order": job.order,
        "created_at": job.created_at,
        "last_attempted_at": job.last_attempted_at,
        "member_id": job.member_id,
        "attempts": job.attempts,
        "next_try_at": job.next_try_at,
        "last_message": job.last_message,
        "last_err": job.last_err,
        "market_closed_fails": job.market_closed_fails,
        "market_closed_wait_started_at": job.market_closed_wait_started_at,
        "market_closed_wait_deadline_at": job.market_closed_wait_deadline_at,
        "line_not_found_wait_started_at": job.line_not_found_wait_started_at,
        "line_not_found_wait_deadline_at": job.line_not_found_wait_deadline_at,
        "unknown_result_started_at": job.unknown_result_started_at,
        "unknown_result_deadline_at": job.unknown_result_deadline_at,
        "unknown_result_check_count": job.unknown_result_check_count,
        "unknown_result_status": job.unknown_result_status,
        "unknown_result_last_check_at": job.unknown_result_last_check_at,
        "split_count": job.split_count,
        "split_index": job.split_index,
        "split_delay_seconds": job.split_delay_seconds,
        "split_stakes": list(job.split_stakes or []),
        "split_tickets": list(job.split_tickets or []),
        "used_member_ids": list(job.used_member_ids or []),
        "split_part_results": dict(job.split_part_results or {}),
        "market_submit_reject_fingerprint": str(job.market_submit_reject_fingerprint or ""),
        "market_submit_reject_code": str(job.market_submit_reject_code or ""),
        "market_submit_reject_count": int(job.market_submit_reject_count or 0),
        "market_wait_count": int(job.market_wait_count or 0),
        "member_wait_count": int(job.member_wait_count or 0),
        "first_candidate_snapshot_at": float(job.first_candidate_snapshot_at or 0.0),
        "context_retry_count": int(job.context_retry_count or 0),
        "last_seen_event_seq": int(job.last_seen_event_seq or 0),
        "snapshot_wait_observed_revision": int(job.snapshot_wait_observed_revision or 0),
        "snapshot_wait_observed_event_seq": int(job.snapshot_wait_observed_event_seq or 0),
        "trigger_reason": str(job.trigger_reason or ""),
        "trigger_event_seq": int(job.trigger_event_seq or 0),
        "trigger_publish_at": float(job.trigger_publish_at or 0.0),
        "trigger_wakeup_at": float(job.trigger_wakeup_at or 0.0),
        "candidate_detected_at": float(job.candidate_detected_at or 0.0),
        "job_marked_runnable_at": float(job.job_marked_runnable_at or 0.0),
        "runnable_queue_entered_at": float(job.runnable_queue_entered_at or 0.0),
        "dispatcher_selected_at": float(job.dispatcher_selected_at or 0.0),
        "attempt_started_at": float(job.attempt_started_at or 0.0),
        "member_acquired_at": float(job.member_acquired_at or 0.0),
    }


def retry_job_from_dict(d: Dict[str, Any], *, now_value: float = 0.0) -> Optional[RetryJob]:
    if not isinstance(d, dict):
        return None
    key = str(d.get("key") or "").strip()
    order_id = str(d.get("order_id") or d.get("orderId") or "").strip()
    target = str(d.get("target") or "").strip()
    if not (key and order_id and target):
        return None
    order = d.get("order")
    if not isinstance(order, dict):
        order = {}
    try:
        raw_split_parts = d.get("split_part_results") if isinstance(d.get("split_part_results"), dict) else {}
        split_parts_norm: Dict[int, Dict[str, Any]] = {}
        try:
            for k0, v0 in raw_split_parts.items():
                try:
                    ik = int(k0)
                except Exception:
                    continue
                if isinstance(v0, dict):
                    split_parts_norm[ik] = v0
        except Exception:
            split_parts_norm = {}
        job = RetryJob(
            key=key,
            parent_key=str(d.get("parent_key") or "").strip(),
            independent_split=bool(d.get("independent_split") or False),
            order_id=order_id,
            target=target,
            stake=_safe_float(d.get("stake"), 0.0),
            order=order,
            created_at=_safe_float(d.get("created_at"), float(now_value or 0.0)),
            last_attempted_at=_safe_float(d.get("last_attempted_at"), 0.0),
            member_id=str(d.get("member_id") or "").strip(),
            attempts=_safe_int(d.get("attempts"), 0),
            next_try_at=_safe_float(d.get("next_try_at"), 0.0),
            last_message=str(d.get("last_message") or ""),
            last_err=str(d.get("last_err") or ""),
            market_closed_fails=_safe_int(d.get("market_closed_fails"), 0),
            market_closed_wait_started_at=_safe_float(d.get("market_closed_wait_started_at"), 0.0),
            market_closed_wait_deadline_at=_safe_float(d.get("market_closed_wait_deadline_at"), 0.0),
            line_not_found_wait_started_at=_safe_float(d.get("line_not_found_wait_started_at"), 0.0),
            line_not_found_wait_deadline_at=_safe_float(d.get("line_not_found_wait_deadline_at"), 0.0),
            unknown_result_started_at=_safe_float(d.get("unknown_result_started_at"), 0.0),
            unknown_result_deadline_at=_safe_float(d.get("unknown_result_deadline_at"), 0.0),
            unknown_result_check_count=_safe_int(d.get("unknown_result_check_count"), 0),
            unknown_result_status=str(d.get("unknown_result_status") or ""),
            unknown_result_last_check_at=_safe_float(d.get("unknown_result_last_check_at"), 0.0),
            split_count=_safe_int(d.get("split_count"), 1),
            split_index=_safe_int(d.get("split_index"), 0),
            split_delay_seconds=_safe_float(d.get("split_delay_seconds"), 0.0),
            split_stakes=[_safe_float(x, 0.0) for x in (d.get("split_stakes") or []) if x is not None],
            split_tickets=[str(x) for x in (d.get("split_tickets") or []) if str(x)],
            used_member_ids=[str(x) for x in (d.get("used_member_ids") or []) if str(x)],
            split_part_results=split_parts_norm,
            market_submit_reject_fingerprint=str(d.get("market_submit_reject_fingerprint") or ""),
            market_submit_reject_code=str(d.get("market_submit_reject_code") or ""),
            market_submit_reject_count=_safe_int(d.get("market_submit_reject_count"), 0),
            market_wait_count=_safe_int(d.get("market_wait_count"), 0),
            member_wait_count=_safe_int(d.get("member_wait_count"), 0),
            first_candidate_snapshot_at=_safe_float(d.get("first_candidate_snapshot_at"), 0.0),
            context_retry_count=_safe_int(d.get("context_retry_count"), 0),
            last_seen_event_seq=_safe_int(d.get("last_seen_event_seq"), 0),
            snapshot_wait_observed_revision=_safe_int(d.get("snapshot_wait_observed_revision"), 0),
            snapshot_wait_observed_event_seq=_safe_int(d.get("snapshot_wait_observed_event_seq"), 0),
            trigger_reason=str(d.get("trigger_reason") or ""),
            trigger_event_seq=_safe_int(d.get("trigger_event_seq"), 0),
            trigger_publish_at=_safe_float(d.get("trigger_publish_at"), 0.0),
            trigger_wakeup_at=_safe_float(d.get("trigger_wakeup_at"), 0.0),
            candidate_detected_at=_safe_float(d.get("candidate_detected_at"), 0.0),
            job_marked_runnable_at=_safe_float(d.get("job_marked_runnable_at"), 0.0),
            runnable_queue_entered_at=_safe_float(d.get("runnable_queue_entered_at"), 0.0),
            dispatcher_selected_at=_safe_float(d.get("dispatcher_selected_at"), 0.0),
            attempt_started_at=_safe_float(d.get("attempt_started_at"), 0.0),
            member_acquired_at=_safe_float(d.get("member_acquired_at"), 0.0),
        )
        if float(getattr(job, "next_try_at", 0.0) or 0.0) <= 0.0:
            base = float(getattr(job, "created_at", 0.0) or float(now_value or 0.0))
            job.next_try_at = base
        return job
    except Exception:
        return None


def split_task_key(parent_key: str, split_index: int, split_count: int) -> str:
    parent = str(parent_key or "").strip()
    if int(split_count or 1) <= 1:
        return parent
    return f"{parent}#part:{int(split_index)}"


def build_independent_split_jobs(
    *,
    parent_key: str,
    order_id: str,
    target: str,
    total_stake: float,
    order: Dict[str, Any],
    parts: List[float],
    created_at: float,
    split_delay_seconds: float = 0.0,
) -> List[RetryJob]:
    normalized_parts = [float(part) for part in parts if float(part) > 0.0]
    split_count = len(normalized_parts)
    if split_count <= 0:
        return []
    independent = split_count > 1
    jobs: List[RetryJob] = []
    for index in range(split_count):
        jobs.append(
            RetryJob(
                key=split_task_key(parent_key, index, split_count),
                parent_key=str(parent_key or "") if independent else "",
                independent_split=independent,
                order_id=str(order_id or ""),
                target=str(target or ""),
                stake=float(total_stake),
                # Each concurrently running split mutates private scheduler
                # metadata on its order (UNKNOWN_RESULT, Snapshot waits, and
                # context retries). Sharing the same dict lets one split
                # clear another split's recovery state.
                order=copy.deepcopy(order),
                created_at=float(created_at),
                next_try_at=float(created_at),
                split_count=split_count,
                split_index=index,
                split_delay_seconds=float(split_delay_seconds or 0.0),
                split_stakes=list(normalized_parts),
            )
        )
    return jobs


def aggregate_independent_split_done(
    *,
    parent_key: str,
    split_count: int,
    done: Dict[str, Dict[str, Any]],
    active_keys: set[str],
    created_at: float,
    now: float,
) -> Optional[Dict[str, Any]]:
    count = max(1, int(split_count or 1))
    part_keys = [split_task_key(parent_key, index, count) for index in range(count)]
    if any(key in active_keys for key in part_keys):
        return None
    records = [done.get(key) for key in part_keys]
    if any(not isinstance(record, dict) for record in records):
        return None

    typed_records = [record for record in records if isinstance(record, dict)]
    statuses = [str(record.get("status") or "").strip().lower() for record in typed_records]
    all_ok = all(status in {"ok", "success", "succeeded", "done"} for status in statuses)
    all_skipped = all(status in {"skipped", "skip"} for status in statuses)
    status = "ok" if all_ok else ("skipped" if all_skipped else "failed")
    tickets = [str(record.get("ticket_id") or "").strip() for record in typed_records]
    tickets = [ticket for ticket in tickets if ticket]
    reasons = [str(record.get("reason") or record.get("message") or "").strip() for record in typed_records]
    reasons = [reason for reason in reasons if reason]
    aggregate: Dict[str, Any] = {
        "status": status,
        "ticket_id": tickets[-1] if tickets else "",
        "tickets": tickets,
        "reason": "；".join(dict.fromkeys(reasons)),
        "ts": int(now),
        "elapsed_s": round(max(0.0, float(now) - float(created_at or now)), 3),
        "split_done": sum(1 for value in statuses if value in {"ok", "success", "succeeded", "done"}),
        "split_total": count,
        "split_parts": typed_records,
    }
    return aggregate


class RetryScheduler:
    def __init__(self, retry_jobs: Optional[Dict[str, RetryJob]] = None):
        self.retry_jobs: Dict[str, RetryJob] = retry_jobs if isinstance(retry_jobs, dict) else {}

    def add_job(self, job: RetryJob) -> None:
        self.retry_jobs[str(job.key)] = job

    def get_job(self, job_key: str) -> Optional[RetryJob]:
        return self.retry_jobs.get(str(job_key or ""))

    def get_jobs_for_parent(self, parent_key: str) -> List[RetryJob]:
        parent = str(parent_key or "").strip()
        return [
            job
            for job in self.retry_jobs.values()
            if str(job.parent_key or job.key).strip() == parent
        ]

    def remove_job(self, job_key: str) -> None:
        self.retry_jobs.pop(str(job_key or ""), None)

    def size(self) -> int:
        return len(self.retry_jobs)

    def to_state_dict(self) -> Dict[str, Dict[str, Any]]:
        return {k: retry_job_to_dict(v) for k, v in self.retry_jobs.items()}

    def get_ready_jobs(self, now: float, *, exclude_keys: Optional[set[str]] = None) -> List[RetryJob]:
        excluded = {str(key) for key in (exclude_keys or set())}
        ready = [
            job
            for job in self.retry_jobs.values()
            if str(job.key) not in excluded and float(job.next_try_at or 0.0) <= float(now)
        ]
        ready.sort(
            key=lambda j: (
                float(j.next_try_at or 0.0),
                float(getattr(j, "last_attempted_at", 0.0) or 0.0),
                float(j.created_at or 0.0),
            )
        )
        return ready

    def get_line_not_found_jobs(self) -> List[RetryJob]:
        return [j for j in self.retry_jobs.values() if str(j.last_err or "") == "LINE_NOT_FOUND"]

    def get_effective_limits(
        self,
        job: RetryJob,
        *,
        retry_max_attempts: int,
        retry_max_seconds: float,
        retry_max_attempts_on_1x001: int = 0,
        retry_max_seconds_on_1x001: float = 0.0,
        wait_1x001_max: float = 0.0,
        wait_line_not_found_max: float,
    ) -> Tuple[int, float]:
        eff_max_attempts = int(retry_max_attempts or 0)
        eff_max_seconds = float(retry_max_seconds or 0.0)
        err = str(job.last_err or "").strip().upper()
        # Once FT_bet has been sent and the outcome is ambiguous, only the
        # dedicated reconciliation window may terminate the job.  Generic
        # submit-attempt/time limits must never turn two not-found checks into
        # a premature ordinary failure.
        if err == UNKNOWN_RESULT:
            return 0, 0.0
        if err == "1X001":
            if float(wait_1x001_max or 0.0) > 0.0:
                eff_max_attempts = 0
                eff_max_seconds = 0.0
            else:
                if int(retry_max_attempts_on_1x001 or 0) > 0:
                    eff_max_attempts = int(retry_max_attempts_on_1x001)
                if float(retry_max_seconds_on_1x001 or 0.0) > 0.0:
                    eff_max_seconds = float(retry_max_seconds_on_1x001)
        if err in {"LINE_NOT_FOUND", "SNAPSHOT_MISSING", "SNAPSHOT_STALE", "TARGET_LINE_MISSING", "SNAPSHOT_INVALID", "MARKET_ERROR"} and float(wait_line_not_found_max or 0.0) > 0.0:
            eff_max_attempts = 0
            eff_max_seconds = 0.0
        # The first three context-only transport failures are bounded, fast
        # retries. After that, preserve the caller's normal attempt/time cap;
        # the dedicated exponential backoff below controls their request rate.
        if err == CONTEXT_RETRYABLE and int(getattr(job, "context_retry_count", 0) or 0) < len(CONTEXT_RETRY_BACKOFF_SECONDS):
            eff_max_attempts = 0
            eff_max_seconds = 0.0
        return eff_max_attempts, eff_max_seconds

    def check_wait_timeout(self, job: RetryJob, *, now: float) -> str:
        policy = _wait_policy_for_err(str(job.last_err or ""))
        if not policy:
            return ""
        deadline_field = str(policy.get("deadline_field") or "").strip()
        if not deadline_field:
            return ""
        deadline = float(getattr(job, deadline_field, 0.0) or 0.0)
        if deadline > 0.0 and float(now) >= deadline:
            return str(policy.get("timeout_reason") or "")
        return ""

    def _schedule_wait_window(
        self,
        job: RetryJob,
        *,
        now: float,
        wait_max: float,
        probe_seconds: float,
        started_field: str,
        deadline_field: str,
    ) -> Tuple[str, float, float]:
        if float(wait_max or 0.0) <= 0.0:
            job.next_try_at = float(now)
            return "immediate", 0.0, 0.0
        if float(getattr(job, started_field, 0.0) or 0.0) <= 0.0 or float(getattr(job, deadline_field, 0.0) or 0.0) <= 0.0:
            setattr(job, started_field, float(now))
            setattr(job, deadline_field, float(now) + float(wait_max))
        deadline = float(getattr(job, deadline_field, 0.0) or 0.0)
        remain = max(0.0, deadline - float(now))
        if remain <= 0.0:
            return "expired", 0.0, 0.0
        next_delay = min(float(probe_seconds or 0.0), float(remain))
        job.next_try_at = float(now) + float(next_delay)
        return "scheduled", remain, next_delay

    def schedule_market_closed(
        self,
        job: RetryJob,
        *,
        now: float,
        wait_max: float,
        probe_seconds: float,
    ) -> Tuple[str, float, float]:
        return self._schedule_wait_window(
            job,
            now=now,
            wait_max=wait_max,
            probe_seconds=probe_seconds,
            started_field="market_closed_wait_started_at",
            deadline_field="market_closed_wait_deadline_at",
        )

    def schedule_line_not_found(
        self,
        job: RetryJob,
        *,
        now: float,
        wait_max: float,
        probe_seconds: float,
    ) -> Tuple[str, float, float]:
        return self._schedule_wait_window(
            job,
            now=now,
            wait_max=wait_max,
            probe_seconds=probe_seconds,
            started_field="line_not_found_wait_started_at",
            deadline_field="line_not_found_wait_deadline_at",
        )

    def schedule_wait_by_err(
        self,
        job: RetryJob,
        *,
        err: str,
        now: float,
        wait_max: float,
        probe_seconds: float,
    ) -> Tuple[str, float, float]:
        policy = _wait_policy_for_err(err)
        if not policy:
            job.next_try_at = float(now)
            return "unsupported", 0.0, 0.0
        return self._schedule_wait_window(
            job,
            now=now,
            wait_max=wait_max,
            probe_seconds=probe_seconds,
            started_field=str(policy.get("started_field") or ""),
            deadline_field=str(policy.get("deadline_field") or ""),
        )

    def schedule_context_retry(
        self,
        job: RetryJob,
        *,
        now: float,
        max_fast_retries: int = len(CONTEXT_RETRY_BACKOFF_SECONDS),
        backoffs: Tuple[float, ...] = CONTEXT_RETRY_BACKOFF_SECONDS,
        slow_delay: float = 1.0,
        max_delay: float = CONTEXT_RETRY_MAX_DELAY_SECONDS,
    ) -> Tuple[int, float, bool]:
        """Schedule a pre-FT_bet context retry without Snapshot waiting.

        Returns ``(retry_count, delay, fast)``.  The count is persisted on
        ``RetryJob`` so a restart cannot turn a bounded retry window into an
        unbounded request burst.
        """
        count = _safe_int(getattr(job, "context_retry_count", 0), 0) + 1
        job.context_retry_count = count
        if count <= max(0, int(max_fast_retries or 0)) and backoffs:
            index = min(count - 1, len(backoffs) - 1)
            delay = max(0.0, float(backoffs[index] or 0.0))
            job.next_try_at = float(now) + delay
            return count, delay, True
        # Once the fast probe window is exhausted, exponentially back off the
        # same context request. This preserves recovery for a transient 502
        # without continuing the old one-second request loop indefinitely.
        base_delay = max(1.0, float(slow_delay or 0.0))
        slow_index = max(0, count - max(1, int(max_fast_retries or 0)) - 1)
        delay = min(max(0.0, base_delay * (2 ** slow_index)), max(0.0, float(max_delay or 0.0)))
        job.next_try_at = float(now) + delay
        return count, delay, False
