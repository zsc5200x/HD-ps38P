import tempfile
import unittest
from pathlib import Path

from ps38app import store


def upstream_row(order_id="1001", member="M001", status="OPEN", stake=100.0,
                 to_win=90.0, wager_status="", created=1_760_000_000_000,
                 settled=None, market="OU", handicap="2.5", selection="UNDER",
                 league="巴西 - 甲级联赛", teams="布拉干萨竞技 -vs- 桑托斯",
                 score=""):
    row = {
        "playerWagerId": order_id,
        "userCode": member,
        "firstName": "会员" + member,
        "status": status,
        "stake": stake,
        "toWin": to_win,
        "odds": 0.98,
        "currencyCode": "CNY",
        "wagerStatusTranslated": wager_status,
        "wagerStatus": "",
        "createdDate": created,
        "choices": [{
            "btName": market,
            "handicap": handicap,
            "selection": selection,
            "selectionName": "",
            "leagueName": league,
            "eventName": teams,
            "eventDate": created + 3_600_000,
            "inplay": True,
            "score": score,
        }],
    }
    if settled is not None:
        row["settlementDateTime"] = settled
    return row


class ParsePageTests(unittest.TestCase):
    def response(self, rows, total, page=1, size=50, pages=None):
        return {
            "httpStatus": 200,
            "json": {"body": {
                "data": rows,
                "paging": {
                    "totalItems": total,
                    "totalPages": pages if pages is not None else (0 if total == 0 else 1),
                    "rowsPerPage": size,
                    "beginIndex": (page - 1) * size,
                    "pageNumber": page,
                },
            }},
        }

    def test_valid_page(self):
        rows, meta = store.parse_page(
            self.response([upstream_row("1"), upstream_row("2")], 2), 1, 50, "OPEN"
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["status"], "OPEN")
        self.assertEqual(meta, (2, 1, 50))

    def test_duplicate_rows_abort(self):
        with self.assertRaises(store.IncompletePage):
            store.parse_page(
                self.response([upstream_row("1"), upstream_row("1")], 2), 1, 50, "OPEN"
            )

    def test_row_count_mismatch_aborts(self):
        with self.assertRaises(store.IncompletePage):
            store.parse_page(self.response([upstream_row("1")], 2), 1, 50, "OPEN")

    def test_unauthorized_is_session_error(self):
        with self.assertRaises(store.SessionExpired):
            store.parse_page({"httpStatus": 401, "json": None}, 1, 50, "OPEN")

    def test_bad_page_size_aborts(self):
        response = self.response([upstream_row("1")], 1)
        response["json"]["body"]["paging"]["rowsPerPage"] = 20
        with self.assertRaises(store.IncompletePage):
            store.parse_page(response, 1, 50, "OPEN")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "orders.sqlite3"
        self.store = store.OrderStore(self.db_path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def commit(self, agent, kind, rows):
        return self.store.commit_snapshot(agent, kind, rows, {
            "started_at": store.now_iso(),
            "finished_at": store.now_iso(),
            "elapsed_seconds": 0.1,
            "request_count": 1,
        })

    def test_open_baseline_then_new_event(self):
        first = self.commit("A1", "OPEN", [upstream_row("1"), upstream_row("2")])
        self.assertEqual(first["new"], 2)
        self.assertEqual(first["events"], 0)  # baseline never banners
        second = self.commit("A1", "OPEN", [
            upstream_row("1"), upstream_row("2"), upstream_row("3"),
        ])
        self.assertEqual(second["new"], 1)
        self.assertEqual(second["events"], 1)
        events = self.store.db.execute("SELECT order_id FROM partner_order_events").fetchall()
        self.assertEqual([row["order_id"] for row in events], ["3"])

    def test_settlement_updates_result_and_never_reopens(self):
        self.commit("A1", "OPEN", [upstream_row("1", stake=100, to_win=90)])
        self.commit("A1", "SETTLED", [
            upstream_row("1", status="SETTLED", stake=100, to_win=90,
                         wager_status="胜", settled=1_760_000_500_000),
        ])
        row = self.store.db.execute(
            "SELECT status, result, win_loss FROM partner_orders"
        ).fetchone()
        self.assertEqual(row["status"], "SETTLED")
        self.assertEqual(row["result"], "WIN")
        self.assertEqual(row["win_loss"], 90.0)

        # A stale OPEN response must not undo the settlement.
        self.commit("A1", "OPEN", [upstream_row("1", stake=100, to_win=90)])
        row = self.store.db.execute("SELECT status FROM partner_orders").fetchone()
        self.assertEqual(row["status"], "SETTLED")

    def test_summary_counts(self):
        self.commit("A1", "OPEN", [upstream_row("1", stake=100), upstream_row("2", stake=50)])
        self.commit("A1", "SETTLED", [
            upstream_row("1", status="SETTLED", stake=100, to_win=80,
                         wager_status="胜", settled=1_760_000_500_000),
            upstream_row("2", status="SETTLED", stake=50, to_win=45,
                         wager_status="负", settled=1_760_000_500_000),
        ])
        summary = store.query_summary(self.store.db)
        self.assertEqual(summary["orders"], 2)
        self.assertEqual(summary["total_stake"], 150.0)
        self.assertEqual(summary["open"], 0)
        self.assertEqual(summary["settled"], 2)
        self.assertEqual(summary["win_count"], 1)
        self.assertEqual(summary["lose_count"], 1)
        self.assertEqual(summary["net"], 30.0)  # +80 - 50

    def test_orders_and_fixtures_queries(self):
        self.commit("A1", "OPEN", [
            upstream_row("1", teams="A -vs- B", created=1_760_000_001_000),
            upstream_row("2", teams="A -vs- B", handicap="3.5", created=1_760_000_001_000),
            upstream_row("3", teams="C -vs- D", member="M002", created=1_760_000_003_000),
        ])
        orders = store.query_orders(self.store.db, "ALL", None, "", 50, 0)
        self.assertEqual(orders["total"], 3)
        self.assertEqual(orders["rows"][0]["order_id"], "3")  # newest first

        fixtures = store.query_fixtures(self.store.db, "ALL")
        self.assertEqual(fixtures["total"], 2)
        first = fixtures["rows"][0]
        self.assertEqual(first["teams"], "C -vs- D")
        ab = next(item for item in fixtures["rows"] if item["teams"] == "A -vs- B")
        self.assertEqual(ab["orders"], 2)
        self.assertEqual(ab["lines"], 2)
        self.assertEqual(ab["members"], 1)

        detail = store.query_fixture_orders(self.store.db, "A -vs- B", ab["event_ms"])
        self.assertEqual(detail["total"], 2)


class ConfigTests(unittest.TestCase):
    def test_defaults_and_agent_parsing(self):
        from ps38app import config as config_module

        self.assertEqual(config_module.DEFAULTS["port"], 8899)
        self.assertEqual(config_module._as_agents("A,B , C"), ["A", "B", "C"])


if __name__ == "__main__":
    unittest.main()


class CopyGroupTests(unittest.TestCase):
    """20-second merge: same match + same line + same score."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = store.OrderStore(Path(self.tmp.name) / "orders.sqlite3")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def order(self, order_id, created, **kwargs):
        """Same kickoff for every row: the merge key includes the event time."""
        row = upstream_row(order_id, created=created, **kwargs)
        row["choices"][0]["eventDate"] = 1_760_003_600_000
        return row

    def commit(self, rows):
        return self.store.commit_snapshot("A1", "OPEN", rows, {
            "started_at": store.now_iso(),
            "finished_at": store.now_iso(),
            "elapsed_seconds": 0.1,
            "request_count": 1,
        })

    def groups(self):
        return store.query_copy_groups(
            self.store.db, "ALL", None, "", 50, 0, None, None, basis="created"
        )

    def test_merge_within_window(self):
        base = 1_760_000_000_000
        self.commit([
            self.order("1", created=base, stake=100, score="0-0"),
            self.order("2", created=base + 3_000, stake=50, score="0-0", member="M002"),
            self.order("3", created=base + 19_999, stake=30, score="0-0", member="M003"),
        ])
        payload = self.groups()
        self.assertEqual(payload["total"], 1)
        group = payload["rows"][0]
        self.assertEqual(group["orders"], 3)
        self.assertEqual(group["stake"], 180.0)
        self.assertEqual(group["members"], 3)
        self.assertEqual(group["duration_ms"], 19_999)
        self.assertEqual(group["score_first"], "0-0")

    def test_after_window_splits(self):
        base = 1_760_000_000_000
        self.commit([
            self.order("1", created=base, stake=100, score="0-0"),
            self.order("2", created=base + 20_001, stake=50, score="0-0", member="M002"),
        ])
        payload = self.groups()
        self.assertEqual(payload["total"], 2)
        self.assertEqual(sorted(group["orders"] for group in payload["rows"]), [1, 1])

    def test_different_line_splits(self):
        base = 1_760_000_000_000
        self.commit([
            self.order("1", created=base, handicap="2.5", score="0-0"),
            self.order("2", created=base + 1_000, handicap="3.0", score="0-0", member="M002"),
        ])
        self.assertEqual(self.groups()["total"], 2)

    def test_different_score_splits(self):
        base = 1_760_000_000_000
        self.commit([
            self.order("1", created=base, score="0-0"),
            self.order("2", created=base + 1_000, score="1-0", member="M002"),
        ])
        self.assertEqual(self.groups()["total"], 2)

    def test_members_are_deduplicated(self):
        base = 1_760_000_000_000
        self.commit([
            self.order("1", created=base, member="M001", score="0-0"),
            self.order("2", created=base + 2_000, member="M001", score="0-0"),
        ])
        group = self.groups()["rows"][0]
        self.assertEqual(group["orders"], 2)
        self.assertEqual(group["members"], 1)


if __name__ == "__main__":
    unittest.main()
