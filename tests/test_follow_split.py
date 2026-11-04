"""分单金额拆分与单场/盘口累计限额。"""

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from ps38app.follow.limits import apply_limits, limit_identity, load_usage
from ps38app.follow.rules import DEFAULT_RULE, split_stakes, validate_rule
from ps38app.follow.store import FollowStore
from tests import test_follow_core as core


def make_group(**extra):
    group = {
        "id": "a" * 32,
        "base_key": "base",
        "start_ms": 1,
        "close_ms": 2,
        "status": "READY",
        "account_id": "crown-1",
        "mapping": {"crownId": "crown-1", "ps38Id": "pin:1"},
        "market_type": "HANDICAP",
        "target_side": "HOME",
        "source_side": "HOME",
        "target_stake": "100",
        "created_at": "2026-11-04T00:00:00+08:00",
        "updated_at": "2026-11-04T00:00:00+08:00",
    }
    group.update(extra)
    return group


class SplitStakesTests(unittest.TestCase):
    def test_split_is_even_and_keeps_total(self):
        self.assertEqual(split_stakes(1000, 2), [500, 500])
        self.assertEqual(split_stakes(1001, 2), [501, 500])
        self.assertEqual(split_stakes(100, 3), [34, 33, 33])
        self.assertEqual(sum(split_stakes(999, 7)), 999)

    def test_split_edge_cases(self):
        self.assertEqual(split_stakes(1200, 1), [1200])
        self.assertEqual(split_stakes(1, 5), [1])
        self.assertEqual(split_stakes(0, 2), [])
        self.assertEqual(split_stakes(100, 0), [100])
        self.assertEqual(len(split_stakes(100, 500)), 50)


class SplitExecutionTests(unittest.TestCase):
    # 复用临时数据库和模拟执行器，不重复收集核心测试。
    setUp = core.FollowServiceTests.setUp
    tearDown = core.FollowServiceTests.tearDown
    commit = core.FollowServiceTests.commit
    enable = core.FollowServiceTests.enable
    add = core.FollowServiceTests.add
    tick = core.FollowServiceTests.tick
    groups = core.FollowServiceTests.groups

    def test_completed_split_releases_account_for_next_group(self):
        self.enable(amount_value="200", min_stake=100, split_count=2)
        self.add("first")
        self.tick()

        first = self.groups()[0]
        self.assertEqual(first["status"], "SUCCEEDED")
        self.assertEqual(len(self.executor.submitted), 2)
        with self.service.store.connect() as connection:
            claims = connection.execute("SELECT COUNT(*) FROM follow_claims").fetchone()[0]
        self.assertEqual(claims, 0)

        self.clock.now += 21
        self.add("second")
        self.tick()

        groups = self.groups()
        self.assertEqual(len(groups), 2)
        self.assertTrue(all(group["status"] == "SUCCEEDED" for group in groups))
        self.assertEqual(len(self.executor.submitted), 4)
        self.assertEqual(
            {account_id for _, _, account_id in self.executor.submitted}, {"crown-test"}
        )
        with self.service.store.connect() as connection:
            claims = connection.execute("SELECT COUNT(*) FROM follow_claims").fetchone()[0]
        self.assertEqual(claims, 0)


class RuleSplitValidationTests(unittest.TestCase):
    def test_split_fields_are_validated(self):
        rule = validate_rule(
            {
                "enabled": False,
                "amount_mode": "fixed",
                "amount_value": "100",
                "split_count": 3,
                "split_delay_seconds": 2,
                "match_limit_amount": "500",
                "market_limit_amount": 300,
            }
        )
        self.assertEqual(rule["split_count"], 3)
        self.assertEqual(rule["split_delay_seconds"], 2)
        self.assertEqual(rule["match_limit_amount"], "500")
        self.assertEqual(rule["market_limit_amount"], "300")

    def test_invalid_split_fields_are_rejected(self):
        for payload in (
            {"split_count": 0},
            {"split_count": 51},
            {"split_delay_seconds": 61},
            {"match_limit_amount": -1},
            {"market_limit_amount": "abc"},
        ):
            with self.assertRaises(ValueError):
                validate_rule({"enabled": False, "amount_value": "100", **payload})
        self.assertEqual(DEFAULT_RULE["split_count"], 1)


class LimitIdentityTests(unittest.TestCase):
    def test_total_market_splits_over_and_under(self):
        over = limit_identity(make_group(market_type="TOTAL", target_side="OVER"))
        under = limit_identity(make_group(market_type="TOTAL", target_side="UNDER"))
        self.assertEqual(over["market_code"], "total_over")
        self.assertEqual(under["market_code"], "total_under")
        self.assertNotEqual(over["market_key"], under["market_key"])
        self.assertEqual(over["match_key"], "crown-1")

    def test_handicap_shares_one_market_key(self):
        first = limit_identity(make_group(target_line="-0.5"))
        second = limit_identity(make_group(target_line="-1.5"))
        self.assertEqual(first["market_key"], second["market_key"])


class ApplyLimitTests(unittest.TestCase):
    def usage(self, match=0, market=0, match_reserved=0, market_reserved=0):
        return {
            "match_committed": {"crown-1": Decimal(match)},
            "market_committed": {"crown-1|handicap": Decimal(market)},
            "match_reserved": {"crown-1": Decimal(match_reserved)},
            "market_reserved": {"crown-1|handicap": Decimal(market_reserved)},
        }

    def test_no_limits_passes_through(self):
        allowed, reason, meta = apply_limits(
            make_group(target_stake="1000"),
            {"match_limit_amount": 0, "market_limit_amount": 0},
            self.usage(),
        )
        self.assertEqual((allowed, reason), (1000, ""))
        self.assertFalse(meta.get("limited"))

    def test_match_limit_caps_to_remaining(self):
        allowed, reason, meta = apply_limits(
            make_group(target_stake="1000"),
            {"match_limit_amount": 800, "market_limit_amount": 0},
            self.usage(match=300),
        )
        self.assertEqual(allowed, 500)
        self.assertTrue(meta["limited"])
        self.assertEqual(meta["match_remaining_amount"], "500")

    def test_market_limit_uses_committed_and_reserved(self):
        allowed, reason, meta = apply_limits(
            make_group(target_stake="1000"),
            {"match_limit_amount": 0, "market_limit_amount": 600},
            self.usage(market=200, market_reserved=300),
        )
        self.assertEqual(allowed, 100)
        self.assertEqual(meta["market_remaining_amount"], "100")

    def test_insufficient_remaining_is_skipped(self):
        allowed, reason, _ = apply_limits(
            make_group(target_stake="1000"),
            {"match_limit_amount": 100, "market_limit_amount": 0},
            self.usage(match=100),
        )
        self.assertEqual(allowed, 0)
        self.assertIn("单场限额", reason)

    def test_limit_without_match_identity_is_blocked(self):
        group = make_group(mapping={})
        allowed, reason, _ = apply_limits(
            group,
            {"match_limit_amount": 500, "market_limit_amount": 0},
            self.usage(),
        )
        self.assertEqual(allowed, 0)
        self.assertIn("无法识别比赛", reason)


class UsageStoreTests(unittest.TestCase):
    def test_success_and_partial_count_as_committed_active_as_reserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "follow.sqlite3"
            store = FollowStore(path)
            groups = [
                make_group(id="1" * 32, status="SUCCEEDED", accepted_stake="300"),
                make_group(id="2" * 32, status="PARTIAL", accepted_stake="120"),
                make_group(id="3" * 32, status="READY", target_stake="80"),
                make_group(id="4" * 32, status="UNKNOWN", target_stake="40"),
            ]
            with store.connect(write=True) as connection:
                for group in groups:
                    store.save_group(connection, group)
            with store.connect() as connection:
                usage = load_usage(connection, exclude_group_id="3" * 32)
            self.assertEqual(usage["match_committed"]["crown-1"], Decimal("420"))
            self.assertEqual(usage["market_committed"]["crown-1|handicap"], Decimal("420"))
            self.assertEqual(usage["match_reserved"]["crown-1"], Decimal("40"))
