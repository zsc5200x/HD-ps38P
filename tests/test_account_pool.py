import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from ps38app.account_pool.crown_member import cookie_header, ping_member
from ps38app.account_pool.providers import CheckResult, LoginResult
from ps38app.account_pool.service import AccountPoolService
from ps38app.account_pool.store import AccountStore, PRIMARY_ID


def pool_config(root: Path):
    return {
        "base_url": "https://www.ps38ag.com",
        "login_path": "/partner/zh-cn/login",
        "dashboard_path": "/partner-ui-v2/report-outstanding-v2",
        "username": "TEST_PRIMARY",
        "password": "",
        "browser_channel": "",
        "profile_dir": root / "profile",
        "pool_db_path": root / "accounts.sqlite3",
        "pool_dir": root / "accounts",
        "pool_check_interval_seconds": 60,
        "pool_login_timeout_seconds": 60,
        "pool_check_timeout_seconds": 5,
    }


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = pool_config(self.root)
        self.store = AccountStore(self.config["pool_db_path"], self.config)

    def tearDown(self):
        self.tmp.cleanup()

    def test_primary_is_seeded_and_publics_are_redacted(self):
        accounts = self.store.accounts()
        self.assertEqual(accounts[0]["id"], PRIMARY_ID)
        self.assertEqual(accounts[0]["managed_by"], "collector")
        self.assertNotIn("password", json.dumps(accounts, ensure_ascii=False).lower())

    def test_create_update_clear_and_delete(self):
        account = self.store.save_account({
            "platform": "crown_member",
            "name": "皇冠会员 A",
            "base_url": "https://member.example.com/",
            "login_path": "/",
            "username": "summer001",
            "password": "secret-pass",
            "check_interval": 120,
        })
        self.assertEqual(account["platform_label"], "皇冠会员")
        self.assertTrue(account["has_credentials"])
        self.assertTrue(self.store.paths(account["id"])["credentials"].is_file())
        if os.name == "posix":
            self.assertEqual(
                stat.S_IMODE(self.store.paths(account["id"])["credentials"].stat().st_mode),
                0o600,
            )
        self.assertNotIn("secret-pass", json.dumps(account, ensure_ascii=False))

        storage = self.store.paths(account["id"])["storage"]
        storage.parent.mkdir(parents=True, exist_ok=True)
        storage.write_text("{}", encoding="utf-8")
        self.assertTrue(self.store.get(account["id"])["has_session"])

        updated = self.store.save_account({"base_url": "https://other.example.com"}, account["id"])
        self.assertFalse(updated["has_session"])

        enabled = self.store.set_enabled(account["id"], False)
        self.assertFalse(enabled["enabled"])
        self.assertEqual(enabled["status"], "disabled")

        self.store.add_heartbeat(account["id"], "online", 123, "ok")
        self.store.add_heartbeat(account["id"], "error", 456, "bad")
        beats = self.store.get(account["id"])["heartbeats"]
        self.assertEqual([item["status"] for item in beats], ["online", "error"])
        self.assertEqual(beats[-1]["latency_ms"], 456)

        self.store.clear_session(account["id"])
        self.assertFalse(self.store.get(account["id"])["has_session"])

        self.store.delete_account(account["id"])
        with self.assertRaises(KeyError):
            self.store.get(account["id"])

    def test_primary_cannot_be_deleted(self):
        with self.assertRaises(ValueError):
            self.store.delete_account(PRIMARY_ID)


class CrownMemberHeartbeatTests(unittest.TestCase):
    def test_online_response(self):
        xml = (b"<serverresponse><code></code><msg></msg><enable>1</enable>"
               b"<currency>CNY</currency><maxcredit>1000</maxcredit></serverresponse>")
        result = ping_member(
            "https://member.example.com",
            {"cookies": [{"name": "sid", "value": "abc", "domain": "member.example.com",
                          "path": "/", "expires": -1}]},
            {"uid": "u1", "ver": "v1", "langx": "zh-cn", "origin": "https://member.example.com"},
            transport=lambda url, form, headers, timeout: (200, xml),
        )
        self.assertEqual(result["status"], "online")

    def test_double_login_is_conflict(self):
        xml = b"<serverresponse><code>error</code><msg>doublelogin</msg></serverresponse>"
        result = ping_member(
            "https://member.example.com",
            {"cookies": [{"name": "sid", "value": "abc", "domain": "member.example.com",
                          "path": "/", "expires": -1}]},
            {"uid": "u1", "ver": "v1", "origin": "https://member.example.com"},
            transport=lambda url, form, headers, timeout: (200, xml),
        )
        self.assertEqual(result["status"], "conflict")

    def test_cookie_header_filters_expired_and_foreign_cookies(self):
        header = cookie_header({"cookies": [
            {"name": "a", "value": "1", "domain": ".example.com", "path": "/", "expires": -1},
            {"name": "b", "value": "2", "domain": "other.com", "path": "/", "expires": -1},
            {"name": "c", "value": "3", "domain": "example.com", "path": "/", "expires": 1},
        ]}, "https://member.example.com")
        self.assertEqual(header, "a=1")


class FakeProvider:
    def __init__(self):
        self.logins = 0
        self.checks = 0

    def login(self, account, *, force=False, should_stop=None, on_status=None):
        self.logins += 1
        if on_status:
            on_status("fake login")
        return LoginResult("online", "fake login ok", {"uid": "u", "ver": "v"})

    def check(self, account):
        self.checks += 1
        return CheckResult("online", "fake check ok", {})


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = pool_config(self.root)
        self.service = AccountPoolService(self.config, collector=None)
        self.account = self.service.store.save_account({
            "platform": "crown_member",
            "name": "会员 A",
            "base_url": "https://member.example.com",
            "login_path": "/",
            "username": "u1",
        })

    def tearDown(self):
        self.service.stop()
        self.tmp.cleanup()

    def _wait_status(self, account_id, statuses, timeout=3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            account = self.service.store.get(account_id)
            if account["status"] in statuses:
                return account
            time.sleep(0.02)
        return self.service.store.get(account_id)

    def test_login_and_check_workers(self):
        fake = FakeProvider()
        with mock.patch("ps38app.account_pool.service.provider_for", return_value=fake):
            self.service.login(self.account["id"])
            account = self._wait_status(self.account["id"], {"online", "error"})
            self.assertEqual(account["status"], "online")
            self.assertEqual(fake.logins, 1)
            self.service.check(self.account["id"])
            account = self._wait_status(self.account["id"], {"online", "error"})
            self.assertEqual(account["status"], "online")
            self.assertEqual(fake.checks, 1)
            beats = self.service.snapshot()["accounts"][-1]["heartbeats"]
            self.assertTrue(beats)
            self.assertEqual(beats[-1]["status"], "online")

    def test_public_snapshot_has_no_private_session_data(self):
        snapshot = self.service.snapshot()
        encoded = json.dumps(snapshot, ensure_ascii=False).lower()
        self.assertNotIn("password", encoded)
        self.assertNotIn("storage_state", encoded)
        self.assertNotIn("uid", encoded)


if __name__ == "__main__":
    unittest.main()
