"""实际服务、皇冠适配器与 HTTP 回执页接口的本地组合回归。"""

import http.client
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from ps38app.fixture_merge import FixtureMergeStore
from ps38app.follow.crown import CrownAdapter
from ps38app.follow.service import FollowService
from ps38app.server import DashboardServer
from ps38app.store import OrderStore
from tests import test_follow_core as source_samples
from tests import test_follow_crown as crown_samples
from tests.support import isolated_config
from tests.test_server import StubPoller


class FollowIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = isolated_config(Path(self.tmp.name))
        self.now = datetime(2026, 11, 3, 5, tzinfo=timezone.utc).timestamp()
        self.calls = []
        self.bet_exception = None
        self.wager_response = None
        self.receipt = crown_samples.response(
            {
                "code": "560",
                "ticket_id": "LOCAL-RECEIPT-1",
                "golds": "50",
                "ioratio": "0.93",
            }
        )
        guard = patch(
            "ps38app.follow.crown.build_opener", side_effect=AssertionError("组合测试禁止联网")
        )
        guard.start()
        self.addCleanup(guard.stop)
        self.orders = OrderStore(self.config["db_path"])
        self.addCleanup(self.orders.close)
        self.commit([])
        mapping_store = FixtureMergeStore(self.config["merge_db_path"])
        self.kickoff_ms = int(datetime(2026, 11, 3, 4, tzinfo=timezone.utc).timestamp() * 1000)
        pinnacle = {
            "id": "pin:101",
            "pinnacleId": "101",
            "leagueId": "p38:测试联赛",
            "league": "测试联赛",
            "home": "测试甲",
            "away": "测试乙",
            "kickoffUtc": "2026-11-03T04:00:00+00:00",
            "isLive": True,
        }
        crown = {
            **crown_samples.MAPPING["crownSnapshot"],
            "id": "match-1",
            "leagueId": "league-1",
            "league": "测试联赛",
            "isLive": True,
            "reversedOrder": False,
        }
        mapping_store.map_leagues(
            {"id": pinnacle["leagueId"], "name": "测试联赛"},
            {"id": crown["leagueId"], "name": "测试联赛"},
        )
        mapping_store.map_events(pinnacle, crown)
        adapter = CrownAdapter(
            self.config,
            transport=self.transport,
            session_loader=lambda account: {**crown_samples.SESSION, "id": account},
            clock=lambda: self.now,
            account_provider=lambda: ["test-account"],
        )
        self.service = FollowService(self.config, executor=adapter, clock=lambda: self.now)
        poller = StubPoller({"total": 0, "open": 0, "settled": 0, "live": 0})
        self.server = DashboardServer(("127.0.0.1", 0), self.config, poller)
        if self.server.follow_service:
            self.server.follow_service.stop()
        self.server.follow_service = self.service
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def commit(self, rows):
        self.orders.commit_snapshot(
            "A",
            "OPEN",
            rows,
            {
                "started_at": "2026-11-03T13:00:00+08:00",
                "finished_at": "2026-11-03T13:00:01+08:00",
                "elapsed_seconds": 1,
                "request_count": 1,
            },
        )

    def transport(self, url, fields, headers, timeout):
        self.calls.append(dict(fields))
        action = fields["p"]
        if action == "get_member_data":
            return 200, crown_samples.response({"enable": "Y", "currency": "CNY"})
        if action == "get_game_list":
            return 200, crown_samples.games_xml([crown_samples.GAME])
        if action == "get_game_more":
            return 200, crown_samples.games_xml([crown_samples.BOARD])
        if action == "FT_order_view":
            return 200, crown_samples.response(
                {
                    "code": "501",
                    "fast_check": "REC",
                    "concede": "-1",
                    "ratio": "-100",
                    "ioratio": "0.93",
                }
            )
        if action == "FT_bet":
            self.assertEqual((fields["gid"], fields["chose_team"]), ("line-1", "C"))
            self.assertEqual(
                (fields["con"], fields["ratio"], fields["golds"]), ("-1", "-100", "50")
            )
            if self.bet_exception is not None:
                raise self.bet_exception
            return 200, self.receipt
        if action == "get_today_wagers" and self.wager_response is not None:
            return 200, self.wager_response
        raise AssertionError("出现未模拟的皇冠请求")

    def request(self, method, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(
                method,
                path,
                None if body is None else json.dumps(body),
                {
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def process_group(self, currency="CNY"):
        status, _ = self.request(
            "POST",
            "/api/follow/rule",
            {
                "enabled": True,
                "direction": "reverse",
                "amount_mode": "ratio",
                "amount_value": "10",
                "min_stake": 0,
                "account_id": "test-account",
            },
        )
        self.assertEqual(status, 200)
        rows = []
        for order_id, stake, seconds in (("source-1", "300", 0), ("source-2", "200", 0)):
            self.now += seconds
            row = source_samples.raw_order(
                order_id, order_id, int(self.now * 1000), stake=stake, currencyCode=currency
            )
            row["choices"][0]["eventDate"] = self.kickoff_ms
            rows.append(row)
            self.commit(rows)
            self.service.run_once()
        # 首单进入 10 秒待跟单窗口，同批次第二单并入后才冻结推送。
        self.now += source_samples.PENDING_SECONDS
        self.service.run_once()
        page = self.request("GET", "/api/follow")[1]
        self.assertEqual(page["total"], 1)
        group = page["rows"][0]
        self.assertEqual(len(group["source_batch_no"]), 8)
        self.assertTrue(group["source_batch_no"].isdigit())
        self.assertEqual((group["order_count"], group["source_stake"]), (2, "500"))

    def test_merged_order_reaches_real_adapter_and_confirmed_receipt_api(self):
        self.process_group()
        status, page = self.request("GET", "/api/follow")
        self.assertEqual(status, 200)
        self.assertEqual(page["total"], 1)
        group = page["rows"][0]
        self.assertEqual((group["status"], group["receipt_id"]), ("SUCCEEDED", "LOCAL-RECEIPT-1"))
        self.assertEqual(group["account_id"], "test-account")
        self.assertEqual(
            (group["order_count"], group["source_stake"], group["accepted_stake"]),
            (2, "500", "50"),
        )
        self.assertEqual((group["target_side"], group["target_line"]), ("AWAY", "0.5"))
        status, detail = self.request("GET", f"/api/follow/groups/{group['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(
            [member["order_id"] for member in detail["members"]], ["source-1", "source-2"]
        )
        self.assertEqual(len(detail["attempts"]), 1)
        for secret in ("fixture-uid", "fixture-ver", "fixture=cookie", "raw_json"):
            self.assertNotIn(secret, json.dumps(detail))
        self.service.run_once()
        self.assertEqual(sum(call["p"] == "FT_bet" for call in self.calls), 1)

    def test_uncertain_response_is_reported_as_failure_and_never_resubmitted(self):
        self.receipt = b"<html>local simulated gateway error</html>"
        self.process_group()
        self.assertEqual(self.request("GET", "/api/follow?status=SUCCEEDED")[1]["total"], 0)
        failed = self.request("GET", "/api/follow?status=FAILED")[1]
        self.assertEqual(failed["total"], 1)
        self.assertIsNone(failed["rows"][0]["receipt_id"])
        for _ in range(3):
            self.now += 5
            self.service.run_once()
        self.assertEqual(sum(call["p"] == "FT_bet" for call in self.calls), 1)

    def test_thb_source_amount_reaches_cny_account_without_exchange(self):
        self.process_group(currency="THB")
        status, page = self.request("GET", "/api/follow?status=SUCCEEDED")
        self.assertEqual(status, 200)
        group = page["rows"][0]
        self.assertEqual(
            (group["currency"], group["source_stake"], group["target_stake"]),
            ("CNY", "500", "50"),
        )
        self.assertEqual(group["receipt_id"], "LOCAL-RECEIPT-1")
        _, detail = self.request("GET", f"/api/follow/groups/{group['id']}")
        self.assertEqual(
            [member["reported_currency"] for member in detail["members"]], ["THB", "THB"]
        )
        self.assertEqual([member["currency"] for member in detail["members"]], ["CNY", "CNY"])
        self.assertEqual(sum(call["p"] == "FT_bet" for call in self.calls), 1)
