"""跟单队列、恢复、分类重试和核对闭环；仅使用临时库与模拟执行器。"""

import copy
import json
import os
import threading
import time
import unittest
from unittest.mock import patch

from ps38app.follow.crown_markets import FollowPreparationError
from ps38app.follow.service import FollowService
from tests import test_follow_core as core


class QueueTests(unittest.TestCase):
    # 复用本地订单及映射样本，不继承原 TestCase，以免重复收集其测试。
    setUp = core.FollowServiceTests.setUp
    tearDown = core.FollowServiceTests.tearDown
    commit = core.FollowServiceTests.commit
    enable = core.FollowServiceTests.enable
    add = core.FollowServiceTests.add
    tick = core.FollowServiceTests.tick
    groups = core.FollowServiceTests.groups

    def stored_group(self, group_id):
        with self.service.store.connect() as connection:
            return self.service.store.get_group(connection, group_id)

    def wait_background(self, condition, service=None):
        service = service or self.service
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            service.run_once(background=True)
            if condition():
                return
            service._wake.wait(0.02)
            service._wake.clear()
        self.fail("模拟后台任务未在限定时间内完成")

    def call_without_network_wait(self, service=None):
        service = service or self.service
        finished = threading.Event()
        failures = []

        def invoke():
            try:
                service.run_once(background=True)
            except BaseException as exc:
                failures.append(exc)
            finally:
                finished.set()

        caller = threading.Thread(target=invoke, daemon=True)
        caller.start()
        self.assertTrue(finished.wait(1), "调度循环被执行中的网络请求阻塞")
        caller.join(timeout=1)
        if failures:
            raise failures[0]

    def block_prepare(self):
        entered, release = threading.Event(), threading.Event()
        original = self.executor.prepare

        def block(mapping, intent, account_id):
            quote = original(mapping, intent, account_id)
            entered.set()
            if not release.wait(4):
                raise TimeoutError("模拟准备请求未被释放")
            # 模拟网络返回时取得的新报价，而非阻塞前就生成的过期报价。
            quote["obtained_at"] = self.clock()
            return quote

        self.executor.prepare = block
        return entered, release

    def test_slow_prepare_does_not_block_ingestion_or_account_queue(self):
        entered, release = self.block_prepare()
        try:
            self.enable()
            self.add("first")
            self.tick()
            self.clock.now += 21
            self.call_without_network_wait()
            self.assertTrue(entered.wait(2))
            first = self.groups()[0]
            self.assertEqual(first["status"], "PREPARING")

            self.add("second")
            self.call_without_network_wait()
            groups = self.groups()
            second = next(group for group in groups if group["id"] != first["id"])
            self.assertEqual(second["status"], "GROUPING")
            self.clock.now += 21
            self.call_without_network_wait()
            self.assertEqual(self.service.detail(second["id"])["group"]["status"], "WAIT_ACCOUNT")
            self.assertEqual(len(self.executor.prepared), 1)
            with self.service.store.connect() as connection:
                claims = connection.execute("SELECT * FROM follow_claims").fetchall()
            self.assertEqual(len(claims), 1)
            self.assertEqual(claims[0]["group_id"], first["id"])

            self.executor.on_prepare = None
            release.set()
            self.wait_background(
                lambda: self.service.detail(first["id"])["group"]["status"] == "SUCCEEDED"
            )
            self.clock.now += 1
            self.wait_background(
                lambda: all(group["status"] == "SUCCEEDED" for group in self.groups())
            )
            self.assertEqual(len(self.executor.submitted), 2)
            self.assertEqual(len({group["receipt_id"] for group in self.groups()}), 2)
        finally:
            release.set()

    def test_second_instance_preserves_live_claim_and_cannot_reprepare(self):
        entered, release = self.block_prepare()
        other = None
        try:
            self.enable()
            self.add("first")
            self.tick()
            self.clock.now += 21
            self.call_without_network_wait()
            self.assertTrue(entered.wait(2))
            first_id = self.groups()[0]["id"]

            self.add("second")
            self.clock.now += 21
            self.call_without_network_wait()
            other_executor = core.Executor(self.clock)
            other = FollowService(self.config, other_executor, self.clock)
            self.assertEqual(other.detail(first_id)["group"]["status"], "PREPARING")
            self.call_without_network_wait(other)
            self.assertEqual(len(self.executor.prepared), 1)
            self.assertEqual(other_executor.prepared, [])
            self.assertEqual(other_executor.submitted, [])
            with other.store.connect() as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM follow_claims").fetchone()[0], 1
                )
            self.executor.on_prepare = None
            release.set()
            self.wait_background(
                lambda: self.service.detail(first_id)["group"]["status"] == "SUCCEEDED"
            )
        finally:
            release.set()
            if other:
                other.stop()

    def test_explicit_market_rejection_waits_and_survives_restart_with_frozen_rule(self):
        self.executor.result = {
            "status": "REJECTED",
            "retryable": True,
            "category": "MARKET",
            "error_code": "1X015",
            "message": "模拟封盘拒绝",
        }
        self.enable(amount_mode="ratio", amount_value="10", direction="forward")
        self.add(stake="609")
        self.tick(21)
        group = self.groups()[0]
        self.assertEqual(group["status"], "RETRY_WAIT")
        stored = self.stored_group(group["id"])
        retry_at = stored["next_check_at"]
        self.assertGreater(retry_at, self.clock())
        self.assertTrue(group["next_action_at"])
        self.assertEqual(len(self.executor.submitted), 1)

        self.service.stop()
        self.service = FollowService(self.config, self.executor, self.clock)
        self.enable(amount_mode="fixed", amount_value="99", direction="reverse")
        self.tick()
        self.assertEqual(len(self.executor.submitted), 1)
        self.executor.result = None
        self.clock.now = retry_at + 0.001
        self.tick()
        completed = self.service.detail(group["id"])
        self.assertEqual(completed["group"]["status"], "SUCCEEDED")
        self.assertEqual([item[1] for item in self.executor.submitted], ["60", "60"])
        self.assertEqual([item[0]["side"] for item in self.executor.submitted], ["HOME", "HOME"])
        self.assertEqual(
            [item["status"] for item in completed["attempts"]], ["REJECTED", "SUCCEEDED"]
        )
        self.assertTrue(completed["history"])
        self.tick(20)
        self.assertEqual(len(self.executor.submitted), 2)

    def test_explicit_market_rejection_stops_at_three_attempts(self):
        self.executor.result = {
            "status": "REJECTED",
            "retryable": True,
            "category": "MARKET",
            "error_code": "1X003",
        }
        self.enable()
        self.add()
        self.tick(21)
        group_id = self.groups()[0]["id"]
        for _ in range(2):
            stored = self.stored_group(group_id)
            self.assertEqual(stored["status"], "RETRY_WAIT")
            self.clock.now = stored["next_check_at"] + 0.001
            self.tick()
        self.assertEqual(self.service.detail(group_id)["group"]["status"], "REJECTED")
        self.assertEqual(len(self.executor.submitted), 3)
        self.tick(30)
        self.assertEqual(len(self.executor.submitted), 3)

    def test_unknown_and_nonwhitelisted_rejections_are_never_resubmitted(self):
        self.executor.result = {
            "status": "REJECTED",
            "retryable": True,
            "category": "MARKET",
            "error_code": "UNRECOGNIZED",
        }
        self.enable()
        self.add()
        self.tick(21)
        self.assertEqual(self.groups()[0]["status"], "REJECTED")
        self.tick(30)
        self.assertEqual(len(self.executor.submitted), 1)

    def test_prepare_failure_has_bounded_persistent_retry_without_submission(self):
        def unavailable():
            raise ValueError("模拟查盘暂不可用")

        self.executor.on_prepare = unavailable
        self.enable(max_age_seconds=600)
        self.add()
        self.tick(21)
        group_id = self.groups()[0]["id"]
        for _ in range(5):
            stored = self.stored_group(group_id)
            self.assertEqual(stored["status"], "WAIT_MARKET")
            self.clock.now = stored["next_check_at"] + 0.001
            self.tick()
        self.assertEqual(self.service.detail(group_id)["group"]["status"], "REJECTED")
        self.assertEqual(len(self.executor.prepared), 6)
        self.assertEqual(self.executor.submitted, [])

    def test_real_adapter_preparation_categories_share_six_attempt_limit(self):
        self.enable(max_age_seconds=600)
        for category in (
            "PREPARE_TIMEOUT",
            "PREPARE_UNAVAILABLE",
            "RESPONSE_INVALID",
            "ACCOUNT_SESSION",
        ):
            with self.subTest(category=category):
                before = len(self.executor.prepared)

                def unavailable():
                    raise FollowPreparationError("模拟适配器暂不可用", category, retryable=True)

                self.executor.on_prepare = unavailable
                self.clock.now += 1
                self.add("prepare-" + category)
                self.tick(21)
                group_id = self.groups()[0]["id"]
                for _ in range(5):
                    stored = self.stored_group(group_id)
                    self.assertEqual(stored["status"], "WAIT_MARKET")
                    self.clock.now = stored["next_check_at"] + 0.001
                    self.tick()
                detail = self.service.detail(group_id)
                self.assertEqual(detail["group"]["status"], "REJECTED")
                self.assertEqual(detail["group"]["error_category"], category)
                self.assertEqual(len(self.executor.prepared) - before, 6)
                self.assertEqual(detail["attempts"], [])
        self.assertEqual(self.executor.submitted, [])

    def test_session_and_local_rejections_retry_only_with_appropriate_send_evidence(self):
        self.enable()
        cases = [("doubleLogin", True, True)] + [
            (error_code, sent, sent is False)
            for error_code in ("QUOTE_EXPIRED", "ACCOUNT_SESSION", "PREPARE_TIMEOUT")
            for sent in (False, True, "missing")
        ]
        for index, (error_code, sent, retryable) in enumerate(cases):
            with self.subTest(error_code=error_code, request_sent=sent):
                result = {
                    "status": "REJECTED",
                    "category": "ACCOUNT_SESSION",
                    "error_code": error_code,
                    "retryable": True,
                }
                if sent != "missing":
                    result["request_sent"] = sent
                self.executor.result = result
                before = len(self.executor.submitted)
                self.clock.now += 1
                self.add("send-evidence-" + str(index))
                self.tick(21)
                group_id = self.groups()[0]["id"]
                group = self.stored_group(group_id)
                self.assertEqual(group["status"], "RETRY_WAIT" if retryable else "REJECTED")
                self.assertEqual(len(self.executor.submitted) - before, 1)
                if retryable:
                    self.executor.result = None
                    self.clock.now = group["next_check_at"] + 0.001
                    self.tick()
                    self.assertEqual(self.service.detail(group_id)["group"]["status"], "SUCCEEDED")
                    self.assertEqual(len(self.executor.submitted) - before, 2)
                else:
                    self.tick(5)
                    self.assertEqual(len(self.executor.submitted) - before, 1)

    def test_unknown_auto_reconciles_only_and_exhaustion_stays_unknown(self):
        calls = []

        def reconcile(attempt, account_id):
            calls.append((attempt, account_id))
            return {"status": "UNKNOWN", "message": "暂未找到唯一匹配票据"}

        self.executor.reconcile = reconcile
        self.executor.result = TimeoutError("模拟响应丢失")
        self.enable()
        self.add()
        self.tick(21)
        group_id = self.groups()[0]["id"]
        for seconds in (1, 10, 20, 30, 60, 120):
            self.tick(seconds)
            self.assertEqual(self.service.detail(group_id)["group"]["status"], "UNKNOWN")
            self.assertEqual(len(self.executor.submitted), 1)
        self.assertGreaterEqual(len(calls), 1)
        self.assertLessEqual(len(calls), 5)
        self.assertTrue(all(account_id == "crown-test" for _, account_id in calls))
        count = len(calls)
        self.tick(300)
        self.assertEqual(len(calls), count)
        self.assertTrue(self.service.detail(group_id)["reconciliations"])

    def test_manual_query_while_paused_can_confirm_unknown_without_new_submission(self):
        self.executor.result = TimeoutError("模拟响应丢失")
        self.enable()
        self.add()
        self.tick(21)
        group_id = self.groups()[0]["id"]
        self.service.pause()
        calls = []

        def reconcile(attempt, account_id):
            calls.append((attempt, account_id))
            return {
                "status": "SUCCEEDED",
                "receipt_id": "READONLY-CONFIRMED",
                "accepted_stake": "50",
            }

        self.executor.reconcile = reconcile
        self.service.request_reconcile(group_id)
        self.tick()
        detail = self.service.detail(group_id)
        self.assertEqual(detail["group"]["status"], "SUCCEEDED")
        self.assertEqual(detail["group"]["receipt_id"], "READONLY-CONFIRMED")
        self.assertEqual(detail["group"]["accepted_stake"], "50")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], "crown-test")
        self.assertEqual(len(self.executor.submitted), 1)
        self.assertTrue(detail["reconciliations"])

    def test_stale_exhausted_auto_snapshot_cannot_erase_new_manual_query(self):
        calls = []

        def reconcile(attempt, account_id):
            calls.append(account_id)
            return {"status": "UNKNOWN", "message": "模拟人工请求仍需核对"}

        self.executor.reconcile = reconcile
        self.executor.result = TimeoutError("模拟响应丢失")
        self.enable()
        self.add()
        self.tick(21)
        group_id = self.groups()[0]["id"]
        with self.service.store.connect(write=True) as connection:
            group = self.service.store.get_group(connection, group_id)
            group.update(
                reconcile_count=5,
                reconcile_deadline=self.clock() - 1,
                reconcile_next_at=self.clock(),
            )
            self.service.store.save_group(connection, group)

        original = self.service.store.unknown_groups

        def read_then_request(connection):
            snapshot = original(connection)
            # 模拟HTTP线程在调度器读取旧快照之后成功安排手动只读核对。
            self.service.request_reconcile(group_id)
            return snapshot

        with patch.object(self.service.store, "unknown_groups", side_effect=read_then_request):
            self.tick()
        queued = self.stored_group(group_id)
        self.assertTrue(queued["manual_reconcile_requested"])
        self.assertIsNotNone(queued["reconcile_next_at"])
        self.assertEqual(calls, [])
        self.tick()
        self.assertEqual(calls, ["crown-test"])
        self.assertEqual(self.service.detail(group_id)["group"]["status"], "UNKNOWN")
        self.assertEqual(len(self.executor.submitted), 1)

    def test_completed_unknown_queries_do_not_starve_later_manual_query(self):
        self.executor.result = TimeoutError("模拟响应丢失")
        self.enable()
        self.add()
        self.tick(21)
        group_id = self.groups()[0]["id"]
        with self.service.store.connect(write=True) as connection:
            original = self.service.store.get_group(connection, group_id)
            for index in range(100):
                older = copy.deepcopy(original)
                older.update(
                    id=f"exhausted-{index:03d}",
                    base_key=f"exhausted-key-{index:03d}",
                    start_ms=original["start_ms"] - 1000 - index,
                    reconcile_count=5,
                    reconcile_deadline=self.clock() - 1,
                    reconcile_next_at=None,
                    manual_reconcile_requested=False,
                    _revision=0,
                )
                self.service.store.save_group(connection, older)
        calls = []

        def reconcile(attempt, account_id):
            calls.append(account_id)
            return {"status": "SUCCEEDED", "receipt_id": "FAIR-MANUAL-QUERY"}

        self.executor.reconcile = reconcile
        self.service.request_reconcile(group_id)
        self.tick()
        detail = self.service.detail(group_id)
        self.assertEqual(detail["group"]["status"], "SUCCEEDED")
        self.assertEqual(detail["group"]["receipt_id"], "FAIR-MANUAL-QUERY")
        self.assertEqual(calls, ["crown-test"])
        self.assertEqual(len(self.executor.submitted), 1)

    def test_future_mapping_checks_do_not_starve_later_ready_group(self):
        self.enable(max_age_seconds=600)
        self.add()
        self.tick()
        group_id = self.groups()[0]["id"]
        self.clock.now += 21
        with self.service.store.connect(write=True) as connection:
            original = self.service.store.get_group(connection, group_id)
            original.update(status="READY", target_stake="50")
            self.service.store.save_group(connection, original)
            for index in range(100):
                older = copy.deepcopy(original)
                older.update(
                    id=f"future-mapping-{index:03d}",
                    base_key=f"future-mapping-key-{index:03d}",
                    start_ms=original["start_ms"] - 1000 - index,
                    status="WAIT_MAPPING",
                    next_check_at=self.clock() + 100,
                    _revision=0,
                )
                self.service.store.save_group(connection, older)
        self.tick()
        self.assertEqual(self.service.detail(group_id)["group"]["status"], "SUCCEEDED")
        self.assertEqual(len(self.executor.prepared), 1)
        self.assertEqual(len(self.executor.submitted), 1)
        with self.service.store.connect() as connection:
            waiting = connection.execute(
                "SELECT COUNT(*) FROM follow_groups WHERE status='WAIT_MAPPING'"
            ).fetchone()[0]
        self.assertEqual(waiting, 100)

    def test_manual_resolution_requires_note_and_receipt_and_does_not_replay(self):
        self.executor.result = TimeoutError("模拟响应丢失")
        self.enable()
        self.add()
        self.tick(21)
        group_id = self.groups()[0]["id"]
        for payload in (
            {"outcome": "SUCCEEDED", "receipt_id": "MANUAL-1"},
            {"outcome": "SUCCEEDED", "note": "平台核查"},
            {"outcome": "READY", "note": "不能重新排队"},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.service.resolve_unknown(group_id, payload)
        self.service.resolve_unknown(
            group_id,
            {"outcome": "SUCCEEDED", "receipt_id": "MANUAL-1", "note": "人工核对平台票号和金额"},
        )
        detail = self.service.detail(group_id)
        self.assertEqual(detail["group"]["status"], "SUCCEEDED")
        self.assertEqual(detail["group"]["receipt_id"], "MANUAL-1")
        self.assertTrue(detail["reconciliations"])
        self.assertIn("人工核对平台票号和金额", str(detail["reconciliations"]))
        with self.assertRaises(ValueError):
            self.service.resolve_unknown(group_id, {"outcome": "REJECTED", "note": "已处理"})
        self.tick(30)
        self.assertEqual(len(self.executor.submitted), 1)

    def test_dead_reconciliation_owner_recovers_schedule_without_resetting_budget(self):
        self.executor.result = TimeoutError("模拟响应丢失")
        self.enable()
        self.add()
        self.tick(21)
        group_id = self.groups()[0]["id"]
        with self.service.store.connect(write=True) as connection:
            group = self.service.store.get_group(connection, group_id)
            deadline = group["reconcile_deadline"]
            group.update(reconcile_count=2, reconcile_next_at=None, next_action_at=None)
            self.service.store.save_group(connection, group)
            self.assertTrue(
                self.service.store.claim(
                    connection,
                    group,
                    "dead-reconciliation-owner",
                    os.getpid(),
                    "RECONCILING",
                    self.clock(),
                )
            )
        self.service.stop()
        self.service = FollowService(self.config, self.executor, self.clock)
        restored = self.stored_group(group_id)
        self.assertEqual(restored["status"], "UNKNOWN")
        self.assertEqual(restored["reconcile_count"], 2)
        self.assertEqual(restored["reconcile_deadline"], deadline)
        self.assertIsNotNone(restored["reconcile_next_at"])
        calls = []

        def reconcile(attempt, account_id):
            calls.append(account_id)
            return {"status": "SUCCEEDED", "receipt_id": "RECOVERED-READONLY"}

        self.executor.reconcile = reconcile
        self.tick()
        self.assertEqual(self.service.detail(group_id)["group"]["status"], "SUCCEEDED")
        self.assertEqual(self.stored_group(group_id)["reconcile_count"], 3)
        self.assertEqual(calls, ["crown-test"])
        self.assertEqual(len(self.executor.submitted), 1)

    def test_unknown_account_blocks_later_group_until_original_result_is_resolved(self):
        self.executor.result = TimeoutError("模拟响应丢失")
        self.enable()
        self.add("unknown")
        self.tick(21)
        unknown_id = self.groups()[0]["id"]
        self.clock.now += 1
        self.add("queued")
        self.executor.result = None
        self.tick(21)
        queued = next(group for group in self.groups() if group["id"] != unknown_id)
        self.assertEqual(queued["status"], "WAIT_ACCOUNT")
        self.assertIn("核对", queued["message"] + queued.get("waiting_reason", ""))
        self.assertEqual(len(self.executor.submitted), 1)
        with self.service.store.connect() as connection:
            stored_account_id = connection.execute(
                "SELECT account_id FROM follow_groups WHERE id=?", (unknown_id,)
            ).fetchone()[0]
        self.assertEqual(stored_account_id, "crown-test")
        self.service.resolve_unknown(
            unknown_id,
            {
                "outcome": "REJECTED",
                "note": "人工核对原请求未成交",
                "accepted_stake": "50",
                "accepted_odds": "0.95",
            },
        )
        rejected = self.service.detail(unknown_id)
        for record in (rejected["group"], rejected["attempts"][0]):
            self.assertIsNone(record["accepted_stake"])
            self.assertIsNone(record["accepted_odds"])
        self.tick(1)
        self.assertEqual(self.service.detail(queued["id"])["group"]["status"], "SUCCEEDED")
        self.assertEqual(self.service.detail(unknown_id)["group"]["status"], "REJECTED")
        self.assertEqual(len(self.executor.submitted), 2)

    def test_active_readonly_reconciliation_blocks_manual_registration(self):
        self.executor.result = TimeoutError("模拟响应丢失")
        self.enable()
        self.add()
        self.tick(21)
        group_id = self.groups()[0]["id"]
        entered, release = threading.Event(), threading.Event()

        def reconcile(attempt, account_id):
            entered.set()
            if not release.wait(3):
                raise TimeoutError("测试核对请求未释放")
            return {"status": "UNKNOWN", "message": "模拟查询仍不明确"}

        self.executor.reconcile = reconcile
        try:
            self.service.request_reconcile(group_id)
            self.call_without_network_wait()
            self.assertTrue(entered.wait(2))
            with self.assertRaisesRegex(ValueError, "执行或核对"):
                self.service.resolve_unknown(
                    group_id,
                    {"outcome": "SUCCEEDED", "receipt_id": "MANUAL-RACE", "note": "平台人工核对"},
                )
            with self.assertRaisesRegex(ValueError, "执行或核对"):
                self.service.request_reconcile(group_id)
            release.set()
            self.wait_background(lambda: bool(self.service.detail(group_id)["reconciliations"]))
            self.assertEqual(self.service.detail(group_id)["group"]["status"], "UNKNOWN")
            self.assertEqual(len(self.executor.submitted), 1)
        finally:
            release.set()

    def test_late_original_receipt_cannot_overwrite_manual_resolution(self):
        self.executor.result = TimeoutError("模拟响应丢失")
        self.enable()
        self.add()
        self.tick(21)
        group_id = self.groups()[0]["id"]
        original_group = self.stored_group(group_id)
        with self.service.store.connect() as connection:
            row = connection.execute(
                "SELECT id,payload FROM follow_attempts WHERE group_id=?", (group_id,)
            ).fetchone()
            attempt_id, original_attempt = row["id"], json.loads(row["payload"])
        self.service.resolve_unknown(
            group_id,
            {"outcome": "SUCCEEDED", "receipt_id": "MANUAL-FINAL", "note": "人工核对票据归属"},
        )
        self.service._finish(
            original_group,
            attempt_id,
            original_attempt,
            {"status": "SUCCEEDED", "receipt_id": "LATE-RECEIPT", "accepted_stake": "99"},
        )
        detail = self.service.detail(group_id)
        self.assertEqual(detail["group"]["receipt_id"], "MANUAL-FINAL")
        self.assertEqual(detail["group"]["receipt_source"], "manual")
        self.assertEqual(detail["attempts"][0]["receipt_id"], "MANUAL-FINAL")
        self.assertEqual(len(self.executor.submitted), 1)

    def test_restart_after_stop_timeout_reaps_completed_worker_before_resuming(self):
        entered, release = self.block_prepare()
        try:
            self.enable()
            self.add()
            self.tick()
            self.clock.now += 21
            self.call_without_network_wait()
            self.assertTrue(entered.wait(2))
            group_id = self.groups()[0]["id"]
            # 模拟stop等待预算已用尽，不在单元测试中真的等待10秒。
            with patch("ps38app.follow.service.time.monotonic", side_effect=[0, 11]):
                self.service.stop()
            self.service._wake.clear()
            release.set()
            self.assertTrue(self.service._wake.wait(2))
            self.service.start()
            self.wait_background(
                lambda: self.service.detail(group_id)["group"]["status"] == "SUCCEEDED"
            )
            self.assertEqual(len(self.executor.submitted), 1)
        finally:
            release.set()


if __name__ == "__main__":
    unittest.main()
