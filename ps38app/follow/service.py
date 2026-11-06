"""独立消费已提交新单，持久化合组、有界执行及结果核对。"""

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
import weakref
from contextlib import closing
from decimal import Decimal
from pathlib import Path

from ..copy_orders import assign_numbers as assign_copy_numbers
from ..monitor import endpoints_by_latency
from ..store import COPY_MERGE_WINDOW_MS, query_copy_groups
from .crown_markets import CROWN_ERRORS, RETRYABLE_ACTIONS
from .limits import apply_limits, load_usage
from .rules import group_stake, split_stakes, target_intent, validate_rule
from .source import decimal_value, number_text, source_currency, source_intent
from .mapping import resolve_fixture
from .store import ACTIVE, FollowStore, dump, iso_time

RETRYABLE_CROWN_CODES = frozenset(
    code for code, (_label, action) in CROWN_ERRORS.items() if action in RETRYABLE_ACTIONS
)

MIN_STAKE = Decimal("100")

# 主页 20 秒展示批次进入跟单前，等待同批次后续源单的真实秒数。
PENDING_SECONDS = 10.0


_SERVICES = weakref.WeakValueDictionary()
_SERVICES_LOCK = threading.RLock()


def _windows_process_alive(pid):
    # Windows 的 os.kill(pid, 0) 会终止进程，必须使用只读进程查询。
    import ctypes
    from ctypes import wintypes

    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            # 无效 PID 可以回收；访问被拒或其他错误不能证明进程已退出。
            return ctypes.get_last_error() not in {87, 1168}
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel.CloseHandle(handle)
    except (AttributeError, OSError):
        return True


def _owner_alive(owner_id, pid):
    if pid == os.getpid():
        with _SERVICES_LOCK:
            service = _SERVICES.get(owner_id)
        return bool(
            service
            and (
                service._run_lock.locked()
                or (service._dispatcher and service._dispatcher.active_count())
                or (service._thread and service._thread.is_alive())
            )
        )
    if pid <= 0:
        return False
    if os.name == "nt":
        return _windows_process_alive(pid)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        # 无法证明持有进程已退出时，不释放可能仍在途的写请求。
        return True


class FollowService:
    def __init__(self, config, executor=None, clock=None):
        self.config = config
        self.clock = clock or time.time
        self.store = FollowStore(config["follow_db_path"])
        self.executor = executor
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.RLock()
        self._run_lock = threading.Lock()
        self._thread = None
        self._dispatcher = None
        self._owner_id = uuid.uuid4().hex
        with _SERVICES_LOCK:
            _SERVICES[self._owner_id] = self
        self.store.recover_inflight(self.clock(), _owner_alive)
        self._migrate_fixed_account_rule()
        self._normalize_pending_currencies()
        self._retire_legacy_pending_groups()

    def _allow_prematch(self):
        return bool(self.config.get("follow_allow_prematch", True))

    def _normalize_pending_currencies(self):
        # 仅整理尚未提交的旧组；成功、未知及已跳过记录绝不重新进入执行队列。
        with self.store.connect(write=True) as connection:
            for group in self.store.active_groups(connection):
                if group["currency"] not in {"THB", "RMB"}:
                    continue
                original_currency = group["currency"]
                group["currency"] = "CNY"
                group["intent"].setdefault("reported_currency", original_currency)
                group["intent"]["currency"] = "CNY"
                group["rule"]["currency"] = "CNY"
                group["base_key"] = self._base_key(group["intent"], group["rule"])
                self.store.save_group(connection, group)
                for member in self.store.get_members(connection, group["id"]):
                    member.setdefault("reported_currency", member["currency"])
                    member["currency"] = source_currency(member["currency"])
                    connection.execute(
                        "UPDATE follow_members SET payload=? WHERE source=? AND member_identity=? AND order_id=?",
                        (
                            dump(member),
                            member["source"],
                            member["member_identity"],
                            member["order_id"],
                        ),
                    )

    def _retire_legacy_pending_groups(self):
        # Old groups have no homepage batch number; never submit them under the new contract.
        pending = {
            "GROUPING",
            "WAIT_MAPPING",
            "READY",
            "WAIT_ACCOUNT",
            "WAIT_MARKET",
            "PREPARED",
            "RETRY_WAIT",
        }
        with self.store.connect(write=True) as connection:
            for group in self.store.active_groups(connection):
                if group.get("source_batch_no") or group["status"] not in pending:
                    continue
                if connection.execute(
                    "SELECT 1 FROM follow_claims WHERE group_id=?", (group["id"],)
                ).fetchone():
                    continue
                group.update(
                    status="SKIPPED",
                    message="旧版合组任务已停用，未按旧规则继续提交",
                    waiting_reason="",
                    error_category="LEGACY_GROUP",
                    next_check_at=None,
                    next_action_at=None,
                    updated_at=iso_time(self.clock()),
                )
                self.store.save_group(connection, group)

    def _executor(self):
        with self._lock:
            if self.executor is None:
                from .crown import CrownAdapter

                self.executor = CrownAdapter(self.config, should_stop=self._stop.is_set)
            return self.executor

    def _available_account_ids(self):
        provider = getattr(self._executor(), "available_accounts", None)
        if not callable(provider):
            return []
        try:
            values = provider()
        except Exception:
            return []
        if not isinstance(values, (list, tuple)):
            return []
        return list(
            dict.fromkeys(
                value
                for item in values
                if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value := str(item or "").strip())
            )
        )

    def _retry_endpoint(self, group):
        """系统类错误重试时换一个域名入口；没有轮换需求时返回空。"""
        turn = int(group.get("endpoint_turn") or 0)
        if turn <= 0:
            return ""
        endpoints = endpoints_by_latency(self.config)
        if len(endpoints) < 2:
            return ""
        return endpoints[turn % len(endpoints)]

    def _account_balances(self):
        provider = getattr(self._executor(), "account_balances", None)
        if not callable(provider):
            return {}
        try:
            values = provider()
        except Exception:
            return {}
        if not isinstance(values, dict):
            return {}
        result = {}
        for key, value in values.items():
            try:
                result[str(key)] = float(value)
            except (TypeError, ValueError):
                continue
        return result

    def _migrate_fixed_account_rule(self):
        with self.store.connect(write=True) as connection:
            rule = self.store.rule(connection)
            changed = False
            if "account_id" in rule:
                rule.pop("account_id", None)
                changed = True
            if rule.get("min_stake") in (None, ""):
                rule["min_stake"] = 100
                changed = True
            if rule.get("split_count") in (None, ""):
                rule["split_count"] = 1
                changed = True
            if rule.get("window_seconds") != 0:
                rule["window_seconds"] = 0
                rule["version"] = int(rule.get("version", 1)) + 1
                changed = True
            if changed:
                self.store.save_rule(connection, rule)
            for group in self.store.active_groups(connection):
                if group["status"] not in {
                    "GROUPING",
                    "WAIT_MAPPING",
                    "WAIT_ACCOUNT",
                    "READY",
                    "WAIT_MARKET",
                    "PREPARED",
                    "RETRY_WAIT",
                }:
                    continue
                if connection.execute(
                    "SELECT 1 FROM follow_attempts WHERE group_id=? LIMIT 1", (group["id"],)
                ).fetchone():
                    continue
                if connection.execute(
                    "SELECT 1 FROM follow_claims WHERE group_id=? LIMIT 1", (group["id"],)
                ).fetchone():
                    continue
                group["account_id"] = ""
                group.get("rule", {}).pop("account_id", None)
                self.store.save_group(connection, group)

    def _dispatch(self, key, account_id, function, *args):
        with self._lock:
            if self._stop.is_set():
                return None
            if self._dispatcher is None:
                from .dispatcher import BoundedDispatcher

                try:
                    workers = int(self.config.get("follow_parallel_attempts") or 5)
                except (TypeError, ValueError):
                    workers = 5
                self._dispatcher = BoundedDispatcher(
                    max_workers=max(1, min(20, workers)),
                    per_account=1,
                    on_done=self._wake.set,
                )
            return self._dispatcher.submit(key, account_id, function, *args)

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            if self._dispatcher:
                for _, _, future in self._dispatcher.poll_completed():
                    if not future.cancelled():
                        try:
                            future.result()
                        except Exception:
                            self.store.recover_inflight(self.clock(), _owner_alive)
            if self._dispatcher and self._dispatcher.active_count():
                raise RuntimeError("跟单任务仍在停止中，请等待在途请求结束")
            self._dispatcher = None
            self.store.recover_inflight(self.clock(), _owner_alive)
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="ps38-follow", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        dispatcher = self._dispatcher
        if dispatcher is not None:
            dispatcher.shutdown(wait=False)
            deadline = time.monotonic() + 10
            while dispatcher.active_count():
                dispatcher.poll_completed()
                if not dispatcher.active_count() or time.monotonic() >= deadline:
                    break
                self._wake.wait(0.05)
                self._wake.clear()

    close = stop

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.run_once(background=True)
            except Exception:
                with self.store.connect() as connection:
                    self.store.set_meta(connection, "message", "跟单服务暂时异常，已保留处理状态")
            self._wake.wait(0.25)
            self._wake.clear()

    def snapshot(self, status="ALL", search="", limit=50, offset=0):
        result = self.store.snapshot(status, search, limit, offset)
        result["service"]["running"] = bool(self._thread and self._thread.is_alive())
        return result

    def available_account_ids(self):
        return self._available_account_ids()

    def detail(self, group_id):
        return self.store.detail(str(group_id))

    def observe_home_batch_rows(self, rows, *, include_settled=False):
        """Record a visible homepage batch without executing it while disabled."""
        if not rows or self._stop.is_set():
            return
        now = self.clock()
        batch = next(
            (
                item
                for item in rows
                if item.get("order_no")
                and item.get("group_key")
                and (include_settled or int(item.get("open_orders") or 0) > 0)
                and item.get("sources")
                and all(
                    isinstance(source, dict) and source.get("_source_row")
                    for source in item["sources"]
                )
            ),
            None,
        )
        if batch is None:
            return
        batch["_event_sources"] = batch["sources"]
        with self._lock, self.store.connect(write=True) as connection:
            rule = self.store.rule(connection)
            if rule["enabled"]:
                return
            self._ingest_copy_batches(connection, [batch], rule, now, enabled=False)
            self.store.set_meta(connection, "message", "跟单未开启")

    def _source_connect(self):
        path = Path(self.config["db_path"])
        if not path.is_file():
            return None
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
        connection.row_factory = sqlite3.Row
        return connection

    def _latest_cursor(self):
        connection = self._source_connect()
        if connection is None:
            return 0
        try:
            return int(
                connection.execute(
                    "SELECT COALESCE(MAX(event_id),0) FROM partner_order_events"
                ).fetchone()[0]
            )
        finally:
            connection.close()

    def save_rule(self, payload):
        with self._lock:
            with self.store.connect(write=True) as connection:
                previous = self.store.rule(connection)
                rule = validate_rule(payload, previous)
                if rule["enabled"] and not self._available_account_ids():
                    raise ValueError("启用前请在号池准备至少一个启用、在线且会话有效的皇冠会员")
                # 每次从暂停转为启用都只接收启用之后的新单，不补处理暂停期间订单。
                cursor = (
                    self._latest_cursor() if rule["enabled"] and not previous["enabled"] else None
                )
                self.store.save_rule(connection, rule)
                if cursor is not None:
                    self.store.set_meta(connection, "cursor", cursor)
                    self.store.set_meta(connection, "enabled_at", iso_time(self.clock()))
                self.store.set_meta(
                    connection,
                    "message",
                    "正在接收主页批次，按 8 位单号单次处理" if rule["enabled"] else "跟单未开启",
                )
                if not rule["enabled"]:
                    self._cancel_pending(connection)
            self._wake.set()
            return rule

    def pause(self):
        rule = self.save_rule({"enabled": False})
        return {"ok": True, "rule": rule, "message": "跟单已暂停；在途请求将保留结果"}

    def _cancel_pending(self, connection):
        for group in self.store.active_groups(connection):
            group.update(
                status="SKIPPED",
                message="规则已暂停，此组不再处理",
                updated_at=iso_time(self.clock()),
            )
            self.store.save_group(connection, group)
        # 未冻结的 10 秒待跟单窗口随暂停/缺口一起作废，避免恢复后补发旧窗口。
        for pending in self.store.pending_batches(connection):
            self.store.drop_pending(connection, pending["id"])

    def _events(self, cursor):
        connection = self._source_connect()
        if connection is None:
            return [], cursor, False
        try:
            connection.execute("BEGIN")
            minimum, maximum = connection.execute(
                "SELECT COALESCE(MIN(event_id),0),COALESCE(MAX(event_id),0) FROM partner_order_events"
            ).fetchone()
            gap = cursor > maximum or (minimum and cursor < minimum - 1)
            if gap:
                return [], cursor, True
            rows = connection.execute(
                "SELECT o.*,e.event_id,e.source AS event_source,"
                "e.member_identity AS event_member_identity,e.order_id AS event_order_id "
                "FROM partner_order_events e LEFT JOIN partner_orders o "
                "ON e.source=o.source AND e.member_identity=o.member_identity AND e.order_id=o.order_id "
                "WHERE e.event_id>? AND e.event_id<=? ORDER BY e.event_id LIMIT 5001",
                (cursor, maximum),
            ).fetchall()
            if rows and rows[-1]["event_id"] < maximum:
                # 保留窗口不超过5001；异常大批次不能在尚未消费完时冻结。
                return [], cursor, True
            return [dict(row) for row in rows], int(maximum), False
        finally:
            connection.rollback()
            connection.close()

    @staticmethod
    def _source_key(row):
        return tuple(str(row.get(key) or "") for key in ("source", "member_identity", "order_id"))

    def _copy_batches(self, events):
        if not events:
            return []
        event_keys = set()
        timestamps = []
        for event in events:
            key = (
                str(event.get("event_source") or event.get("source") or ""),
                str(event.get("event_member_identity") or event.get("member_identity") or ""),
                str(event.get("event_order_id") or event.get("order_id") or ""),
            )
            if not all(key):
                return None
            event_keys.add(key)
            try:
                timestamps.append(int(event["created_ms"]))
            except (KeyError, TypeError, ValueError):
                return None

        connection = self._source_connect()
        if connection is None:
            return None
        try:
            page = query_copy_groups(
                connection,
                status="ALL",
                limit=20_000,
                offset=0,
                from_ms=max(1, min(timestamps) - COPY_MERGE_WINDOW_MS),
                to_ms=max(timestamps) + COPY_MERGE_WINDOW_MS,
                basis="created",
                window_ms=COPY_MERGE_WINDOW_MS,
                scan_limit=20_000,
                include_internal=True,
            )
        finally:
            connection.close()

        batches = []
        covered = set()
        for batch in page["rows"]:
            sources = batch.get("sources") or []
            event_sources = [
                source
                for source in sources
                if self._source_key(source.get("_source_row") or {}) in event_keys
            ]
            if not event_sources:
                continue
            batch["_event_sources"] = event_sources
            batches.append(batch)
            covered.update(self._source_key(source["_source_row"]) for source in event_sources)
        if covered != event_keys:
            return None

        numbers = assign_copy_numbers(
            self.config["copy_orders_db_path"], [batch["group_key"] for batch in batches]
        )
        for batch in batches:
            batch["order_no"] = numbers.get(str(batch["group_key"]), "")
            if not batch["order_no"]:
                return None
        return batches

    def run_once(self, *, background=False):
        if not self._run_lock.acquire(blocking=False):
            return
        try:
            if self._dispatcher:
                for _, _, future in self._dispatcher.poll_completed():
                    if not future.cancelled():
                        # 每个任务自行原子保存结果。这里观察异常，保留数据库状态供恢复。
                        try:
                            future.result()
                        except Exception:
                            self.store.recover_inflight(self.clock(), _owner_alive)
                            with self.store.connect() as connection:
                                self.store.set_meta(
                                    connection, "message", "执行任务异常，已保留状态供核对"
                                )
            with self._lock:
                with self.store.connect() as connection:
                    rule = self.store.rule(connection)
                    cursor = self.store.meta(connection, "cursor")
                if self._stop.is_set():
                    return
                if cursor is None:
                    # 不完整旧状态也不能从零回放。
                    cursor = self._latest_cursor()
                    with self.store.connect(write=True) as connection:
                        if (
                            self.store.rule(connection) != rule
                            or self.store.meta(connection, "cursor") is not None
                        ):
                            return
                        self.store.set_meta(connection, "cursor", cursor)
                    return
                events, next_cursor, gap = self._events(cursor)
                now = self.clock()
                try:
                    batches = None if gap else self._copy_batches(events)
                except (OSError, sqlite3.Error, KeyError, TypeError, ValueError):
                    batches = None
                with self.store.connect(write=True) as connection:
                    # 源库读取期间另一服务可能已消费、暂停或改规则，不能写回旧游标。
                    if (
                        self.store.rule(connection) != rule
                        or self.store.meta(connection, "cursor") != cursor
                    ):
                        return
                    if gap:
                        rule["enabled"] = False
                        self.store.save_rule(connection, rule)
                        self._cancel_pending(connection)
                        self.store.set_meta(
                            connection, "message", "新单事件存在缺口，已暂停；请核对后重新启用"
                        )
                        return
                    # 10 秒待跟单窗口按持久化的绝对截止时间判到期，重启不重新计时。
                    if rule["enabled"]:
                        self._freeze_pending_batches(connection, now)
                    elif self.store.pending_count(connection):
                        self._cancel_pending(connection)
                    if batches is None:
                        # 源单库暂不可用时不推进执行，避免把原单校验失败误判为跳过。
                        self.store.set_meta(
                            connection,
                            "message",
                            "主页跟单批次暂不可用，已保留新单游标等待重试",
                        )
                        return
                    self._ingest_copy_batches(
                        connection, batches, rule, now, enabled=rule["enabled"]
                    )
                    self.store.set_meta(connection, "cursor", next_cursor)
                    groups = self.store.active_groups(connection) if rule["enabled"] else []
            due_groups = [
                group
                for group in groups
                if group["status"] != "PREPARING"
                and (
                    now * 1000 >= group["deadline_ms"]
                    or group.get("next_check_at") is None
                    or now >= group.get("next_check_at", 0)
                )
            ]
            for group in due_groups[:100]:
                if self._stop.is_set():
                    break
                self._advance(group["id"], background=background)
        finally:
            self._run_lock.release()

    def _base_key(self, intent, rule):
        return hashlib.sha256(
            dump(
                [
                    rule["id"],
                    rule["version"],
                    intent["fixture_key"],
                    intent["period"],
                    intent["market_type"],
                    intent["side"],
                    intent["line"],
                    intent["score"],
                    intent["is_live"],
                    intent["currency"],
                ]
            ).encode()
        ).hexdigest()

    def _new_group(self, intent, rule, now, base_key):
        start_ms = intent["created_ms"]
        target = (
            target_intent(intent, rule)
            if intent.get("market_type") in {"HANDICAP", "TOTAL"}
            and intent.get("side") in {"HOME", "AWAY", "OVER", "UNDER"}
            else intent
        )
        return {
            "id": uuid.uuid4().hex,
            "base_key": base_key,
            "rule": dict(rule),
            "rule_version": rule["version"],
            "intent": dict(intent),
            "mapping": None,
            "start_ms": start_ms,
            "close_ms": start_ms,
            "deadline_ms": start_ms + rule["max_age_seconds"] * 1000,
            "status": "READY",
            "created_at": iso_time(now),
            "updated_at": iso_time(now),
            "source_created_at": iso_time(start_ms / 1000),
            "deadline_at": iso_time(start_ms / 1000 + rule["max_age_seconds"]),
            "league": intent["league"],
            "home": intent["home"],
            "away": intent["away"],
            "crown_home": "",
            "crown_away": "",
            "period": intent["period"],
            "market_type": intent["market_type"],
            "source_side": intent["side"],
            "source_line": intent["line"],
            "target_side": target["side"],
            "target_line": target["line"],
            "direction": rule["direction"],
            "source_stake": "0",
            "target_stake": None,
            "accepted_stake": None,
            "accepted_odds": None,
            "currency": intent["currency"],
            "order_count": 0,
            "member_count": 0,
            "account_id": "",
            "score": intent["score"],
            "odds": None,
            "odds_format": "HK",
            "receipt_id": None,
            "message": "主页批次已接收，立即开始处理",
            "prepare_count": 0,
            "retry_count": 0,
            "reject_count": 0,
            "reconcile_count": 0,
            "next_action_at": None,
            "waiting_reason": "等待赛事映射与皇冠会员分配",
            "error_category": "",
        }

    def _fallback_intent(self, row, now):
        created_ms = row.get("created_ms")
        if not isinstance(created_ms, int) or not 0 < created_ms <= 32_503_680_000_000:
            created_ms = int(now * 1000)
        try:
            stake = number_text(decimal_value(row.get("stake") or "0"))
        except ValueError:
            stake = "0"
        return {
            "source": str(row.get("source") or row.get("event_source") or "ps38ag"),
            "member_identity": str(
                row.get("member_identity") or row.get("event_member_identity") or ""
            ),
            "order_id": str(row.get("order_id") or row.get("event_order_id") or ""),
            "member_code": str(row.get("member_user_code") or ""),
            "created_ms": created_ms,
            "league": str(row.get("league") or ""),
            "home": str(row.get("teams") or ""),
            "away": "",
            "period": "",
            "market_type": "",
            "side": "",
            "line": "0",
            "score": "",
            "currency": "CNY"
            if str(row.get("currency") or "").upper() in {"CNY", "RMB", "THB"}
            else str(row.get("currency") or ""),
            "reported_currency": str(row.get("currency") or ""),
            "stake": stake,
            "content_hash": str(row.get("content_hash") or ""),
        }

    def _save_copy_batch(self, connection, batch, event_sources, intents, rule, now, message=""):
        order_no = str(batch["order_no"])
        if self.store.group_for_source_batch(connection, order_no):
            return False
        source_rows = [source["_source_row"] for source in event_sources]
        if not intents:
            first_row = source_rows[0] if source_rows else {}
            representative = self._fallback_intent(first_row, now)
            safe_rule = {**rule, "direction": "forward"}
            saved_intents = [representative]
        else:
            saved_intents = sorted(intents, key=lambda item: item["created_ms"])
            representative = saved_intents[0]
            safe_rule = rule

        start_ms = min(item["created_ms"] for item in saved_intents)
        base_key = "homepage-batch:" + order_no
        group = self._new_group(representative, safe_rule, now, base_key)
        group.update(
            source_batch_no=order_no,
            source_batch_key=str(batch["group_key"]),
            start_ms=start_ms,
            close_ms=start_ms,
            deadline_ms=start_ms + rule["max_age_seconds"] * 1000,
            source_created_at=iso_time(start_ms / 1000),
            deadline_at=iso_time(start_ms / 1000 + rule["max_age_seconds"]),
            status="SKIPPED" if message else "READY",
            source_stake=number_text(
                sum((Decimal(item["stake"]) for item in intents), Decimal("0"))
            ),
            order_count=len(source_rows) if message else len(intents),
            member_count=len({item["member_identity"] for item in saved_intents}),
            message=message or "主页批次已接收，立即开始处理",
            waiting_reason="" if message else "等待赛事映射与皇冠会员分配",
            error_category=(
                "RULE_DISABLED"
                if message == "跟单未开启，未发送"
                else "BATCH_SKIPPED"
                if message
                else ""
            ),
        )
        try:
            self.store.save_group(connection, group)
        except sqlite3.IntegrityError:
            if self.store.group_for_source_batch(connection, order_no):
                return False
            raise

        members = []
        for item in saved_intents:
            if self.store.contains_member(connection, item):
                continue
            self.store.add_member(connection, group["id"], item)
            members.append(item)
        group["member_count"] = len({item["member_identity"] for item in members})
        self.store.save_group(connection, group)
        return True

    def _ingest_copy_batches(self, connection, batches, rule, now, *, enabled=True):
        for batch in batches:
            if enabled:
                self._queue_copy_batch(connection, batch, rule, now)
            else:
                self._record_disabled_batch(connection, batch, rule, now)

    def _record_disabled_batch(self, connection, batch, rule, now):
        """跟单未开启时只登记展示批次，不进入 10 秒待跟单窗口。"""
        order_no = str(batch["order_no"])
        if self.store.group_for_source_batch(connection, order_no):
            return
        event_sources = batch.get("_event_sources") or batch.get("sources") or []
        if any(
            self.store.contains_member(connection, source.get("_source_row") or {})
            for source in batch.get("sources", [])
        ):
            self._save_copy_batch(
                connection,
                batch,
                event_sources,
                [],
                rule,
                now,
                "页面批次包含已处理源单，后续加入订单不追加",
            )
            return
        intents = []
        for source in event_sources:
            row = source["_source_row"]
            try:
                intent = source_intent(row, self._allow_prematch())
            except ValueError:
                intent = self._fallback_intent(row, now)
            intents.append(intent)
        self._save_copy_batch(
            connection, batch, event_sources, intents, rule, now, "跟单未开启，未发送"
        )

    def _queue_copy_batch(self, connection, batch, rule, now):
        """把新源单并入同一 20 秒展示批次的 10 秒待跟单窗口，绝不重置倒计时。"""
        event_sources = batch.get("_event_sources") or batch.get("sources") or []
        sources = [
            source
            for source in event_sources
            if (source.get("_source_row") or {})
            and not self.store.contains_member(connection, source["_source_row"])
            and not self.store.pending_contains_member(connection, source["_source_row"])
        ]
        if not sources:
            return
        intents = []
        reason = ""
        for source in sources:
            row = source["_source_row"]
            if (
                rule["member_codes"]
                and str(row.get("member_user_code") or "") not in rule["member_codes"]
            ):
                continue
            try:
                intent = source_intent(row, self._allow_prematch())
            except ValueError as exc:
                reason = reason or str(exc)
                continue
            age = now * 1000 - intent["created_ms"]
            if age < -5000 or age >= rule["max_age_seconds"] * 1000:
                reason = reason or "原单时间异常或已超过最长处理时效"
                continue
            intents.append(intent)
        if reason:
            self._save_copy_batch(connection, batch, sources, intents, rule, now, reason)
            return
        if not intents:
            self._save_copy_batch(
                connection,
                batch,
                sources,
                [],
                rule,
                now,
                "主页批次内没有符合当前来源会员规则的新单",
            )
            return
        self._open_pending_batch(connection, batch, intents, rule, now)

    def _open_pending_batch(self, connection, batch, intents, rule, now):
        """首单进入时写入绝对截止时间；后续订单只并入，不修改截止时间。"""
        key = str(batch["group_key"])
        pending = self.store.pending_for_key(connection, key)
        if pending is not None and int(pending["freeze_at_ms"]) <= int(now * 1000):
            # 已到期但还没轮到冻结：新单归下一批，不能顺延旧窗口。
            pending = None
        if pending is None:
            pending = {
                "id": uuid.uuid4().hex,
                "source_batch_key": key,
                "page_batch_no": str(batch.get("order_no") or ""),
                "freeze_at_ms": int(now * 1000) + int(PENDING_SECONDS * 1000),
                "status": "OPEN",
                "created_at": iso_time(now),
                "rule": dict(rule),
            }
            self.store.save_pending(connection, pending)
        for intent in sorted(intents, key=lambda item: item["created_ms"]):
            if self.store.contains_member(connection, intent):
                continue
            self.store.add_pending_member(connection, pending["id"], intent)

    def _freeze_pending_batches(self, connection, now):
        deadline_ms = int(now * 1000)
        for pending in self.store.pending_batches(connection):
            if int(pending.get("freeze_at_ms") or 0) > deadline_ms:
                continue
            self._freeze_pending_batch(connection, pending, now)

    def _freeze_pending_batch(self, connection, pending, now):
        """满 10 秒冻结：统计最终单数/总额，生成批次号并交给原执行链路。"""
        members = [
            member
            for member in self.store.pending_members(connection, pending["id"])
            if not self.store.contains_member(connection, member)
        ]
        if not members:
            self.store.drop_pending(connection, pending["id"])
            return None
        rule = {**pending["rule"], "currency": "CNY"}
        representative = min(members, key=lambda item: item["created_ms"])
        source_batch_key = str(pending["source_batch_key"])
        start_ms = min(int(item["created_ms"]) for item in members)
        group = None
        for _attempt in range(3):
            batch_no = self._next_batch_no(
                connection, str(pending.get("page_batch_no") or ""), source_batch_key
            )
            group = self._new_group(representative, rule, now, "homepage-batch:" + batch_no)
            group.update(
                source_batch_no=batch_no,
                source_batch_key=source_batch_key,
                start_ms=start_ms,
                close_ms=start_ms,
                deadline_ms=start_ms + rule["max_age_seconds"] * 1000,
                source_created_at=iso_time(start_ms / 1000),
                deadline_at=iso_time(start_ms / 1000 + rule["max_age_seconds"]),
                status="READY",
                source_stake=number_text(
                    sum((Decimal(item["stake"]) for item in members), Decimal("0"))
                ),
                order_count=len(members),
                member_count=len({item["member_identity"] for item in members}),
                message="10 秒跟单窗口已冻结，等待执行",
                waiting_reason="等待赛事映射与皇冠会员分配",
                error_category="",
            )
            try:
                self.store.save_group(connection, group)
                break
            except sqlite3.IntegrityError:
                # 批次号被并发冻结的实例占用；换下一个后缀重试，不丢成员。
                group = None
        if group is None:
            # 连续冲突时保留待跟单组，下一轮继续冻结而不是丢单。
            return None
        for item in members:
            self.store.add_member(connection, group["id"], item)
        self.store.drop_pending(connection, pending["id"])
        return group

    def _next_batch_no(self, connection, page_batch_no, source_batch_key):
        """首个跟单批次沿用页面 8 位号；同一展示批次再冻结时用 -2、-3 后缀。"""
        base = str(page_batch_no or "").strip() or "P" + str(source_batch_key)[:8]
        candidate = base
        index = 1
        while connection.execute(
            "SELECT 1 FROM follow_groups WHERE json_extract(payload,'$.source_batch_no')=?",
            (candidate,),
        ).fetchone():
            index += 1
            candidate = f"{base}-{index}"
        return candidate

    def _skip(self, connection, row, rule, now, message, intent=None):
        if intent is None:
            # 不可解析来源只保留安全业务字段；绝不返回 raw_json。
            intent = self._fallback_intent(row, now)
        safe_rule = {**rule, "direction": "forward"} if not intent["side"] else rule
        group = self._new_group(intent, safe_rule, now, "skipped:" + uuid.uuid4().hex)
        group.update(
            status="SKIPPED",
            message=message,
            source_stake=intent.get("stake", "0"),
            order_count=1,
            member_count=1,
        )
        self.store.save_group(connection, group)
        self.store.add_member(connection, group["id"], intent)

    # ── 失败策略：按皇冠返回码决定重查 / 换号 / 等待 / 停止 ─────────────
    def _apply_error_policy(self, group, result, connection):
        action = str(result.get("action") or "")
        error_code = str(result.get("error_code") or "")[:60]
        message = str(group.get("message") or "")[:160]
        now = self.clock()
        deadline_ms = float(group.get("deadline_ms") or 0)
        rule = self.store.rule(connection)
        running = rule["enabled"] and not self._stop.is_set()

        def guarded(next_at):
            return running and next_at * 1000 < deadline_ms

        def wait_until(next_at, status, text, **extra):
            group.update(
                status=status,
                next_check_at=next_at,
                next_action_at=iso_time(next_at),
                waiting_reason=message,
                message=text,
                **extra,
            )

        if action == "switch_account" or action == "disable_account":
            if action == "disable_account":
                self._disable_pool_account(group.get("account_id") or "")
            tried = set(group.get("tried_accounts") or [])
            if group.get("account_id"):
                tried.add(str(group["account_id"]))
            group["tried_accounts"] = sorted(tried)
            others = [
                account_id
                for account_id in self._available_account_ids()
                if account_id not in tried
            ]
            if others and guarded(now + 2):
                wait_until(now + 2, "RETRY_WAIT", f"换号重试（还剩 {len(others)} 个号）：{message}")
                return
            group.update(status="REJECTED", message=f"所有会员都试过，已停止跟单：{message}")
            return

        if action == "retry_bet_switch":
            # 通用拒绝：先逐个换号试，3 个号都不行就保留订单，每 15 秒探一次。
            until = float(group.get("recheck_until") or 0)
            if until and now < until:
                next_at = min(now + 15, until)
                wait_until(
                    next_at,
                    "RETRY_WAIT",
                    f"换号仍被拒，保留订单再试（剩 {int(until - now)} 秒）：{message}",
                )
                return
            tried = set(group.get("tried_accounts") or [])
            if group.get("account_id"):
                tried.add(str(group["account_id"]))
            group["tried_accounts"] = sorted(tried)
            untried = [
                account_id
                for account_id in self._available_account_ids()
                if account_id not in tried
            ]
            if untried and len(tried) < 3 and guarded(now + 0.4):
                wait_until(
                    now + 0.4,
                    "RETRY_WAIT",
                    f"换号重试（第 {len(tried) + 1} 个号）：{message}",
                )
                return
            window = float(group.get("recheck_until") or (now + 90))
            group["deadline_ms"] = max(deadline_ms, (window + 5) * 1000)
            deadline_ms = float(group["deadline_ms"])
            if now >= window or not running:
                group.update(status="REJECTED", message=f"换号重试仍被拒，已停止跟单：{message}")
                return
            group["recheck_until"] = window
            group["tried_accounts"] = []
            wait_until(
                min(now + 15, window),
                "RETRY_WAIT",
                f"换号仍被拒，保留订单再试（剩 {int(window - now)} 秒）：{message}",
            )
            return

        if action == "wait_1x001":
            until = float(group.get("market_wait_until") or (now + 260))
            group["deadline_ms"] = max(float(deadline_ms), (until + 5) * 1000)
            deadline_ms = float(group["deadline_ms"])
            if now >= until or now * 1000 >= deadline_ms or not running:
                group.update(status="REJECTED", message=f"盘口封盘等待超时，已停止跟单：{message}")
                return
            group["market_wait_until"] = until
            wait_until(
                min(now + 30, until),
                "WAIT_MARKET",
                f"盘口封盘，保留订单等待开盘（剩 {int(until - now)} 秒）：{message}",
            )
            return

        if action == "retry_3x10":
            count = int(group.get("order_retry_count") or 0) + 1
            group["order_retry_count"] = count
            if count <= 3 and guarded(now + 10):
                wait_until(now + 10, "RETRY_WAIT", f"投注失败，10 秒后第 {count} 次重试：{message}")
                return
            group.update(status="REJECTED", message=f"投注失败重试 3 次仍失败：{message}")
            return

        if action == "retry_system":
            count = int(group.get("system_retry_count") or 0) + 1
            group["system_retry_count"] = count
            if count <= 3 and guarded(now + 2):
                group["endpoint_turn"] = int(group.get("endpoint_turn") or 0) + 1
                wait_until(
                    now + 2,
                    "RETRY_WAIT",
                    f"系统繁忙，换号换域名重试（第 {count} 次）：{message}",
                )
                return
            group.update(status="REJECTED", message=f"系统繁忙重试 3 次仍失败：{message}")
            return

        if action == "retry_market_90":
            count = int(group.get("market_retry_count") or 0) + 1
            group["market_retry_count"] = count
            if count <= 2 and guarded(now + 0.4):
                # 盘口变动要在同一个账号上立刻重查重提，排队等待只会错过窗口。
                group["fast_retry"] = True
                wait_until(now + 0.4, "RETRY_WAIT", f"重新查盘再提交：{message}")
                return
            until = float(group.get("recheck_until") or (now + 90))
            group["deadline_ms"] = max(float(deadline_ms), (until + 5) * 1000)
            deadline_ms = float(group["deadline_ms"])
            if now >= until or now * 1000 >= deadline_ms or not running:
                group.update(status="REJECTED", message=f"盘口多次变化，已停止跟单：{message}")
                return
            group["recheck_until"] = until
            wait_until(
                min(now + 30, until),
                "RETRY_WAIT",
                f"盘口已变化，保留订单再试（剩 {int(until - now)} 秒）：{message}",
            )
            return

        if action == "split_half":
            try:
                stake = Decimal(str(group.get("target_stake") or "0"))
            except Exception:
                stake = Decimal("0")
            half = (stake / 2).quantize(Decimal("1"))
            if stake > 100 and half >= 100 and guarded(now + 2):
                group["target_stake"] = number_text(half)
                group["split_remaining"] = number_text(
                    Decimal(str(group.get("split_remaining") or stake)) - half
                )
                wait_until(now + 2, "RETRY_WAIT", f"单注超限，金额减半重新提交：{message}")
                return
            group.update(status="REJECTED", message=f"单注超限且无法拆分：{message}")
            return

        if action == "pending":
            group.update(
                status="REJECTED",
                message="注单处理中但未取得票号，请到平台核对：" + message,
            )
            return

        # 未标注动作：沿用旧的“白名单”判定。
        known = (
            error_code in RETRYABLE_CROWN_CODES
            or error_code == "doubleLogin"
            or error_code.startswith("betError")
        )
        proven_unsent = result.get("request_sent") is False and error_code in {
            "QUOTE_EXPIRED",
            "ACCOUNT_SESSION",
            "PREPARE_TIMEOUT",
            "TRANSPORT_CONNECT",
        }
        retryable = result.get("retryable") is True and (known or proven_unsent)
        if group.get("status") == "REJECTED":
            group["reject_count"] = int(group.get("reject_count") or 0) + 1
        fast = error_code.startswith("betError") or error_code in {
            "1X005",
            "1X006",
            "1X013",
            "1X015",
            "1X016",
        }
        delay = 0.4 if fast else 2
        if fast:
            group["fast_retry"] = True
        if retryable and int(group.get("reject_count") or 0) < 3 and guarded(now + delay):
            wait_until(now + delay, "RETRY_WAIT", f"等待重新查盘：{message}")
            return
        if str(result.get("category") or "") == "AMOUNT_REJECTED":
            group["message"] = f"金额受限，已停止跟单：{message}"

    def _disable_pool_account(self, account_id):
        """皇冠返回账户停用/禁登录时，把该会员标记为停用，后续不再派单。"""
        if not account_id:
            return
        try:
            database = Path(self.config["pool_db_path"])
            with closing(sqlite3.connect(str(database), timeout=5)) as connection:
                connection.execute(
                    "UPDATE accounts SET enabled=0, status='expired', status_message=?, "
                    "last_error=?, updated_at=? WHERE id=?",
                    (
                        "皇冠账户已被停用/禁止登录",
                        "user_disabled",
                        iso_time(self.clock()),
                        account_id,
                    ),
                )
                connection.commit()
        except (OSError, KeyError, sqlite3.Error):
            pass

    def _set_status(self, group, status, message):
        with self.store.connect(write=True) as connection:
            current = self.store.get_group(connection, group["id"])
            if (
                current is None
                or current["status"] not in ACTIVE
                or current.get("_revision", 0) != group.get("_revision", 0)
            ):
                return False
            group.update(status=status, message=message, updated_at=iso_time(self.clock()))
            if status not in {"WAIT_MAPPING", "WAIT_MARKET", "RETRY_WAIT"}:
                group["next_action_at"] = None
            if status not in ACTIVE:
                group["waiting_reason"] = ""
            self.store.save_group(connection, group)
            return True

    # 只比对会影响跟单语义的字段；页面上的浮动展示字段不参与判断。
    _MEMBER_FIELD_LABELS = {
        "league": "联赛",
        "home": "主队",
        "away": "客队",
        "event_ms": "开赛时间",
        "market_type": "玩法",
        "period": "时段",
        "side": "方向",
        "line": "盘口线",
        "is_live": "滚球状态",
        "score": "比分",
        "stake": "金额",
        "odds": "赔率",
    }

    @classmethod
    def _changed_member_field(cls, current, member):
        for key, label in cls._MEMBER_FIELD_LABELS.items():
            if key not in member:
                continue
            left = current.get(key)
            right = member.get(key)
            left = "" if left is None else str(left)
            right = "" if right is None else str(right)
            if left != right:
                return label
        return ""

    def _validate_members(self, members):
        connection = self._source_connect()
        if connection is None:
            raise ValueError("原单库暂不可用")
        try:
            for member in members:
                row = connection.execute(
                    "SELECT * FROM partner_orders WHERE source=? AND member_identity=? AND order_id=?",
                    (member["source"], member["member_identity"], member["order_id"]),
                ).fetchone()
                if row is None:
                    raise ValueError("组内原单已不存在")
                current = source_intent(dict(row), self._allow_prematch())
                changed = self._changed_member_field(current, member)
                if changed:
                    raise ValueError(f"组内原单{changed}已变化，需重新核对")
        finally:
            connection.close()

    def _enabled(self):
        with self.store.connect() as connection:
            return self.store.rule(connection)["enabled"] and not self._stop.is_set()

    def _assign_ready_account(self, group_id, *, background=False):
        accounts = self._available_account_ids()
        now = self.clock()
        with self._lock, self.store.connect(write=True) as connection:
            group = self.store.get_group(connection, group_id)
            if group is None or group["status"] != "READY":
                return None
            if background and self._dispatcher and self._dispatcher.available_slots() == 0:
                group.update(
                    account_id="",
                    next_check_at=now + 0.25,
                    next_action_at=iso_time(now + 0.25),
                    waiting_reason="等待可用执行名额",
                    message="跟单任务排队中",
                    updated_at=iso_time(now),
                )
                self.store.save_group(connection, group)
                return None
            if not accounts:
                group.update(
                    account_id="",
                    status="WAIT_ACCOUNT",
                    next_check_at=now + 2,
                    next_action_at=iso_time(now + 2),
                    waiting_reason="号池没有启用、在线且会话有效的皇冠会员",
                    message="等待号池恢复可用皇冠会员",
                    error_category="ACCOUNT_UNAVAILABLE",
                    updated_at=iso_time(now),
                )
                self.store.save_group(connection, group)
                return None
            # 与 follow_claims 的账号唯一约束一致，同一账号始终只执行一单。
            limit = 1
            inflight = self.store.account_inflight(connection, group_id)
            if background and self._dispatcher:
                for account_id in self._dispatcher.active_accounts():
                    inflight[account_id] = inflight.get(account_id, 0) + 1
            busy = {account_id for account_id, count in inflight.items() if count >= limit}
            # 盘口变动重试：同一个号立刻再打一次，不重新排队选号。
            if group.get("fast_retry") and group.get("account_id") in accounts:
                keep = str(group["account_id"])
                if keep not in busy:
                    group.update(
                        fast_retry=False,
                        next_check_at=now,
                        next_action_at=None,
                        waiting_reason="同账号快速重试",
                        updated_at=iso_time(now),
                    )
                    self.store.save_group(connection, group)
                    return keep
            tried = set(group.get("tried_accounts") or [])
            free = [
                account_id
                for account_id in accounts
                if account_id not in busy and account_id not in tried
            ]
            if not free:
                group.update(
                    account_id="",
                    status="WAIT_ACCOUNT",
                    next_check_at=now + 1,
                    next_action_at=iso_time(now + 1),
                    waiting_reason="可用皇冠会员正在执行中",
                    message="等待皇冠会员空闲",
                    error_category="ACCOUNT_BUSY",
                    updated_at=iso_time(now),
                )
                self.store.save_group(connection, group)
                return None
            # 只挑额度够本次跟单金额的账号：跟 10000 不能派只有 8000 的号。
            balances = self._account_balances()
            try:
                stake = float(group.get("target_stake") or 0)
            except (TypeError, ValueError):
                stake = 0.0

            def affordable(account_id):
                value = balances.get(account_id)
                return value is None or value + 1e-9 >= stake

            funded = [account_id for account_id in free if affordable(account_id)]
            if not funded:
                detail = "、".join(
                    f"{account_id[:6]}… {balances.get(account_id, 0):.0f}"
                    for account_id in free[:4]
                )
                group.update(
                    account_id="",
                    status="WAIT_ACCOUNT",
                    next_check_at=now + 2,
                    next_action_at=iso_time(now + 2),
                    waiting_reason=f"会员额度不足（本次需要 {stake:.0f}，现有 {detail}）",
                    message="等待有足够额度的皇冠会员",
                    error_category="ACCOUNT_CREDIT",
                    updated_at=iso_time(now),
                )
                self.store.save_group(connection, group)
                return None
            free = funded
            used = set()
            for row in connection.execute(
                "SELECT payload FROM follow_attempts WHERE group_id=?", (group_id,)
            ).fetchall():
                try:
                    account_id = str(json.loads(row[0]).get("account_id") or "")
                except (TypeError, ValueError):
                    account_id = ""
                if account_id:
                    used.add(account_id)
            cursor = str(self.store.meta(connection, "account_rotation_cursor", "") or "")
            start = accounts.index(cursor) + 1 if cursor in accounts else 0
            ordered = accounts[start:] + accounts[:start]
            candidates = [account_id for account_id in ordered if account_id in free]
            # 本组还没用过的号优先；同等条件下额度越大越优先。
            fresh = [account_id for account_id in candidates if account_id not in used]
            pool = fresh or candidates
            pool.sort(key=lambda account_id: -(balances.get(account_id) or 0.0))
            selected = pool[0]
            group.update(
                account_id=selected,
                next_check_at=now,
                next_action_at=None,
                waiting_reason="已从在线皇冠会员池分配执行账号",
                message="订单组已分配皇冠会员，等待执行",
                error_category="",
                updated_at=iso_time(now),
            )
            self.store.save_group(connection, group)
            self.store.set_meta(connection, "account_rotation_cursor", selected)
            return selected

    def _advance(self, group_id, *, background=False):
        # 调度循环只做本地校验与排队，网络请求在独立执行线程内完成。
        with self._lock:
            if not self._enabled():
                return
            if background and self._dispatcher and self._dispatcher.is_key_active(group_id):
                return
            with self.store.connect() as connection:
                group = self.store.get_group(connection, group_id)
                members = self.store.get_members(connection, group_id)
            if group is None or group["status"] not in ACTIVE or group["status"] == "PREPARING":
                return
            now = self.clock()
            if now * 1000 >= group["deadline_ms"]:
                self._set_status(group, "EXPIRED", "超过最长源单处理时效")
                return
            # 失败策略设置的等待窗口到期后不再重试。
            market_until = group.get("market_wait_until")
            if market_until and now >= float(market_until):
                self._set_status(group, "REJECTED", "盘口封盘等待超时，已停止跟单")
                return
            recheck_until = group.get("recheck_until")
            if recheck_until and now >= float(recheck_until):
                self._set_status(group, "REJECTED", "盘口多次变化等待超时，已停止跟单")
                return
            if group.get("next_check_at") is not None and now < group["next_check_at"]:
                return
            try:
                mapping = resolve_fixture(self.config["merge_db_path"], group["intent"])
            except ValueError as exc:
                group.update(
                    next_check_at=now + 3,
                    next_action_at=iso_time(now + 3),
                    waiting_reason=str(exc),
                    error_category="MAPPING",
                )
                self._set_status(group, "WAIT_MAPPING", str(exc))
                return
            if group.get("mapping") and group["mapping"]["version"] != mapping["version"]:
                self._set_status(group, "SKIPPED", "赛事映射已变化，此组需重新核对")
                return
            group["mapping"] = mapping
            group["crown_home"] = mapping["crownSnapshot"]["home"]
            group["crown_away"] = mapping["crownSnapshot"]["away"]
            try:
                self._validate_members(members)
                group["target_stake"] = group_stake(group["source_stake"], group["rule"])
            except ValueError as exc:
                self._set_status(group, "SKIPPED", str(exc))
                return
            raw_min = group["rule"].get("min_stake")
            min_stake = Decimal(str(100 if raw_min is None else raw_min))
            if min_stake and Decimal(str(group["target_stake"])) < min_stake:
                self._set_status(
                    group,
                    "SKIPPED",
                    f"目标金额 {group['target_stake']} 低于皇冠最低投注 {number_text(min_stake)}，未跟单",
                )
                return
            with self.store.connect() as connection:
                usage = load_usage(connection, group_id)
            allowed, limit_reason, limit_meta = apply_limits(group, group["rule"], usage)
            group["limit"] = limit_meta
            if not allowed:
                self._set_status(group, "SKIPPED", limit_reason)
                return
            if limit_meta.get("limited"):
                group["target_stake"] = number_text(Decimal(allowed))
                group["message"] = f"按累计限额调整为 {group['target_stake']} 元"
            group.update(
                next_check_at=now,
                next_action_at=None,
                waiting_reason="等待在线皇冠会员自动分配",
            )
            if not self._set_status(group, "READY", "订单组已冻结，等待执行"):
                return
        account_id = self._assign_ready_account(group_id, background=background)
        if account_id is None:
            return
        if background:
            if self._dispatch(group_id, account_id, self._execute_group, group_id) is None:
                with self._lock, self.store.connect(write=True) as connection:
                    current = self.store.get_group(connection, group_id)
                    if (
                        current
                        and current["status"] == "READY"
                        and current.get("account_id") == account_id
                    ):
                        current.update(
                            account_id="",
                            status="WAIT_ACCOUNT",
                            next_check_at=self.clock() + 1,
                            next_action_at=iso_time(self.clock() + 1),
                            waiting_reason="等待执行名额，稍后重新分配会员",
                            message="跟单任务排队中",
                            error_category="ACCOUNT_BUSY",
                            updated_at=iso_time(self.clock()),
                        )
                        self.store.save_group(connection, current)
        else:
            self._execute_group(group_id)

    def _execute_group(self, group_id):
        with self._lock:
            if not self._enabled():
                return
            with self.store.connect(write=True) as connection:
                group = self.store.get_group(connection, group_id)
                if not group or group["status"] != "READY":
                    return
                if not self.store.claim(
                    connection, group, self._owner_id, os.getpid(), "PREPARING", self.clock()
                ):
                    group.update(
                        account_id="",
                        status="WAIT_ACCOUNT",
                        next_check_at=self.clock() + 1,
                        next_action_at=iso_time(self.clock() + 1),
                        waiting_reason="皇冠会员刚被其他任务占用，等待重新分配",
                        message="等待皇冠会员空闲",
                        error_category="ACCOUNT_BUSY",
                        updated_at=iso_time(self.clock()),
                    )
                    self.store.save_group(connection, group)
                    return
                group.update(
                    status="PREPARING",
                    prepare_count=group.get("prepare_count", 0) + 1,
                    waiting_reason="正在核验皇冠盘口",
                    message="正在核验皇冠盘口",
                    updated_at=iso_time(self.clock()),
                )
                self.store.save_group(connection, group)
                members = self.store.get_members(connection, group_id)
            mapping = group["mapping"]
            intent = target_intent(group["intent"], group["rule"])
            retry_endpoint = self._retry_endpoint(group)
            if retry_endpoint:
                intent["_endpoint"] = retry_endpoint
        try:
            if int(group["rule"].get("split_count") or 1) > 1:
                self._execute_split_group(group, members, mapping, intent)
                return
            try:
                wager_baseline = self._today_wager_baseline(group["account_id"])
                quote = self._executor().prepare(mapping, intent, group["account_id"])
                self._validate_quote(quote, intent, group)
            except Exception as exc:
                self._prepare_failed(group, exc)
                return
            with self._lock:
                if not self._enabled():
                    return
                if self.clock() * 1000 >= group["deadline_ms"]:
                    self._set_status(group, "EXPIRED", "盘口核验完成时原单已过期")
                    return
                try:
                    self._validate_members(members)
                    latest = resolve_fixture(self.config["merge_db_path"], group["intent"])
                    if latest["version"] != mapping["version"]:
                        raise ValueError("盘口核验期间赛事映射发生变化")
                    self._validate_quote(quote, intent, group)
                except ValueError as exc:
                    self._set_status(group, "SKIPPED", str(exc))
                    return
                with self.store.connect(write=True) as connection:
                    current = self.store.get_group(connection, group_id)
                    if (
                        not current
                        or current["status"] != "PREPARING"
                        or current.get("_revision", 0) != group.get("_revision", 0)
                        or not self.store.rule(connection)["enabled"]
                        or self._stop.is_set()
                        or not self.store.owns_claim(connection, group_id, self._owner_id)
                    ):
                        return
                    group.update(
                        status="SUBMITTING",
                        odds=str(quote["odds"]),
                        message=(
                            "皇冠变盘未复核，已按盘口报价提交，等待回执"
                            if quote.get("verified") is False
                            else "请求处理中，等待平台回执"
                        ),
                        waiting_reason="等待平台回执",
                        updated_at=iso_time(self.clock()),
                        next_action_at=None,
                    )
                    group["quote"] = {
                        key: quote.get(key)
                        for key in (
                            "gid",
                            "master_gid",
                            "period",
                            "market_type",
                            "side",
                            "line",
                            "score",
                            "odds",
                            "odds_format",
                            "currency",
                            "obtained_at",
                            "wtype",
                            "rtype",
                            "chose_team",
                            "con",
                            "ratio",
                            "verified",
                            "verification",
                        )
                    }
                    self.store.save_group(connection, group)
                    attempt = {
                        "group_id": group_id,
                        "account_id": group["account_id"],
                        "status": "SUBMITTING",
                        "started_at": iso_time(self.clock()),
                        "stake": group["target_stake"],
                        "quote": group["quote"],
                        "submitted_quote": group["quote"],
                        "target": intent,
                    }
                    attempt_id = connection.execute(
                        "INSERT INTO follow_attempts(group_id,status,payload) VALUES (?,?,?)",
                        (group_id, "SUBMITTING", dump(attempt)),
                    ).lastrowid
                    connection.execute(
                        "UPDATE follow_claims SET phase='SUBMITTING' WHERE group_id=? AND owner_id=?",
                        (group_id, self._owner_id),
                    )
            # 先持久化在途尝试，再发一次写请求；结果只有成功或失败两种。
            try:
                result = self._executor().submit(quote, group["target_stake"], group["account_id"])
            except Exception as exc:
                result = {
                    "status": "REJECTED",
                    "category": "SUBMIT_FAILED",
                    "error_code": "SUBMIT_FAILED",
                    "message": f"提交未完成（{type(exc).__name__}），按失败处理",
                    "retryable": False,
                    "request_sent": False,
                }
            if result.get("category") == "UNCERTAIN_RESULT" and result.get("request_sent"):
                result = self._reconcile_uncertain(
                    group.get("account_id"),
                    (group.get("quote") or {}).get("gid"),
                    group.get("target_stake"),
                    result,
                    wager_baseline,
                )
            self._finish(group, attempt_id, attempt, result)
        finally:
            with self.store.connect(write=True) as connection:
                self.store.release_claim(connection, group_id, self._owner_id)
            self._wake.set()

    @staticmethod
    def _split_result(result, stake):
        """校验单份提交结果，返回 (状态, 票号, 成交金额, 成交水位)。"""
        result = result if isinstance(result, dict) else {}
        status = result.get("status")
        receipt = str(result.get("receipt_id") or "").strip()
        if status not in {"SUCCEEDED", "REJECTED"} or (status == "SUCCEEDED" and not receipt):
            status = "REJECTED"
        if receipt and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", receipt):
            status, receipt = "REJECTED", ""
        accepted_stake = accepted_odds = None
        for key, target in (("accepted_stake", "stake"), ("accepted_odds", "odds")):
            try:
                value = decimal_value(result.get(key), "回执数值")
                if value > 0:
                    if target == "stake":
                        accepted_stake = number_text(value)
                    else:
                        accepted_odds = number_text(value)
            except ValueError:
                pass
        if status == "SUCCEEDED" and not accepted_stake:
            accepted_stake = number_text(Decimal(stake))
        return status, receipt, accepted_stake, accepted_odds

    def _execute_split_group(self, group, members, mapping, intent):
        """同一组按规则拆成 N 份依次提交，全部成功才算整体成功。"""
        group_id = group["id"]
        rule = group["rule"]
        raw_min = rule.get("min_stake")
        min_stake = Decimal(str(100 if raw_min is None else raw_min))
        total_stake = Decimal(str(group["target_stake"]))
        if min_stake and total_stake < min_stake:
            self._set_status(
                group,
                "SKIPPED",
                f"目标金额 {group['target_stake']} 低于最低投注 {number_text(min_stake)}，未跟单",
            )
            return
        count = max(1, int(rule.get("split_count") or 1))
        parts = split_stakes(group["target_stake"], count)
        # 每一份都必须 >= 100，否则减少份数（最低 1 份）。
        while len(parts) > 1 and min_stake and min(Decimal(str(p)) for p in parts) < min_stake:
            parts = split_stakes(group["target_stake"], len(parts) - 1)
        delay = float(rule.get("split_delay_seconds") or 0)
        results = []
        tickets = []
        accepted_total = Decimal("0")
        accepted_odds = None
        success = 0
        aborted = ""
        for index, stake in enumerate(parts):
            if self._stop.is_set() or not self._enabled():
                aborted = "跟单已停止"
                break
            if self.clock() * 1000 >= group["deadline_ms"]:
                aborted = "超过最长源单处理时效"
                break
            try:
                wager_baseline = self._today_wager_baseline(group["account_id"])
                quote = self._executor().prepare(mapping, intent, group["account_id"])
                self._validate_quote(quote, intent, group)
            except Exception as exc:  # noqa: BLE001 - 单份查盘失败，停止后续分单
                aborted = (
                    str(exc)[:180]
                    if isinstance(exc, ValueError)
                    else "皇冠盘口查询暂不可用，请检查账号会话"
                )
                results.append(
                    {
                        "id": uuid.uuid4().hex,
                        "group_id": group_id,
                        "part_index": len(results),
                        "parent_part_index": index,
                        "status": "REJECTED",
                        "stake": number_text(Decimal(stake)),
                        "account_id": group["account_id"],
                        "ticket_id": None,
                        "message": aborted,
                    }
                )
                break
            attempt = {
                "group_id": group_id,
                "account_id": group["account_id"],
                "status": "SUBMITTING",
                "started_at": iso_time(self.clock()),
                "stake": number_text(Decimal(stake)),
                "part_index": index,
                "part_total": len(parts),
                "quote": quote,
                "target": intent,
            }
            with self._lock, self.store.connect(write=True) as connection:
                current = self.store.get_group(connection, group_id)
                if (
                    not current
                    or current["status"] not in {"PREPARING", "SUBMITTING"}
                    or not self.store.owns_claim(connection, group_id, self._owner_id)
                ):
                    return
                attempt_id = connection.execute(
                    "INSERT INTO follow_attempts(group_id,status,payload) VALUES (?,?,?)",
                    (group_id, "SUBMITTING", dump(attempt)),
                ).lastrowid
                group.update(
                    status="SUBMITTING",
                    odds=str(quote["odds"]),
                    message=(
                        f"分单 {index + 1}/{len(parts)} 变盘未复核提交中"
                        if quote.get("verified") is False
                        else f"分单 {index + 1}/{len(parts)} 提交中"
                    ),
                    waiting_reason="等待平台回执",
                    updated_at=iso_time(self.clock()),
                    next_action_at=None,
                )
                group["quote"] = {
                    key: quote.get(key)
                    for key in (
                        "gid",
                        "master_gid",
                        "period",
                        "market_type",
                        "side",
                        "line",
                        "score",
                        "odds",
                        "odds_format",
                        "currency",
                        "obtained_at",
                        "wtype",
                        "rtype",
                        "chose_team",
                        "con",
                        "ratio",
                        "verified",
                        "verification",
                    )
                }
                self.store.save_group(connection, group)
                connection.execute(
                    "UPDATE follow_claims SET phase='SUBMITTING' WHERE group_id=? AND owner_id=?",
                    (group_id, self._owner_id),
                )
            try:
                result = self._executor().submit(quote, stake, group["account_id"])
            except Exception as exc:  # noqa: BLE001 - 单份提交异常按失败处理
                result = {
                    "status": "REJECTED",
                    "category": "SUBMIT_FAILED",
                    "error_code": "SUBMIT_FAILED",
                    "message": f"提交未完成（{type(exc).__name__}），按失败处理",
                    "retryable": False,
                    "request_sent": False,
                }
            # 1X020：这一份的金额减半再提交，两份都成交才算这份完成。
            extra_parts = []
            if result.get("action") == "split_half" and Decimal(stake) >= min_stake * 2:
                half = (Decimal(stake) / 2).quantize(Decimal("1"))
                rest = Decimal(stake) - half
                for sub_index, amount in enumerate((half, rest)):
                    sub_baseline = self._today_wager_baseline(group["account_id"])
                    try:
                        sub_quote = self._executor().prepare(mapping, intent, group["account_id"])
                        self._validate_quote(sub_quote, intent, group)
                        sub_result = self._executor().submit(sub_quote, amount, group["account_id"])
                    except Exception as exc:  # noqa: BLE001 - 拆分后的单份失败
                        sub_result = {
                            "status": "REJECTED",
                            "category": "SUBMIT_FAILED",
                            "error_code": "SUBMIT_FAILED",
                            "message": f"拆分提交失败（{type(exc).__name__}）",
                            "retryable": False,
                            "request_sent": False,
                        }
                    if sub_result.get("category") == "UNCERTAIN_RESULT" and sub_result.get(
                        "request_sent"
                    ):
                        sub_result = self._reconcile_uncertain(
                            group.get("account_id"),
                            sub_quote.get("gid"),
                            amount,
                            sub_result,
                            sub_baseline,
                        )
                    sub_status, sub_receipt, sub_stake, sub_odds = self._split_result(
                        sub_result, amount
                    )
                    extra_parts.append(
                        {
                            "id": uuid.uuid4().hex,
                            "group_id": group_id,
                            "part_index": len(results) + sub_index,
                            "parent_part_index": index,
                            "sub_index": sub_index,
                            "status": sub_status,
                            "stake": number_text(amount),
                            "account_id": group["account_id"],
                            "ticket_id": sub_receipt,
                            "accepted_stake": sub_stake,
                            "accepted_odds": sub_odds,
                            "message": str(sub_result.get("message") or "")[:180],
                            "updated_at": iso_time(self.clock()),
                        }
                    )
                    with self.store.connect(write=True) as connection:
                        self.store.save_part(connection, extra_parts[-1])
                results.extend(extra_parts)
                successful_subparts = [
                    item for item in extra_parts if item["status"] == "SUCCEEDED"
                ]
                tickets.extend(
                    item["ticket_id"] for item in successful_subparts if item["ticket_id"]
                )
                accepted_total += sum(
                    (
                        Decimal(item["accepted_stake"] or item["stake"])
                        for item in successful_subparts
                    ),
                    Decimal("0"),
                )
                accepted_odds = next(
                    (
                        item["accepted_odds"]
                        for item in successful_subparts
                        if item["accepted_odds"]
                    ),
                    accepted_odds,
                )
                if all(item["status"] == "SUCCEEDED" for item in extra_parts):
                    success += 1
                if index < len(parts) - 1 and delay > 0:
                    self._stop.wait(delay)
                continue
            if result.get("category") == "UNCERTAIN_RESULT" and result.get("request_sent"):
                result = self._reconcile_uncertain(
                    group.get("account_id"), quote.get("gid"), stake, result, wager_baseline
                )
            status, receipt, accepted_stake, part_odds = self._split_result(result, stake)
            part = {
                "id": uuid.uuid4().hex,
                "group_id": group_id,
                "part_index": len(results),
                "parent_part_index": index,
                "status": status,
                "stake": number_text(Decimal(stake)),
                "account_id": group["account_id"],
                "ticket_id": receipt,
                "accepted_stake": accepted_stake,
                "accepted_odds": part_odds,
                "message": str(result.get("message") or "")[:180],
                "updated_at": iso_time(self.clock()),
            }
            with self.store.connect(write=True) as connection:
                self.store.save_part(connection, part)
                connection.execute(
                    "UPDATE follow_attempts SET status=?,payload=? WHERE id=?",
                    (status, dump({**attempt, "result": result}), attempt_id),
                )
            results.append(part)
            if status == "SUCCEEDED" and receipt:
                success += 1
                tickets.append(receipt)
                accepted_total += Decimal(accepted_stake or stake)
                accepted_odds = part_odds or accepted_odds
            if index < len(parts) - 1 and delay > 0:
                self._stop.wait(delay)

        total = len(parts)
        if success == total:
            status, message = "SUCCEEDED", f"分单全部成功（{success}/{total}）"
        elif accepted_total > 0:
            status = "PARTIAL"
            message = f"分单部分成功（{success}/{total}）"
            if aborted:
                message = f"{message}：{aborted}"
        else:
            status = "REJECTED"
            message = f"分单全部失败（0/{total}）"
            if aborted:
                message = f"{message}：{aborted}"
        with self._lock, self.store.connect(write=True) as connection:
            current = self.store.get_group(connection, group_id)
            if current is None:
                return
            current.update(
                status=status,
                receipt_id=tickets[0] if tickets else None,
                receipt_source="platform" if tickets else None,
                accepted_stake=number_text(accepted_total) if accepted_total > 0 else None,
                accepted_odds=accepted_odds,
                message=message,
                split_total=total,
                split_done=success,
                split_tickets=tickets,
                split_parts=results,
                waiting_reason="",
                next_action_at=None,
                updated_at=iso_time(self.clock()),
            )
            self.store.save_group(connection, current)

    def _today_wager_baseline(self, account_id):
        reader = getattr(self._executor(), "today_wager_ids", None)
        if not callable(reader):
            return None
        try:
            tickets = reader(account_id)
            return set(tickets) if isinstance(tickets, (set, list, tuple)) else None
        except Exception:
            # 基线不可用仍可正常提交，但中断后不得认领无法确定归属的旧票。
            return None

    def _reconcile_uncertain(self, account_id, gid, stake, result, baseline=None):
        """提交响应中断时，用皇冠今日注单核对是否真的下出去了。"""
        finder = getattr(self._executor(), "find_today_wager", None)
        if not callable(finder) or not gid or baseline is None:
            return {
                **result,
                "message": "提交中断，缺少发送前注单基线，请到平台人工核对",
            }
        try:
            wager = finder(account_id, gid, stake, excluded_tickets=baseline)
        except Exception:  # noqa: BLE001 - 核对失败不能当成未下注
            return {
                **result,
                "status": "REJECTED",
                "message": "提交中断，且核对皇冠今日注单失败，请到平台人工核对",
                "category": "UNCERTAIN_RESULT",
            }
        if not wager:
            return {
                **result,
                "status": "REJECTED",
                "message": "提交中断，今日注单未找到该笔的唯一新票，请到平台人工核对",
                "category": "UNCERTAIN_RESULT",
            }
        ticket = str(wager.get("w_id") or "").strip()
        if not ticket:
            return result
        with self.store.connect() as connection:
            used = connection.execute(
                "SELECT 1 FROM follow_groups WHERE account_id=? AND receipt_id=? "
                "UNION ALL SELECT 1 FROM follow_parts WHERE account_id=? AND ticket_id=? LIMIT 1",
                (account_id, ticket, account_id, ticket),
            ).fetchone()
        if used:
            return {
                **result,
                "status": "REJECTED",
                "message": f"提交中断，皇冠今日注单 {ticket} 已被其他订单记录，请人工核对",
                "category": "UNCERTAIN_RESULT",
            }
        return {
            "status": "SUCCEEDED",
            "receipt_id": ticket,
            "accepted_stake": str(wager.get("gold") or ""),
            "accepted_odds": str(wager.get("ioratio") or ""),
            "message": "提交中断，已从皇冠今日注单核对到票号",
            "category": "RECONCILED",
            "request_sent": True,
            "retryable": False,
        }

    def _prepare_failed(self, group, error):
        now = self.clock()
        message = (
            str(error)[:180]
            if isinstance(error, ValueError)
            else "皇冠盘口查询暂不可用，请检查账号会话"
        )
        category = str(getattr(error, "category", "PREPARE"))[:60]
        retryable = getattr(error, "retryable", True) is True
        count = group.get("retry_count", 0) + 1
        # 暂缺盘口由源单期限约束；传输等连续异常另设次数上限。
        exhausted = (
            category
            in {
                "TRANSPORT",
                "NETWORK",
                "PREPARE",
                "PREPARE_TIMEOUT",
                "PREPARE_UNAVAILABLE",
                "RESPONSE_INVALID",
                "ACCOUNT_SESSION",
                "MARKET_AMBIGUOUS",
            }
            and count >= 6
        )
        # 盘口出现多条同线属于瞬时抖动，1.5 秒后重查通常就唯一了。
        delay = 1.5 if category == "MARKET_AMBIGUOUS" else min(15, 3 * 2 ** min(count - 1, 3))
        if category == "VARIABLE_STANDARD" and group.get("account_id"):
            tried = set(group.get("tried_accounts") or [])
            tried.add(str(group["account_id"]))
            available = set(self._available_account_ids())
            if available and tried >= available:
                tried = set()
            group["tried_accounts"] = sorted(tried)
            group["account_id"] = ""
        group.update(
            error_category=category,
            waiting_reason=message,
            retry_count=count,
            next_check_at=now + delay,
            next_action_at=iso_time(now + delay),
        )
        if category == "SCORE_CHANGED":
            self._set_status(group, "REJECTED", "比分已变化，停止跟单")
            return
        if now * 1000 >= group["deadline_ms"]:
            self._set_status(group, "EXPIRED", "盘口核验时原单已过期")
        elif not retryable or exhausted:
            self._set_status(
                group, "REJECTED", message if not exhausted else "查盘失败次数已达上限：" + message
            )
        else:
            self._set_status(group, "WAIT_MARKET", message)

    def _validate_quote(self, quote, intent, group):
        if not isinstance(quote, dict) or not quote.get("gid") or not quote.get("master_gid"):
            raise ValueError("皇冠盘口身份不完整")
        if quote.get("account_id") != group["account_id"]:
            raise ValueError("皇冠报价账号与处理账号不一致")
        for key in ("period", "market_type", "side"):
            if quote.get(key) != intent[key]:
                raise ValueError("皇冠盘口时段、玩法或方向不一致")
        if decimal_value(quote.get("line"), "皇冠盘口线") != Decimal(intent["line"]):
            raise ValueError("皇冠盘口线与目标不一致")
        if quote.get("currency") != "CNY":
            raise ValueError("目标皇冠账号须为人民币账号，当前报价币种不是 CNY")
        if quote.get("odds_format") != "HK":
            raise ValueError("皇冠水位制式不明确")
        odds = decimal_value(quote.get("odds"), "皇冠水位")
        if odds <= 0:
            raise ValueError("皇冠水位无效")
        for key, lower in (("min_odds", True), ("max_odds", False)):
            bound = group["rule"].get(key)
            if bound is not None and (
                (lower and odds < Decimal(bound)) or (not lower and odds > Decimal(bound))
            ):
                raise ValueError("皇冠水位超出规则范围")
        try:
            fetched = float(quote["obtained_at"])
        except (ValueError, TypeError, KeyError):
            raise ValueError("皇冠报价缺少有效时间") from None
        age = self.clock() - fetched
        if not 0 <= age <= 5:
            raise ValueError("皇冠报价已过期或时间无效")
        stake = Decimal(group["target_stake"])
        if quote.get("min_stake") is not None and stake < decimal_value(
            quote["min_stake"], "最低金额"
        ):
            raise ValueError("目标金额低于当前盘口最低金额")
        if quote.get("max_stake") is not None and stake > decimal_value(
            quote["max_stake"], "最高金额"
        ):
            raise ValueError("目标金额超过当前盘口最高金额")

    def _finish(self, group, attempt_id, attempt, result, *, source="submission"):
        result = result if isinstance(result, dict) else {}
        status = result.get("status")
        receipt = str(result.get("receipt_id") or "").strip()
        if status not in {"SUCCEEDED", "REJECTED"} or (status == "SUCCEEDED" and not receipt):
            status = "REJECTED"
        if receipt and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", receipt):
            status, receipt = "REJECTED", ""
        accepted = {}
        for key in ("accepted_stake", "accepted_odds"):
            try:
                value = decimal_value(result.get(key), "回执数值")
                accepted[key] = number_text(value) if value > 0 else None
            except ValueError:
                accepted[key] = None
        with self.store.connect(write=True) as connection:
            current = self.store.get_group(connection, group["id"])
            stored_attempt = connection.execute(
                "SELECT status,payload FROM follow_attempts WHERE id=? AND group_id=?",
                (attempt_id, group["id"]),
            ).fetchone()
            # 中断恢复会先记失败；同一次请求的真实成功回执仍可把结果纠正回来。
            late_success = status == "SUCCEEDED" and bool(receipt) and source == "submission"
            allowed = {"SUBMITTING", "UNKNOWN"} | ({"REJECTED"} if late_success else set())
            if (
                current is None
                or current["status"] not in allowed
                or stored_attempt is None
                or stored_attempt["status"] not in allowed
                or connection.execute(
                    "SELECT MAX(id) FROM follow_attempts WHERE group_id=?", (group["id"],)
                ).fetchone()[0]
                != attempt_id
            ):
                return
            # 历史记录里可能还留着 UNKNOWN；新的结果一律写成成功或失败。
            group = current
            attempt = json.loads(stored_attempt["payload"])
            if (
                status == "SUCCEEDED"
                and connection.execute(
                    "SELECT 1 FROM follow_groups WHERE account_id=? AND receipt_id=? AND id<>?",
                    (group["account_id"], receipt, group["id"]),
                ).fetchone()
            ):
                status, receipt = "REJECTED", ""
                result = {"message": "该平台回执已关联另一订单组，未重复记账"}
            message = {
                "SUCCEEDED": "平台已确认成功",
                "REJECTED": "平台未确认成功",
            }[status]
            # 适配器只返回脱敏业务错误；不持久化原始 HTTP 响应或会话。
            if result.get("message"):
                message = str(result["message"])[:180]
            if status != "SUCCEEDED":
                accepted = {"accepted_stake": None, "accepted_odds": None}
            group.update(
                status=status,
                receipt_id=receipt if status == "SUCCEEDED" else None,
                message=message,
                updated_at=iso_time(self.clock()),
                **accepted,
            )
            category = str(result.get("category") or "")[:60]
            error_code = str(result.get("error_code") or "")[:60]
            group.update(error_category=category, waiting_reason="", next_action_at=None)
            if status == "SUCCEEDED":
                group["receipt_source"] = "platform"
            if status != "SUCCEEDED":
                group.update(accepted_stake=None, accepted_odds=None)
            # 平台明确拒绝，或适配器证明写请求尚未发送，才允许重新查盘。
            # 回执事件：把皇冠返回的码和中文一起写进时间线。
            if status == "SUCCEEDED" and receipt:
                group["message"] = f"皇冠返回成功：票号 {receipt}"
                group["history_kind"] = "CROWN"
                group["error_code"] = "560"
            elif error_code:
                group["message"] = f"皇冠返回 {error_code}：{message}"
                group["history_kind"] = "CROWN"
                group["error_code"] = error_code
            self._apply_error_policy(group, result, connection)
            # 1X020 减半后还要补提交剩下的一半，两笔都成交才算完成。
            if status == "SUCCEEDED":
                try:
                    remaining = Decimal(str(group.get("split_remaining") or "0"))
                except Exception:
                    remaining = Decimal("0")
                if remaining > 0:
                    receipts = list(group.get("receipts") or [])
                    if receipt:
                        receipts.append(receipt)
                    group.update(
                        receipts=receipts,
                        receipt_id=receipts[0] if receipts else group.get("receipt_id"),
                        split_remaining="0",
                        target_stake=number_text(remaining),
                        status="READY",
                        next_check_at=self.clock() + 0.2,
                        next_action_at=iso_time(self.clock() + 0.2),
                        message=(
                            f"已成交 {len(receipts)} 笔，继续提交剩余 {number_text(remaining)}"
                        ),
                    )
            self.store.save_group(connection, group)
            attempt.update(
                status=status,
                receipt_id=group["receipt_id"],
                finished_at=group["updated_at"],
                message=message,
                **accepted,
                category=category,
                error_code=error_code,
                retryable=result.get("retryable") is True,
                request_sent=result.get("request_sent"),
            )
            connection.execute(
                "UPDATE follow_attempts SET status=?,payload=? WHERE id=?",
                (status, dump(attempt), attempt_id),
            )
