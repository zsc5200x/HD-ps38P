"""跟单接口只使用临时数据库和本地 HTTP 服务。"""

import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from ps38app.server import DashboardServer
from tests.support import isolated_config
from tests.test_server import StubPoller


class FakeFollow:
    def __init__(self):
        self.saved = None
        self.paused = False
        self.calls = []

    def snapshot(self, **kwargs):
        self.calls.append(kwargs)
        return {"ok": True, "rows": [], "total": 0, "rule": {"enabled": False}}

    def available_account_ids(self):
        return ["crown-test"]

    def save_rule(self, body):
        if body.get("amount_value") == "bad":
            raise ValueError("金额必须是有效数字")
        self.saved = body
        return body

    def pause(self):
        self.paused = True

    def detail(self, group_id):
        if group_id == "missing":
            raise KeyError(group_id)
        return {"ok": True, "group": {"id": group_id}, "members": [], "attempts": []}

    def request_reconcile(self, group_id):
        if group_id == "missing":
            raise KeyError(group_id)
        self.calls.append(("reconcile", group_id))
        return {"ok": True, "message": "已安排只读核对"}

    def resolve_unknown(self, group_id, body):
        if group_id == "missing":
            raise KeyError(group_id)
        if not body.get("note"):
            raise ValueError("请填写核对依据")
        self.calls.append(("resolution", group_id, body))
        return {"ok": True, "message": "核对已登记"}

    def stop(self):
        pass


class FakePool:
    def snapshot(self):
        return {
            "accounts": [
                {
                    "id": "crown-test",
                    "platform": "crown_member",
                    "label": "测试皇冠",
                    "username": "test-member",
                    "enabled": True,
                    "status": "online",
                    "cookie": "must-not-leak",
                    "password": "must-not-leak",
                },
                {"id": "source-test", "platform": "ps38_backend"},
            ]
        }


class FollowApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = isolated_config(Path(self.tmp.name))
        poller = StubPoller({"total": 0, "open": 0, "settled": 0, "live": 0})
        self.server = DashboardServer(("127.0.0.1", 0), self.config, poller, FakePool())
        self.real_service = self.server.follow_service
        self.follow = FakeFollow()
        self.server.follow_service = self.follow
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        if self.real_service is not None:
            self.real_service.stop()
        self.thread.join(timeout=2)

    def request(self, method, path, body=None, origin=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if origin is not None:
            headers["Origin"] = origin
        try:
            connection.request(method, path, None if body is None else json.dumps(body), headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_default_query_is_confirmed_success_and_accounts_are_redacted(self):
        status, payload = self.request("GET", "/api/follow")
        self.assertEqual(status, 200)
        self.assertEqual(self.follow.calls[-1]["status"], "SUCCEEDED")
        self.assertEqual([a["id"] for a in payload["accounts"]], ["crown-test"])
        self.assertTrue(payload["accounts"][0]["follow_eligible"])
        self.assertNotIn("must-not-leak", json.dumps(payload))

    def test_search_and_pagination_are_passed_to_service(self):
        status, _ = self.request(
            "GET", "/api/follow?status=UNKNOWN&search=ticket&limit=20&offset=40"
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            self.follow.calls[-1],
            {
                "status": "UNKNOWN",
                "search": "ticket",
                "limit": 20,
                "offset": 40,
            },
        )
        self.assertEqual(self.request("GET", "/api/follow?status=bogus")[0], 400)
        self.assertEqual(self.request("GET", "/api/follow?limit=bad")[0], 400)

    def test_rule_save_pause_and_validation(self):
        rule = {"enabled": False, "direction": "reverse", "amount_value": "10"}
        status, payload = self.request("POST", "/api/follow/rule", rule)
        self.assertEqual(status, 200)
        self.assertEqual(payload["rule"], rule)
        self.assertEqual(self.follow.saved, rule)
        self.assertEqual(self.request("POST", "/api/follow/rule", {})[0], 400)
        status, payload = self.request("POST", "/api/follow/rule", {"amount_value": "bad"})
        self.assertEqual(status, 400)
        self.assertIn("金额", payload["error"])
        self.assertEqual(self.request("POST", "/api/follow/pause", {})[0], 200)
        self.assertTrue(self.follow.paused)

    def test_unrestricted_receipt_write_endpoint_is_unavailable(self):
        status, _ = self.request("POST", "/api/follow/receipt", {"status": "SUCCEEDED"})
        self.assertEqual(status, 404)
        self.assertIsNone(self.follow.saved)

    def test_cross_origin_rule_change_is_rejected(self):
        status, _ = self.request(
            "POST", "/api/follow/rule", {"enabled": True}, "https://foreign.invalid"
        )
        self.assertEqual(status, 403)
        self.assertIsNone(self.follow.saved)

    def test_reconcile_and_manual_resolution_routes(self):
        self.assertEqual(
            self.request("POST", "/api/follow/groups/group-test/reconcile", {})[0], 200
        )
        self.assertEqual(self.follow.calls[-1], ("reconcile", "group-test"))
        body = {"outcome": "SUCCEEDED", "receipt_id": "TICKET-1", "note": "已在平台确认票号"}
        self.assertEqual(
            self.request("POST", "/api/follow/groups/group-test/resolution", body)[0], 200
        )
        self.assertEqual(self.follow.calls[-1], ("resolution", "group-test", body))
        self.assertEqual(
            self.request("POST", "/api/follow/groups/group-test/resolution", {})[0], 400
        )
        self.assertEqual(self.request("POST", "/api/follow/groups/missing/reconcile", {})[0], 404)
        self.assertEqual(
            self.request("POST", "/api/follow/groups/missing/resolution", body)[0], 404
        )
        self.assertEqual(self.request("POST", "/api/follow/groups/a%2Fb/reconcile", {})[0], 400)
        self.assertEqual(self.request("POST", "/api/follow/groups/group-test/resubmit", {})[0], 404)

    def test_cross_origin_reconcile_and_resolution_are_rejected(self):
        for action in ("reconcile", "resolution"):
            with self.subTest(action=action):
                status, _ = self.request(
                    "POST",
                    "/api/follow/groups/group-test/" + action,
                    {"outcome": "SUCCEEDED", "note": "平台已确认"},
                    "https://foreign.invalid",
                )
                self.assertEqual(status, 403)
        self.assertEqual(self.follow.calls, [])

    def test_detail_and_missing_group(self):
        status, payload = self.request("GET", "/api/follow/groups/group-test")
        self.assertEqual(status, 200)
        self.assertEqual(payload["group"]["id"], "group-test")
        self.assertEqual(self.request("GET", "/api/follow/groups/missing")[0], 404)
        self.assertEqual(self.request("GET", "/api/follow/groups/a%2Fb")[0], 400)

    def test_unavailable_service_is_explicit(self):
        self.server.follow_service = None
        status, payload = self.request("GET", "/api/follow")
        self.assertEqual(status, 503)
        self.assertFalse(payload["ok"])

    def test_real_service_initializes_disabled_in_isolated_directory(self):
        self.assertIsNotNone(self.real_service, self.server.follow_error)
        self.assertFalse(self.real_service.snapshot()["rule"]["enabled"])
        self.assertTrue((Path(self.tmp.name) / "follow.sqlite3").is_file())

    def test_second_start_on_busy_port_does_not_recover_live_submission(self):
        store = self.real_service.store
        group = {
            "id": "inflight-test",
            "base_key": "isolated-test",
            "start_ms": 1,
            "close_ms": 20001,
            "status": "SUBMITTING",
            "account_id": "crown-test",
        }
        with store.connect() as connection:
            store.save_group(connection, group)
        with self.assertRaises(OSError):
            DashboardServer(self.server.server_address, self.config, self.server.poller)
        with store.connect() as connection:
            self.assertEqual(store.get_group(connection, group["id"])["status"], "SUBMITTING")
