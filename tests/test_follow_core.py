"""主页批次处理与恢复测试，所有订单、映射和执行器均为本地模拟。"""

import copy
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from ps38app.fixture_merge import FixtureMergeStore
from ps38app.follow.mapping import resolve_fixture
from ps38app.follow.rules import group_stake, target_intent, validate_rule
from ps38app.follow.service import FollowService
from ps38app.follow.source import parse_line, source_intent
from ps38app.store import OrderStore, parse_order
from tests.support import isolated_config


class Clock:
    def __init__(self):
        self.now = 1_793_600_000.0

    def __call__(self):
        return self.now


class Executor:
    def __init__(self, clock):
        self.clock = clock
        self.account_ids = ["crown-test"]
        self.balances = {}
        self.prepared = []
        self.submitted = []
        self.result = None
        self.results = []
        self.on_prepare = None
        self.quote_changes = {}

    def validate_account(self, account_id, currency):
        if account_id != "crown-test" or currency != "CNY":
            raise ValueError("测试账号不匹配")

    def available_accounts(self):
        return list(self.account_ids)

    def account_balances(self):
        return dict(self.balances)

    def prepare(self, mapping, intent, account_id):
        self.prepared.append(copy.deepcopy(intent))
        quote = {
            "gid": "market-1",
            "master_gid": mapping["crownId"],
            "period": intent["period"],
            "market_type": intent["market_type"],
            "side": intent["side"],
            "line": intent["line"],
            "score": intent["score"],
            "currency": "CNY",
            "account_id": account_id,
            "odds": "0.95",
            "odds_format": "HK",
            "obtained_at": self.clock(),
            **self.quote_changes,
        }
        if self.on_prepare:
            self.on_prepare()
        return quote

    def submit(self, quote, stake, account_id):
        self.submitted.append((quote, stake, account_id))
        if isinstance(self.result, Exception):
            raise self.result
        if self.results:
            return self.results.pop(0)
        return (
            self.result
            if self.result is not None
            else {
                "status": "SUCCEEDED",
                "receipt_id": f"TEST-{len(self.submitted)}",
                "accepted_stake": stake,
                "accepted_odds": quote["odds"],
            }
        )


def raw_order(order_id="1", member="M1", created_ms=1_793_600_000_000, **overrides):
    row = {
        "playerWagerId": order_id,
        "userCode": member,
        "status": "OPEN",
        "stake": "100",
        "currencyCode": "CNY",
        "odds": "0.97",
        "oddsFormat": "HK",
        "createdDate": created_ms,
        "choices": [
            {
                "sportName": "足球",
                "btName": "AH",
                "handicap": "-0.5",
                "selection": "HOME",
                "leagueName": "测试联赛",
                "eventName": "测试甲 -vs- 测试乙",
                "eventDate": 1_793_596_400_000,
                "eventId": "101",
                "inplay": True,
                "periodType": 0,
                "score": "0-0",
            }
        ],
    }
    row.update(overrides)
    return row


class PrematchSourceTests(unittest.TestCase):
    def prematch_raw(self):
        return raw_order(choices=[{
            "sportName": "足球", "btName": "AH", "handicap": "-0.5",
            "selection": "HOME", "leagueName": "测试联赛",
            "eventName": "测试甲 -vs- 测试乙", "eventDate": 1_793_596_400_000,
            "eventId": "101", "inplay": False, "periodType": 0,
        }])

    def test_prematch_order_is_rejected_when_disabled(self):
        with self.assertRaisesRegex(ValueError, "滚球"):
            source_intent(parse_order(self.prematch_raw(), "A"))

    def test_prematch_order_keeps_not_live_when_enabled(self):
        intent = source_intent(parse_order(self.prematch_raw(), "A"), allow_prematch=True)
        self.assertIs(intent["is_live"], False)
        self.assertEqual(intent["market_type"], "HANDICAP")

    def test_live_order_still_marked_live(self):
        self.assertIs(source_intent(parse_order(raw_order(), "A"))["is_live"], True)


class RuleTests(unittest.TestCase):
    def test_signed_split_lines_and_invalid_numbers(self):
        self.assertEqual(parse_line("-0/0.5"), "-0.25")
        self.assertEqual(parse_line("-0.5/1"), "-0.75")
        for value in ("", "nan", "Infinity", "1/3", "0.3", True, "0/-0.5"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_line(value)

    def test_reverse_once_and_amount_modes(self):
        intent = source_intent(parse_order(raw_order(), "A"))
        rule = validate_rule({"direction": "reverse", "amount_mode": "ratio", "amount_value": "10"})
        target = target_intent(intent, rule)
        self.assertEqual((target["side"], target["line"]), ("AWAY", "0.5"))
        self.assertEqual((intent["side"], intent["line"]), ("HOME", "-0.5"))
        self.assertEqual(group_stake("609", rule), "60")
        total = {**intent, "market_type": "TOTAL", "side": "OVER", "line": "2.75"}
        self.assertEqual(target_intent(total, rule)["line"], "2.75")
        self.assertEqual(target_intent(total, rule)["side"], "UNDER")

    def test_rule_rejects_nonfinite_fractional_fixed_and_missing_enable_fields(self):
        for payload in (
            {"amount_value": "NaN"},
            {"amount_value": "0.2"},
            {"enabled": "false"},
            {"enabled": True},
            {"max_age_seconds": 20},
            {"max_age_seconds": 601},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                validate_rule(payload)

    def test_source_never_uses_bet_time_as_missing_kickoff(self):
        raw = raw_order()
        del raw["choices"][0]["eventDate"]
        with self.assertRaisesRegex(ValueError, "开赛时间"):
            source_intent(parse_order(raw, "A"))

    def test_source_rejects_multi_leg_corner_and_unknown_period(self):
        raw = raw_order()
        cases = [copy.deepcopy(raw) for _ in range(3)]
        cases[0]["choices"].append(copy.deepcopy(raw["choices"][0]))
        cases[1]["choices"][0]["cornerEventUnit"] = True
        cases[2]["choices"][0]["periodType"] = 9
        for value in cases:
            with self.assertRaises(ValueError):
                source_intent(parse_order(value, "A"))

    def test_source_currency_labels_are_cny_without_changing_amount_or_hash(self):
        for currency in ("THB", "RMB", "CNY"):
            with self.subTest(currency=currency):
                row = parse_order(raw_order(currencyCode=currency, stake="123.45"), "A")
                intent = source_intent(row)
                self.assertEqual(intent["currency"], "CNY")
                self.assertEqual(intent["reported_currency"], currency)
                self.assertEqual(intent["stake"], "123.45")
                self.assertEqual(intent["content_hash"], row["content_hash"])
        for currency in ("USD", "EUR", "", "unknown"):
            with self.subTest(currency=currency), self.assertRaisesRegex(ValueError, "人民币口径"):
                source_intent(parse_order(raw_order(currencyCode=currency), "A"))

    def test_rule_no_longer_requires_currency_and_keeps_cny_compatibility(self):
        payload = {"enabled": True, "amount_value": "100"}
        self.assertEqual(validate_rule(payload)["currency"], "CNY")
        legacy = validate_rule({**payload, "account_id": "old-fixed-account"})
        self.assertNotIn("account_id", legacy)
        for currency in ("", "THB", "RMB", "USD"):
            with self.subTest(currency=currency):
                self.assertEqual(
                    validate_rule({**payload, "currency": currency})["currency"], "CNY"
                )


class FollowServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = isolated_config(self.root)
        self.config["follow_db_path"] = self.root / "follow.sqlite3"
        self.clock = Clock()
        self.orders = OrderStore(self.config["db_path"])
        self.rows = []
        self.commit([])  # 首轮基线不产生新单事件。
        self.mappings = FixtureMergeStore(self.config["merge_db_path"])
        kickoff = datetime.fromtimestamp(1_793_596_400, timezone.utc).isoformat()
        self.ps = {
            "id": "pin:101",
            "pinnacleId": "101",
            "leagueId": "p38:测试联赛",
            "league": "测试联赛",
            "home": "测试甲",
            "away": "测试乙",
            "kickoffUtc": kickoff,
            "isLive": True,
        }
        self.crown = {
            "id": "crown-1",
            "leagueId": "77",
            "league": "测试联赛",
            "home": "皇冠甲",
            "away": "皇冠乙",
            "kickoffUtc": kickoff,
            "isLive": True,
            "reversedOrder": False,
        }
        self.mappings.map_leagues(
            {"id": self.ps["leagueId"], "name": self.ps["league"]},
            {"id": self.crown["leagueId"], "name": self.crown["league"]},
        )
        self.mappings.map_events(self.ps, self.crown)
        self.executor = Executor(self.clock)
        self.service = FollowService(self.config, self.executor, self.clock)

    def tearDown(self):
        self.service.stop()
        self.orders.close()
        self.tmp.cleanup()

    def commit(self, rows, kind="OPEN"):
        return self.orders.commit_snapshot(
            "A",
            kind,
            rows,
            {
                "started_at": "2026-11-03T00:00:00+08:00",
                "finished_at": "2026-11-03T00:00:01+08:00",
                "elapsed_seconds": 1,
                "request_count": 1,
            },
        )

    def enable(self, **changes):
        return self.service.save_rule(
            {
                "enabled": True,
                "currency": "CNY",
                "amount_mode": "fixed",
                "amount_value": "50",
                "min_stake": 0,
                **changes,
            }
        )

    def add(self, order_id="1", member="M1", **changes):
        self.rows.append(raw_order(order_id, member, int(self.clock() * 1000), **changes))
        self.commit(self.rows)

    def tick(self, seconds=0):
        self.clock.now += seconds
        self.service.run_once()

    def groups(self):
        return self.service.snapshot("ALL")["rows"]

    def test_disabled_and_first_enable_never_replay_existing_orders(self):
        self.add()
        events, _, gap = self.service._events(0)
        self.assertFalse(gap)
        batches = self.service._copy_batches(events)
        self.service.observe_home_batch_rows(batches)
        disabled = self.groups()
        self.assertEqual(len(disabled), 1)
        self.assertEqual(disabled[0]["status"], "SKIPPED")
        self.assertEqual(disabled[0]["message"], "跟单未开启，未发送")
        self.assertRegex(disabled[0]["source_batch_no"], r"^\d{8}$")
        self.assertEqual(self.executor.submitted, [])
        self.clock.now += 21
        self.enable()
        self.tick()
        self.assertEqual(len(self.groups()), 1)
        self.add("2")
        self.tick()
        active = next(group for group in self.groups() if group["status"] != "SKIPPED")
        self.assertEqual(active["status"], "SUCCEEDED")
        self.assertEqual(active["order_count"], 1)
        self.assertEqual(len(self.executor.submitted), 1)

    def test_disabled_homepage_observation_keeps_stale_batch_details_and_is_idempotent(self):
        first_ms = int((self.clock.now - 150) * 1000)
        second_ms = first_ms + 5_000
        first = parse_order(raw_order("first", "M1", first_ms, stake="100"), "A")
        second = parse_order(raw_order("second", "M2", second_ms, stake="200"), "A")
        batch = {
            "order_no": "12345678",
            "group_key": "homepage-test-batch",
            "open_orders": 0,
            "end_ms": first_ms,
            "sources": [{"_source_row": first}],
        }
        first["status"] = "SETTLED"

        self.service.observe_home_batch_rows([batch], include_settled=True)
        self.service.observe_home_batch_rows([batch], include_settled=True)
        self.assertEqual(len(self.groups()), 1)
        group = self.groups()[0]
        self.assertEqual((group["status"], group["error_category"]), ("SKIPPED", "RULE_DISABLED"))
        self.assertEqual(group["message"], "跟单未开启，未发送")
        self.assertEqual((group["source_batch_no"], group["order_count"], group["source_stake"]),
                         ("12345678", 1, "100"))
        members = self.service.detail(group["id"])["members"]
        self.assertEqual([(member["order_id"], member["stake"]) for member in members],
                         [("first", "100")])

        batch["sources"].append({"_source_row": second})
        batch["open_orders"] = 2
        batch["end_ms"] = second_ms
        self.service.observe_home_batch_rows([batch])
        self.assertEqual(len(self.groups()), 1)
        group = self.groups()[0]
        self.assertEqual((group["order_count"], group["source_stake"]), (1, "100"))
        self.assertEqual(len(self.service.detail(group["id"])["members"]), 1)
        self.assertEqual(self.executor.submitted, [])

    def test_groups_rotate_across_healthy_crown_members(self):
        self.executor.account_ids = ["crown-a", "crown-b"]
        self.enable()
        self.add("one")
        self.tick(21)
        self.add("two")
        self.tick(21)
        self.assertEqual([item[2] for item in self.executor.submitted], ["crown-a", "crown-b"])
        self.assertNotIn("account_id", self.service.snapshot("ALL")["rule"])
        self.assertEqual([group["account_id"] for group in self.groups()], ["crown-b", "crown-a"])

    def test_group_waits_for_available_pool_member_then_resumes(self):
        self.enable()
        self.add()
        self.executor.account_ids = []
        self.tick(21)
        self.assertEqual(self.groups()[0]["status"], "WAIT_ACCOUNT")
        self.assertEqual(self.executor.submitted, [])
        self.executor.account_ids = ["crown-test"]
        self.tick(2)
        self.assertEqual(self.groups()[0]["status"], "SUCCEEDED")

    def test_match_limit_caps_target_before_submit(self):
        self.enable(amount_value="1000", match_limit_amount="400")
        self.add()
        self.tick()
        group = self.groups()[0]
        self.assertEqual(group["target_stake"], "400")
        self.assertEqual(float(self.executor.submitted[0][1]), 400.0)
        self.assertTrue(group["limit"]["limited"])

    def test_split_order_splits_amount_and_requires_all_parts(self):
        self.enable(amount_value="1000", split_count=2)
        self.add()
        self.tick()
        group = self.groups()[0]
        self.assertEqual(group["status"], "SUCCEEDED")
        self.assertEqual([item[1] for item in self.executor.submitted], [500.0, 500.0])
        self.assertEqual((group["split_total"], group["split_done"]), (2, 2))
        self.assertEqual(group["split_tickets"], ["TEST-1", "TEST-2"])
        self.assertEqual(group["receipt_id"], "TEST-1")
        self.assertEqual(group["accepted_stake"], "1000")
        parts = self.service.detail(group["id"])["parts"]
        self.assertEqual([part["ticket_id"] for part in parts], ["TEST-1", "TEST-2"])

    def test_split_partial_success_is_marked_partial(self):
        self.enable(amount_value="1000", split_count=2)
        self.executor.results = [
            {
                "status": "SUCCEEDED",
                "receipt_id": "PART-1",
                "accepted_stake": "500",
                "accepted_odds": "0.95",
            },
            {"status": "REJECTED", "message": "平台拒绝"},
        ]
        self.add()
        self.tick()
        group = self.groups()[0]
        self.assertEqual(group["status"], "PARTIAL")
        self.assertEqual((group["split_total"], group["split_done"]), (2, 1))
        self.assertEqual(group["split_tickets"], ["PART-1"])
        self.assertEqual(group["accepted_stake"], "500")
        self.assertIn("部分成功", group["message"])

    def test_enabling_requires_one_eligible_pool_member(self):
        self.executor.account_ids = []
        with self.assertRaisesRegex(ValueError, "在线且会话有效"):
            self.enable()

    def test_same_home_batch_is_processed_immediately_without_appending_later_orders(self):
        self.enable(amount_mode="ratio", amount_value="10", direction="reverse")
        self.add(stake="600")
        self.tick()
        self.clock.now += 8
        self.add("2", "M2", stake="200")
        self.tick()
        self.clock.now += 11
        self.add("3", "M3", stake="300")
        self.tick()
        group = self.groups()[0]
        self.assertEqual(
            (group["status"], group["order_count"], group["member_count"]), ("SUCCEEDED", 1, 1)
        )
        self.assertEqual(
            (group["source_stake"], group["target_stake"], group["accepted_stake"]),
            ("600", "60", "60"),
        )
        self.assertEqual(
            (self.executor.prepared[0]["side"], self.executor.prepared[0]["line"]), ("AWAY", "0.5")
        )
        self.assertEqual(group["receipt_id"], "TEST-1")
        detail = self.service.detail(group["id"])
        self.assertEqual(len(detail["members"]), 1)
        self.assertEqual(detail["members"][0]["order_id"], "1")
        self.assertRegex(group["source_batch_no"], r"^\d{8}$")
        self.assertNotIn("raw_json", str(detail))
        self.tick(30)
        self.assertEqual(len(self.executor.submitted), 1)

    def test_homepage_batch_boundary_and_later_batch_are_processed_without_waiting(self):
        self.enable()
        self.add()
        self.tick()
        self.assertEqual(len(self.executor.submitted), 1)
        self.clock.now += 20
        self.add("2")
        self.tick()
        self.assertEqual(len(self.groups()), 1)
        self.assertEqual(self.groups()[0]["order_count"], 1)
        self.assertEqual(len(self.executor.submitted), 1)
        self.clock.now += 0.001
        self.add("3")
        self.tick()
        self.assertEqual(len(self.groups()), 2)
        self.assertTrue(all(group["order_count"] == 1 for group in self.groups()))
        self.assertEqual(len(self.executor.submitted), 2)
        self.assertEqual([item[1] for item in self.executor.submitted], ["50", "50"])

    def test_thb_fixed_stake_remains_one_hundred_cny(self):
        self.enable(amount_value="100")
        self.add(currencyCode="THB", stake="800")
        self.tick()
        group = self.groups()[0]
        self.assertEqual((group["status"], group["currency"]), ("SUCCEEDED", "CNY"))
        self.assertEqual((group["source_stake"], group["target_stake"]), ("800", "100"))
        self.assertEqual(self.executor.submitted[0][1], "100")
        detail = self.service.detail(group["id"])
        self.assertEqual(detail["members"][0]["reported_currency"], "THB")
        self.assertEqual(detail["members"][0]["currency"], "CNY")

    def test_later_currencies_in_same_home_batch_do_not_change_first_order_amount(self):
        self.enable(amount_mode="ratio", amount_value="10")
        for order_id, currency, stake in (
            ("1", "THB", "100"),
            ("2", "CNY", "200"),
            ("3", "RMB", "309"),
        ):
            self.add(order_id, currencyCode=currency, stake=stake)
            self.tick()
        self.assertEqual(len(self.groups()), 1)
        group = self.groups()[0]
        self.assertEqual((group["source_stake"], group["target_stake"]), ("100", "10"))
        self.assertEqual(group["order_count"], 1)
        self.assertEqual(self.executor.submitted[0][1], "10")
        self.assertEqual(
            {item["reported_currency"] for item in self.service.detail(group["id"])["members"]},
            {"THB"},
        )

    def test_old_rule_currency_needs_no_resave_and_terminal_orders_never_replay(self):
        self.enable(amount_value="100")
        with self.service.store.connect(write=True) as connection:
            rule = self.service.store.rule(connection)
            rule["currency"] = "THB"
            self.service.store.save_rule(connection, rule)
        self.add(currencyCode="THB")
        self.tick()
        with self.service.store.connect(write=True) as connection:
            group = self.service.store.get_group(connection, self.groups()[0]["id"])
            self.assertEqual(group["status"], "SUCCEEDED")
            self.assertEqual(group["currency"], "CNY")
            self.service.store.save_group(connection, group)
        self.service.stop()
        self.service = FollowService(self.config, self.executor, self.clock)
        self.assertEqual(self.service.snapshot()["rule"]["currency"], "CNY")
        self.clock.now += 21
        self.add("2", currencyCode="THB")
        self.tick(21)
        self.assertEqual(len(self.executor.submitted), 2)
        self.assertEqual({group["status"] for group in self.groups()}, {"SUCCEEDED"})
        self.service.stop()
        self.service = FollowService(self.config, self.executor, self.clock)
        with self.service.store.connect(write=True) as connection:
            self.service.store.set_meta(connection, "cursor", 0)
        self.tick()
        self.assertEqual(len(self.executor.submitted), 2)

    def test_existing_home_batch_is_not_extended_after_restart(self):
        self.enable(amount_mode="ratio", amount_value="10")
        self.add(currencyCode="THB", stake="100")
        self.tick()
        first = self.groups()[0]
        self.assertEqual((first["status"], first["source_stake"], first["target_stake"]),
                         ("SUCCEEDED", "100", "10"))
        self.service.stop()
        self.service = FollowService(self.config, self.executor, self.clock)
        self.add("2", currencyCode="CNY", stake="200")
        self.tick()
        self.assertEqual(len(self.groups()), 1)
        group = self.groups()[0]
        self.assertEqual((group["status"], group["source_stake"], group["target_stake"]),
                         ("SUCCEEDED", "100", "10"))
        self.assertEqual(group["order_count"], 1)
        self.assertEqual(len(self.executor.submitted), 1)
        members = self.service.detail(group["id"])["members"]
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0]["reported_currency"], "THB")

    def test_same_order_number_different_members_and_replayed_events(self):
        self.enable()
        self.add("1", "M1")
        self.tick()
        self.add("1", "M2")
        self.tick()
        with self.service.store.connect() as connection:
            self.service.store.set_meta(connection, "cursor", 0)
        self.tick()
        self.assertEqual(len(self.groups()), 1)
        self.assertEqual(self.groups()[0]["order_count"], 1)
        self.assertEqual(len(self.service.detail(self.groups()[0]["id"])["members"]), 1)
        self.assertEqual(len(self.executor.submitted), 1)

    def test_restart_does_not_resubmit_a_processed_home_batch(self):
        self.enable()
        self.add()
        self.tick()
        self.assertEqual(self.groups()[0]["status"], "SUCCEEDED")
        self.service.stop()
        self.service = FollowService(self.config, self.executor, self.clock)
        self.tick()
        self.assertEqual(self.groups()[0]["status"], "SUCCEEDED")
        self.service.stop()
        self.service = FollowService(self.config, self.executor, self.clock)
        self.tick()
        self.assertEqual(len(self.executor.submitted), 1)

    def test_submit_exception_is_recorded_as_failure_and_never_retried(self):
        self.executor.result = TimeoutError("模拟提交响应丢失")
        self.enable()
        self.add()
        self.tick()
        self.assertEqual(self.groups()[0]["status"], "REJECTED")
        self.service.stop()
        self.service = FollowService(self.config, self.executor, self.clock)
        self.tick(30)
        self.assertEqual(self.groups()[0]["status"], "REJECTED")
        self.assertEqual(len(self.executor.submitted), 1)

    def test_timeout_is_failure_and_never_retried(self):
        self.executor.result = TimeoutError("不可显示的连接细节")
        self.enable()
        self.add()
        self.tick(21)
        self.assertEqual(self.groups()[0]["status"], "REJECTED")
        self.assertNotIn("不可显示", self.groups()[0]["message"])
        self.tick(30)
        self.assertEqual(len(self.executor.submitted), 1)

    def test_success_requires_ticket_but_not_echoed_amount(self):
        self.executor.result = {"status": "SUCCEEDED", "receipt_id": "TICKET-1"}
        self.enable()
        self.add()
        self.tick(21)
        group = self.groups()[0]
        self.assertEqual(group["status"], "SUCCEEDED")
        self.assertIsNone(group["accepted_stake"])
        self.assertEqual(self.service.snapshot()["summary"]["pending_amount_count"], 1)
        self.assertEqual(self.service.snapshot()["summary"]["confirmed_stake"], "0")

    def test_success_without_ticket_is_rejected(self):
        self.executor.result = {"status": "SUCCEEDED", "accepted_stake": "50"}
        self.enable()
        self.add()
        self.tick(21)
        self.assertEqual(self.groups()[0]["status"], "REJECTED")
        self.assertEqual(self.service.snapshot("SUCCEEDED")["rows"], [])

    def test_quote_wrong_currency_or_direction_cannot_submit(self):
        self.enable()
        self.add()
        self.executor.quote_changes = {"currency": "USD"}
        self.tick(21)
        self.assertEqual(self.groups()[0]["status"], "WAIT_MARKET")
        self.assertEqual(self.executor.submitted, [])
        self.executor.quote_changes = {"side": "AWAY"}
        self.tick(4)
        self.assertEqual(self.executor.submitted, [])

    def test_pause_during_prepare_prevents_submit(self):
        self.enable()
        self.add()
        self.executor.on_prepare = self.service.pause
        self.tick(21)
        self.assertEqual(self.groups()[0]["status"], "SKIPPED")
        self.assertEqual(self.executor.submitted, [])

    def test_rule_edit_keeps_existing_group_snapshot(self):
        self.enable(direction="forward")
        self.add()
        self.tick()
        self.enable(direction="reverse", amount_value="99")
        self.tick(21)
        self.assertEqual(self.executor.submitted[0][1], "50")
        self.assertEqual(self.executor.prepared[0]["side"], "HOME")

    def test_settled_source_blocks_frozen_group(self):
        self.mappings.unmap("pin:101")
        self.enable()
        self.add()
        self.tick()
        self.assertEqual(self.groups()[0]["status"], "WAIT_MAPPING")
        settled = {**self.rows[0], "status": "SETTLED"}
        self.commit([settled], "SETTLED")
        self.mappings.map_events(self.ps, self.crown)
        self.tick(3)
        self.assertEqual(self.groups()[0]["status"], "SKIPPED")
        self.assertEqual(self.executor.submitted, [])

    def test_missing_mapping_waits_then_uses_only_confirmed_pair(self):
        self.mappings.unmap("pin:101")
        self.enable()
        self.add()
        self.tick()
        self.assertEqual(self.groups()[0]["status"], "WAIT_MAPPING")
        self.mappings.map_events(self.ps, self.crown)
        self.tick(21)
        self.assertEqual(self.groups()[0]["status"], "SUCCEEDED")

    def test_unbound_league_blocks_saved_fixture(self):
        self.mappings.unmap_league("77")
        self.enable()
        self.add()
        self.tick(21)
        self.assertEqual(self.groups()[0]["status"], "WAIT_MAPPING")
        self.assertEqual(self.executor.submitted, [])

    def test_source_id_conflict_never_falls_back_to_names(self):
        raw = raw_order()
        raw["choices"][0]["eventId"] = "different"
        intent = source_intent(parse_order(raw, "A"))
        with self.assertRaises(ValueError):
            resolve_fixture(self.config["merge_db_path"], intent)

    def test_source_without_event_id_can_resolve_confirmed_same_platform_identity(self):
        raw = raw_order()
        del raw["choices"][0]["eventId"]
        intent = source_intent(parse_order(raw, "A"))
        self.assertEqual(
            resolve_fixture(self.config["merge_db_path"], intent)["crownId"], "crown-1"
        )

    def test_amount_below_min_stake_is_skipped(self):
        self.enable(amount_mode="fixed", amount_value="60", min_stake=100)
        self.add()
        self.tick(21)
        group = self.groups()[0]
        self.assertEqual(group["status"], "SKIPPED")
        self.assertIn("最低投注 100", group["message"])
        self.assertEqual(self.executor.submitted, [])

    def test_split_parts_never_go_below_min_stake(self):
        self.enable(amount_mode="fixed", amount_value="250", min_stake=100, split_count=4)
        self.add()
        self.tick(21)
        group = self.groups()[0]
        self.assertNotEqual(group["status"], "SKIPPED")
        parts = [item for item in group.get("split_parts") or []]
        if parts:
            self.assertTrue(all(float(item["stake"]) >= 100 for item in parts))

    def test_account_with_enough_balance_is_preferred(self):
        self.executor.account_ids = ["crown-small", "crown-rich", "crown-tight"]
        self.executor.balances = {"crown-small": 0, "crown-rich": 5000, "crown-tight": 0}
        self.enable()
        self.add()
        self.tick(21)
        self.assertEqual(self.groups()[0]["account_id"], "crown-rich")
        self.assertEqual(len(self.executor.submitted), 1)

    def test_insufficient_balance_waits_instead_of_submitting(self):
        self.executor.account_ids = ["crown-small", "crown-tight"]
        self.executor.balances = {"crown-small": 0, "crown-tight": 0}
        self.enable()
        self.add()
        self.tick(21)
        group = self.groups()[0]
        self.assertEqual(group["status"], "WAIT_ACCOUNT")
        self.assertEqual(group["error_category"], "ACCOUNT_CREDIT")
        self.assertIn("额度不足", group["waiting_reason"])
        self.assertEqual(self.executor.submitted, [])

    def test_kickoff_time_is_not_used_when_the_mapping_is_confirmed(self):
        # 映射已确认"这场比赛 ↔ 那场比赛"，执行期不再拿开赛时间淘汰。
        intent = source_intent(parse_order(raw_order(), "A"))
        for drift_hours in (-5, -1, 1, 5, 30):
            shifted = {**intent, "event_ms": intent["event_ms"] + drift_hours * 3600 * 1000}
            with self.subTest(drift=drift_hours):
                self.assertEqual(
                    resolve_fixture(self.config["merge_db_path"], shifted)["crownId"], "crown-1"
                )

    def test_late_order_never_changes_completed_group(self):
        self.enable()
        self.add()
        self.tick()
        self.add("2", createdDate=1_793_600_010_000)
        self.tick()
        self.assertEqual(len(self.executor.submitted), 1)
        self.assertEqual(len(self.groups()), 1)
        self.assertEqual(self.groups()[0]["status"], "SUCCEEDED")
        self.assertEqual(self.groups()[0]["order_count"], 1)

    def test_event_gap_disables_rule_without_replaying(self):
        self.enable()
        self.add("1")
        self.add("2")
        with self.orders.db:
            self.orders.db.execute("DELETE FROM partner_order_events WHERE event_id=1")
        self.tick()
        snapshot = self.service.snapshot()
        self.assertFalse(snapshot["rule"]["enabled"])
        self.assertIn("缺口", snapshot["service"]["message"])
        self.assertEqual(self.executor.submitted, [])

    def test_changed_mapping_during_prepare_blocks_submit(self):
        self.enable()
        self.add()
        self.executor.on_prepare = lambda: self.mappings.map_events(
            self.ps, {**self.crown, "homeId": "changed"}
        )
        self.tick(21)
        self.assertEqual(self.groups()[0]["status"], "SKIPPED")
        self.assertEqual(self.executor.submitted, [])

    def test_deadline_during_prepare_blocks_submit(self):
        self.enable(max_age_seconds=30)
        self.add()
        self.executor.on_prepare = lambda: setattr(self.clock, "now", self.clock.now + 20)
        self.tick(21)
        self.assertEqual(self.executor.submitted, [])
        self.tick()
        self.assertEqual(self.groups()[0]["status"], "EXPIRED")

    def test_invalid_order_is_recorded_without_disrupting_other_orders(self):
        self.enable()
        raw = raw_order("invalid")
        raw["choices"][0]["periodType"] = 9
        self.rows.append(raw)
        self.add("valid", periodType=1)
        self.tick()
        self.assertEqual({group["status"] for group in self.groups()}, {"SKIPPED", "SUCCEEDED"})

    def test_two_service_instances_claim_only_one_submission(self):
        self.enable()
        self.add()
        second = FollowService(self.config, self.executor, self.clock)
        first_preparing = threading.Event()
        release_prepare = threading.Event()

        def prepared():
            first_preparing.set()
            if not release_prepare.wait(timeout=3):
                raise AssertionError("未及时释放查盘请求")

        self.executor.on_prepare = prepared
        errors = []

        def run(service):
            try:
                service.run_once()
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=run, args=(service,)) for service in (self.service, second)
        ]
        try:
            self.executor.on_prepare = prepared
            threads[0].start()
            self.assertTrue(first_preparing.wait(timeout=3))
            threads[1].start()
            threads[1].join(timeout=2)
            self.assertFalse(threads[1].is_alive())
            self.assertEqual(len(self.executor.prepared), 1)
            release_prepare.set()
            for thread in threads:
                thread.join(timeout=5)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(len(self.executor.submitted), 1)
            self.assertEqual(self.groups()[0]["status"], "SUCCEEDED")
        finally:
            release_prepare.set()
            second.stop()

    def test_stale_status_cannot_replace_updated_group_amount(self):
        self.mappings.unmap("pin:101")
        self.enable(amount_mode="ratio", amount_value="10")
        self.add("1", stake="100")
        self.tick()
        self.assertEqual(self.groups()[0]["status"], "WAIT_MAPPING")
        with self.service.store.connect() as connection:
            stale = self.service.store.active_groups(connection)[0]
        stale_snapshot = copy.deepcopy(stale)
        self.service._set_status(stale, "WAIT_MAPPING", "赛事映射仍未恢复")
        self.assertFalse(self.service._set_status(stale_snapshot, "READY", "旧快照已过期"))
        group = self.groups()[0]
        self.assertEqual((group["source_stake"], group["order_count"]), ("100", 1))
        self.assertNotIn("_revision", group)
        with self.service.store.connect() as connection:
            with self.assertRaisesRegex(RuntimeError, "旧快照"):
                self.service.store.save_group(connection, stale_snapshot)
        self.assertEqual(self.executor.submitted, [])

    def test_status_read_and_update_exclude_competing_claim(self):
        self.mappings.unmap("pin:101")
        self.enable()
        self.add()
        self.tick()
        store = self.service.store
        original_get = store.get_group
        with store.connect() as connection:
            group = store.active_groups(connection)[0]
        competing_writes = []

        def read_and_try_claim(connection, group_id):
            current = original_get(connection, group_id)
            competing = sqlite3.connect(store.path, timeout=0)
            try:
                try:
                    competing.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as exc:
                    self.assertIn("locked", str(exc))
                    competing_writes.append("blocked")
                else:
                    claim = original_get(competing, group_id)
                    claim["status"] = "SUBMITTING"
                    store.save_group(competing, claim)
                    competing.commit()
                    competing_writes.append("claimed")
            finally:
                competing.close()
            return current

        with patch.object(store, "get_group", side_effect=read_and_try_claim):
            self.assertTrue(self.service._set_status(group, "WAIT_MARKET", "查询稍后重试"))
        self.assertEqual(competing_writes, ["blocked"])
        with store.connect(write=True) as connection:
            claimed = original_get(connection, group["id"])
            claimed["status"] = "SUBMITTING"
            store.save_group(connection, claimed)
        self.assertFalse(self.service._set_status(group, "READY", "旧请求返回"))
        self.assertEqual(self.groups()[0]["status"], "SUBMITTING")

    def test_prepare_error_after_pause_cannot_restore_active_group(self):
        self.enable()
        self.add()

        def pause_and_fail():
            self.service.pause()
            raise ValueError("模拟查询失败")

        self.executor.on_prepare = pause_and_fail
        self.tick(21)
        self.assertEqual(self.groups()[0]["status"], "SKIPPED")
        self.assertEqual(self.executor.submitted, [])

    def test_recovery_holds_write_lock_and_late_receipt_finishes_attempt(self):
        self.enable()
        self.add()
        store = self.service.store
        original_save = store.save_group
        competing_writes = []

        def recover_save(connection, group):
            competing = sqlite3.connect(store.path, timeout=0)
            try:
                try:
                    competing.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as exc:
                    self.assertIn("locked", str(exc))
                    competing_writes.append("blocked")
                else:
                    competing_writes.append("acquired")
                    competing.rollback()
            finally:
                competing.close()
            original_save(connection, group)

        def recover_while_request_inflight(quote, stake, account_id):
            with patch.object(store, "save_group", side_effect=recover_save):
                store.recover_inflight(self.clock())
            self.assertEqual(self.groups()[0]["status"], "REJECTED")
            return {"status": "SUCCEEDED", "receipt_id": "LATE-TICKET"}

        with patch.object(self.executor, "submit", side_effect=recover_while_request_inflight):
            self.tick(21)
        self.assertEqual(competing_writes, ["blocked"])
        self.assertEqual(self.groups()[0]["status"], "SUCCEEDED")
        detail = self.service.detail(self.groups()[0]["id"])
        self.assertEqual(detail["attempts"][0]["status"], "SUCCEEDED")
        store.recover_inflight(self.clock())
        self.assertEqual(self.groups()[0]["receipt_id"], "LATE-TICKET")

    def test_old_event_batch_cannot_roll_back_another_service_cursor(self):
        self.enable()
        self.add("1")
        second = FollowService(self.config, self.executor, self.clock)
        original_events = self.service._events

        def events_then_other_consumer(cursor):
            batch = original_events(cursor)
            self.add("2")
            second.run_once()
            return batch

        try:
            with patch.object(self.service, "_events", side_effect=events_then_other_consumer):
                self.tick()
            with self.service.store.connect() as connection:
                self.assertEqual(self.service.store.meta(connection, "cursor"), 2)
            self.assertEqual(len(self.groups()), 1)
            self.assertEqual(self.groups()[0]["order_count"], 2)
            self.assertEqual(len(self.executor.submitted), 1)
        finally:
            second.stop()

    def test_pause_during_source_read_prevents_old_rule_ingestion(self):
        self.enable()
        self.add()
        second = FollowService(self.config, self.executor, self.clock)
        original_events = self.service._events

        def events_then_pause(cursor):
            batch = original_events(cursor)
            second.pause()
            return batch

        try:
            with patch.object(self.service, "_events", side_effect=events_then_pause):
                self.tick()
            self.assertFalse(self.service.snapshot()["rule"]["enabled"])
            self.assertEqual(self.groups(), [])
            with self.service.store.connect() as connection:
                self.assertEqual(self.service.store.meta(connection, "cursor"), 0)
        finally:
            second.stop()

    def test_search_matches_member_and_original_order_with_pagination(self):
        self.enable()
        self.add("SOURCE-ORDER-1", "UNIQUE-MEMBER")
        self.tick(21)
        for query in ("source-order-1", "unique-member", "测试甲"):
            result = self.service.snapshot("SUCCEEDED", search=query, limit=1)
            self.assertEqual(result["total"], 1)
            self.assertEqual(len(result["rows"]), 1)
        result = self.service.snapshot("ALL", search="UNIQUE-MEMBER", limit=1, offset=1)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["rows"], [])

    def test_legacy_success_without_receipt_is_never_presented_as_success(self):
        self.enable()
        self.add()
        self.tick()
        with self.service.store.connect() as connection:
            group = self.service.store.get_group(connection, self.groups()[0]["id"])
            group.update(status="SUCCEEDED", receipt_id=None)
            self.service.store.save_group(connection, group)
        result = self.service.snapshot("SUCCEEDED")
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["summary"]["succeeded"], 0)
        self.assertEqual(result["summary"]["unknown"], 1)
        self.assertEqual(self.service.detail(group["id"])["group"]["status"], "UNKNOWN")

    def test_quote_for_different_account_never_submits(self):
        self.enable()
        self.add()
        self.executor.quote_changes = {"account_id": "another-account"}
        self.tick(21)
        self.assertEqual(self.executor.submitted, [])
        self.assertIn("账号", self.groups()[0]["message"])

    def test_missing_detail_has_distinct_not_found_error(self):
        with self.assertRaises(KeyError):
            self.service.detail("nonexistent")


if __name__ == "__main__":
    unittest.main()
