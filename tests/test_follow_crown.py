"""皇冠适配器只使用临时号池与模拟响应，不访问实际站点。"""

import json
import pathlib
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
    FollowPreparationError,
    encode_line,
    game_rows,
    mapped_game,
    parse_receipt,
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
        "ecid": "event-1",
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

    def test_confirmed_snapshot_can_locate_fixture_without_live_game_list(self):
        self.assertEqual(
            mapped_game(MAPPING),
            {
                "gid": "match-1",
                "lid": "league-1",
                "ecid": "event-1",
                "team_h_id": "team-1",
                "team_c_id": "team-2",
                "team_h": "本地主队",
                "team_c": "本地客队",
            },
        )
        legacy = {
            **MAPPING,
            "crownSnapshot": {k: v for k, v in MAPPING["crownSnapshot"].items() if k != "ecid"},
        }
        self.assertIsNone(mapped_game(legacy))

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
            {"team_name_h": "其他主队"},
            {"team_id_h": "other-team"},
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

    def test_crown_error_codes_map_to_actions(self):
        cases = {
            "1X001": ("此选项目前不开放投注（盘口封盘）", "wait_1x001"),
            "1X002": ("已超过停止交易时间", "stop"),
            "1X008": ("交易金额超过股东单场总信用额度", "switch_account"),
            "1X012": ("您的总投注金额已超过余额", "switch_account"),
            "1X015": ("让球数、赔率或比分已更新", "retry_market_90"),
            "1X020": ("单注最高可赢金额超过上限", "split_half"),
            "0X001": ("网站流量较高，请重试", "retry_system"),
            "user_stop": ("您的账户已被停用，请联系上线", "disable_account"),
        }
        for code, (label, action) in cases.items():
            with self.subTest(code=code):
                result = parse_receipt(response({"code": "555", "errormsg": code}))
                self.assertEqual(result["status"], "REJECTED")
                self.assertEqual(result["message"], label)
                self.assertEqual(result["action"], action)
                self.assertEqual(result["retryable"], action != "stop")
                self.assertEqual(result["error_code"], code)

    def test_ambiguity_is_retryable_but_score_change_is_not(self):
        base = {
            "mode": "FT",
            "sw_rou": "Y",
            "score_h": "0",
            "score_c": "0",
            "ratio_rouo": "2",
            "ior_rouc": "0.75",
            "master": "Y",
        }
        game = {"gid": "1", "team_h": "A", "team_c": "B"}
        intent = {
            "market_type": "TOTAL",
            "side": "OVER",
            "line": "2",
            "period": "FT",
            "score": "0:0",
            "is_live": True,
        }
        with self.assertRaises(FollowPreparationError) as ambiguous:
            select_market([{**base, "gid": "1"}, {**base, "gid": "2"}], game, intent)
        self.assertEqual(ambiguous.exception.category, "MARKET_AMBIGUOUS")
        self.assertTrue(ambiguous.exception.retryable)

        changed = {**base, "score_h": "1", "gid": "3"}
        with self.assertRaises(FollowPreparationError) as score:
            select_market([changed], game, intent)
        self.assertEqual(score.exception.category, "SCORE_CHANGED")
        self.assertFalse(score.exception.retryable)

    def test_traditional_chinese_team_name_is_not_a_mismatch(self):
        # 皇冠译名会变（雷昂內斯 / 雷昂内斯），有队伍 ID 时按 ID 比对。
        quote = {
            "rtype": "ROUC",
            "con": "1",
            "ratio": "50",
            "gid": "9180921",
            "chose_team": "C",
            "wtype": "ROU",
            "home": "亚拉克莱恩斯",
            "away": "雷昂內斯",
            "line": "1.25",
            "side": "OVER",
            "market_type": "TOTAL",
            "period": "FT",
            "score": "0:0",
            "odds": "0.88",
            "team_h_id": "103895",
            "team_c_id": "123222",
        }
        body = response(
            {
                "code": "501",
                "con": "1",
                "ratio": "50",
                "ioratio": "0.88",
                "gid": "9180921",
                "wtype": "ROU",
                "chose_team": "C",
                "team_name_h": "亚拉克莱恩斯",
                "team_name_c": "雷昂内斯",
                "team_id_h": "103895",
                "team_id_c": "123222",
            }
        )
        confirmed = verify_context(body, quote)
        self.assertEqual(confirmed["odds"], "0.88")

    def test_duplicate_line_prefers_master_row(self):
        # 主盘与附加盘报同一条线时，必须优先主盘（master=Y）。
        base = {
            "mode": "FT",
            "sw_rou": "Y",
            "score_h": "0",
            "score_c": "0",
            "ratio_rouo": "2",
            "ior_rouc": "0.75",
        }
        rows = [
            {**base, "gid": "main", "master": "Y"},
            {**base, "gid": "alt", "master": "N"},
        ]
        game = {"gid": "1", "team_h": "古晋城", "team_c": "江原"}
        intent = {
            "market_type": "TOTAL",
            "side": "OVER",
            "line": "2",
            "period": "FT",
            "score": "0:0",
            "is_live": True,
        }
        quote = select_market(rows, game, intent)
        self.assertEqual(quote["gid"], "main")
        self.assertEqual(quote["master"], "Y")

    def test_order_view_never_sends_rtype(self):
        # 滚球/赛前的 FT_order_view 带上 rtype 一律返回 VariableStandard。
        calls = []

        class Recorder:
            def __init__(self):
                self.prepared = []

            def validate_account(self, account_id, currency):
                return None

            def prepare(self, mapping, intent, account_id):
                calls.append(intent)
                return {
                    "gid": "1",
                    "master_gid": "1",
                    "period": "FT",
                    "market_type": "TOTAL",
                    "side": "OVER",
                    "line": "3",
                    "score": "",
                    "currency": "CNY",
                    "account_id": account_id,
                    "odds": "0.95",
                    "odds_format": "HK",
                    "obtained_at": 1.0,
                    "quote_id": "q",
                    "wtype": "ROU",
                    "rtype": "ROUC",
                    "chose_team": "C",
                    "con": "4",
                    "ratio": "0",
                    "is_live": True,
                }

            def submit(self, quote, stake, account_id):
                return {
                    "status": "SUCCEEDED",
                    "receipt_id": "T1",
                    "retryable": False,
                    "message": "ok",
                    "request_sent": True,
                }

        source = pathlib.Path("ps38app/follow/crown.py").read_text(encoding="utf-8")
        self.assertNotIn('order_view["rtype"]', source)
        self.assertIn('"p": "FT_order_view"', source)

    def test_bet_rejected_with_ticket_is_never_a_success(self):
        result = parse_receipt(
            response({"code": "bet_rejected", "ticket_id": "T-9", "errormsg": "bet_rejected"})
        )
        self.assertEqual(result["status"], "REJECTED")
        self.assertFalse(result["retryable"])

    def test_unknown_error_code_falls_back_to_platform_rejection(self):
        result = parse_receipt(response({"code": "555", "errormsg": "1X999"}))
        self.assertEqual(result["category"], "PLATFORM_REJECTED")
        self.assertFalse(result["retryable"])

    def test_insufficient_credit_switches_account(self):
        # 1X012 是会员额度不足：换号可以继续，不能当成普通盘口拒绝。
        result = parse_receipt(response({"code": "555", "errormsg": "1X012"}))
        self.assertEqual(result["status"], "REJECTED")
        self.assertTrue(result["retryable"])
        self.assertEqual(result["category"], "ACCOUNT_CREDIT")
        self.assertEqual(result["action"], "switch_account")
        self.assertEqual(result["error_code"], "1X012")

    def test_bet_error_prefix_is_a_retryable_market_rejection(self):
        # 参考项目把 betError* 视为盘口临时拒绝，换最新盘口可以再试。
        result = parse_receipt(response({"code": "555", "errormsg": "betError0001"}))
        self.assertEqual(result["status"], "REJECTED")
        self.assertTrue(result["retryable"])
        self.assertEqual(result["category"], "MARKET_REJECTED")
        self.assertEqual(result["error_code"], "betError0001")

    def test_plain_555_is_member_error_and_can_try_another_member(self):
        result = parse_receipt(response({"code": "555"}))
        self.assertEqual(result["status"], "REJECTED")
        self.assertTrue(result["retryable"])
        self.assertEqual(result["category"], "ACCOUNT_SESSION")

    def test_unknown_error_code_stays_non_retryable(self):
        result = parse_receipt(response({"code": "555", "errormsg": "somethingElse"}))
        self.assertEqual(result["status"], "REJECTED")
        self.assertFalse(result["retryable"])
        self.assertEqual(result["category"], "PLATFORM_REJECTED")


class PrematchMarketTests(unittest.TestCase):
    """赛前盘口字段与滚球不同，这里固定字段映射。"""

    def row(self, **over):
        data = {
            "mode": "FT",
            "gid": "9170085",
            "hgid": "9170086",
            "sw_r": "Y",
            "ratio": "0 / 0.5",
            "ior_rh": "0.900",
            "ior_rc": "0.940",
            "strong": "H",
            "hstrong": "H",
            "sw_ou": "Y",
            "ratio_o": "2 / 2.5",
            "ratio_u": "2 / 2.5",
            "ior_ouh": "0.970",
            "ior_ouc": "0.790",
            "sw_hr": "Y",
            "hratio": "0 / 0.5",
            "ior_hrh": "0.820",
            "ior_hrc": "0.940",
            "sw_hou": "Y",
            "ratio_ho": "1",
            "ratio_hu": "1",
            "ior_houh": "0.800",
            "ior_houc": "0.900",
        }
        data.update(over)
        return data

    def intent(self, market, side, line, period="FT"):
        return {
            "is_live": False,
            "period": period,
            "market_type": market,
            "side": side,
            "line": line,
            "score": "",
        }

    def game(self):
        return {"gid": "9170085", "lid": "l1", "ecid": "", "team_h": "主", "team_c": "客"}

    def test_prematch_handicap_uses_ratio_and_r_type(self):
        quote = select_market([self.row()], self.game(), self.intent("HANDICAP", "HOME", "-0.25"))
        self.assertEqual((quote["wtype"], quote["rtype"], quote["chose_team"]), ("R", "RH", "H"))
        self.assertEqual(quote["odds"], "0.900")
        self.assertEqual((quote["con"], quote["ratio"]), ("0", "-50"))
        away = select_market([self.row()], self.game(), self.intent("HANDICAP", "AWAY", "0.25"))
        self.assertEqual((away["rtype"], away["odds"]), ("RC", "0.940"))

    def test_prematch_totals_use_ratio_side_fields(self):
        quote = select_market([self.row()], self.game(), self.intent("TOTAL", "OVER", "2.25"))
        self.assertEqual((quote["wtype"], quote["rtype"]), ("OU", "OUC"))
        self.assertEqual(quote["odds"], "0.790")
        under = select_market([self.row()], self.game(), self.intent("TOTAL", "UNDER", "2.25"))
        self.assertEqual((under["rtype"], under["odds"]), ("OUH", "0.970"))

    def test_prematch_half_time_uses_hratio_and_hou(self):
        total = select_market([self.row()], self.game(), self.intent("TOTAL", "OVER", "1", "HT"))
        self.assertEqual((total["gid"], total["wtype"], total["rtype"]), ("9170086", "HOU", "HOUC"))
        handicap = select_market(
            [self.row()], self.game(), self.intent("HANDICAP", "HOME", "-0.25", "HT")
        )
        self.assertEqual(
            (handicap["gid"], handicap["wtype"], handicap["rtype"]), ("9170086", "HR", "HRH")
        )

    def test_prematch_handicap_uses_1m_marker_and_sizes(self):
        # 皇冠下注来源标记：让球用 1M，大小用 1R。
        def marker(wtype):
            return "1M" if wtype in {"R", "HR"} else "1R"

        self.assertEqual(marker("R"), "1M")
        self.assertEqual(marker("HR"), "1M")
        self.assertEqual(marker("OU"), "1R")
        self.assertEqual(marker("HOU"), "1R")

    def test_prematch_verification_accepts_missing_rtype(self):
        quote = {
            "rtype": "OUC",
            "con": "3",
            "ratio": "-50",
            "gid": "9141879",
            "chose_team": "C",
            "wtype": "OU",
            "home": "PSV",
            "away": "布鲁日",
            "line": "3.25",
            "side": "OVER",
            "market_type": "TOTAL",
            "period": "FT",
            "score": "",
            "odds": "0.750",
            "master_gid": "9141877",
        }
        body = response(
            {
                "code": "501",
                "con": "3",
                "ratio": "-50",
                "ioratio": "0.76",
                "gid": "9141879",
                "wtype": "OU",
                "chose_team": "C",
            }
        )
        confirmed = verify_context(body, quote)
        self.assertEqual(confirmed["odds"], "0.76")

    def test_prematch_market_fields_match_crown_keys(self):
        from ps38app.follow.crown_markets import market_fields

        self.assertEqual(market_fields("HANDICAP", live=False)["line"], "ratio")
        self.assertEqual(market_fields("HANDICAP", half=True, live=False)["line"], "hratio")
        self.assertEqual(market_fields("TOTAL", live=False)["line_o"], "ratio_o")
        self.assertEqual(market_fields("TOTAL", half=True, live=False)["line_u"], "ratio_hu")
        self.assertEqual(market_fields("TOTAL", live=True)["line_o"], "ratio_rouo")

    def test_closed_prematch_market_is_not_selected(self):
        with self.assertRaises(ValueError):
            select_market(
                [self.row(sw_r="N")], self.game(), self.intent("HANDICAP", "HOME", "-0.25")
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
            ["get_member_data", "get_game_more", "FT_order_view"],
        )
        market_query = next(item for item in self.calls if item["p"] == "get_game_more")
        self.assertEqual((market_query["lid"], market_query["ecid"]), ("league-1", "event-1"))
        self.assertEqual(
            (quote["account_id"], quote["currency"], quote["odds"]), ("test-account", "CNY", "0.89")
        )
        public = json.dumps(quote)
        for private in ("fixture-uid", "fixture-ver", "fixture=cookie", "https://example.test"):
            self.assertNotIn(private, public)

    def test_find_history_wager_matches_ticket_across_dates(self):
        def transport(url, fields, headers, timeout):
            self.calls.append(dict(fields))
            action = fields["p"]
            if action == "get_history_data":
                return 200, (
                    "<serverresponse><history><date>2026-11-05</date>"
                    "<date>2026-11-04</date></history></serverresponse>"
                ).encode()
            if action == "history_switch":
                if fields["today_gmt"] != "2026-11-05":
                    return 200, b"<serverresponse><code>609</code></serverresponse>"
                return 200, (
                    '<serverresponse><code>609</code><wagers tid="OU123">'
                    "<w_id>OU123</w_id><league>测试杯</league>"
                    "<team_h_show>主队</team_h_show><team_c_show>客队</team_c_show>"
                    "<ioratio>0.91</ioratio><gold>123</gold><win_gold>112.7</win_gold>"
                    "<result_data>4 - 0</result_data></wagers></serverresponse>"
                ).encode()
            raise AssertionError(action)

        self.adapter._transport = transport
        wager = self.adapter.find_history_wager("test-account", "123")
        self.assertEqual(wager["league"], "测试杯")
        self.assertEqual(wager["win_gold"], "112.7")
        self.assertEqual(wager["date"], "2026-11-05")

    def test_find_today_wager_matches_gid_and_stake(self):
        def transport(url, fields, headers, timeout):
            self.calls.append(dict(fields))
            if fields["p"] != "get_today_wagers":
                raise AssertionError(fields["p"])
            payload = {
                "count": 3,
                "wagers": [
                    {"w_id": "OLD-1", "gid": "other", "gold": "100", "ioratio": "0.80"},
                    {"w_id": "W-9", "gid": "line-1", "gold": "100", "ioratio": "0.91"},
                    {"w_id": "W-8", "gid": "line-1", "gold": "50", "ioratio": "0.90"},
                ],
            }
            return 200, json.dumps(payload).encode()

        self.adapter._transport = transport
        wager = self.adapter.find_today_wager(
            "test-account", "line-1", "100", excluded_tickets={"OLD-1"}
        )
        self.assertEqual(wager["w_id"], "W-9")

    def test_today_wager_reconciliation_requires_baseline_and_unique_exact_new_ticket(self):
        wagers = [{"w_id": "OLD", "gid": "line-1", "gold": "100", "ioratio": "0.91"}]

        def transport(url, fields, headers, timeout):
            return 200, json.dumps({"count": len(wagers), "wagers": wagers}).encode()

        self.adapter._transport = transport
        self.assertIsNone(self.adapter.find_today_wager("test-account", "line-1", "100"))
        baseline = self.adapter.today_wager_ids("test-account")
        self.assertEqual(baseline, {"OLD"})
        self.assertIsNone(
            self.adapter.find_today_wager(
                "test-account", "line-1", "100", excluded_tickets=baseline
            )
        )
        wagers.append({"w_id": "DECIMAL", "gid": "line-1", "gold": "100.9"})
        self.assertIsNone(
            self.adapter.find_today_wager(
                "test-account", "line-1", "100", excluded_tickets=baseline
            )
        )
        wagers.extend(
            [
                {"w_id": "NEW-1", "gid": "line-1", "gold": "100"},
                {"w_id": "NEW-2", "gid": "line-1", "gold": "100"},
            ]
        )
        self.assertIsNone(
            self.adapter.find_today_wager(
                "test-account", "line-1", "100", excluded_tickets=baseline
            )
        )

    def test_today_wager_baseline_rejects_truncated_or_invalid_lists(self):
        for payload in (
            {},
            {"wagers": [], "count": 1},
            {"wagers": [{"gid": "line-1"}], "count": 1},
        ):
            with self.subTest(payload=payload):
                self.adapter._transport = lambda *_args: (200, json.dumps(payload).encode())
                with self.assertRaises(ValueError):
                    self.adapter.today_wager_ids("test-account")

    def test_variable_standard_triggers_warmup_then_verifies(self):
        views = {"count": 0}

        def transport(url, fields, headers, timeout):
            self.calls.append(dict(fields))
            action = fields["p"]
            if action == "get_member_data":
                return 200, response({"enable": "Y", "currency": "CNY"})
            if action == "get_game_more":
                return 200, games_xml([BOARD])
            if action in {"right_menu", "sport_menu", "order"}:
                return 200, response({"code": "ok"})
            if action == "FT_order_view":
                views["count"] += 1
                if views["count"] == 1:
                    return 200, b"VariableStandard"
                return 200, response(
                    {
                        "code": "501",
                        "fast_check": "REH",
                        "concede": "1",
                        "ratio": "100",
                        "ioratio": "0.89",
                    }
                )
            raise AssertionError(action)

        self.adapter._transport = transport
        quote = self.prepare()
        self.assertEqual(views["count"], 2)
        actions = [item["p"] for item in self.calls]
        self.assertEqual(
            actions,
            [
                "get_member_data",
                "get_game_more",
                "FT_order_view",
                "right_menu",
                "sport_menu",
                "order",
                "FT_order_view",
            ],
        )
        self.assertNotEqual(quote.get("verified"), False)
        sport_menu = next(item for item in self.calls if item["p"] == "sport_menu")
        self.assertEqual((sport_menu["gtype"], sport_menu["showtype"]), ("ft", "live"))

    def test_variable_standard_retries_instead_of_blind_submit(self):
        def transport(url, fields, headers, timeout):
            self.calls.append(dict(fields))
            action = fields["p"]
            if action == "get_member_data":
                return 200, response({"enable": "Y", "currency": "CNY"})
            if action == "get_game_more":
                return 200, games_xml([BOARD])
            if action in {"right_menu", "sport_menu", "order"}:
                return 200, response({"code": "ok"})
            if action == "FT_order_view":
                return 200, b"VariableStandard"
            raise AssertionError(action)

        self.adapter._transport = transport
        with self.assertRaises(FollowPreparationError) as raised:
            self.prepare()
        self.assertEqual(raised.exception.category, "VARIABLE_STANDARD")
        self.assertTrue(raised.exception.retryable)

    def test_legacy_mapping_without_ecid_is_rejected_without_live_list_lookup(self):
        legacy = {
            **MAPPING,
            "crownSnapshot": {k: v for k, v in MAPPING["crownSnapshot"].items() if k != "ecid"},
        }
        with self.assertRaisesRegex(ValueError, "缺少皇冠盘口 ID"):
            self.adapter.prepare(legacy, INTENT, "test-account")
        # 不再去拉整份滚球列表猜比赛。
        self.assertEqual([item["p"] for item in self.calls], ["get_member_data"])

    def test_direct_lookup_returns_over_one_one_half_market_gid_and_odds(self):
        intent = {
            **INTENT,
            "market_type": "TOTAL",
            "side": "OVER",
            "line": "1.25",
        }

        def transport(url, fields, headers, timeout):
            if fields["p"] == "get_game_more":
                self.calls.append(dict(fields))
                board = {**BOARD, "ratio_rouo": "1/1.5", "ior_rouc": "0.93"}
                return 200, games_xml([board])
            if fields["p"] == "FT_order_view":
                self.calls.append(dict(fields))
                return 200, response(
                    {
                        "code": "501",
                        "fast_check": "ROUC",
                        "con": "1",
                        "ratio": "-50",
                        "ioratio": "0.93",
                    }
                )
            return self.transport(url, fields, headers, timeout)

        self.adapter._transport = transport
        quote = self.adapter.prepare(MAPPING, intent, "test-account")
        self.assertEqual(
            (quote["master_gid"], quote["gid"], quote["line"], quote["odds"]),
            ("match-1", "line-1", "1.25", "0.93"),
        )
        self.assertEqual(
            [item["p"] for item in self.calls],
            ["get_member_data", "get_game_more", "FT_order_view"],
        )
        self.assertEqual(self.calls[-1]["chose_team"], "C")

    def test_missing_market_score_is_confirmed_by_the_order_view(self):
        # 主盘口行不完全回显比分时，用 FT_order_view 的 score 字段做最终核对。
        no_score_board = {
            key: value for key, value in BOARD.items() if key not in {"score_h", "score_c"}
        }

        def transport(url, fields, headers, timeout):
            self.calls.append(dict(fields))
            if fields["p"] == "get_member_data":
                return 200, response({"enable": "Y", "currency": "CNY"})
            if fields["p"] == "get_game_more":
                return 200, games_xml([no_score_board])
            if fields["p"] == "FT_order_view":
                return 200, response(
                    {
                        "code": "501",
                        "fast_check": "REH",
                        "concede": "1",
                        "ratio": "100",
                        "ioratio": "0.89",
                        "score": "0:0",
                    }
                )
            return 200, response({})

        self.adapter._transport = transport
        quote = self.adapter.prepare(MAPPING, INTENT, "test-account")
        self.assertEqual(quote["gid"], "line-1")
        self.assertEqual(
            [item["p"] for item in self.calls],
            ["get_member_data", "get_game_more", "FT_order_view"],
        )

    def test_order_view_score_change_blocks_the_submission(self):
        def transport(url, fields, headers, timeout):
            self.calls.append(dict(fields))
            if fields["p"] == "FT_order_view":
                return 200, response(
                    {
                        "code": "501",
                        "fast_check": "REH",
                        "concede": "1",
                        "ratio": "100",
                        "ioratio": "0.89",
                        "score": "1:0",
                    }
                )
            return self.transport(url, fields, headers, timeout)

        self.adapter._transport = transport
        with self.assertRaisesRegex(ValueError, "比分"):
            self.adapter.prepare(MAPPING, INTENT, "test-account")

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
            if len(timeouts) == 3:
                self.elapsed += 20
            return self.transport(url, fields, headers, timeout)

        self.adapter._transport = slow_transport
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual(timeouts, [10, 10, 10])
        self.assertEqual(
            [item["p"] for item in self.calls],
            ["get_member_data", "get_game_more", "FT_order_view"],
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
        self.assertEqual(timeouts, [10, 8, 6, 4, 2])
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
            self.assertEqual(adapter.available_accounts(), [account_id])
            with store._db() as db:
                db.execute("UPDATE accounts SET env_id=? WHERE id=?", ("test-env", account_id))
                self.assertEqual(db.execute("SELECT env_id FROM accounts WHERE id=?", (account_id,)).fetchone()[0], "test-env")
            with patch("ps38app.follow.crown.AdsPowerClient") as ads:
                ads.return_value.proxy.side_effect = RuntimeError("not available")
                with self.assertRaisesRegex(ValueError, "环境代理无法读取"):
                    adapter.validate_account(account_id, "CNY")
                self.assertEqual(adapter.available_accounts(), [])
            store.set_status(account_id, "expired", "测试失效")
            self.assertEqual(adapter.available_accounts(), [])
            with self.assertRaises(ValueError):
                adapter.validate_account(account_id, "CNY")


class WarmupBetTests(unittest.TestCase):
    """正式下注前先用只读请求预热连接。"""

    def make_adapter(self, connection):
        adapter = CrownAdapter({}, session_loader=lambda _: dict(SESSION), clock=lambda: 1000)
        adapter._bet_context = lambda session, timeout: connection
        adapter._opener_cookie = lambda opener, request: "fixture=cookie"
        return adapter

    def test_warmup_failure_never_sends_the_bet(self):
        calls = []

        class BrokenConnection:
            def connect(self):
                pass

            def request(self, method, path, body=None, headers=None):
                calls.append("request")
                raise OSError("unexpected eof")

            def close(self):
                pass

        adapter = self.make_adapter(BrokenConnection())
        body, failure = adapter._post_bet(
            dict(SESSION), {"p": "FT_bet", "golds": "100"}, None, adapter._monotonic() + 5
        )

        self.assertIsNone(body)
        self.assertTrue(failure["reason"].startswith("连接预热失败"))
        self.assertFalse(failure.get("maybe_sent"))
        # 预热失败时只走了预热请求，没有发第二次真正的下注请求。
        self.assertEqual(len(calls), 2)

    def test_warmup_then_bet_uses_the_same_connection(self):
        order = []

        class FakeResponse:
            def __init__(self, body, status=200):
                self._body = body
                self.status = status

            def read(self, _limit=None):
                return self._body

        class FakeConnection:
            def __init__(self):
                self.count = 0

            def connect(self):
                order.append("connect")

            def request(self, method, path, body=None, headers=None):
                self.count += 1
                order.append("warmup" if self.count == 1 else "bet")

            def getresponse(self):
                return FakeResponse(
                    b"<serverresponse><code>560</code><ticket_id>T-1</ticket_id></serverresponse>"
                )

            def close(self):
                pass

        adapter = self.make_adapter(FakeConnection())
        body, failure = adapter._post_bet(
            dict(SESSION), {"p": "FT_bet", "golds": "100"}, None, adapter._monotonic() + 5
        )

        self.assertIsNone(failure)
        self.assertIn(b"ticket_id", body)
        self.assertEqual(order, ["connect", "warmup", "bet"])

    def test_bound_proxy_is_used_for_read_warmup_and_single_bet_with_cookie_updates(self):
        from email.message import Message
        from http.cookiejar import CookieJar
        from ps38app.net import HttpResponse
        from urllib.request import HTTPCookieProcessor

        proxy = {"host": "127.0.0.1", "port": "1080"}
        session = {**SESSION, "proxy": proxy}
        calls = []
        adapter = CrownAdapter({})
        opener = SimpleNamespace(handlers=[HTTPCookieProcessor(CookieJar())])

        class Reply:
            status = 200

            def read(self, _limit=None):
                return response({"code": "560", "ticket_id": "PROXY-1"})

            def info(self):
                headers = Message()
                headers.add_header("Set-Cookie", "warm=two; Path=/")
                return headers

        class Connection:
            def connect(self):
                calls.append(("connect", None))

            def request(self, method, path, body=None, headers=None):
                action = parse_qs(body.decode())["p"][0]
                calls.append((action, headers.get("Cookie")))

            def getresponse(self):
                return Reply()

            def close(self):
                pass

        with (
            patch("ps38app.follow.crown.HttpClient") as client,
            patch("ps38app.follow.crown._SocksConnection", return_value=Connection()) as connection,
            patch("ps38app.follow.crown.http.client.HTTPSConnection") as direct,
        ):
            client.return_value.request.return_value = HttpResponse(
                200, {"set-cookie": ["context=one; Path=/"]}, b"ok"
            )
            adapter._post(session, {"p": "FT_order_view"}, opener)
            body, failure = adapter._post_bet(
                session, {"p": "FT_bet", "golds": "100"}, opener, adapter._monotonic() + 5
            )
            self.assertIsNone(failure)
            self.assertIn(b"PROXY-1", body)
            self.assertEqual(client.call_args.kwargs["proxy"], proxy)
            self.assertEqual(connection.call_args.kwargs["proxy"], proxy)
            direct.assert_not_called()
        self.assertEqual([action for action, _ in calls], ["connect", "get_member_data", "FT_bet"])
        self.assertIn("context=one", calls[1][1])
        self.assertIn("context=one", calls[2][1])
        self.assertIn("warm=two", calls[2][1])


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
