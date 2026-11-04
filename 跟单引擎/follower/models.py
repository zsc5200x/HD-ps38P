from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class BetParams:
    gid: str
    gtype: str
    wtype: str
    rtype: str
    chose_team: str
    ioratio: str
    con: str
    ratio: str
    f: str


@dataclass
class RetryJob:
    key: str
    order_id: str
    target: str
    stake: float
    order: Dict[str, Any]
    created_at: float
    parent_key: str = ""
    independent_split: bool = False
    # 调度公平性：最近一次尝试时间（用于“失败后让出队列，优先处理下一单”）
    last_attempted_at: float = 0.0
    # 最近一次尝试使用的会员（随机选择时用于尽量避免连续命中同一个）
    member_id: str = ""
    attempts: int = 0
    next_try_at: float = 0.0
    last_message: str = ""
    last_err: str = ""
    market_closed_fails: int = 0
    # 1X001（封盘/暂停下注）封盘等待：开始时间与截止时间（用于“固定频率探测 + 超时终止”）
    market_closed_wait_started_at: float = 0.0
    market_closed_wait_deadline_at: float = 0.0
    # LINE_NOT_FOUND（目标盘口线暂时找不到）等待：开始时间与截止时间（用于“固定频率探测 + 超时终止”）
    line_not_found_wait_started_at: float = 0.0
    line_not_found_wait_deadline_at: float = 0.0
    # UNKNOWN_RESULT 核验状态：核验期间固定原会员，禁止重新调用 FT_bet。
    unknown_result_started_at: float = 0.0
    unknown_result_deadline_at: float = 0.0
    unknown_result_check_count: int = 0
    unknown_result_status: str = ""
    unknown_result_last_check_at: float = 0.0
    # 分单：新任务使用 independent_split 独立并发；旧状态仍兼容顺序执行。
    split_count: int = 1
    split_index: int = 0  # 0-based
    split_delay_seconds: float = 0.0
    split_stakes: List[float] = field(default_factory=list)
    split_tickets: List[str] = field(default_factory=list)
    used_member_ids: List[str] = field(default_factory=list)
    # Telegram 展示用：记录每一笔分单（按 split_index）最近一次结果，避免编辑消息时丢失历史
    split_part_results: Dict[int, Dict[str, Any]] = field(default_factory=dict)
    # 同一精确盘口连续收到明确 betError* 时的有限重试计数。
    market_submit_reject_fingerprint: str = ""
    market_submit_reject_code: str = ""
    market_submit_reject_count: int = 0
    # 等待统计不计入真实下注 attempts。
    market_wait_count: int = 0
    member_wait_count: int = 0
    first_candidate_snapshot_at: float = 0.0
    # FT_order_view 在 FT_bet 之前失败时的快速重试窗口；与
    # UNKNOWN_RESULT 完全分离，进程重启后仍可恢复退避次数。
    context_retry_count: int = 0
    # Snapshot publish 事件消费游标及当前 attempt 的不可变触发元数据。
    last_seen_event_seq: int = 0
    snapshot_wait_observed_revision: int = 0
    snapshot_wait_observed_event_seq: int = 0
    trigger_reason: str = ""
    trigger_event_seq: int = 0
    trigger_publish_at: float = 0.0
    trigger_wakeup_at: float = 0.0
    candidate_detected_at: float = 0.0
    job_marked_runnable_at: float = 0.0
    runnable_queue_entered_at: float = 0.0
    dispatcher_selected_at: float = 0.0
    attempt_started_at: float = 0.0
    member_acquired_at: float = 0.0
