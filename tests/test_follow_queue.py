"""跟单队列、恢复、分类重试和核对闭环；仅使用临时库与模拟执行器。"""

import copy
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
    pending_batches = core.FollowServiceTests.pending_batches

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
            self.call_without_network_wait()
            self.assertTrue(self.pending_batches())
            self.clock.now += core.PENDING_SECONDS
            self.call_without_network_wait()
            self.assertTrue(entered.wait(2))
            first = self.groups()[0]
            self.assertEqual(first["status"], "PREPARING")

            self.clock.now += 21
            self.add("second")
            self.call_without_network_wait()
            self.clock.now += core.PENDING_SECONDS
            self.call_without_network_wait()
            groups = self.groups()
            second = next(group for group in groups if group["id"] != first["id"])
            self.assertEqual(self.service.detail(second["id"])["group"]["status"], "WAIT_ACCOUNT")
            self.assertEqual(len(self.executor.prepared), 1)
            with self.service.store.connect() as connection:
                claims = connection.execute("SELECT * FROM follow_claims").fetchall()
            self.assertEqual(len(claims), 1)
            self.assertEqual(claims[0]["group_id"], first["id"])

            self.executor.on_prepare = None
            release.set()
            # 成功状态先于账号释放和 Future 完成写入；等任务回收后再推进假时钟，
            # 避免下一组因清理尚未完成而重新排到固定时钟之后。
            self.wait_background(
                lambda: (
                    self.service.detail(first["id"])["group"]["status"] == "SUCCEEDED"
                    and self.service._dispatcher.active_count() == 0
                )
            )
            self.clock.now += 2
            self.wait_background(
                lambda: all(group["status"] == "SUCCEEDED" for group in self.groups())
            )
            self.assertEqual(len(self.executor.submitted), 2)
            self.assertEqual(len({group["receipt_id"] for group in self.groups()}), 2)
        finally:
            release.set()

    def test_same_account_can_run_parallel_when_limit_allows(self):
        # 每号并发=3 时，同一个号可以同时挂 3 个在途任务。
        from ps38app.follow.dispatcher import BoundedDispatcher

        dispatcher = BoundedDispatcher(max_workers=5, per_account=3)
        release = threading.Event()

        def job():
            release.wait(2)

        try:
            futures = [dispatcher.submit(f"k{i}", "crown-a", job) for i in range(3)]
            self.assertTrue(all(f is not None for f in futures))
            # 第 4 个同号任务应被拒绝
            self.assertIsNone(dispatcher.submit("k4", "crown-a", job))
        finally:
            release.set()
            dispatcher.shutdown()

    def test_dispatcher_workers_follow_config(self):
        from ps38app.follow.dispatcher import BoundedDispatcher

        config = dict(self.config, follow_parallel_attempts=5)
        dispatcher = BoundedDispatcher(max_workers=config["follow_parallel_attempts"])
        try:
            self.assertEqual(dispatcher.available_slots(), 5)
        finally:
            dispatcher.shutdown()

    def test_second_instance_preserves_live_claim_and_cannot_reprepare(self):
        entered, release = self.block_prepare()
        other = None
        try:
            self.enable()
            self.add("first")
            self.call_without_network_wait()
            self.clock.now += core.PENDING_SECONDS
            self.call_without_network_wait()
            self.assertTrue(entered.wait(2))
            first_id = self.groups()[0]["id"]

            self.clock.now += 21
            self.add("second")
            self.call_without_network_wait()
            self.clock.now += core.PENDING_SECONDS
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
            "category": "MARKET_REJECTED",
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
        self.assertEqual([item[1] for item in self.executor.submitted], ["61", "61"])
        self.assertEqual([item[0]["side"] for item in self.executor.submitted], ["HOME", "HOME"])
        self.assertEqual(
            [item["status"] for item in completed["attempts"]], ["REJECTED", "SUCCEEDED"]
        )
        self.assertTrue(completed["history"])
        self.tick(20)
        self.assertEqual(len(self.executor.submitted), 2)

    def test_market_change_fast_retries_then_waits_within_window(self):
        self.executor.result = {
            "status": "REJECTED",
            "retryable": True,
            "category": "MARKET_REJECTED",
            "error_code": "1X015",
            "action": "retry_market_90",
        }
        self.enable()
        self.add()
        self.tick(21)
        group_id = self.groups()[0]["id"]
        # 前两次：亚秒级重试（盘口变动要立刻重查）
        for _ in range(2):
            stored = self.stored_group(group_id)
            self.assertEqual(stored["status"], "RETRY_WAIT")
            self.assertLess(stored["next_check_at"] - self.clock.now, 1.0)
            self.clock.now = stored["next_check_at"] + 0.001
            self.tick()
        # 第三次仍失败：进入 90 秒观察窗口
        stored = self.stored_group(group_id)
        self.assertEqual(stored["status"], "RETRY_WAIT")
        self.assertGreater(stored["recheck_until"], self.clock.now)
        # 窗口耗尽后判失败
        self.clock.now = stored["recheck_until"] + 0.001
        self.tick()
        self.assertEqual(self.service.detail(group_id)["group"]["status"], "REJECTED")
        self.assertEqual(len(self.executor.submitted), 3)

    def test_generic_bet_rejection_switches_accounts_then_waits(self):
        self.executor.account_ids = ["crown-a", "crown-b", "crown-c", "crown-d"]
        self.executor.result = {
            "status": "REJECTED",
            "retryable": True,
            "category": "MARKET_REJECTED",
            "error_code": "betError000",
            "action": "retry_bet_switch",
        }
        self.enable()
        self.add()
        self.tick(21)
        group_id = self.groups()[0]["id"]
        accounts = []
        for _ in range(3):
            stored = self.stored_group(group_id)
            accounts.append(stored["account_id"])
            self.assertEqual(stored["status"], "RETRY_WAIT")
            self.clock.now = stored["next_check_at"] + 0.001
            self.tick()
        self.assertEqual(len(set(accounts)), 3, f"应换 3 个不同账号: {accounts}")
        stored = self.stored_group(group_id)
        self.assertEqual(stored["status"], "RETRY_WAIT")
        self.assertGreater(stored["recheck_until"], self.clock.now)
        self.assertLessEqual(stored["next_check_at"] - self.clock.now, 15.1)
        self.clock.now = stored["recheck_until"] + 0.001
        self.tick()
        self.assertEqual(self.service.detail(group_id)["group"]["status"], "REJECTED")

    def test_unknown_and_nonwhitelisted_rejections_are_never_resubmitted(self):
        self.executor.result = {
            "status": "REJECTED",
            "retryable": False,
            "category": "MARKET_REJECTED",
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

    def test_future_mapping_checks_do_not_starve_later_ready_group(self):
        self.mappings.unmap("pin:101")
        self.enable(max_age_seconds=600)
        self.add()
        self.tick()
        group_id = self.groups()[0]["id"]
        self.clock.now += 21
        with self.service.store.connect(write=True) as connection:
            original = self.service.store.get_group(connection, group_id)
            original.update(status="READY", target_stake="50", receipt_id=None, next_check_at=None)
            self.service.store.save_group(connection, original)
            for index in range(100):
                older = copy.deepcopy(original)
                older.update(
                    id=f"future-mapping-{index:03d}",
                    base_key=f"future-mapping-key-{index:03d}",
                    source_batch_no=None,
                    source_batch_key=None,
                    receipt_id=None,
                    start_ms=original["start_ms"] - 1000 - index,
                    status="WAIT_MAPPING",
                    next_check_at=self.clock() + 100,
                    _revision=0,
                )
                self.service.store.save_group(connection, older)
        self.mappings.map_events(self.ps, self.crown)
        self.tick()
        self.assertEqual(self.service.detail(group_id)["group"]["status"], "SUCCEEDED")
        self.assertEqual(len(self.executor.prepared), 1)
        self.assertEqual(len(self.executor.submitted), 1)
        with self.service.store.connect() as connection:
            waiting = connection.execute(
                "SELECT COUNT(*) FROM follow_groups WHERE status='WAIT_MAPPING'"
            ).fetchone()[0]
        self.assertEqual(waiting, 100)

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
