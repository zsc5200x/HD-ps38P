"""通过事件控制网络任务时序，验证容量、账号隔离和退出回收。"""

import threading
import unittest
from concurrent.futures import Future
from unittest.mock import patch

from ps38app.follow.dispatcher import BoundedDispatcher


class DispatcherTests(unittest.TestCase):
    def test_capacity_account_and_key_stay_reserved_until_collected(self):
        release_a, release_b = threading.Event(), threading.Event()
        started_a, started_b = threading.Event(), threading.Event()
        dispatcher = BoundedDispatcher(max_workers=2)

        def blocked(started, release, value):
            started.set()
            if not release.wait(3):
                raise TimeoutError("测试任务未释放")
            return value

        try:
            first = dispatcher.submit("a", "member-a", blocked, started_a, release_a, "A")
            self.assertTrue(started_a.wait(2))
            self.assertIsNone(dispatcher.submit("b", "member-a", lambda: None))
            self.assertIsNone(dispatcher.submit("a", "member-b", lambda: None))
            second = dispatcher.submit("b", "member-b", blocked, started_b, release_b, "B")
            self.assertTrue(started_b.wait(2))
            self.assertEqual(dispatcher.active_count(), 2)
            self.assertEqual(dispatcher.available_slots(), 0)
            self.assertIsNone(dispatcher.submit("c", "member-c", lambda: None))

            release_a.set()
            self.assertEqual(first.result(timeout=2), "A")
            self.assertTrue(dispatcher.is_key_active("a"))
            self.assertEqual(dispatcher.available_slots(), 0)
            self.assertEqual(dispatcher.poll_completed(), [("a", "member-a", first)])
            self.assertFalse(dispatcher.is_key_active("a"))
            self.assertEqual(dispatcher.available_slots(), 1)
            replacement = dispatcher.submit("a", "member-a", lambda: "新尝试")
            self.assertEqual(replacement.result(timeout=2), "新尝试")

            release_b.set()
            self.assertEqual(second.result(timeout=2), "B")
            self.assertEqual(len(dispatcher.poll_completed()), 2)
            self.assertEqual(dispatcher.poll_completed(), [])
            self.assertEqual(dispatcher.active_count(), 0)
        finally:
            release_a.set()
            release_b.set()
            dispatcher.shutdown(wait=True)

    def test_exception_is_returned_to_caller_and_releases_account(self):
        dispatcher = BoundedDispatcher()

        def fail():
            raise ValueError("模拟平台失败")

        try:
            future = dispatcher.submit("job", "member", fail)
            with self.assertRaisesRegex(ValueError, "模拟平台失败"):
                future.result(timeout=2)
            self.assertEqual(dispatcher.poll_completed(), [("job", "member", future)])
            retry = dispatcher.submit("job", "member", lambda: 1)
            self.assertEqual(retry.result(timeout=2), 1)
            dispatcher.poll_completed()
        finally:
            dispatcher.shutdown(wait=True)

    def test_done_callback_wakes_caller_and_its_error_cannot_change_result(self):
        notified = threading.Event()

        def notify():
            notified.set()
            raise RuntimeError("模拟唤醒失败")

        dispatcher = BoundedDispatcher(on_done=notify)
        try:
            future = dispatcher.submit("job", "member", lambda: 7)
            self.assertEqual(future.result(timeout=2), 7)
            self.assertTrue(notified.wait(2))
            self.assertEqual(dispatcher.poll_completed(), [("job", "member", future)])
        finally:
            dispatcher.shutdown(wait=True)

    def test_shutdown_rejects_new_work_without_waiting_for_active_request(self):
        release, started = threading.Event(), threading.Event()
        notified = threading.Event()
        dispatcher = BoundedDispatcher(max_workers=1, on_done=notified.set)

        def blocked():
            started.set()
            if not release.wait(3):
                raise TimeoutError("测试任务未释放")
            return "最终回执"

        try:
            future = dispatcher.submit("inflight", "member", blocked)
            self.assertTrue(started.wait(2))
            dispatcher.shutdown(wait=False)
            self.assertFalse(future.done())
            self.assertIsNone(dispatcher.submit("new", "other-member", lambda: None))
            release.set()
            self.assertEqual(future.result(timeout=2), "最终回执")
            self.assertTrue(notified.wait(2))
            self.assertEqual(dispatcher.poll_completed(), [("inflight", "member", future)])
            self.assertEqual(dispatcher.active_count(), 0)
        finally:
            release.set()
            dispatcher.shutdown(wait=True)

    def test_shutdown_cancels_not_started_future_and_poll_releases_it(self):
        # 替代线程池以确定性保留“已提交、尚未开始”的窗口。
        with patch("ps38app.follow.dispatcher.ThreadPoolExecutor") as pool_type:
            pending = Future()
            pool = pool_type.return_value
            pool.submit.return_value = pending
            pool.shutdown.side_effect = lambda **kwargs: pending.cancel()
            dispatcher = BoundedDispatcher(max_workers=1)
            future = dispatcher.submit("queued", "member", lambda: None)
            dispatcher.shutdown(wait=False)
            pool.shutdown.assert_called_once_with(wait=False, cancel_futures=True)
            self.assertTrue(future.cancelled())
            self.assertEqual(dispatcher.poll_completed(), [("queued", "member", future)])
            self.assertEqual(dispatcher.available_slots(), 1)
            self.assertIsNone(dispatcher.submit("new", "member", lambda: None))


if __name__ == "__main__":
    unittest.main()
