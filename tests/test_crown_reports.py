"""皇冠会员报表入库：解析、去重、增量更新。"""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from ps38app.crown_reports import CrownReportStore, ReportSyncService, report_db_path, sync_account

DATES_XML = (
    "<serverresponse><code>607</code>"
    "<date>2026-11-05</date><date>2026-11-04</date>"
    "</serverresponse>"
).encode()


def wager_xml(ticket, stake, win_gold, result="主队", result_data="2 - 0", league="测试杯"):
    return (
        f'<wagers tid="OU{ticket}"><w_id>OU{ticket}</w_id>'
        f"<addtime>12:00:00</addtime><league>{league}</league>"
        f"<team_h_show>主队</team_h_show><team_c_show>客队</team_c_show>"
        f"<wtype>让球</wtype><ioratio>0.90</ioratio><gold>{stake}</gold>"
        f"<win_gold>{win_gold}</win_gold><result>{result}</result>"
        f"<result_data>{result_data}</result_data><score>(0 - 0)</score></wagers>"
    )


class FakeAdapter:
    def __init__(self):
        self.win_gold = {"111": "10", "222": "-5"}
        self.today_wagers = []

    def _load_session(self, _account_id):
        return {"uid": "u", "ver": "v"}

    def _new_opener(self, _session):
        return None

    def _post(self, _session, fields, _opener, deadline=None):
        action = fields["p"]
        if action == "get_history_data":
            return DATES_XML
        if action == "get_today_wagers":
            return json.dumps({"wagers": self.today_wagers}).encode()
        if action == "history_switch":
            if fields["today_gmt"] != "2026-11-05":
                return b"<serverresponse><code>609</code></serverresponse>"
            return (
                "<serverresponse><code>609</code>"
                + wager_xml("111", "100", self.win_gold["111"])
                + wager_xml("222", "50", self.win_gold["222"])
                + "</serverresponse>"
            ).encode()
        raise AssertionError(action)


class SyncServiceTests(unittest.TestCase):
    def test_stop_interrupts_remaining_dates_and_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CrownReportStore(Path(tmp) / "reports.sqlite3")
            adapter = FakeAdapter()
            service = ReportSyncService(
                {},
                store=store,
                adapter=adapter,
                account_provider=lambda: [
                    {"id": "first", "status": "online"},
                    {"id": "second", "status": "online"},
                ],
            )
            original_post = adapter._post

            def stop_during_today(session, fields, opener, deadline=None):
                result = original_post(session, fields, opener, deadline)
                if fields["p"] == "get_today_wagers":
                    service.stop()
                return result

            with mock.patch.object(adapter, "_post", side_effect=stop_during_today) as post:
                results = service.run_once()["results"]
            self.assertEqual(len(results), 1)
            self.assertIn("已停止", results[0]["error"])
            self.assertEqual(post.call_count, 2)
            self.assertEqual(store.rows(), [])

    def test_service_scans_online_accounts_and_uses_full_days_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CrownReportStore(Path(tmp) / "reports.sqlite3")
            adapter = FakeAdapter()
            accounts = [
                {"id": "online-1", "name": "在线会员", "status": "online"},
                {"id": "offline-1", "name": "离线会员", "status": "expired"},
            ]
            service = ReportSyncService(
                {"pool_db_path": str(Path(tmp) / "accounts.sqlite3")},
                store=store,
                adapter=adapter,
                account_provider=lambda: accounts,
                interval=60,
            )
            first = service.run_once()["results"]
            self.assertEqual(first[0]["inserted"], 2)
            self.assertEqual(first[0]["days"], 8)
            self.assertIn("不在线", first[1]["error"])
            self.assertEqual(service.status()["known_accounts"], ["online-1"])

            second = service.run_once()["results"]
            self.assertEqual(second[0]["days"], 2)
            self.assertEqual(second[0]["skipped"], 2)
            self.assertEqual(second[0]["inserted"], 0)


class StoreTests(unittest.TestCase):
    def test_report_database_relative_path_uses_project_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("ps38app.config.PROJECT_DIR", Path(tmp)):
                self.assertEqual(
                    report_db_path({"crown_report_db_path": "custom/reports.sqlite3"}),
                    Path(tmp) / "custom/reports.sqlite3",
                )

    def test_settled_today_wager_preserves_numeric_zero_profit(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CrownReportStore(Path(tmp) / "reports.sqlite3")
            adapter = FakeAdapter()
            adapter.today_wagers = [
                {
                    "w_id": "OU333",
                    "gold": 80,
                    "win_gold": 0,
                    "result_data": "1 - 1",
                    "adddate": "2026-11-06",
                }
            ]
            stats = sync_account(adapter, store, "acct")
            self.assertEqual(stats["error"], "")
            totals = store.totals("2026-11-06", "2026-11-06")
            self.assertEqual((totals["settled"], totals["pending"], totals["pushes"]), (1, 0, 1))

    def test_network_reads_do_not_hold_a_database_write_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CrownReportStore(Path(tmp) / "reports.sqlite3")
            adapter = FakeAdapter()
            original_post = adapter._post

            def concurrent_write(session, fields, opener, deadline=None):
                if fields["p"] == "history_switch":
                    with closing(sqlite3.connect(store.path, timeout=0.05)) as connection:
                        connection.execute(
                            "INSERT OR REPLACE INTO crown_sync(account_id) VALUES ('other')"
                        )
                        connection.commit()
                return original_post(session, fields, opener, deadline)

            with mock.patch.object(adapter, "_post", side_effect=concurrent_write):
                stats = sync_account(adapter, store, "acct")
            self.assertEqual(stats["error"], "")
            self.assertEqual(stats["inserted"], 2)

    def test_failed_transaction_does_not_report_rolled_back_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CrownReportStore(Path(tmp) / "reports.sqlite3")
            original_upsert = store.upsert

            def fail_second(connection, account_id, fields):
                if fields["ticket"] == "222":
                    raise RuntimeError("写入失败")
                return original_upsert(connection, account_id, fields)

            with mock.patch.object(store, "upsert", side_effect=fail_second):
                stats = sync_account(FakeAdapter(), store, "acct")
            self.assertIn("写入失败", stats["error"])
            self.assertEqual((stats["inserted"], stats["updated"], stats["skipped"]), (0, 0, 0))
            self.assertEqual(store.rows(), [])

    def test_sync_is_idempotent_and_updates_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CrownReportStore(Path(tmp) / "reports.sqlite3")
            adapter = FakeAdapter()

            first = sync_account(adapter, store, "acct", days=8)
            self.assertEqual((first["inserted"], first["updated"], first["skipped"]), (2, 0, 0))
            summary = store.summary("acct")
            self.assertEqual(summary[0]["orders"], 2)
            self.assertAlmostEqual(summary[0]["stake"], 150.0)
            self.assertAlmostEqual(summary[0]["win_loss"], 5.0)

            second = sync_account(adapter, store, "acct", days=8)
            self.assertEqual((second["inserted"], second["updated"], second["skipped"]), (0, 0, 2))

            adapter.win_gold["111"] = "12"
            third = sync_account(adapter, store, "acct", days=8)
            self.assertEqual((third["inserted"], third["updated"], third["skipped"]), (0, 1, 1))
            self.assertAlmostEqual(store.summary("acct")[0]["win_loss"], 7.0)

    def test_today_wagers_are_pending_until_result_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CrownReportStore(Path(tmp) / "reports.sqlite3")
            adapter = FakeAdapter()
            adapter.today_wagers = [
                {
                    "w_id": "OU333",
                    "gold": "80",
                    "win_gold": "60",
                    "result_data": "",
                    "adddate": "2026-11-06",
                    "addtime": "10:00:00",
                    "league": "测试杯",
                    "team_h_show": "主队",
                    "team_c_show": "客队",
                    "wtype": "让球",
                    "ioratio": "0.9",
                    "result": "主队",
                    "score": "(0 - 0)",
                }
            ]
            sync_account(adapter, store, "acct", days=8)
            stats = store.totals("2026-11-06", "2026-11-06")
            self.assertEqual((stats["total"], stats["settled"], stats["pending"]), (1, 0, 1))
            self.assertEqual(stats["pnl"], 0.0)

    def test_account_failure_is_recorded_without_raising(self):
        class Broken:
            def _load_session(self, _account_id):
                raise ValueError("会话失效")

        with tempfile.TemporaryDirectory() as tmp:
            store = CrownReportStore(Path(tmp) / "reports.sqlite3")
            stats = sync_account(Broken(), store, "acct", days=8)
            self.assertIn("会话失效", stats["error"])
            states = store.sync_states()
            self.assertEqual(states[0]["account_id"], "acct")
            self.assertIn("会话失效", states[0]["last_error"])
