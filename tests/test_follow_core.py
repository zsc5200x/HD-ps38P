"""持久合组与恢复测试，所有订单、映射和执行器均为本地模拟。"""

import copy
import json
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
        self.prepared = []
        self.submitted = []
        self.result = None
        self.on_prepare = None
        self.quote_changes = {}

    def validate_account(self, account_id, currency):
        if account_id != "crown-test" or currency != "CNY":
            raise ValueError("测试账号不匹配")

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
        payload = {"enabled": True, "account_id": "crown-test", "amount_value": "100"}
        self.assertEqual(validate_rule(payload)["currency"], "CNY")
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
                "account_id": "crown-test",
                "currency": "CNY",
                "amount_mode": "fixed",
                "amount_value": "50",
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
        self.tick(30)
        self.assertEqual(self.groups(), [])
        self.enable()
        self.tick()
        self.assertEqual(self.groups(), [])
        self.add("2")
        self.tick()
        self.assertEqual(self.groups()[0]["status"], "GROUPING")

    def test_group_three_orders_ratio_reverse_and_real_receipt(self):
        self.enable(amount_mode="ratio", amount_value="10", direction="reverse")
        self.add(stake="100")
        self.tick()
        self.clock.now += 8
        self.add("2", "M2", stake="200")
        self.tick()
        self.clock.now += 11
        self.add("3", "M3", stake="300")
        self.tick()
        self.assertEqual(len(self.executor.submitted), 0)
        self.tick(2)
        group = self.groups()[0]
        self.assertEqual(
            (group["status"], group["order_count"], group["member_count"]), ("SUCCEEDED", 3, 3)
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
        self.assertEqual(len(detail["members"]), 3)
        self.assertNotIn("raw_json", str(detail))
        self.tick(30)
        self.assertEqual(len(self.executor.submitted), 1)

    def test_exact_twenty_second_boundary_and_later_group(self):
        self.enable()
        self.add()
        self.tick()
        self.clock.now += 20
        self.add("2")
        self.tick()
        self.assertEqual(self.groups()[0]["order_count"], 2)
        self.clock.now += 0.001
        self.add("3")
        self.tick()
        self.assertEqual(len(self.groups()), 2)
        self.assertEqual(len(self.executor.submitted), 1)
        self.assertEqual(self.executor.submitted[0][1], "50")

    def test_thb_fixed_stake_remains_one_hundred_cny(self):
        self.enable(amount_value="100")
        self.add(currencyCode="THB", stake="800")
        self.tick(21)
        group = self.groups()[0]
        self.assertEqual((group["status"], group["currency"]), ("SUCCEEDED", "CNY"))
        self.assertEqual((group["source_stake"], group["target_stake"]), ("800", "100"))
        self.assertEqual(self.executor.submitted[0][1], "100")
        detail = self.service.detail(group["id"])
        self.assertEqual(detail["members"][0]["reported_currency"], "THB")
        self.assertEqual(detail["members"][0]["currency"], "CNY")

    def test_thb_cny_rmb_join_one_ratio_group_without_conversion(self):
        self.enable(amount_mode="ratio", amount_value="10")
        for order_id, currency, stake in (
            ("1", "THB", "100"),
            ("2", "CNY", "200"),
            ("3", "RMB", "309"),
        ):
            self.add(order_id, currencyCode=currency, stake=stake)
            self.tick()
        self.tick(21)
        self.assertEqual(len(self.groups()), 1)
        group = self.groups()[0]
        self.assertEqual((group["source_stake"], group["target_stake"]), ("609", "60"))
        self.assertEqual(self.executor.submitted[0][1], "60")
        self.assertEqual(
            {item["reported_currency"] for item in self.service.detail(group["id"])["members"]},
            {"THB", "CNY", "RMB"},
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
            group = self.service.store.active_groups(connection)[0]
            group.update(status="SKIPPED", message="原单币种与规则币种不同", currency="THB")
            self.service.store.save_group(connection, group)
        self.service.stop()
        self.service = FollowService(self.config, self.executor, self.clock)
        self.assertEqual(self.service.snapshot()["rule"]["currency"], "CNY")
        self.clock.now += 21
        self.add("2", currencyCode="THB")
        self.tick(21)
        self.assertEqual(len(self.executor.submitted), 1)
        self.assertEqual({group["status"] for group in self.groups()}, {"SKIPPED", "SUCCEEDED"})
        self.service.stop()
        self.service = FollowService(self.config, self.executor, self.clock)
        with self.service.store.connect(write=True) as connection:
            self.service.store.set_meta(connection, "cursor", 0)
        self.tick()
        self.assertEqual(len(self.executor.submitted), 1)

    def test_pending_legacy_thb_group_restores_and_combines_cny_order(self):
        self.enable(amount_mode="ratio", amount_value="10")
        self.add(currencyCode="THB", stake="100")
        self.tick()
        with self.service.store.connect(write=True) as connection:
            group = self.service.store.active_groups(connection)[0]
            group["currency"] = group["intent"]["currency"] = group["rule"]["currency"] = "THB"
            group["intent"].pop("reported_currency")
            group["base_key"] = self.service._base_key(group["intent"], group["rule"])
            self.service.store.save_group(connection, group)
            member = self.service.store.get_members(connection, group["id"])[0]
            member["currency"] = "THB"
            member.pop("reported_currency")
            connection.execute(
                "UPDATE follow_members SET payload=? WHERE group_id=?",
                (json.dumps(member), group["id"]),
            )
        self.service.stop()
        self.service = FollowService(self.config, self.executor, self.clock)
        self.add("2", currencyCode="CNY", stake="200")
        self.tick(21)
        self.assertEqual(len(self.groups()), 1)
        group = self.groups()[0]
        self.assertEqual(
            (group["status"], group["source_stake"], group["target_stake"]),
            ("SUCCEEDED", "300", "30"),
        )
        self.assertEqual(len(self.executor.submitted), 1)
        self.assertEqual(self.service.detail(group["id"])["members"][0]["reported_currency"], "THB")

    def test_same_order_number_different_members_and_replayed_events(self):
        self.enable()
        self.add("1", "M1")
        self.add("1", "M2")
        self.tick()
        self.tick(21)
        with self.service.store.connect() as connection:
            self.service.store.set_meta(connection, "cursor", 0)
        self.tick()
        self.assertEqual(self.groups()[0]["order_count"], 2)
        self.assertEqual(len(self.executor.submitted), 1)

    def test_restart_recovers_pending_group_without_duplicate_submission(self):
        self.enable()
        self.add()
        self.tick()
        self.service.stop()
        self.service = FollowService(self.config, self.executor, self.clock)
        self.tick(21)
        self.assertEqual(self.groups()[0]["status"], "SUCCEEDED")
        self.service.stop()
        self.service = FollowService(self.config, self.executor, self.clock)
        self.tick()
        self.assertEqual(len(self.executor.submitted), 1)

    def test_restart_inflight_becomes_unknown(self):
        self.enable()
        self.add()
        self.tick()
        with self.service.store.connect() as connection:
            group = self.service.store.active_groups(connection)[0]
            group["status"] = "SUBMITTING"
            self.service.store.save_group(connection, group)
        self.service.stop()
        self.service = FollowService(self.config, self.executor, self.clock)
        self.tick(30)
        self.assertEqual(self.groups()[0]["status"], "UNKNOWN")
        self.assertEqual(self.executor.submitted, [])

    def test_timeout_is_unknown_and_never_retried(self):
        self.executor.result = TimeoutError("不可显示的连接细节")
        self.enable()
        self.add()
        self.tick(21)
        self.assertEqual(self.groups()[0]["status"], "UNKNOWN")
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

    def test_success_without_ticket_is_unknown(self):
        self.executor.result = {"status": "SUCCEEDED", "accepted_stake": "50"}
        self.enable()
        self.add()
        self.tick(21)
        self.assertEqual(self.groups()[0]["status"], "UNKNOWN")
        self.assertEqual(self.service.snapshot()["rows"], [])

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
        self.enable()
        self.add()
        self.tick()
        settled = {**self.rows[0], "status": "SETTLED"}
        self.commit([settled], "SETTLED")
        self.tick(21)
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

    def test_late_order_never_changes_completed_group(self):
        self.enable()
        self.add()
        self.tick(21)
        self.add("2", createdDate=1_793_600_010_000)
        self.tick()
        self.assertEqual(len(self.executor.submitted), 1)
        self.assertEqual({group["status"] for group in self.groups()}, {"SKIPPED", "SUCCEEDED"})

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
        self.add("valid")
        self.tick(21)
        self.assertEqual({group["status"] for group in self.groups()}, {"SKIPPED", "SUCCEEDED"})

    def test_two_service_instances_claim_only_one_submission(self):
        self.enable()
        self.add()
        self.tick()
        second = FollowService(self.config, self.executor, self.clock)
        self.clock.now += 21
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
        self.enable(amount_mode="ratio", amount_value="10")
        self.add("1", stake="100")
        self.tick()
        with self.service.store.connect() as connection:
            stale = self.service.store.active_groups(connection)[0]
        self.clock.now += 5
        self.add("2", stake="500")
        self.tick()
        self.assertFalse(self.service._set_status(stale, "READY", "旧快照已过期"))
        group = self.groups()[0]
        self.assertEqual((group["source_stake"], group["order_count"]), ("600", 2))
        self.assertNotIn("_revision", group)
        with self.service.store.connect() as connection:
            with self.assertRaisesRegex(RuntimeError, "旧快照"):
                self.service.store.save_group(connection, stale)
        self.tick(16)
        self.assertEqual(self.executor.submitted[0][1], "60")

    def test_status_read_and_update_exclude_competing_claim(self):
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
            self.assertEqual(self.groups()[0]["status"], "UNKNOWN")
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
            self.assertEqual(self.groups()[0]["order_count"], 2)
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
            group = self.service.store.active_groups(connection)[0]
            group.update(status="SUCCEEDED", receipt_id=None)
            self.service.store.save_group(connection, group)
        result = self.service.snapshot()
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
