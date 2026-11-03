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
from decimal import Decimal
from pathlib import Path

from .mapping import resolve_fixture
from .rules import group_stake, target_intent, validate_rule
from .source import decimal_value, number_text, source_currency, source_intent
from .store import ACTIVE, FollowStore, dump, iso_time


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

    def _migrate_fixed_account_rule(self):
        with self.store.connect(write=True) as connection:
            rule = self.store.rule(connection)
            if "account_id" in rule:
                rule.pop("account_id", None)
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

                self._dispatcher = BoundedDispatcher(max_workers=2, on_done=self._wake.set)
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

    def snapshot(self, status="SUCCEEDED", search="", limit=50, offset=0):
        result = self.store.snapshot(status, search, limit, offset)
        result["service"]["running"] = bool(self._thread and self._thread.is_alive())
        return result

    def available_account_ids(self):
        return self._available_account_ids()

    def detail(self, group_id):
        return self.store.detail(str(group_id))

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
                    "正在接收新单，20 秒同条件合组" if rule["enabled"] else "跟单已暂停",
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
                "SELECT o.*,e.event_id FROM partner_order_events e LEFT JOIN partner_orders o "
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
            if not self._stop.is_set():
                self._schedule_reconciliations(background)
            with self._lock:
                with self.store.connect() as connection:
                    rule = self.store.rule(connection)
                    cursor = self.store.meta(connection, "cursor")
                if not rule["enabled"] or self._stop.is_set():
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
                events, next_cursor, gap = self._events(cursor)
                now = self.clock()
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
                    for row in sorted(
                        events, key=lambda row: (row.get("created_ms") or 0, row["event_id"])
                    ):
                        self._ingest(connection, row, rule, now)
                    self.store.set_meta(connection, "cursor", next_cursor)
                    groups = self.store.active_groups(connection)
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
        target = target_intent(intent, rule)
        return {
            "id": uuid.uuid4().hex,
            "base_key": base_key,
            "rule": dict(rule),
            "rule_version": rule["version"],
            "intent": dict(intent),
            "mapping": None,
            "start_ms": start_ms,
            "close_ms": start_ms + 20_000,
            "deadline_ms": start_ms + rule["max_age_seconds"] * 1000,
            "status": "GROUPING",
            "created_at": iso_time(now),
            "updated_at": iso_time(now),
            "source_created_at": iso_time(start_ms / 1000),
            "window_ends_at": iso_time((start_ms + 20_000) / 1000),
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
            "message": "正在收集 20 秒内的同条件订单",
            "prepare_count": 0,
            "retry_count": 0,
            "reject_count": 0,
            "reconcile_count": 0,
            "next_action_at": None,
            "waiting_reason": "收集同条件订单",
            "error_category": "",
        }

    def _skip(self, connection, row, rule, now, message, intent=None):
        if intent is None:
            # 展示不可解析订单时仅保留现有业务列；绝不返回 raw_json。
            created_ms = row.get("created_ms")
            if not isinstance(created_ms, int) or not 0 < created_ms <= 32_503_680_000_000:
                created_ms = int(now * 1000)
            intent = {
                "source": str(row.get("source") or "ps38ag"),
                "member_identity": str(row.get("member_identity") or ""),
                "order_id": str(row.get("order_id") or ""),
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
                "stake": "0",
                "content_hash": str(row.get("content_hash") or ""),
            }
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

    def _ingest(self, connection, row, rule, now):
        if not row.get("source") or not row.get("order_id") or not row.get("member_identity"):
            raise ValueError("新单事件对应的原始订单不存在")
        if self.store.contains_member(connection, row):
            return
        try:
            intent = source_intent(row)
        except ValueError as exc:
            self._skip(connection, row, rule, now, str(exc))
            return
        if rule["member_codes"] and intent["member_code"] not in rule["member_codes"]:
            self._skip(connection, row, rule, now, "会员不在当前规则范围内", intent)
            return
        age = now * 1000 - intent["created_ms"]
        if age < -5000 or age >= rule["max_age_seconds"] * 1000:
            self._skip(connection, row, rule, now, "原单时间异常或已超过最长处理时效", intent)
            return
        key = self._base_key(intent, rule)
        previous = self.store.groups_for_key(connection, key)
        group = next(
            (
                item
                for item in previous
                if item["start_ms"] <= intent["created_ms"] <= item["close_ms"]
            ),
            None,
        )
        if group and group["status"] not in {"GROUPING", "WAIT_MAPPING"}:
            self._skip(connection, row, rule, now, "迟到订单所属窗口已冻结，需核对", intent)
            return
        if group is None and previous and intent["created_ms"] < previous[0]["start_ms"]:
            self._skip(
                connection, row, rule, now, "源单时间早于已建立窗口，按迟到订单待核对", intent
            )
            return
        group = group or self._new_group(intent, rule, now, key)
        group["source_stake"] = number_text(
            Decimal(group["source_stake"]) + Decimal(intent["stake"])
        )
        group["order_count"] += 1
        group["updated_at"] = iso_time(now)
        self.store.save_group(connection, group)
        self.store.add_member(connection, group["id"], intent)
        group["member_count"] = len(
            {
                member["member_identity"]
                for member in self.store.get_members(connection, group["id"])
            }
        )
        self.store.save_group(connection, group)

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
                current = source_intent(dict(row))
                if current["content_hash"] != member["content_hash"]:
                    raise ValueError("组内原单内容已变化，需重新核对")
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
            busy = {
                str(row[0])
                for row in connection.execute("SELECT account_id FROM follow_claims").fetchall()
            }
            busy.update(
                str(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT account_id FROM follow_groups WHERE status='UNKNOWN' "
                    "AND account_id<>''"
                ).fetchall()
            )
            busy.update(
                str(row[0])
                for row in connection.execute(
                    "SELECT account_id FROM follow_groups "
                    "WHERE id<>? AND status IN ('READY','PREPARING','SUBMITTING') "
                    "AND account_id<>''",
                    (group_id,),
                ).fetchall()
            )
            if background and self._dispatcher:
                busy.update(self._dispatcher.active_accounts())
            free = [account_id for account_id in accounts if account_id not in busy]
            if not free:
                group.update(
                    account_id="",
                    status="WAIT_ACCOUNT",
                    next_check_at=now + 1,
                    next_action_at=iso_time(now + 1),
                    waiting_reason="可用皇冠会员正在执行或有未知结果待核对",
                    message="等待皇冠会员空闲",
                    error_category="ACCOUNT_BUSY",
                    updated_at=iso_time(now),
                )
                self.store.save_group(connection, group)
                return None
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
            selected = next(
                (account_id for account_id in candidates if account_id not in used), None
            )
            if selected is None:
                selected = candidates[0]
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
            if now * 1000 <= group["close_ms"]:
                self._set_status(group, "GROUPING", "正在收集 20 秒内的同条件订单")
                return
            try:
                self._validate_members(members)
                group["target_stake"] = group_stake(group["source_stake"], group["rule"])
            except ValueError as exc:
                self._set_status(group, "SKIPPED", str(exc))
                return
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
                if connection.execute(
                    "SELECT 1 FROM follow_groups WHERE account_id=? AND status='UNKNOWN' LIMIT 1",
                    (group["account_id"],),
                ).fetchone():
                    group.update(
                        next_check_at=self.clock() + 1,
                        next_action_at=iso_time(self.clock() + 1),
                        waiting_reason="该账号存在待核对请求，先确认原交易结果",
                        message="等待该账号的未知结果核对完成",
                        error_category="ACCOUNT_UNCERTAIN",
                        updated_at=iso_time(self.clock()),
                    )
                    self.store.save_group(connection, group)
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
        try:
            try:
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
                        message="请求处理中，等待平台回执",
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
            # 先持久化在途尝试，再发一次写请求；任何不明响应只能进入核对流程。
            try:
                result = self._executor().submit(quote, group["target_stake"], group["account_id"])
            except Exception:
                result = {
                    "status": "UNKNOWN",
                    "category": "TRANSPORT_UNKNOWN",
                    "message": "请求结果不明确，需核对平台回执",
                }
            self._finish(group, attempt_id, attempt, result)
        finally:
            with self.store.connect(write=True) as connection:
                self.store.release_claim(connection, group_id, self._owner_id)
            self._wake.set()

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
            }
            and count >= 6
        )
        delay = min(15, 3 * 2 ** min(count - 1, 3))
        group.update(
            error_category=category,
            waiting_reason=message,
            retry_count=count,
            next_check_at=now + delay,
            next_action_at=iso_time(now + delay),
        )
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
        if status not in {"SUCCEEDED", "REJECTED", "UNKNOWN"} or (
            status == "SUCCEEDED" and not receipt
        ):
            status = "UNKNOWN"
        if receipt and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", receipt):
            status, receipt = "UNKNOWN", ""
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
            if (
                current is None
                or current["status"] not in {"SUBMITTING", "UNKNOWN"}
                or stored_attempt is None
                or stored_attempt["status"] not in {"SUBMITTING", "UNKNOWN"}
                or connection.execute(
                    "SELECT MAX(id) FROM follow_attempts WHERE group_id=?", (group["id"],)
                ).fetchone()[0]
                != attempt_id
            ):
                return
            # 重启恢复可以先标 UNKNOWN；仍允许原请求的明确回执完成这个尝试。
            group = current
            attempt = json.loads(stored_attempt["payload"])
            if (
                status == "SUCCEEDED"
                and connection.execute(
                    "SELECT 1 FROM follow_groups WHERE account_id=? AND receipt_id=? AND id<>?",
                    (group["account_id"], receipt, group["id"]),
                ).fetchone()
            ):
                status, receipt = "UNKNOWN", ""
                result = {"message": "该平台回执已关联另一订单组，需核对"}
            message = {
                "SUCCEEDED": "平台已确认成功",
                "REJECTED": "平台已明确拒绝",
                "UNKNOWN": "结果不明确，需核对平台回执",
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
                group["receipt_source"] = (
                    "platform_query" if source == "reconciliation" else "platform"
                )
            if status != "SUCCEEDED":
                group.update(accepted_stake=None, accepted_odds=None)
            if status == "UNKNOWN":
                group.setdefault("reconcile_deadline", self.clock() + 120)
                group.setdefault("reconcile_next_at", self.clock() + 2)
                group["next_action_at"] = iso_time(group["reconcile_next_at"])
                group["waiting_reason"] = "等待只读核对平台注单，禁止重复提交"
            # 平台明确拒绝，或适配器证明写请求尚未发送，才允许重新查盘。
            known_rejection = error_code in {"1X001", "1X003", "1X015", "1X016", "doubleLogin"}
            proven_unsent = result.get("request_sent") is False and error_code in {
                "QUOTE_EXPIRED",
                "ACCOUNT_SESSION",
                "PREPARE_TIMEOUT",
            }
            retryable = (
                status == "REJECTED"
                and not receipt
                and result.get("retryable") is True
                and (known_rejection or proven_unsent)
            )
            if status == "REJECTED":
                group["reject_count"] = group.get("reject_count", 0) + 1
            if (
                retryable
                and group["reject_count"] < 3
                and self.store.rule(connection)["enabled"]
                and not self._stop.is_set()
                and self.clock() * 1000 < group["deadline_ms"]
            ):
                next_check = min(
                    self.clock() + 2 ** group["reject_count"], group["deadline_ms"] / 1000
                )
                group.update(
                    status="RETRY_WAIT",
                    retry_count=group.get("retry_count", 0) + 1,
                    next_check_at=next_check,
                    next_action_at=iso_time(next_check),
                    waiting_reason=message,
                    message="明确拒绝，等待重新查盘：" + message,
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
                retryable=retryable,
                request_sent=(
                    attempt.get("request_sent")
                    if source == "reconciliation"
                    else result.get("request_sent")
                ),
            )
            connection.execute(
                "UPDATE follow_attempts SET status=?,payload=? WHERE id=?",
                (status, dump(attempt), attempt_id),
            )

    def _schedule_reconciliations(self, background):
        with self.store.connect() as connection:
            groups = self.store.unknown_groups(connection)
        if not groups:
            return
        if not callable(getattr(self._executor(), "reconcile", None)):
            return
        now = self.clock()
        due_groups = [
            group
            for group in groups
            if group.get("reconcile_next_at") is not None and now >= group["reconcile_next_at"]
        ]
        for group in due_groups[:100]:
            if self._stop.is_set():
                break
            manual = group.get("manual_reconcile_requested", False)
            due = group.get("reconcile_next_at")
            if due is None or now < due:
                continue
            if not manual and (
                group.get("reconcile_count", 0) >= 5 or now >= group.get("reconcile_deadline", 0)
            ):
                with self.store.connect(write=True) as connection:
                    current = self.store.get_group(connection, group["id"])
                    if (
                        current
                        and current["status"] == "UNKNOWN"
                        and not current.get("manual_reconcile_requested", False)
                        and current.get("reconcile_next_at") is not None
                        and now >= current["reconcile_next_at"]
                        and (
                            current.get("reconcile_count", 0) >= 5
                            or now >= current.get("reconcile_deadline", 0)
                        )
                        and not connection.execute(
                            "SELECT 1 FROM follow_claims WHERE group_id=?", (group["id"],)
                        ).fetchone()
                    ):
                        current.update(
                            reconcile_next_at=None,
                            next_action_at=None,
                            waiting_reason="自动核对已结束，请到平台核对后登记",
                            message="自动核对未能唯一确认结果，保留待核对",
                            updated_at=iso_time(now),
                        )
                        self.store.save_group(connection, current)
                continue
            if background:
                self._dispatch(
                    "reconcile:" + group["id"],
                    group["account_id"],
                    self._reconcile_group,
                    group["id"],
                )
            else:
                self._reconcile_group(group["id"])

    def request_reconcile(self, group_id):
        """只安排一次只读核对，不接受前端提供的平台结果。"""
        if self._stop.is_set():
            raise ValueError("跟单服务已停止，请重新启动服务后核对")
        if not callable(getattr(self._executor(), "reconcile", None)):
            raise ValueError("当前平台适配器尚不支持自动核对，请人工核对后登记")
        with self._lock, self.store.connect(write=True) as connection:
            group = self.store.get_group(connection, str(group_id))
            if group is None:
                raise KeyError("跟单记录不存在")
            if group["status"] != "UNKNOWN":
                raise ValueError("只有待核对记录可以查询平台结果")
            if connection.execute(
                "SELECT 1 FROM follow_claims WHERE group_id=?", (group["id"],)
            ).fetchone():
                raise ValueError("该任务仍在执行或核对中，请等待结果")
            group.update(
                manual_reconcile_requested=True,
                reconcile_next_at=self.clock(),
                next_action_at=iso_time(self.clock()),
                message="已安排只读核对，禁止重复提交",
                updated_at=iso_time(self.clock()),
            )
            self.store.save_group(connection, group)
        self._wake.set()
        return {"ok": True, "message": "已安排只读核对，不会重新提交"}

    def _reconcile_group(self, group_id):
        with self._lock, self.store.connect(write=True) as connection:
            group = self.store.get_group(connection, group_id)
            if not group or group["status"] != "UNKNOWN" or self._stop.is_set():
                return
            now = self.clock()
            manual = group.get("manual_reconcile_requested", False)
            if (
                group.get("reconcile_next_at") is None
                or now < group["reconcile_next_at"]
                or (
                    not manual
                    and (
                        group.get("reconcile_count", 0) >= 5
                        or now >= group.get("reconcile_deadline", 0)
                    )
                )
            ):
                return
            if not self.store.claim(
                connection,
                group,
                self._owner_id,
                os.getpid(),
                "RECONCILING_REQUESTED" if manual else "RECONCILING",
                now,
            ):
                return
            row = connection.execute(
                "SELECT id,payload FROM follow_attempts WHERE group_id=? ORDER BY id DESC LIMIT 1",
                (group_id,),
            ).fetchone()
            attempt = {**json.loads(row["payload"]), "id": row["id"]} if row else None
            group.update(
                reconcile_count=group.get("reconcile_count", 0) + 1,
                manual_reconcile_requested=False,
                reconcile_next_at=None,
                next_action_at=None,
                waiting_reason="正在只读查询原账号平台注单",
                updated_at=iso_time(now),
            )
            self.store.save_group(connection, group)
        method = "requested" if manual else "automatic"
        try:
            if attempt:
                attempt.setdefault("target", target_intent(group["intent"], group["rule"]))
                attempt.setdefault("submitted_quote", group.get("quote", {}))
                try:
                    result = self._executor().reconcile(attempt, group["account_id"])
                except Exception:
                    result = {
                        "status": "UNKNOWN",
                        "category": "RECONCILE_UNAVAILABLE",
                        "message": "平台查询暂不可用，仍需核对",
                    }
            else:
                result = {
                    "status": "UNKNOWN",
                    "category": "RECONCILE_MISSING_ATTEMPT",
                    "message": "缺少原提交尝试，请人工核对后登记",
                }
            if not isinstance(result, dict):
                result = {"status": "UNKNOWN", "message": "平台查询结果无法识别"}
            if result.get("status") == "SUCCEEDED" and attempt:
                self._finish(group, attempt["id"], attempt, result, source="reconciliation")
            with self.store.connect(write=True) as connection:
                current = self.store.get_group(connection, group_id)
                if current is None:
                    return
                if current["status"] == "UNKNOWN":
                    next_try = min(
                        self.clock() + min(30, 3 * 2 ** min(current["reconcile_count"] - 1, 4)),
                        current.get("reconcile_deadline", self.clock()),
                    )
                    retry = current["reconcile_count"] < 5 and next_try > self.clock()
                    current.update(
                        reconcile_next_at=next_try if retry else None,
                        next_action_at=iso_time(next_try) if retry else None,
                        waiting_reason="等待再次只读核对" if retry else "请到平台核对后登记",
                        message=str(result.get("message") or "平台未能唯一确认结果，保留待核对")[
                            :180
                        ],
                        error_category=str(result.get("category") or "RECONCILE_UNKNOWN")[:60],
                        updated_at=iso_time(self.clock()),
                    )
                    self.store.save_group(connection, current)
                self._record_reconciliation(
                    connection,
                    current,
                    method,
                    current["status"],
                    current.get("message", ""),
                    current.get("receipt_id"),
                )
        finally:
            with self.store.connect(write=True) as connection:
                self.store.release_claim(connection, group_id, self._owner_id)
            self._wake.set()

    def _record_reconciliation(
        self, connection, group, method, status, message, receipt=None, note=""
    ):
        connection.execute(
            "INSERT INTO follow_reconciliations(group_id,payload) VALUES (?,?)",
            (
                group["id"],
                dump(
                    {
                        "at": iso_time(self.clock()),
                        "method": method,
                        "status": status,
                        "message": message,
                        "receipt_id": receipt,
                        "note": note,
                    }
                ),
            ),
        )

    def resolve_unknown(self, group_id, payload):
        """人工核对登记保留单独审计来源，不伪装成平台即时回执。"""
        if not isinstance(payload, dict):
            raise ValueError("核对登记必须是对象")
        outcome = payload.get("outcome")
        note = str(payload.get("note") or "").strip()
        receipt = str(payload.get("receipt_id") or "").strip()
        if outcome not in {"SUCCEEDED", "REJECTED"}:
            raise ValueError("请选择已成功或已确认未成交")
        if not 3 <= len(note) <= 500:
            raise ValueError("请填写 3 至 500 字的核对依据")
        if outcome == "SUCCEEDED" and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", receipt):
            raise ValueError("确认成功必须填写有效的平台票号")
        accepted = {}
        for key in ("accepted_stake", "accepted_odds"):
            raw = payload.get(key)
            if raw in (None, ""):
                accepted[key] = None
            else:
                value = decimal_value(raw, "核对金额或水位")
                if value <= 0:
                    raise ValueError("核对金额和水位必须大于零")
                accepted[key] = number_text(value)
        if outcome == "REJECTED":
            accepted = {"accepted_stake": None, "accepted_odds": None}
        with self._lock, self.store.connect(write=True) as connection:
            group = self.store.get_group(connection, str(group_id))
            if group is None:
                raise KeyError("跟单记录不存在")
            if group["status"] != "UNKNOWN":
                raise ValueError("只有待核对记录可以登记结果")
            if connection.execute(
                "SELECT 1 FROM follow_claims WHERE group_id=?", (group["id"],)
            ).fetchone():
                raise ValueError("该任务仍在执行或核对中，请等待结果")
            if (
                outcome == "SUCCEEDED"
                and connection.execute(
                    "SELECT 1 FROM follow_groups WHERE account_id=? AND receipt_id=? AND id<>?",
                    (group["account_id"], receipt, group["id"]),
                ).fetchone()
            ):
                raise ValueError("该平台票号已关联其他订单组")
            message = "人工核对已成交" if outcome == "SUCCEEDED" else "人工核对未成交，此组不再处理"
            group.update(
                status=outcome,
                receipt_id=receipt if outcome == "SUCCEEDED" else None,
                receipt_source="manual",
                message=message,
                waiting_reason="",
                next_action_at=None,
                reconcile_next_at=None,
                updated_at=iso_time(self.clock()),
                **(
                    accepted
                    if outcome == "SUCCEEDED"
                    else {"accepted_stake": None, "accepted_odds": None}
                ),
            )
            self.store.save_group(connection, group)
            row = connection.execute(
                "SELECT id,payload FROM follow_attempts WHERE group_id=? ORDER BY id DESC LIMIT 1",
                (group["id"],),
            ).fetchone()
            if row:
                attempt = json.loads(row["payload"])
                attempt.update(
                    status=outcome,
                    receipt_id=group["receipt_id"],
                    message=message,
                    finished_at=group["updated_at"],
                    resolution="manual",
                    **accepted,
                )
                connection.execute(
                    "UPDATE follow_attempts SET status=?,payload=? WHERE id=?",
                    (outcome, dump(attempt), row["id"]),
                )
            self._record_reconciliation(
                connection, group, "manual", outcome, message, group["receipt_id"], note
            )
        return {"ok": True, "message": "人工核对结果已登记", "group_id": str(group_id)}
