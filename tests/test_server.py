import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import quote

from ps38app import store
from ps38app.server import DashboardServer
from tests.support import isolated_config


def upstream_row(order_id, member="M001", status="OPEN", created=1_760_000_000_000):
    return {
        "playerWagerId": order_id,
        "userCode": member,
        "firstName": "会员" + member,
        "status": status,
        "stake": 100.0,
        "toWin": 90.0,
        "odds": 0.98,
        "currencyCode": "CNY",
        "wagerStatusTranslated": "胜" if status == "SETTLED" else "",
        "createdDate": created,
        "settlementDateTime": created + 500_000 if status == "SETTLED" else None,
        "choices": [{
            "btName": "OU",
            "handicap": "2.5",
            "selection": "UNDER",
            "leagueName": "巴西 - 甲级联赛",
            "eventName": "布拉干萨竞技 -vs- 桑托斯",
            "eventDate": 1_760_010_000_000,
            "inplay": False,
        }],
    }


class StubPoller:
    def __init__(self, counts):
        self.counts = counts
        self.refresh_calls = 0
        self.backfill_calls = 0
        self.login_calls = 0
        self.show_window_calls = 0

    def snapshot(self):
        return {
            "status": "online",
            "message": "测试会话在线",
            "since": store.now_iso(),
            "last_error": "",
            "last_open_at": store.now_iso(),
            "last_settled_at": "",
            "last_cycle_seconds": 0.02,
            "profile": {"user_code": "TEST", "user_id": 1},
            "agents": ["A1"],
            "poll_count": 3,
            "new_orders": 0,
            "backfill": {"running": False, "done": 0, "total": 0, "message": ""},
            "counts": self.counts,
        }

    def request_refresh(self):
        self.refresh_calls += 1

    def request_backfill(self):
        self.backfill_calls += 1
        return True

    def request_relogin(self):
        self.login_calls += 1

    def request_show_window(self):
        self.show_window_calls += 1


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        db_path = Path(cls.tmp.name) / "orders.sqlite3"
        writable = store.OrderStore(db_path)
        writable.save_agents([{"code": "A1", "user_id": 11, "level": "1", "name": "代理一"}])
        stats = {"started_at": store.now_iso(), "finished_at": store.now_iso(),
                 "elapsed_seconds": 0.1, "request_count": 1}
        writable.commit_snapshot("A1", "OPEN", [upstream_row("1"), upstream_row("2")], stats)
        writable.commit_snapshot("A1", "SETTLED", [upstream_row("3", status="SETTLED", created=1_760_000_005_000)], stats)
        cls.counts = writable.counts()
        writable.close()

        config = isolated_config(Path(cls.tmp.name))
        cls.poller = StubPoller(cls.counts)
        cls.server = DashboardServer(("127.0.0.1", 0), config, cls.poller)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.tmp.cleanup()

    def get(self, path):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, response.getheader("Content-Type"), response.read()
        finally:
            connection.close()

    def post(self, path):
        return self.post_body(path, {})

    def post_body(self, path, payload):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request("POST", path, body=json.dumps(payload).encode("utf-8"),
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_index_and_static(self):
        status, content_type, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        text = body.decode("utf-8")
        self.assertIn("<title>HD PS38 Sports</title>", text)
        self.assertIn("static/app.js", text)
        self.assertIn('href="#/pool"', text)
        self.assertIn("号池管理", text)
        self.assertIn('id="appPageTitle"', text)
        self.assertIn('data-page-actions="pool"', text)

        status, content_type, body = self.get("/static/app.js")
        self.assertEqual(status, 200)
        self.assertIn("text/javascript", content_type)
        self.assertIn(b"bindMyEventsEvents", body)

    def test_orders_api(self):
        status, _content_type, body = self.get("/api/my-events?status=ALL&limit=50")
        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["total"], 3)
        self.assertEqual(len(payload["rows"]), 3)
        self.assertEqual(payload["rows"][0]["order_id"], "3")
        self.assertEqual(payload["agents"][0]["code"], "A1")
        self.assertIn("market_scope", payload["rows"][0])

    def test_summary_api(self):
        _status, _ct, body = self.get("/api/my-events/summary")
        payload = json.loads(body)
        self.assertEqual(payload["orders"], 3)
        self.assertEqual(payload["open"], 2)
        self.assertEqual(payload["settled"], 1)
        self.assertEqual(payload["total_stake"], 300.0)

    def test_fixtures_and_detail_api(self):
        _status, _ct, body = self.get("/api/my-events/fixtures?status=ALL")
        payload = json.loads(body)
        self.assertEqual(payload["total"], 1)
        fixture = payload["rows"][0]
        self.assertEqual(fixture["orders"], 3)

        _status, _ct, body = self.get(
            "/api/my-events/fixture?status=ALL&teams="
            + quote("布拉干萨竞技 -vs- 桑托斯")
            + "&event=" + str(fixture["event_ms"])
        )
        detail = json.loads(body)
        self.assertEqual(detail["total"], 3)
        self.assertIsNone(detail["crown"])

    def test_copy_groups_api(self):
        _status, _ct, body = self.get("/api/my-events/copy-groups?status=ALL&limit=50")
        payload = json.loads(body)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["rows"][0]["orders"], 3)
        self.assertEqual(payload["rows"][0]["members"], 1)
        self.assertEqual(payload["window_ms"], 20000)

    def test_status_api(self):
        _status, _ct, body = self.get("/api/status")
        payload = json.loads(body)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["session"]["status"], "online")
        self.assertEqual(payload["counts"]["total"], 3)
        self.assertEqual(payload["config"]["host"], "127.0.0.1")
        self.assertNotIn("password", payload["config"])
        self.assertNotIn("username", payload["config"])

    def test_actions(self):
        _status, payload = self.post("/api/actions/refresh")
        self.assertTrue(payload["ok"])
        self.assertEqual(self.poller.refresh_calls, 1)
        _status, payload = self.post("/api/actions/backfill")
        self.assertTrue(payload["ok"])
        self.assertEqual(self.poller.backfill_calls, 1)
        _status, payload = self.post("/api/actions/login")
        self.assertTrue(payload["ok"])
        self.assertEqual(self.poller.login_calls, 1)
        _status, payload = self.post("/api/actions/open-login")
        self.assertTrue(payload["ok"])
        self.assertEqual(self.poller.show_window_calls, 1)

    def test_post_keeps_connection_usable(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request("POST", "/api/actions/refresh", body=b"{}",
                               headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()

            connection.request("GET", "/api/health")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertTrue(json.loads(response.read())["ok"])
        finally:
            connection.close()

    def test_account_pool_api(self):
        class FakePool:
            def __init__(self):
                self.logins = 0
                self.checks = 0

            def snapshot(self):
                return {"ok": True, "accounts": [{"id": "a1", "name": "账号一", "status": "online"}],
                        "logs": [], "summary": {"total": 1}}

            def save_account(self, payload, account_id=None):
                return {"id": account_id or "a1", "name": payload.get("name", ""), "status": "idle"}

            def login(self, account_id, force=False):
                self.logins += 1
                return {"message": "login started"}

            def check(self, account_id):
                self.checks += 1
                return {"message": "check started"}

            def check_all(self):
                self.checks += 1
                return {"message": "check all started"}

            def clear_session(self, account_id):
                return {"id": account_id, "has_session": False, "status": "idle"}

            def delete_account(self, account_id):
                return None

            def toggle(self, account_id, enabled):
                return {"id": account_id, "enabled": enabled, "status": "idle" if enabled else "disabled"}

            def cancel(self, account_id):
                return {"message": "cancelled"}

        pool = FakePool()
        self.server.pool = pool
        try:
            status, _content_type, body = self.get("/api/pool")
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body)["ok"])

            status, payload = self.post_body("/api/pool/save", {"platform": "crown_member", "name": "账号一"})
            self.assertEqual(status, 200)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["account"]["name"], "账号一")

            _status, payload = self.post_body("/api/pool/login", {"id": "a1"})
            self.assertTrue(payload["ok"])
            self.assertEqual(pool.logins, 1)

            _status, payload = self.post_body("/api/pool/check", {"id": "a1"})
            self.assertTrue(payload["ok"])
            self.assertEqual(pool.checks, 1)

            _status, payload = self.post_body("/api/pool/clear", {"id": "a1"})
            self.assertTrue(payload["ok"])
            self.assertFalse(payload["account"]["has_session"])
        finally:
            self.server.pool = None

    def test_account_pool_is_unavailable_without_service(self):
        status, _content_type, body = self.get("/api/pool")
        self.assertEqual(status, 503)
        self.assertFalse(json.loads(body)["ok"])

    def test_unknown_api_is_404_static(self):
        status, _content_type, _body = self.get("/api/does-not-exist")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
