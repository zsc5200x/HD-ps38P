"""接口监控：探测、存储与快照，全部使用临时目录和本地服务。"""

import sqlite3
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from ps38app.monitor import MonitorService, MonitorStore, probe


class _Quiet(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.send_response(204)
        self.end_headers()


class ProbeTests(unittest.TestCase):
    def test_open_port_is_reported_online_with_latency(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Quiet)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}"
            result = probe(url, timeout=3)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(result["latency_ms"], 0)
        self.assertEqual(result["url"], url)

    def test_closed_port_is_reported_down(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Quiet)
        url = f"http://127.0.0.1:{server.server_port}"
        server.server_close()
        result = probe(url, timeout=2)
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"])

    def test_invalid_address_is_rejected(self):
        result = probe("ftp://example.test", timeout=1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "地址无效")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MonitorStore(Path(self.tmp.name) / "monitor.db", ["https://a.test"])

    def test_snapshot_reports_uptime_and_status(self):
        self.store.record(
            {
                "url": "https://a.test",
                "ok": True,
                "latency_ms": 12,
                "error": "",
                "checked_at": "2026-01-01T00:00:00+00:00",
            }
        )
        self.store.record(
            {
                "url": "https://a.test",
                "ok": False,
                "latency_ms": 30,
                "error": "TLS",
                "checked_at": "2026-01-01T00:01:00+00:00",
            }
        )
        item = self.store.snapshot()[0]
        self.assertEqual(item["host"], "a.test")
        self.assertEqual(item["samples"], 2)
        self.assertEqual(item["uptime"], 50.0)
        self.assertEqual(item["status"], "down")
        self.assertEqual(item["error"], "TLS")
        self.assertEqual([beat["ok"] for beat in item["beats"]], [False, True])

    def test_history_is_trimmed_to_the_keep_limit(self):
        store = MonitorStore(Path(self.tmp.name) / "trim.db", ["https://a.test"], keep=4)
        for index in range(7):
            store.record(
                {
                    "url": "https://a.test",
                    "ok": True,
                    "latency_ms": index,
                    "error": "",
                    "checked_at": f"2026-01-01T00:0{index}:00+00:00",
                }
            )
        item = store.snapshot()[0]
        self.assertEqual(item["samples"], 4)
        self.assertEqual(item["uptime"], 100.0)

    def test_unknown_endpoint_has_no_samples(self):
        item = self.store.snapshot()[0]
        self.assertEqual(item["samples"], 0)
        self.assertEqual(item["status"], "unknown")

    def test_failed_write_rolls_back_and_closes_connection(self):
        with self.assertRaisesRegex(RuntimeError, "中断写入"):
            with self.store._connect() as connection:
                connection.execute(
                    "INSERT INTO monitor_endpoints(url, added_at) VALUES (?, ?)",
                    ("https://rollback.test", "2026-01-01"),
                )
                raise RuntimeError("中断写入")
        self.assertEqual(self.store.endpoints(), ["https://a.test"])
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = MonitorService(
            {
                "monitor_db_path": Path(self.tmp.name) / "monitor.db",
                "monitor_endpoints": ["https://a.test", "https://b.test"],
                "monitor_interval_seconds": 30,
                "monitor_timeout_seconds": 2,
                "monitor_beats": 8,
            }
        )

    def test_run_once_records_every_endpoint(self):
        probed, recorded = [], []

        def fake_probe(url, timeout):
            probed.append(url)
            return {
                "url": url,
                "ok": url.endswith("a.test"),
                "latency_ms": 5,
                "error": "" if url.endswith("a.test") else "TLS",
                "checked_at": "2026-01-01T00:00:00+00:00",
            }

        self.service.store.record = recorded.append
        import ps38app.monitor as monitor_module

        original = monitor_module.probe
        monitor_module.probe = fake_probe
        try:
            snapshot = self.service.run_once()
        finally:
            monitor_module.probe = original
        self.assertEqual(sorted(probed), ["https://a.test", "https://b.test"])
        self.assertEqual(len(recorded), 2)
        self.assertEqual(len(snapshot), 2)

    def test_close_stops_the_background_thread(self):
        with mock.patch.object(self.service, "run_once", return_value=[]):
            self.service.start()
            thread = self.service._thread
            self.service.stop()
        self.assertFalse(thread.is_alive())

    def test_stop_timeout_keeps_background_worker_until_it_exits(self):
        entered, release = threading.Event(), threading.Event()

        def blocked_round():
            entered.set()
            release.wait(5)
            return []

        with mock.patch.object(self.service, "run_once", side_effect=blocked_round) as run_once:
            self.service.start()
            thread = self.service._thread
            try:
                self.assertTrue(entered.wait(2))
                with mock.patch.object(thread, "join"):
                    self.service.stop()
                self.service.start()
                self.assertIs(self.service._thread, thread)
                self.assertTrue(self.service._stop.is_set())
                self.assertFalse(self.service.refresh_async())
            finally:
                release.set()
                thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(run_once.call_count, 1)

    def test_stop_timeout_keeps_manual_round_and_prevents_restart(self):
        entered, release = threading.Event(), threading.Event()

        def blocked_round():
            entered.set()
            release.wait(5)
            return []

        with mock.patch.object(self.service, "run_once", side_effect=blocked_round) as run_once:
            self.assertTrue(self.service.refresh_async())
            thread = self.service._round
            try:
                self.assertTrue(entered.wait(2))
                with mock.patch.object(thread, "join") as join:
                    self.service.stop()
                join.assert_called_once()
                self.service.start()
                self.assertIs(self.service._round, thread)
                self.assertIsNone(self.service._thread)
                self.assertTrue(self.service._stop.is_set())
                self.assertFalse(self.service.refresh_async())
            finally:
                release.set()
                thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(run_once.call_count, 1)
