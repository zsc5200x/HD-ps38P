"""未知结果只读核对，全部响应来自本地构造的脱敏样本。"""

import copy
import json
import unittest
from unittest.mock import patch

from ps38app.follow.crown import CrownAdapter, FollowPreparationError
from ps38app.follow.crown_markets import (
    game_rows,
    parse_receipt,
    select_market,
    verify_context,
    xml_root,
)
from ps38app.follow.reconcile import reconcile_response
from tests.test_follow_crown import BOARD, GAME, INTENT, SESSION, response


def attempt():
    quote = select_market([BOARD], GAME, INTENT)
    quote.update(account_id="test-account", currency="CNY")
    return {
        "id": 1,
        "account_id": "test-account",
        "started_at": "2026-11-03T12:00:00.000+08:00",
        "stake": "100",
        "target": copy.deepcopy(INTENT),
        "submitted_quote": quote,
    }


def wager(**changes):
    return {
        "ticket_id": "VERIFIED-1",
        "member_id": "test-account",
        "gid": "line-1",
        "master_gid": "match-1",
        "gtype": "FT",
        "wtype": "RE",
        "rtype": "REH",
        "chose_team": "H",
        "line": "-0.5",
        "currency": "CNY",
        "golds": "100",
        "bet_ts": "2026-11-03T12:00:01+08:00",
        "status": "OPEN",
        **changes,
    }


def envelope(*rows, **changes):
    return {"ok": True, "complete": True, "wagers": list(rows), **changes}


class StrictReconcileTests(unittest.TestCase):
    def check(self, payload, expected="UNKNOWN", submitted=None):
        result = reconcile_response(payload, submitted or attempt(), "test-account")
        self.assertEqual(result["status"], expected, result)
        self.assertFalse(result["retryable"])
        self.assertFalse(result["request_sent"])
        return result

    def test_unique_full_fingerprint_confirms_ticket_without_invented_odds(self):
        result = self.check(envelope(wager()), "SUCCEEDED")
        self.assertEqual(result["receipt_id"], "VERIFIED-1")
        self.assertEqual(result["accepted_stake"], "100")
        self.assertIsNone(result["accepted_odds"])
        self.assertEqual(
            self.check(envelope(wager(odds_format="HK", ioratio="0.88")), "SUCCEEDED")[
                "accepted_odds"
            ],
            "0.88",
        )

    def test_success_and_complete_query_markers_are_required(self):
        for payload in (
            [wager()],
            {"wagers": [wager()]},
            envelope(wager(), ok=False),
            envelope(wager(), complete=False),
            envelope(wager(), has_more=True),
            envelope(wager(), next_cursor="later"),
            envelope(wager(), page=2),
            envelope(wager(), total=2),
            envelope(wager(), code="error"),
            envelope(wager(), status="error"),
            envelope(wager(), has_more="true"),
            envelope(wager(), complete="true", total=1),
            {"ok": True, "wagers": [wager()]},
            {"ok": True, "complete": True, "data": {"other": [wager()]}},
        ):
            with self.subTest(payload=payload):
                self.check(payload)

    def test_total_count_can_establish_completeness_but_not_an_empty_failure(self):
        self.check({"code": 200, "total": 1, "wagers": [wager()]}, "SUCCEEDED")
        self.assertEqual(self.check(envelope())["category"], "RECONCILE_NOT_FOUND")
        self.check({"code": "error", "total": 0, "wagers": []})

    def test_same_amount_and_game_with_wrong_market_side_period_or_line_do_not_match(self):
        for changes in (
            {"gid": "another-line", "master_gid": "another-match"},
            {"wtype": "HRE", "rtype": "HREH"},
            {"wtype": "ROU", "rtype": "ROUC", "chose_team": "C", "line": "2.5"},
            {"rtype": "REC", "chose_team": "C", "line": "0.5"},
            {"line": "-0.25"},
            {"golds": "101"},
            {"is_live": False},
            {"bet_ts": "2026-11-03T11:59:00+08:00"},
            {"bet_ts": "2026-11-03T12:02:00+08:00"},
        ):
            with self.subTest(changes=changes):
                self.check(envelope(wager(**changes)))

    def test_competing_candidates_never_choose_the_closest_ticket(self):
        result = self.check(
            envelope(wager(), wager(ticket_id="VERIFIED-2", bet_ts="2026-11-03T12:00:02+08:00"))
        )
        self.assertEqual(result["category"], "RECONCILE_AMBIGUOUS")
        self.check(envelope(wager(), wager()))

    def test_incomplete_conflicting_or_unaccepted_candidate_keeps_unknown(self):
        for key in (
            "member_id",
            "gtype",
            "wtype",
            "chose_team",
            "line",
            "currency",
            "golds",
            "bet_ts",
            "status",
            "ticket_id",
        ):
            row = wager()
            row.pop(key)
            with self.subTest(missing=key):
                self.check(envelope(row))
        for changes in (
            {"member_id": "other-account"},
            {"uid": "different-session"},
            {"currency": "THB"},
            {"status": "PENDING"},
            {"status": "REJECTED"},
            {"accepted": True, "status": "REJECTED"},
            {"accepted": False, "status": "OPEN"},
            {"ticket_id": "<bad>"},
            {"bet_ts": "2026-11-03 12:00:01"},
            {"golds": "NaN"},
            {"master_gid": "other-match"},
            {"rtype": "REC"},
            {"period": "HT"},
            {"con": "1", "ratio": "0"},
            {"con": "1"},
            {"wtype": "UNVERIFIED", "period": "FT", "market_type": "HANDICAP", "is_live": True},
            {"bet_ts": "1e1000000"},
            {"con": "1e1000000", "ratio": "0"},
            {"odds_format": "HK", "ioratio": "1e-1000000"},
        ):
            with self.subTest(changes=changes):
                self.check(envelope(wager(**changes)))

    def test_incomplete_record_cannot_hide_another_matching_candidate(self):
        incomplete = wager(ticket_id="VERIFIED-2")
        incomplete.pop("line")
        self.check(envelope(wager(), incomplete))

    def test_explicit_master_to_line_relation_is_required(self):
        self.check(envelope(wager(gid="match-1")))
        self.check(envelope(wager(gid="match-1", market_gid="line-1")), "SUCCEEDED")

    def test_encoded_signed_handicap_and_split_line_remain_distinct(self):
        row = wager(con="1", ratio="100")
        row.pop("line")
        self.check(envelope(row), "SUCCEEDED")
        self.check(envelope(wager(line="0.5")))
        self.check(envelope(wager(line="-0/0.5")))

    def test_half_total_direction_and_encoded_line_are_checked(self):
        submitted = attempt()
        target = {**INTENT, "period": "HT", "market_type": "TOTAL", "side": "UNDER", "line": "0.75"}
        submitted["target"] = target
        submitted["submitted_quote"] = {
            **select_market([BOARD], GAME, target),
            "account_id": "test-account",
            "currency": "CNY",
        }
        row = wager(
            gid="half-line-1",
            wtype="HROU",
            rtype="HROUH",
            chose_team="H",
            line="0.75",
            con="-1",
            ratio="-50",
        )
        self.check(envelope(row), "SUCCEEDED", submitted)
        self.check(envelope({**row, "rtype": "HROUC", "chose_team": "C"}), submitted=submitted)

    def test_response_account_binding_and_uid_are_validated(self):
        row = wager()
        row.pop("member_id")
        self.check(envelope(row, account_id="test-account"), "SUCCEEDED")
        result = reconcile_response(
            envelope({**row, "uid": "fixture-uid"}),
            attempt(),
            "test-account",
            account_uid="fixture-uid",
        )
        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertNotIn("fixture-uid", json.dumps(result))
        self.check(envelope(wager(member_id=False), account_id="test-account"))
        self.check(envelope(wager(member_id=0), account_id="test-account"))

    def test_conflicting_original_attempt_never_confirms_result(self):
        for change in ("account", "target", "currency"):
            submitted = attempt()
            if change == "account":
                submitted["account_id"] = "other-account"
            elif change == "target":
                submitted["target"]["side"] = "AWAY"
            else:
                submitted["submitted_quote"]["currency"] = "THB"
            with self.subTest(change=change):
                self.check(envelope(wager()), submitted=submitted)

    def test_nested_response_with_verified_total_is_supported(self):
        self.check({"code": "200", "response": {"total": 1, "wagers": [wager()]}}, "SUCCEEDED")
        self.check(
            {
                "ok": True,
                "complete": True,
                "data": {"wagers": [wager()]},
                "response": {"wagers": [wager()]},
            }
        )
        self.check(
            {
                "ok": True,
                "account_id": "different",
                "response": {"account_id": "test-account", "complete": True, "wagers": [wager()]},
            }
        )

    def test_duplicate_json_fields_never_confirm_a_receipt(self):
        body = json.dumps(envelope(wager()))
        body = body.replace('"ok": true', '"ok": false, "ok": true')
        self.check(body)


class ReconcileAdapterTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.body = json.dumps(envelope(wager())).encode()
        self.elapsed = 0
        self.stopped = False
        self.adapter = CrownAdapter(
            {},
            transport=self.transport,
            session_loader=lambda _: dict(SESSION),
            clock=lambda: 1000,
            monotonic=lambda: self.elapsed,
            should_stop=lambda: self.stopped,
        )
        guard = patch("ps38app.follow.crown.build_opener", side_effect=AssertionError("禁止外网"))
        guard.start()
        self.addCleanup(guard.stop)

    def transport(self, url, fields, headers, timeout):
        self.calls.append(dict(fields))
        return 200, self.body

    def test_reconcile_only_reads_today_wagers_and_never_submits(self):
        result = self.adapter.reconcile(attempt(), "test-account")
        self.assertEqual(result["receipt_id"], "VERIFIED-1")
        self.assertEqual([item["p"] for item in self.calls], ["get_today_wagers"])
        self.assertEqual(self.calls[0]["format"], "json")

    def test_account_mismatch_or_stopped_service_does_not_make_a_request(self):
        self.assertEqual(self.adapter.reconcile(attempt(), "different")["status"], "UNKNOWN")
        self.stopped = True
        self.assertEqual(self.adapter.reconcile(attempt(), "test-account")["status"], "UNKNOWN")
        self.assertEqual(self.calls, [])

    def test_transport_failure_and_invalid_response_keep_unknown_without_secrets(self):
        for body in (
            b"not-json fixture-secret",
            b"<serverresponse><code>error</code></serverresponse>",
        ):
            self.body = body
            result = self.adapter.reconcile(attempt(), "test-account")
            self.assertEqual(result["status"], "UNKNOWN")
            self.assertNotIn("fixture-secret", json.dumps(result))
        self.adapter._transport = lambda *_args: (_ for _ in ()).throw(
            TimeoutError("fixture-secret")
        )
        result = self.adapter.reconcile(attempt(), "test-account")
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertNotIn("fixture-secret", json.dumps(result))

    def test_only_allowlisted_explicit_rejection_is_retryable(self):
        for fields, status, retryable in (
            ({"code": "555", "errormsg": "1X015"}, "REJECTED", True),
            ({"code": "555", "errormsg": "unknownError"}, "REJECTED", False),
            ({"code": "555", "errormsg": "1X015", "ticket_id": "MAYBE-1"}, "UNKNOWN", False),
            ({"code": "560", "ticket_id": "OK-1"}, "SUCCEEDED", False),
            ({"code": "200"}, "UNKNOWN", False),
        ):
            with self.subTest(fields=fields):
                result = parse_receipt(response(fields))
                self.assertEqual(
                    (result["status"], result["retryable"], result["request_sent"]),
                    (status, retryable, True),
                )

    def test_missing_market_is_retryable_but_ambiguous_market_is_not(self):
        with self.assertRaises(FollowPreparationError) as missing:
            select_market([], GAME, INTENT)
        self.assertTrue(missing.exception.retryable)
        with self.assertRaises(FollowPreparationError) as ambiguous:
            select_market([BOARD, BOARD], GAME, INTENT)
        self.assertFalse(ambiguous.exception.retryable)

    def test_transient_response_and_session_errors_have_stable_categories(self):
        for action, category in (
            (lambda: xml_root(b"gateway returned html"), "RESPONSE_INVALID"),
            (lambda: game_rows(response({"code": "error"})), "ACCOUNT_SESSION"),
        ):
            with self.subTest(category=category):
                with self.assertRaises(FollowPreparationError) as caught:
                    action()
                self.assertEqual(caught.exception.category, category)
                self.assertTrue(caught.exception.retryable)
                self.assertFalse(caught.exception.request_sent)

    def test_context_keeps_only_actual_amount_limits_and_rejects_invalid_range(self):
        quote = select_market([BOARD], GAME, INTENT)
        fields = {
            "code": "501",
            "fast_check": "REH",
            "concede": "1",
            "ratio": "100",
            "ioratio": "0.89",
        }
        confirmed = verify_context(
            response({**fields, "gold_gmin": "50", "gold_gmax": "500"}), quote
        )
        self.assertEqual((confirmed["min_stake"], confirmed["max_stake"]), ("50", "500"))
        self.assertNotIn("min_stake", verify_context(response(fields), quote))
        with self.assertRaises(FollowPreparationError):
            verify_context(response({**fields, "gold_gmin": "500", "gold_gmax": "50"}), quote)


if __name__ == "__main__":
    unittest.main()
