import http.client
import json
import tempfile
import threading
import unittest
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from ps38app.account_pool.service import AccountPoolService
from ps38app.adspower import (
    AdsPowerClient,
    AdsPowerError,
    detect_platform,
    environment_projection,
    parse_account_name,
    parse_expiry,
)
from ps38app.poller import Poller
from ps38app.server import DashboardServer
from tests.support import isolated_config


def env_row(profile_id, profile_no, name, ip="163.227.3.10", group_name=""):
    return {
        "profile_id": profile_id,
        "profile_no": profile_no,
        "name": name,
        "group_name": group_name,
        "ip": ip,
        "user_proxy_config": {
            "proxy_type": "socks5",
            "proxy_host": ip,
            "proxy_port": "443",
            "proxy_user": "proxy-user",
            "proxy_password": "proxy-secret",
        },
        "last_open_time": "1793869897",
    }


class FakeAdsPower:
    base_url = "http://localhost:50325"

    def __init__(self, rows):
        self.rows = list(rows)
        self.calls = 0

    def list_all_profiles(self):
        self.calls += 1
        return [dict(row) for row in self.rows]


class ParsingTests(unittest.TestCase):
    def test_platform_detection(self):
        self.assertEqual(detect_platform("平博后台cokdex77."), "ps38_backend")
        self.assertEqual(detect_platform("PS3838 主账号"), "ps38_backend")
        self.assertEqual(detect_platform("1号会员：iigg1989 过期12-05"), "crown_member")
        self.assertEqual(detect_platform("皇冠登录环境测试"), "crown_member")
        self.assertEqual(detect_platform("random env"), "")

    def test_account_names(self):
        cases = {
            "1号会员：iigg1989 过期12-05": "iigg1989",
            "登zy2411 会：llyyl120 过期12-04": "llyyl120",
            "全折登zy2411会zy11647 过期11-27": "zy11647",
            "Liubo153123登 过期11-27": "Liubo153123",
            "国2 zzz1007kk 过期11-24": "zzz1007kk",
            "1.5九折登jmna1299": "jmna1299",
            "全折登tk2266会ttkk334488 11-20": "ttkk334488",
            "ccyykk5566": "ccyykk5566",
            "皇冠登录环境测试": "",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(parse_account_name(name), expected)

    def test_expiry(self):
        today = date(2026, 11, 5)
        self.assertEqual(
            parse_expiry("1号会员：iigg1989 过期12-05", today=today),
            {"expire_date": "2026-12-05", "expired": False},
        )
        self.assertEqual(
            parse_expiry("特投 ccpp775533 过期8-27", today=today),
            {"expire_date": "2026-08-27", "expired": True},
        )
        self.assertEqual(
            parse_expiry("全折登tk2266会ttkk334488 11-20", today=today),
            {"expire_date": "2026-11-20", "expired": False},
        )
        self.assertEqual(
            parse_expiry("皇冠登录环境测试", today=today),
            {"expire_date": "", "expired": False},
        )

    def test_projection_hides_proxy_credentials(self):
        payload = environment_projection(env_row("k1aaa", "301", "1号会员：aaa123 过期12-05"))
        text = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("proxy-secret", text)
        self.assertNotIn("proxy-user", text)
        self.assertEqual(payload["account"], "aaa123")
        self.assertEqual(payload["proxy_host"], "163.227.3.10")


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = isolated_config(Path(self.tmp.name))
        self.service = AccountPoolService(self.config)
        self.rows = [
            env_row("k1aaa", "301", "1号会员：aaa123 过期12-05"),
            env_row("k1bbb", "302", "2号会员：bbb456 过期12-04"),
            env_row("k1ccc", "303", "3号会员：ccc789 过期8-27"),
            env_row("k1ddd", "304", "4号会员：ddd000 过期12-04"),
            env_row("k1eee", "305", "皇冠登录环境测试"),
            env_row("k1fff", "306", "6号会员：fff111 过期12-04"),
        ]
        self.service.adspower = FakeAdsPower(self.rows)

    def import_payload(self, env_ids, **extra):
        payload = {
            "platform": "crown_member",
            "base_url": "https://mos022.com",
            "env_ids": env_ids,
        }
        payload.update(extra)
        return payload

    def test_environments_are_sorted_and_bounded(self):
        self.service.import_environments(self.import_payload(["k1aaa"]))
        payload = self.service.environments(refresh=True)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["summary"]["total"], 6)
        self.assertEqual(payload["summary"]["bound"], 1)
        self.assertEqual(payload["summary"]["expired"], 1)
        self.assertEqual(payload["items"][0]["profile_no"], "306")
        bound = [item for item in payload["items"] if item["profile_id"] == "k1aaa"][0]
        self.assertIsNotNone(bound["bound_account"])
        self.assertNotIn("proxy-secret", json.dumps(payload, ensure_ascii=False))

    def test_import_creates_accounts_and_skips_invalid(self):
        result = self.service.import_environments(
            self.import_payload(["k1aaa", "k1bbb", "k1ccc", "k1eee", "missing"])
        )
        self.assertEqual(len(result["created"]), 3)
        reasons = {item["env_id"]: item["reason"] for item in result["skipped"]}
        self.assertIn("无法从环境名识别账号", reasons["k1eee"])
        self.assertIn("环境不存在", reasons["missing"])

        accounts = {account["username"]: account for account in self.service.snapshot()["accounts"]}
        self.assertEqual(accounts["aaa123"]["env_id"], "k1aaa")
        self.assertEqual(accounts["aaa123"]["env_no"], "301")
        self.assertEqual(accounts["aaa123"]["session_backend"], "adspower")
        self.assertEqual(accounts["aaa123"]["base_url"], "https://mos022.com")
        self.assertTrue(accounts["aaa123"]["enabled"])
        self.assertEqual(accounts["aaa123"]["status"], "idle")
        self.assertFalse(accounts["ccc789"]["enabled"])
        self.assertEqual(accounts["ccc789"]["status"], "disabled")

    def test_duplicate_environment_and_username_are_skipped(self):
        self.service.import_environments(self.import_payload(["k1aaa"]))
        result = self.service.import_environments(self.import_payload(["k1aaa", "k1fff"]))
        self.assertEqual([item["env_id"] for item in result["created"]], ["k1fff"])
        self.assertIn("已绑定账号", result["skipped"][0]["reason"])

        with self.assertRaises(ValueError):
            self.service.import_environments(
                self.import_payload(["k1ddd"], usernames={"k1ddd": "aaa123"})
            )

    def test_empty_selection_is_rejected(self):
        with self.assertRaises(ValueError):
            self.service.import_environments(self.import_payload([]))

    def test_manual_username_rescues_unparsed_environment(self):
        result = self.service.import_environments(
            self.import_payload(["k1eee"], usernames={"k1eee": "manual999"})
        )
        self.assertEqual(result["created"][0]["username"], "manual999")

    def test_auto_platform_uses_ps38_for_pingbo_environment(self):
        self.service.adspower = FakeAdsPower(
            self.rows + [env_row("k1ps38", "366", "平博后台cokdex77.")]
        )
        result = self.service.import_environments(
            {
                "platform": "auto",
                "env_ids": ["k1ps38"],
                "usernames": {"k1ps38": "okcodex77."},
            }
        )
        created = result["created"][0]
        account = self.service.store.get(created["id"])
        self.assertEqual(account["platform"], "ps38_backend")
        self.assertEqual(account["base_url"], "https://www.ps38ag.com")
        self.assertEqual(account["login_path"], "/partner/zh-cn/login")
        self.assertEqual(account["session_backend"], "adspower")

    def test_primary_can_bind_an_environment(self):
        from ps38app.account_pool.store import PRIMARY_ID

        self.service.adspower = FakeAdsPower(
            self.rows + [env_row("k1ps38", "366", "平博后台cokdex77.")]
        )
        account = self.service.bind_environment(PRIMARY_ID, "k1ps38")
        self.assertEqual(account["env_id"], "k1ps38")
        self.assertEqual(account["env_no"], "366")
        self.assertEqual(account["session_backend"], "adspower")
        self.assertEqual(account["managed_by"], "collector")
        other = self.service.store.save_account(
            {
                "platform": "crown_member",
                "name": "另一个账号",
                "base_url": "https://mos022.com",
                "login_path": "/",
                "username": "other999",
            }
        )
        with self.assertRaises(ValueError):
            self.service.bind_environment(other["id"], "k1ps38")

        poller = Poller(self.config)
        self.assertEqual(poller._primary_env_id(), "k1ps38")

    def test_environment_cannot_bind_two_accounts(self):
        payload = {
            "platform": "crown_member",
            "name": "会员 A",
            "base_url": "https://mos022.com",
            "login_path": "/",
            "username": "aaa123",
            "env_id": "k1aaa",
            "env_no": "301",
        }
        self.service.store.save_account(payload)
        with self.assertRaises(ValueError):
            self.service.store.save_account({**payload, "username": "other999"})

    def test_credentials_are_saved_with_totp_and_not_exposed(self):
        account = self.service.store.save_account(
            {
                "platform": "crown_member",
                "name": "凭据测试",
                "base_url": "https://mos022.com",
                "login_path": "/",
                "username": "cred001",
            }
        )
        updated = self.service.save_credentials(
            account["id"],
            {
                "username": "cred001",
                "password": "secret-pass",
                "totp_secret": "JBSWY3DPEHPK3PXP",
            },
        )
        self.assertTrue(updated["has_credentials"])
        self.assertTrue(updated["has_totp"])
        stored = self.service.store.credentials(account["id"])
        self.assertEqual(stored["password"], "secret-pass")
        self.assertEqual(stored["totp_secret"], "JBSWY3DPEHPK3PXP")
        text = json.dumps(updated, ensure_ascii=False)
        self.assertNotIn("secret-pass", text)
        self.assertNotIn("JBSWY3DPEHPK3PXP", text)

        # Empty password keeps the existing one; a new TOTP secret replaces it.
        self.service.save_credentials(
            account["id"],
            {
                "username": "cred001",
                "password": "",
                "totp_secret": "GEZDGNBVGY3TQOJQ",
            },
        )
        stored = self.service.store.credentials(account["id"])
        self.assertEqual(stored["password"], "secret-pass")
        self.assertEqual(stored["totp_secret"], "GEZDGNBVGY3TQOJQ")

        with self.assertRaises(ValueError):
            self.service.save_credentials(
                account["id"],
                {
                    "username": "cred001",
                    "totp_secret": "not-base32!",
                },
            )

    def test_primary_can_store_credentials(self):
        from ps38app.account_pool.store import PRIMARY_ID

        updated = self.service.save_credentials(
            PRIMARY_ID,
            {
                "username": "okcodex77.",
                "password": "pass",
                "totp_secret": "JBSWY3DPEHPK3PXP",
            },
        )
        self.assertTrue(updated["has_totp"])
        self.assertEqual(updated["managed_by"], "collector")

    def test_environment_change_clears_saved_session(self):
        account = self.service.store.save_account(
            {
                "platform": "crown_member",
                "name": "会员 A",
                "base_url": "https://mos022.com",
                "login_path": "/",
                "username": "aaa123",
                "env_id": "k1aaa",
                "env_no": "301",
                "env_name": "1号会员：aaa123 过期12-05",
            }
        )
        storage = self.service.store.paths(account["id"])["storage"]
        storage.parent.mkdir(parents=True, exist_ok=True)
        storage.write_text("{}", encoding="utf-8")
        self.assertTrue(self.service.store.get(account["id"])["has_session"])

        updated = self.service.store.save_account({"env_id": "k1bbb"}, account["id"])
        self.assertEqual(updated["env_id"], "k1bbb")
        self.assertEqual(updated["session_backend"], "adspower")
        self.assertFalse(updated["has_session"])

    def test_environment_cache_is_reused(self):
        fake = FakeAdsPower(self.rows)
        self.service.adspower = fake
        self.service.environments(refresh=True)
        self.service.environments()
        self.assertEqual(fake.calls, 1)
        self.service.environments(refresh=True)
        self.assertEqual(fake.calls, 2)


class _ApiHandler(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, fmt, *args):  # noqa: A003 - silence test server
        return

    def _send(self, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/status":
            self._send({"code": 0, "msg": "success"})
        else:
            self._send({"code": -1, "msg": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        self.calls.append((self.path, body))
        page = int(body.get("page") or 1)
        if page == 1:
            rows = [env_row("k1p1", "1", "a1 过期12-05"), env_row("k1p2", "2", "a2 过期12-05")]
        else:
            rows = [env_row("k1p3", "3", "a3 过期12-05")]
        self._send({"code": 0, "msg": "success", "data": {"list": rows, "page_size": len(rows)}})


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _ApiHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        _ApiHandler.calls = []

    def test_status_and_paginated_list(self):
        port = self.server.server_address[1]
        client = AdsPowerClient(base_url=f"http://127.0.0.1:{port}", timeout=5)
        self.assertEqual(client.status().get("code"), 0)
        with (
            mock.patch("ps38app.adspower.PAGE_LIMIT", 2),
            mock.patch("ps38app.adspower.LIST_INTERVAL", 0),
        ):
            rows = client.list_all_profiles()
        self.assertEqual([row["profile_id"] for row in rows], ["k1p1", "k1p2", "k1p3"])
        pages = [body.get("page") for path, body in _ApiHandler.calls]
        self.assertEqual(pages, [1, 2])

    def test_error_code_is_raised(self):
        port = self.server.server_address[1]
        client = AdsPowerClient(base_url=f"http://127.0.0.1:{port}", timeout=5)
        with self.assertRaises(AdsPowerError):
            client.active("k1missing")


class _StubPoller:
    def snapshot(self):
        return {
            "status": "online",
            "message": "",
            "since": "",
            "last_error": "",
            "last_open_at": "",
            "last_settled_at": "",
            "last_cycle_seconds": 0.0,
            "profile": {},
            "agents": [],
            "poll_count": 0,
            "new_orders": 0,
            "backfill": {"running": False, "done": 0, "total": 0, "message": ""},
            "counts": {"total": 0, "open": 0, "settled": 0, "live": 0},
        }


class _StubPool:
    def __init__(self):
        self.imported = None

    def environments(self, refresh=False):
        return {
            "ok": True,
            "base_url": "http://localhost:50325",
            "items": [
                {
                    "profile_id": "k1aaa",
                    "profile_no": "301",
                    "name": "1号会员：aaa123 过期12-05",
                    "account": "aaa123",
                    "bound_account": None,
                }
            ],
            "summary": {"total": 1, "bound": 0, "expired": 0, "available": 1},
        }

    def import_environments(self, payload):
        self.imported = payload
        return {
            "ok": True,
            "created": [{"id": "acc-1", "env_id": "k1aaa"}],
            "skipped": [],
            "message": "已导入 1 个账号",
        }


class ServerRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = isolated_config(Path(self.tmp.name))
        self.pool = _StubPool()
        self.server = DashboardServer(("127.0.0.1", 0), self.config, _StubPoller(), self.pool)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def test_profiles_endpoint(self):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=5
        )
        try:
            connection.request("GET", "/api/adspower/profiles?refresh=1")
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()
        self.assertEqual(response.status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["items"][0]["account"], "aaa123")

    def test_import_endpoint(self):
        body = json.dumps(
            {
                "platform": "crown_member",
                "base_url": "https://mos022.com",
                "env_ids": ["k1aaa"],
            }
        ).encode("utf-8")
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=5
        )
        try:
            connection.request(
                "POST",
                "/api/pool/import-envs",
                body=body,
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["created"][0]["env_id"], "k1aaa")
        self.assertEqual(self.pool.imported["env_ids"], ["k1aaa"])
