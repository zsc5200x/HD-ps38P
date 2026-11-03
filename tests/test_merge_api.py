import http.client
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from ps38app import store
from ps38app.fixture_merge import prepare_ps38_event
from ps38app.server import DashboardServer
from tests.support import isolated_config, upcoming_today


class StubPoller:
    def snapshot(self):
        return {
            "status": "online",
            "message": "测试会话",
            "since": store.now_iso(),
            "last_error": "",
            "last_open_at": store.now_iso(),
            "last_settled_at": "",
            "last_cycle_seconds": 1.0,
            "profile": {"user_code": "TEST"},
            "agents": ["A1"],
            "poll_count": 1,
            "new_orders": 0,
            "backfill": {"running": False, "done": 0, "total": 0},
            "counts": {"live": 1, "open": 1, "settled": 0},
        }

    def request_refresh(self):
        pass

    def request_backfill(self):
        return False

    def request_relogin(self):
        pass

    def request_show_window(self):
        pass


EVENT_MS = int(upcoming_today().timestamp() * 1000)


def upstream_row():
    return {
        "playerWagerId": 9001,
        "userCode": "M001",
        "firstName": "会员一",
        "status": "OPEN",
        "stake": 100.0,
        "toWin": 90.0,
        "odds": 0.98,
        "currencyCode": "CNY",
        "createdDate": EVENT_MS - 600_000,
        "choices": [{
            "btName": "OU",
            "handicap": "2.5",
            "selection": "UNDER",
            "leagueName": "巴西 - 甲级联赛",
            "eventName": "布拉干萨竞技 -vs- 桑托斯",
            "eventDate": EVENT_MS,
            "inplay": False,
        }],
    }


def crown_label():
    local = datetime.fromtimestamp(EVENT_MS / 1000, timezone.utc).astimezone(
        timezone(timedelta(hours=-4))
    )
    return local.strftime("%m-%d %I:%M%p").lower()


CROWN_RAW = {
    "id": "crown-1",
    "league_id": "1001",
    "league": "巴西甲组联赛",
    "home": "布拉干萨竞技",
    "away": "桑托斯",
    "start_label": crown_label(),
    "is_live": False,
    "is_finished": False,
}


class MergeApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        db_path = Path(cls.tmp.name) / "orders.sqlite3"
        writable = store.OrderStore(db_path)
        stats = {
            "started_at": store.now_iso(), "finished_at": store.now_iso(),
            "elapsed_seconds": 0.1, "request_count": 1,
        }
        writable.commit_snapshot("A1", "OPEN", [upstream_row()], stats)
        writable.close()

        config = isolated_config(Path(cls.tmp.name))
        cls.server = DashboardServer(("127.0.0.1", 0), config, StubPoller())
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.tmp.cleanup()

    def setUp(self):
        event = prepare_ps38_event({
            "teams": "布拉干萨竞技 -vs- 桑托斯", "league": "巴西 - 甲级联赛",
            "event_ms": EVENT_MS,
        })
        event.update({"id": "pin:123", "source": "pinnacle"})
        patcher = mock.patch("ps38app.fixture_merge.fetch_pinnacle_events",
                             return_value=([event], None, store.now_iso()))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server.merge_service._crown_cache = None
        self.server.merge_service._pinnacle_cache = None
        self.server.merge_service.invalidate_payload()
        with self.server.merge_service.store._connect() as connection:
            connection.execute("DELETE FROM fixture_mappings")
            connection.execute("DELETE FROM league_mappings")

    def request(self, method, path, payload=None, *, wait_merge=True):
        deadline = time.monotonic() + 5
        while True:
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=8)
            try:
                body = json.dumps(payload).encode("utf-8") if payload is not None else b""
                headers = {"Content-Type": "application/json"} if payload is not None else {}
                connection.request(method, path, body=body, headers=headers)
                response = connection.getresponse()
                result = response.status, json.loads(response.read())
            finally:
                connection.close()
            if (not wait_merge or method != "GET" or path != "/api/merge"
                    or result[0] != 200 or result[1].get("ready") is not False
                    or time.monotonic() >= deadline):
                return result
            time.sleep(0.01)

    def reset_cache(self):
        self.server.merge_service._crown_cache = None

    def test_payload_and_manual_mapping(self):
        with mock.patch(
            "ps38app.fixture_merge.fetch_crown_events",
            return_value=([CROWN_RAW], None, store.now_iso()),
        ):
            status, payload = self.request("GET", "/api/merge")
            self.assertEqual(status, 200)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["window"]["scope"], "provider_live_today")
            self.assertEqual(payload["window"]["calendarBasis"], "provider")
            self.assertEqual(payload["sources"]["ps38"]["count"], 1)
            self.assertEqual(payload["sources"]["crown"]["count"], 1)
            self.assertEqual(len(payload["ps38Events"]), 1)
            self.assertTrue(payload["leagueSuggestions"])
            self.assertEqual(payload["suggestions"], [])
            self.assertEqual(payload["matchEvents"], [])
            self.assertEqual([e["id"] for e in payload["crownEvents"]], ["crown-1"])
            self.assertEqual(payload["matchingPerspective"], "crown")

            ps_league = payload["ps38Leagues"][0]
            crown_league = payload["crownLeagues"][0]
            status, result = self.request(
                "POST", "/api/merge/league-mapping",
                {"action": "map", "ps38LeagueId": ps_league["id"],
                 "crownLeagueId": crown_league["id"]},
            )
            self.assertEqual(status, 200)
            self.assertTrue(result["ok"])
            self.assertEqual(result["mapping"]["autoBound"], 1)

            status, result = self.request(
                "POST", "/api/merge/mapping",
                {"action": "map", "ps38Id": payload["ps38Events"][0]["id"],
                 "crownId": "crown-1"},
            )
            self.assertEqual(status, 200)
            self.assertTrue(result["ok"])

            self.reset_cache()
            status, refreshed = self.request("GET", "/api/merge")
            self.assertEqual(refreshed["counts"], {"leagueMappings": 1, "fixtureMappings": 1})
            self.assertEqual(len(refreshed["mappings"]), 1)
            self.assertEqual([e["id"] for e in refreshed["matchEvents"]], ["crown-1"])

            status, result = self.request(
                "POST", "/api/merge/mapping",
                {"action": "unmap", "ps38Id": payload["ps38Events"][0]["id"]},
            )
            self.assertEqual(status, 200)
            status, result = self.request(
                "POST", "/api/merge/league-mapping",
                {"action": "unmap", "crownLeagueId": crown_league["id"]},
            )
            self.assertEqual(status, 200)
            status, refreshed = self.request("GET", "/api/merge")
            self.assertEqual(refreshed["matchEvents"], [])
            self.assertEqual([e["id"] for e in refreshed["crownEvents"]], ["crown-1"])

    def test_cold_merge_request_returns_quickly_and_builds_only_once(self):
        self.reset_cache()
        self.server.merge_service.invalidate_payload()
        saved = self.server.merge_service.store.map_events(
            {"id": "pin:saved", "name": "旧主队 vs 旧客队"},
            {"id": "crown:saved", "name": "旧主队 vs 旧客队"},
        )
        entered = threading.Event()
        release = threading.Event()

        def slow_crown_fetch(_config):
            entered.set()
            release.wait(timeout=2)
            return [CROWN_RAW], None, store.now_iso()

        with mock.patch("ps38app.fixture_merge.fetch_crown_events", side_effect=slow_crown_fetch) as fetch:
            try:
                started = time.monotonic()
                status, initial = self.request("GET", "/api/merge", wait_merge=False)
                self.assertEqual(status, 200)
                self.assertFalse(initial["ready"])
                self.assertTrue(initial["refreshing"])
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertEqual(initial["counts"]["fixtureMappings"], 1)
                self.assertEqual(
                    [(item["ps38Id"], item["crownId"]) for item in initial["mappings"]],
                    [(saved["ps38Id"], saved["crownId"])],
                )
                self.assertTrue(entered.wait(timeout=1))

                status, repeated = self.request("GET", "/api/merge", wait_merge=False)
                self.assertEqual(status, 200)
                self.assertFalse(repeated["ready"])
                self.assertEqual(
                    [(item["ps38Id"], item["crownId"]) for item in repeated["mappings"]],
                    [(saved["ps38Id"], saved["crownId"])],
                )
                self.assertEqual(fetch.call_count, 1)

                release.set()
                status, ready = self.request("GET", "/api/merge")
                self.assertEqual(status, 200)
                self.assertTrue(ready["ready"])
                self.assertFalse(ready["refreshing"])
                self.assertEqual(ready["crownEvents"][0]["id"], "crown-1")
                self.assertEqual(
                    [(item["ps38Id"], item["crownId"]) for item in ready["mappings"]],
                    [(saved["ps38Id"], saved["crownId"])],
                )
            finally:
                release.set()

    def test_auto_merge_is_explicit_and_unique(self):
        self.reset_cache()
        with mock.patch(
            "ps38app.fixture_merge.fetch_crown_events",
            return_value=([CROWN_RAW], None, store.now_iso()),
        ):
            status, result = self.request("POST", "/api/merge/auto", {})
            self.assertEqual(status, 200)
            self.assertTrue(result["ok"])
            self.assertIn("mappedLeagues", result)
            self.assertIn("mappedFixtures", result)

    def test_library_unbind_preserves_merged_fixture_and_stops_matching(self):
        with mock.patch("ps38app.fixture_merge.fetch_crown_events",
                        return_value=([CROWN_RAW], None, store.now_iso())):
            status, payload = self.request("GET", "/api/merge")
            self.assertEqual(status, 200)
            crown_id = payload["crownLeagues"][0]["id"]
            status, result = self.request("POST", "/api/merge/league-mapping", {
                "action": "map", "ps38LeagueId": payload["ps38Leagues"][0]["id"],
                "crownLeagueId": crown_id,
            })
            self.assertEqual(status, 200)
            self.assertEqual(result["autoBound"], 1)
            status, result = self.request("POST", "/api/merge/league-mapping", {
                "action": "unmap", "crownLeagueId": crown_id,
            })
            self.assertEqual(status, 200)
            self.assertTrue(result["ok"])
            status, payload = self.request("GET", "/api/merge")
            self.assertEqual(status, 200)
            self.assertEqual(payload["leagueMappings"], [])
            self.assertEqual(payload["counts"], {"leagueMappings": 0, "fixtureMappings": 1})
            self.assertEqual(payload["matchEvents"], [])
            self.assertEqual(payload["suggestions"], [])
            self.assertEqual(payload["mappings"][0]["crownId"], "crown-1")
            status, _ = self.request("POST", "/api/merge/mapping", {
                "action": "map", "ps38Id": "pin:123", "crownId": "crown-1",
            })
            self.assertEqual(status, 400)
            status, _ = self.request("POST", "/api/merge/mapping", {
                "action": "unmap", "ps38Id": "pin:123",
            })
            self.assertEqual(status, 200)
            status, result = self.request("POST", "/api/merge/auto", {})
            self.assertEqual(status, 200)
            self.assertEqual(result["mappedFixtures"], [])

    def test_crown_unavailable_is_visible_but_page_loads(self):
        self.reset_cache()
        with mock.patch(
            "ps38app.fixture_merge.fetch_crown_events",
            return_value=([], "皇冠服务暂不可用；请先启动皇冠数据服务后重试。", ""),
        ):
            status, payload = self.request("GET", "/api/merge")
            self.assertEqual(status, 200)
            self.assertFalse(payload["sources"]["crown"]["ok"])
            self.assertIn("皇冠服务暂不可用", payload["crownError"])
            status, result = self.request(
                "POST", "/api/merge/league-mapping",
                {"action": "map", "ps38LeagueId": "p38:x", "crownLeagueId": "c1"},
            )
            self.assertEqual(status, 503)
            self.assertFalse(result["ok"])

    def test_invalid_action_and_body(self):
        status, result = self.request("POST", "/api/merge/mapping", {"action": "nope"})
        self.assertEqual(status, 400)
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
