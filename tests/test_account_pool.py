import json
import os
import stat
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from ps38app.account_pool.crown_member import cookie_header, ping_member
from ps38app.account_pool.providers import CheckResult, CrownProvider, LoginResult
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

    def test_public_balance_snapshot_is_validated_and_session_meta_stays_private(self):
        account = self.store.save_account({
            "platform": "crown_member",
            "name": "会员 A",
            "base_url": "https://member.example.com",
            "login_path": "/",
            "username": "u1",
        })
        self.store.save_meta(account["id"], {
            "uid": "private-uid", "ver": "private-ver", "balance": "2,500.25",
            "balance_currency": "CNY", "balance_checked_at": "2026-11-04T10:00:00+08:00",
            "password": "must-not-leak",
        })
        public = self.store.get(account["id"])
        self.assertEqual(public["balance"], "2500.25")
        self.assertEqual(public["balance_currency"], "CNY")
        self.assertEqual(public["balance_checked_at"], "2026-11-04T10:00:00+08:00")
        encoded = json.dumps(public, ensure_ascii=False).lower()
        self.assertNotIn("private-uid", encoded)
        self.assertNotIn("private-ver", encoded)
        self.assertNotIn("must-not-leak", encoded)

        self.store.save_meta(account["id"], {"balance": "NaN", "balance_currency": "???"})
        unknown = self.store.get(account["id"])
        self.assertIsNone(unknown["balance"])
        self.assertEqual(unknown["balance_currency"], "")


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
        self.assertEqual(result["balance"], "1000")
        self.assertEqual(result["balance_currency"], "CNY")
        self.assertTrue(result["balance_checked_at"])

    def test_balance_uses_hdproc_field_priority_and_reports_unknown_values(self):
        storage = {"cookies": [{"name": "sid", "value": "abc",
                                 "domain": "member.example.com", "path": "/", "expires": -1}]}
        meta = {"uid": "u1", "ver": "v1", "origin": "https://member.example.com"}

        def ping(fields):
            xml = "<serverresponse><enable>1</enable><currency>RMB</currency>" + fields + "</serverresponse>"
            return ping_member(
                "https://member.example.com", storage, meta,
                transport=lambda url, form, headers, timeout: (200, xml.encode()),
            )

        self.assertEqual(ping("<maxcredit>1,200.50</maxcredit><nowcredit>900</nowcredit><cash>800</cash>")
                         ["balance"], "1200.50")
        self.assertEqual(ping("<nowcredit>900</nowcredit><cash>800</cash>")["balance"], "900")
        self.assertEqual(ping("<cash>800</cash>")["balance"], "800")
        for raw in ("NaN", "Infinity", "-1", "not-a-number"):
            with self.subTest(raw=raw):
                result = ping(f"<maxcredit>{raw}</maxcredit><nowcredit>900</nowcredit>")
                self.assertIsNone(result["balance"])
                self.assertEqual(result["balance_currency"], "CNY")

    def test_crown_provider_check_returns_fresh_balance_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = pool_config(root)
            store = AccountStore(config["pool_db_path"], config)
            account = store.save_account({
                "platform": "crown_member",
                "name": "测试会员",
                "base_url": "https://member.example.com",
                "login_path": "/",
                "username": "u1",
            })
            store.save_meta(account["id"], {"uid": "u1", "ver": "v1"})
            storage = store.paths(account["id"])["storage"]
            storage.parent.mkdir(parents=True, exist_ok=True)
            storage.write_text("{}", encoding="utf-8")
            provider = CrownProvider(config, store, "crown_member")
            with mock.patch.object(provider, "_member_ping", return_value={
                "status": "online", "message": "ok", "balance": "750",
                "balance_currency": "CNY", "balance_checked_at": "2026-11-04T10:01:00+08:00",
            }):
                result = provider.check(account)
            self.assertEqual(result.status, "online")
            self.assertEqual(result.meta["balance"], "750")
            self.assertEqual(result.meta["balance_currency"], "CNY")
            self.assertEqual(result.meta["balance_checked_at"], "2026-11-04T10:01:00+08:00")

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

    def _save_member_session(self, **snapshot):
        paths = self.service.store.paths(self.account["id"])
        meta = {
            "uid": "private-uid",
            "ver": "private-ver",
            "langx": "zh-cn",
            "origin": "https://member.example.com",
            **snapshot,
        }
        self.service.store._write_private(paths["meta"], meta)
        self.service.store._write_private(
            paths["storage"],
            {
                "cookies": [
                    {
                        "name": "sid",
                        "value": "private-cookie",
                        "domain": "member.example.com",
                        "path": "/",
                        "expires": -1,
                    }
                ],
                "origins": [],
            },
        )

    def test_member_heartbeat_populates_old_session_public_balance_snapshot(self):
        self._save_member_session()
        self.assertNotIn("balance", self.service.store.load_meta(self.account["id"]))
        xml = (
            b"<serverresponse><enable>Y</enable><currency>RMB</currency>"
            b"<maxcredit>2,500.25</maxcredit></serverresponse>"
        )
        with mock.patch(
            "ps38app.account_pool.crown_member._http_post",
            return_value=(200, xml),
        ) as transport:
            self.service._check_account(self.account["id"], False)
        transport.assert_called_once()
        self.assertIn(b"p=get_member_data", transport.call_args.args[1])
        self.assertEqual(transport.call_args.args[2]["cookie"], "sid=private-cookie")

        snapshot = self.service.snapshot()
        account = next(item for item in snapshot["accounts"] if item["id"] == self.account["id"])
        self.assertEqual(account["status"], "online")
        self.assertEqual(account["balance"], "2500.25")
        self.assertEqual(account["balance_currency"], "CNY")
        self.assertIsNotNone(datetime.fromisoformat(account["balance_checked_at"]).tzinfo)
        encoded = json.dumps(snapshot, ensure_ascii=False).lower()
        for private in ("private-uid", "private-ver", "private-cookie"):
            self.assertNotIn(private, encoded)
        for key in ("uid", "ver", "cookie", "cookies", "storage_state"):
            self.assertNotIn(f'"{key}"', encoded)

    def test_member_heartbeat_network_failure_preserves_balance_snapshot(self):
        previous = {
            "balance": "2500.25",
            "balance_currency": "CNY",
            "balance_checked_at": "2026-11-04T10:00:00+08:00",
        }
        self._save_member_session(**previous)
        self.service.store.set_status(self.account["id"], "online", "测试会话")
        with mock.patch(
            "ps38app.account_pool.crown_member._http_post",
            side_effect=TimeoutError(),
        ) as transport:
            self.service._check_account(self.account["id"], False)
        transport.assert_called_once()

        snapshot = self.service.snapshot()
        account = next(item for item in snapshot["accounts"] if item["id"] == self.account["id"])
        self.assertEqual(account["status"], "error")
        self.assertTrue(account["has_session"])
        self.assertTrue(account["last_check"])
        self.assertEqual({key: account[key] for key in previous}, previous)

    def test_member_partial_check_preserves_snapshot_and_explicit_unknown_clears_balance(self):
        previous = {
            "balance": "2500.25",
            "balance_currency": "CNY",
            "balance_checked_at": "2026-11-04T10:00:00+08:00",
        }
        self._save_member_session(**previous)
        with mock.patch.object(CrownProvider, "_member_ping", return_value={"status": "online"}):
            self.service._check_account(self.account["id"], False)
        account = self.service.store.get(self.account["id"])
        self.assertEqual(account["status"], "online")
        self.assertEqual({key: account[key] for key in previous}, previous)

        unknown = {
            "balance": None,
            "balance_currency": "",
            "balance_checked_at": "2026-11-04T10:01:00+08:00",
        }
        with mock.patch.object(
            CrownProvider,
            "_member_ping",
            return_value={
                "status": "online",
                **unknown,
            },
        ):
            self.service._check_account(self.account["id"], False)
        account = self.service.store.get(self.account["id"])
        self.assertEqual(account["status"], "online")
        self.assertEqual({key: account[key] for key in unknown}, unknown)

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
