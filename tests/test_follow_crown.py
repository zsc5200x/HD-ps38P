"""皇冠适配器只使用临时号池与模拟响应，不访问实际站点。"""

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs
from xml.sax.saxutils import escape

from ps38app.account_pool.store import AccountStore
from ps38app.crown_fixtures import parse_games
from ps38app.fixture_merge import prepare_crown_event
from ps38app.follow.crown import CrownAdapter
from ps38app.follow.crown_markets import (
    encode_line,
    game_rows,
    parse_receipt,
    resolve_game,
    select_market,
    verify_context,
)
from tests.support import isolated_config


def fields_xml(fields):
    return "".join(f"<{key}>{escape(str(value))}</{key}>" for key, value in fields.items())


def response(fields):
    return ("<serverresponse>" + fields_xml(fields) + "</serverresponse>").encode()


def games_xml(rows):
    return (
        "<serverresponse>"
        + "".join('<game mode="FT">' + fields_xml(row) + "</game>" for row in rows)
        + "</serverresponse>"
    ).encode()


GAME = {
    "gid": "match-1",
    "lid": "league-1",
    "ecid": "event-1",
    "team_h_id": "team-1",
    "team_c_id": "team-2",
    "team_h": "本地主队",
    "team_c": "本地客队",
    "league": "测试联赛",
    "game_date_time": "2026-11-03T12:00:00+08:00",
    "score_h": "0",
    "score_c": "0",
}
MAPPING = {
    "ps38Id": "pin:1",
    "crownId": "match-1",
    "crownSnapshot": {
        "providerLeagueId": "league-1",
        "homeId": "team-1",
        "awayId": "team-2",
        "home": "本地主队",
        "away": "本地客队",
        "kickoffUtc": "2026-11-03T04:00:00+00:00",
    },
}
BOARD = {
    **GAME,
    "gid": "line-1",
    "hgid": "half-line-1",
    "mode": "FT",
    "sw_re": "Y",
    "ratio_re": "0.5",
    "strong": "H",
    "ior_reh": "0.90",
    "ior_rec": "0.95",
    "sw_hre": "Y",
    "ratio_hre": "0/0.5",
    "hstrong": "C",
    "ior_hreh": "0.88",
    "ior_hrec": "0.92",
    "sw_rou": "Y",
    "ratio_rouo": "2/2.5",
    "ratio_rouu": "2/2.5",
    "ior_rouc": "0.98",
    "ior_rouh": "0.84",
    "sw_hrou": "Y",
    "ratio_hrouo": "0.5/1",
    "ratio_hrouu": "0.5/1",
    "ior_hrouc": "0.96",
    "ior_hrouh": "0.86",
}
INTENT = {
    "period": "FT",
    "market_type": "HANDICAP",
    "side": "HOME",
    "line": "-0.5",
    "is_live": True,
    "score": "0:0",
}
SESSION = {
    "id": "test-account",
    "name": "测试会员",
    "origin": "https://example.test",
    "uid": "fixture-uid",
    "ver": "fixture-ver",
    "cookie": "fixture=cookie",
}


class MarketParsingTests(unittest.TestCase):
    def test_identity_ids_survive_feed_and_merge_snapshot(self):
        game = {key.upper(): value for key, value in GAME.items()}
        rows = parse_games(games_xml([game]), live=True)
        saved = prepare_crown_event(rows[0])
        self.assertEqual(
            (saved["homeId"], saved["awayId"], saved["ecid"]), ("team-1", "team-2", "event-1")
        )

    def test_split_line_sign_and_half_strong_are_not_interchangeable(self):
        quote = select_market([BOARD], GAME, INTENT)
        self.assertEqual(
            (quote["gid"], quote["master_gid"], quote["line"]), ("line-1", "match-1", "-0.5")
        )
        with self.assertRaises(ValueError):
            select_market([BOARD], GAME, {**INTENT, "line": "0.5"})
        half = select_market([BOARD], GAME, {**INTENT, "period": "HT", "line": "0.25"})
        self.assertEqual(
            (half["gid"], half["wtype"], half["side"], half["line"]),
            ("half-line-1", "HRE", "HOME", "0.25"),
        )
        fallback = select_market(
            [{key: value for key, value in BOARD.items() if key != "hgid"}],
            GAME,
            {**INTENT, "period": "HT", "line": "0.25"},
        )
        self.assertEqual((fallback["gid"], fallback["rtype"]), ("line-1", "HREH"))
        with self.assertRaises(ValueError):
            verify_context(
                response(
                    {
                        "code": "501",
                        "gid": "line-1",
                        "rtype": "REH",
                        "con": fallback["con"],
                        "ratio": fallback["ratio"],
                        "ioratio": "0.89",
                    }
                ),
                fallback,
            )

    def test_total_side_has_its_own_line_and_direction(self):
        for side, choice, encoded in (("OVER", "C", ("2", "-50")), ("UNDER", "H", ("-2", "50"))):
            quote = select_market(
                [BOARD], GAME, {**INTENT, "market_type": "TOTAL", "side": side, "line": "2.25"}
            )
            self.assertEqual(quote["chose_team"], choice)
            self.assertEqual((quote["con"], quote["ratio"]), encoded)
        with self.assertRaises(ValueError):
            select_market(
                [{**BOARD, "ratio_rouu": "3.5"}],
                GAME,
                {**INTENT, "market_type": "TOTAL", "side": "UNDER", "line": "2.25"},
            )

    def test_zero_line_is_preserved(self):
        quote = select_market(
            [{**BOARD, "ratio_re": "0", "strong": ""}], GAME, {**INTENT, "line": "0"}
        )
        self.assertEqual((quote["con"], quote["ratio"]), ("0", "0"))

    def test_ambiguous_closed_corner_and_wrong_identity_are_rejected(self):
        bad_rows = (
            [BOARD, BOARD],
            [{**BOARD, "sw_re": "N"}],
            [{**BOARD, "mode": "CN"}],
            [{**BOARD, "ecid": "other-event"}],
            [{**BOARD, "strong": ""}],
            [{**BOARD, "ior_reh": "NaN"}],
            [{**BOARD, "score_h": "1"}],
        )
        for rows in bad_rows:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                select_market(rows, GAME, INTENT)

    def test_fixture_identity_requires_date_names_and_unique_ids(self):
        self.assertEqual(resolve_game([GAME], MAPPING, INTENT), GAME)
        for rows in (
            [GAME, GAME],
            [{**GAME, "team_h": "另一队"}],
            [{**GAME, "game_date_time": "2026-11-04T12:00:00+08:00"}],
            [{**GAME, "team_h_id": "other-team"}],
            [{**GAME, "score_h": "1"}],
        ):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                resolve_game(rows, MAPPING, INTENT)

    def test_context_rebinds_to_current_member_quote_and_validates_structure(self):
        quote = select_market([BOARD], GAME, INTENT)
        context = {
            "code": "501",
            "fast_check": "REH",
            "concede": "1",
            "ratio": "100",
            "ioratio": "0.89",
        }
        self.assertEqual(verify_context(response(context), quote)["odds"], "0.89")
        for changes in (
            {"code": "200"},
            {"ratio": "-100"},
            {"gid": "other"},
            {"chose_team": "C"},
            {"ioratio": "0"},
            {"ptype": "角球"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                verify_context(response({**context, **changes}), quote)

    def test_invalid_lines_and_xml_are_not_defaulted(self):
        for line in ("NaN", "Infinity", "0.3", "0/3", "", "abc"):
            with self.subTest(line=line), self.assertRaises(ValueError):
                encode_line(line, market_type="HANDICAP", side="HOME")
        for body in (
            b"<html/>",
            b"<!DOCTYPE html><serverresponse/>",
            b"<serverresponse><code>1</code><code>2</code></serverresponse>",
        ):
            with self.subTest(body=body), self.assertRaises(ValueError):
                game_rows(body)

    def test_receipt_requires_exact_success_and_never_invents_amounts(self):
        received = parse_receipt(response({"code": "560", "ticket_id": "T-1"}))
        self.assertEqual(received["status"], "SUCCEEDED")
        self.assertIsNone(received["accepted_stake"])
        self.assertIsNone(received["accepted_odds"])
        full = parse_receipt(
            response({"code": "560", "ticket_id": "T-1", "golds": "100", "ioratio": "0.91"})
        )
        self.assertEqual((full["accepted_stake"], full["accepted_odds"]), ("100", "0.91"))
        for fields in ({"code": "200", "ticket_id": "T-1"}, {"code": "560"}, {"code": "501"}):
            with self.subTest(fields=fields):
                self.assertEqual(parse_receipt(response(fields))["status"], "UNKNOWN")
        self.assertEqual(
            parse_receipt(response({"code": "555", "errormsg": "1X001"}))["status"], "REJECTED"
        )


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.elapsed = 0.0
        self.stopped = False
        self.calls = []
        self.submit_body = response(
            {"code": "560", "ticket_id": "T-1", "golds": "100", "ioratio": "0.89"}
        )
        self.submit_exception = None
        self.adapter = CrownAdapter(
            {},
            transport=self.transport,
            session_loader=lambda _: dict(SESSION),
            clock=lambda: self.now,
            monotonic=lambda: self.elapsed,
            should_stop=lambda: self.stopped,
        )
        guard = patch(
            "ps38app.follow.crown.build_opener", side_effect=AssertionError("测试禁止外网")
        )
        guard.start()
        self.addCleanup(guard.stop)

    def transport(self, url, fields, headers, timeout):
        self.calls.append(dict(fields))
        action = fields["p"]
        if action == "get_member_data":
            return 200, response({"enable": "Y", "currency": "CNY"})
        if action == "get_game_list":
            return 200, games_xml([GAME])
        if action == "get_game_more":
            return 200, games_xml([BOARD])
        if action == "FT_order_view":
            return 200, response(
                {
                    "code": "501",
                    "fast_check": "REH",
                    "concede": "1",
                    "ratio": "100",
                    "ioratio": "0.89",
                }
            )
        if action == "FT_bet":
            if self.submit_exception:
                raise self.submit_exception
            return 200, self.submit_body
        raise AssertionError("出现非预期接口")

    def prepare(self):
        return self.adapter.prepare(MAPPING, INTENT, "test-account")

    def test_prepare_is_readonly_and_returns_no_session_secrets(self):
        quote = self.prepare()
        self.assertEqual(
            [item["p"] for item in self.calls],
            ["get_member_data", "get_game_list", "get_game_more", "FT_order_view"],
        )
        self.assertEqual(
            (quote["account_id"], quote["currency"], quote["odds"]), ("test-account", "CNY", "0.89")
        )
        public = json.dumps(quote)
        for private in ("fixture-uid", "fixture-ver", "fixture=cookie", "https://example.test"):
            self.assertNotIn(private, public)

    def test_success_uses_line_gid_and_quote_can_only_be_consumed_once(self):
        quote = self.prepare()
        result = self.adapter.submit(quote, "100", "test-account")
        self.assertEqual(result["receipt_id"], "T-1")
        posted = self.calls[-1]
        self.assertEqual(
            (posted["p"], posted["gid"], posted["golds"], posted["ioratio"]),
            ("FT_bet", "line-1", "100", "0.89"),
        )
        self.assertEqual(self.adapter.submit(quote, "100", "test-account")["status"], "REJECTED")
        self.assertEqual(sum(item["p"] == "FT_bet" for item in self.calls), 1)

    def test_half_markets_use_half_gid_for_confirmation_and_submit(self):
        for market_type, side, line, wtype in (
            ("HANDICAP", "HOME", "0.25", "HRE"),
            ("TOTAL", "OVER", "0.75", "HROU"),
        ):
            with self.subTest(market_type=market_type):
                intent = {
                    **INTENT,
                    "period": "HT",
                    "market_type": market_type,
                    "side": side,
                    "line": line,
                }
                selected = select_market([BOARD], GAME, intent)

                def transport(url, fields, headers, timeout):
                    if fields["p"] != "FT_order_view":
                        return self.transport(url, fields, headers, timeout)
                    self.calls.append(dict(fields))
                    return 200, response(
                        {
                            "code": "501",
                            "gid": selected["gid"],
                            "rtype": selected["rtype"],
                            "con": selected["con"],
                            "ratio": selected["ratio"],
                            "ioratio": "0.89",
                        }
                    )

                self.calls.clear()
                self.adapter._transport = transport
                quote = self.adapter.prepare(MAPPING, intent, "test-account")
                self.assertEqual(quote["gid"], "half-line-1")
                self.assertEqual(
                    self.adapter.submit(quote, "100", "test-account")["status"], "SUCCEEDED"
                )
                actions = [item for item in self.calls if item["p"] in {"FT_order_view", "FT_bet"}]
                self.assertEqual(
                    [(item["gid"], item["wtype"]) for item in actions],
                    [("half-line-1", wtype), ("half-line-1", wtype)],
                )
                with self.assertRaises(ValueError):
                    verify_context(
                        response(
                            {
                                "code": "501",
                                "gid": "line-1",
                                "rtype": selected["rtype"],
                                "con": selected["con"],
                                "ratio": selected["ratio"],
                                "ioratio": "0.89",
                            }
                        ),
                        selected,
                    )

    def test_prepare_has_one_total_deadline_across_all_requests(self):
        timeouts = []

        def slow_transport(url, fields, headers, timeout):
            timeouts.append(timeout)
            self.elapsed += 4
            return self.transport(url, fields, headers, timeout)

        self.adapter._transport = slow_transport
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual(timeouts, [5, 5, 2])
        self.assertEqual(
            [item["p"] for item in self.calls],
            ["get_member_data", "get_game_list", "get_game_more"],
        )

    def test_stop_during_prepare_prevents_further_requests(self):
        def stopping_transport(url, fields, headers, timeout):
            result = self.transport(url, fields, headers, timeout)
            self.stopped = True
            return result

        self.adapter._transport = stopping_transport
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual([item["p"] for item in self.calls], ["get_member_data"])

    def test_submit_rechecks_cancel_age_and_deadline_after_loading_session(self):
        for condition in ("stopped", "age", "deadline"):
            with self.subTest(condition=condition):
                self.stopped = False
                self.adapter._loader = lambda _: dict(SESSION)
                quote = self.prepare()

                def delayed_loader(_account_id):
                    if condition == "stopped":
                        self.stopped = True
                    elif condition == "age":
                        self.now += 6
                    else:
                        self.elapsed += 6
                    return dict(SESSION)

                self.adapter._loader = delayed_loader
                self.assertEqual(
                    self.adapter.submit(quote, "100", "test-account")["status"], "REJECTED"
                )
        self.assertFalse(any(item["p"] == "FT_bet" for item in self.calls))

    def test_submit_timeout_after_send_is_unknown_and_cannot_repeat(self):
        quote = self.prepare()

        def slow_submit(url, fields, headers, timeout):
            self.assertEqual(timeout, 5)
            self.elapsed += 6
            return self.transport(url, fields, headers, timeout)

        self.adapter._transport = slow_submit
        self.assertEqual(self.adapter.submit(quote, "100", "test-account")["status"], "UNKNOWN")
        self.assertEqual(self.adapter.submit(quote, "100", "test-account")["status"], "REJECTED")
        self.assertEqual(sum(item["p"] == "FT_bet" for item in self.calls), 1)

    def test_chunked_read_updates_remaining_timeout_and_closes_on_deadline(self):
        timeouts = []
        adapter = self.adapter
        adapter._transport = None

        class SlowResponse:
            status = 200
            closed = False
            fp = SimpleNamespace(
                raw=SimpleNamespace(_sock=SimpleNamespace(settimeout=timeouts.append))
            )

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.closed = True

            def read1(response_self, _size):
                self.elapsed += 2
                return b"x"

        reply = SlowResponse()
        opener = SimpleNamespace(open=lambda *_args, **_kwargs: reply)
        with self.assertRaises(ValueError):
            adapter._post(SESSION, {"p": "get_member_data"}, opener)
        self.assertEqual(timeouts, [5, 3, 1])
        self.assertTrue(reply.closed)

    def test_stale_modified_wrong_account_and_fractional_stake_never_post(self):
        for change, stake, account, delay in (
            ({}, "100", "other", 0),
            ({"gid": "wrong"}, "100", "test-account", 0),
            ({}, "100", "test-account", 6),
            ({}, "100.5", "test-account", 0),
        ):
            with self.subTest(change=change, stake=stake, delay=delay):
                quote = self.prepare()
                self.now += delay
                self.assertEqual(
                    self.adapter.submit({**quote, **change}, stake, account)["status"], "REJECTED"
                )
        self.assertFalse(any(item["p"] == "FT_bet" for item in self.calls))

    def test_timeout_is_unknown_and_not_retried(self):
        quote = self.prepare()
        self.submit_exception = TimeoutError("sensitive fake response")
        result = self.adapter.submit(quote, "100", "test-account")
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertNotIn("sensitive", str(result))
        self.assertEqual(sum(item["p"] == "FT_bet" for item in self.calls), 1)

    def test_changed_session_blocks_submit_before_network(self):
        quote = self.prepare()
        self.adapter._loader = lambda _: {**SESSION, "uid": "new-fixture-uid"}
        self.assertEqual(self.adapter.submit(quote, "100", "test-account")["status"], "REJECTED")
        self.assertFalse(any(item["p"] == "FT_bet" for item in self.calls))

    def test_local_account_validation_uses_only_isolated_pool(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = isolated_config(Path(temporary))
            store = AccountStore(config["pool_db_path"], config)
            account = store.save_account(
                {
                    "platform": "crown_member",
                    "name": "测试会员",
                    "username": "fixture-user",
                    "base_url": "https://example.test",
                    "login_path": "/",
                }
            )
            account_id = account["id"]
            store.save_meta(
                account_id,
                {"uid": "fixture-uid", "ver": "fixture-ver", "origin": "https://example.test"},
            )
            paths = store.paths(account_id)
            paths["storage"].parent.mkdir(parents=True, exist_ok=True)
            paths["storage"].write_text(
                json.dumps(
                    {
                        "cookies": [
                            {
                                "name": "fixture",
                                "value": "test",
                                "domain": "example.test",
                                "path": "/",
                                "expires": -1,
                            }
                        ]
                    }
                )
            )
            store.set_status(account_id, "online", "测试在线")
            adapter = CrownAdapter(config)
            self.assertEqual(adapter.validate_account(account_id, "CNY")["id"], account_id)
            store.set_status(account_id, "expired", "测试失效")
            with self.assertRaises(ValueError):
                adapter.validate_account(account_id, "CNY")


class LocalTransportTests(unittest.TestCase):
    def test_context_cookie_is_carried_into_single_submit(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                payload = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
                action = payload["p"][0]
                received.append((action, self.headers.get("Cookie", "")))
                bodies = {
                    "get_member_data": response({"enable": "Y", "currency": "CNY"}),
                    "get_game_list": games_xml([GAME]),
                    "get_game_more": games_xml([BOARD]),
                    "FT_order_view": response(
                        {
                            "code": "501",
                            "fast_check": "REH",
                            "concede": "1",
                            "ratio": "100",
                            "ioratio": "0.89",
                        }
                    ),
                    "FT_bet": response({"code": "560", "ticket_id": "LOCAL-1"}),
                }
                body = bodies[action]
                self.send_response(200)
                if action == "FT_order_view":
                    self.send_header("Set-Cookie", "context=local-only; Path=/")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            session = {**SESSION, "origin": f"http://127.0.0.1:{server.server_port}"}
            adapter = CrownAdapter({}, session_loader=lambda _: dict(session), clock=lambda: 1000)
            quote = adapter.prepare(MAPPING, INTENT, "test-account")
            result = adapter.submit(quote, "100", "test-account")
            self.assertEqual(result["receipt_id"], "LOCAL-1")
            self.assertEqual(received[-1][0], "FT_bet")
            self.assertIn("fixture=cookie", received[-1][1])
            self.assertIn("context=local-only", received[-1][1])
            self.assertEqual(sum(action == "FT_bet" for action, _cookie in received), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
