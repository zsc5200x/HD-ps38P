import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ps38app.fixture_merge import (
    FixtureMergeStore,
    MappingConflict,
    build_aliases,
    competition_conflict,
    normalize_league,
    normalize_team,
    parse_crown_start,
    ps38_league_id,
    prepare_crown_event,
    rank_fixture_candidates,
    rank_league_candidates,
)


def ps_event(name, league, kickoff="2026-11-03T04:00:00+00:00", league_id=None):
    home, away = name.split(" vs ")
    return {
        "id": f"p38_{name}",
        "source": "ps38",
        "name": name,
        "home": home,
        "away": away,
        "league": league,
        "leagueId": league_id or f"p38:{league}",
        "kickoffUtc": kickoff,
        "kickoffLocal": kickoff,
        "isLive": False,
        "orders": 5,
        "stake": 1000.0,
    }


def crown_event(name, league, league_id, label):
    home, away = name.split(" vs ")
    return {
        "id": f"crown_{name}",
        "home": home,
        "away": away,
        "league": league,
        "league_id": league_id,
        "start_label": label,
        "is_live": False,
    }


class NormalizationTests(unittest.TestCase):
    def test_league_synonyms_meet(self):
        self.assertEqual(
            normalize_league("哥伦比亚 - 甲级联赛"),
            normalize_league("哥伦比亚甲组联赛"),
        )

    def test_team_suffixes_removed(self):
        self.assertEqual(normalize_team("哥本哈根足球俱乐部"), normalize_team("哥本哈根"))
        self.assertEqual(normalize_team("Newcastle United FC"), normalize_team("Newcastle United"))

    def test_competition_classes_conflict(self):
        self.assertTrue(competition_conflict("土耳其超级联赛", "土耳其超级联赛U19"))
        self.assertTrue(competition_conflict("英格兰女子超级联赛", "英格兰超级联赛"))
        self.assertFalse(competition_conflict("荷兰乙组联赛", "荷兰 - 乙级联赛"))


class CrownTimeTests(unittest.TestCase):
    def test_crown_label_uses_gmt_minus_four(self):
        reference = datetime(2026, 11, 3, 2, 0, tzinfo=timezone.utc)
        parsed = parse_crown_start("11-02 09:15p", -240, reference)
        self.assertEqual(parsed, datetime(2026, 11, 3, 1, 15, tzinfo=timezone.utc))

    def test_iso_label_is_accepted(self):
        parsed = parse_crown_start("2026-11-03T04:00:00Z", -240)
        self.assertEqual(parsed, datetime(2026, 11, 3, 4, 0, tzinfo=timezone.utc))


class RankingTests(unittest.TestCase):
    def setUp(self):
        self.ps = ps_event("布卡拉曼加竞技 vs 麦德林国民竞技", "哥伦比亚 - 甲级联赛")
        self.crown = prepare_crown_event(
            crown_event("布卡拉曼加竞技 vs 麦德林国民竞技", "哥伦比亚甲组联赛", "1001", "11-03 12:00a"),
            -240,
            datetime(2026, 11, 3, 2, 0, tzinfo=timezone.utc),
        )

    def test_exact_time_match_ranks_high(self):
        rows = rank_fixture_candidates(
            [self.ps], [self.crown], None, {"1001": self.ps["leagueId"]}, limit=3
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["crownId"], self.crown["id"])
        self.assertGreaterEqual(rows[0]["score"], 70)
        self.assertEqual(rows[0]["kickoffDeltaMinutes"], 0.0)

    def test_time_mismatch_is_flagged(self):
        late = prepare_crown_event(
            crown_event("布卡拉曼加竞技 vs 麦德林国民竞技", "哥伦比亚甲组联赛", "1001", "11-03 02:15a"),
            -240,
            datetime(2026, 11, 3, 2, 0, tzinfo=timezone.utc),
        )
        rows = rank_fixture_candidates(
            [self.ps], [late], None, {"1001": self.ps["leagueId"]}, limit=3
        )
        self.assertTrue(rows[0]["timeConflict"])
        self.assertGreater(rows[0]["kickoffDeltaMinutes"], 15)

    def test_league_suggestion_uses_event_evidence(self):
        ps_events = [self.ps]
        rows = rank_league_candidates(ps_events, [self.crown], None, limit=3)
        self.assertTrue(rows)
        self.assertEqual(rows[0]["crownLeagueId"], "1001")
        self.assertGreaterEqual(rows[0]["evidenceCount"], 1)

    def test_alias_beats_translation(self):
        translated = prepare_crown_event(
            crown_event("布卡拉曼加体育会 vs 国家体育会", "哥伦比亚甲组联赛", "1001", "11-03 12:00a"),
            -240,
            datetime(2026, 11, 3, 2, 0, tzinfo=timezone.utc),
        )
        plain = rank_fixture_candidates(
            [self.ps], [translated], None, {"1001": self.ps["leagueId"]}, limit=3
        )
        aliases = build_aliases(team_pairs=[("布卡拉曼加竞技", "布卡拉曼加体育会")])
        aliased = rank_fixture_candidates(
            [self.ps], [translated], aliases, {"1001": self.ps["leagueId"]}, limit=3
        )
        self.assertGreater(aliased[0]["score"], plain[0]["score"])
        self.assertTrue(aliased[0]["aliasHit"])


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = FixtureMergeStore(Path(self.tmp.name) / "merge.sqlite3")
        self.ps_league = {"id": "p38:A", "name": "A - 甲级", "eventCount": 1}
        self.crown_league = {"id": "c1", "name": "A甲组", "eventCount": 1}
        self.ps_event = ps_event("甲 vs 乙", "A - 甲级", league_id="p38:A")
        self.crown_event = {
            "id": "crown-e1", "home": "甲", "away": "乙", "league": "A甲组",
            "leagueId": "c1", "kickoffUtc": "2026-11-03T04:00:00+00:00",
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_league_and_fixture_mapping_roundtrip(self):
        self.store.map_leagues(self.ps_league, self.crown_league)
        self.store.map_events(self.ps_event, self.crown_event)
        self.assertEqual(len(self.store.list_league_mappings()), 1)
        self.assertEqual(len(self.store.list_mappings()), 1)
        self.assertEqual(self.store.counts(), (1, 1))

    def test_duplicate_owner_is_rejected(self):
        self.store.map_leagues(self.ps_league, self.crown_league)
        with self.assertRaises(MappingConflict):
            self.store.map_leagues(
                {"id": "p38:B", "name": "B", "eventCount": 1}, self.crown_league
            )

    def test_manual_league_binding_survives_restart_without_expiry(self):
        self.store.map_leagues(self.ps_league, self.crown_league)
        with self.store._connect() as connection:
            connection.execute("UPDATE league_mappings SET updated_at='2020-01-01T00:00:00+08:00'")
        reopened = FixtureMergeStore(self.store.path)
        rows = reopened.list_league_mappings()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["crownSnapshot"], self.crown_league)
        self.assertEqual(rows[0]["ps38Snapshot"], self.ps_league)

    def test_league_unmap_preserves_fixture_history_after_restart(self):
        self.store.map_leagues(self.ps_league, self.crown_league)
        self.store.map_events(
            {**self.ps_event, "kickoffUtc": "2020-01-01T00:00:00+00:00"},
            {**self.crown_event, "kickoffUtc": "2020-01-01T00:00:00+00:00"},
        )
        history = self.store.list_mappings()
        self.store.unmap_league("c1")
        reopened = FixtureMergeStore(self.store.path)
        self.assertEqual(reopened.counts(), (0, 1))
        self.assertEqual(reopened.list_mappings(), history)
        reopened.unmap(self.ps_event["id"])
        self.assertEqual(reopened.counts(), (0, 0))

    def test_fixture_conflict(self):
        self.store.map_events(self.ps_event, self.crown_event)
        with self.assertRaises(MappingConflict):
            self.store.map_events(
                ps_event("丙 vs 丁", "A - 甲级", league_id="p38:A"),
                self.crown_event,
            )


if __name__ == "__main__":
    unittest.main()


class LeagueLibraryTests(unittest.TestCase):
    def test_normalized_league_ids_merge_spacing_variants(self):
        self.assertEqual(
            ps38_league_id("丹麦- 超级联赛"),
            ps38_league_id("丹麦 - 超级联赛"),
        )
        self.assertEqual(
            ps38_league_id("中国- 甲级联赛"),
            ps38_league_id("中国甲组联赛"),
        )

    def test_seed_imports_once_and_unbind_sticks(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed = Path(tmp) / "seed.tsv"
            seed.write_text(
                "crown_id\tcrown_name\tpinnacle_id\tpinnacle_name\n"
                "100021\t英格兰超级联赛\t1980\t英格兰 - 超级联赛\n"
                "100030\t西班牙甲组联赛\t2196\t西班牙 - 西甲\n",
                encoding="utf-8",
            )
            db = Path(tmp) / "merge.sqlite3"
            store = FixtureMergeStore(db, seed)
            self.assertEqual(store.counts(), (2, 0))
            rows = store.list_league_mappings()
            by_name = {row["crownSnapshot"]["name"]: row for row in rows}
            self.assertEqual(by_name["英格兰超级联赛"]["ps38Snapshot"]["providerId"], "1980")
            self.assertEqual(
                by_name["西班牙甲组联赛"]["ps38Id"],
                ps38_league_id("西班牙 - 西甲"),
            )

            store.map_events({"id": "pin:1"}, {"id": "c:1", "leagueId": "100021"})
            store.unmap_league("100021")
            self.assertEqual(store.counts()[0], 1)

            reopened = FixtureMergeStore(db, seed)
            self.assertEqual(reopened.counts(), (1, 1))
            self.assertEqual([row["crownId"] for row in reopened.list_league_mappings()], ["100030"])
