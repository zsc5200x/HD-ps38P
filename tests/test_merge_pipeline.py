import json
from contextlib import closing
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from ps38app.fixture_merge import (
    MergeService, align_league_ids, current_fixture, fetch_crown_events, prepare_crown_event,
    fetch_pinnacle_events, _merge_event_sources, rank_fixture_candidates,
)
from ps38app.fixture_identity import fixture_identity_score
from ps38app.crown_fixtures import CrownFeedError, fetch_pool_crown_events, parse_games
from tests.support import upcoming_today


class MatchingPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.now = datetime.now(timezone.utc)
        self.kickoff = upcoming_today(self.now)
        self.ps = {
            "id": "pin:1", "source": "pinnacle", "home": "River Plate", "away": "Boca Juniors",
            "name": "River Plate vs Boca Juniors", "league": "Argentina League", "leagueId": "p1",
            "kickoffUtc": self.kickoff.isoformat(), "orders": 0, "isLive": False,
        }
        self.crown = {
            "id": "c1", "home": "River Plate", "away": "Boca Juniors", "league": "Argentina League",
            "league_id": "100", "start_label": self.kickoff.isoformat(), "is_live": False,
        }
        self.service = MergeService({"db_path": root / "orders.db", "merge_db_path": root / "merge.db"})
        self.service._pinnacle_source = Mock(return_value=([self.ps], None, self.now.isoformat()))
        self.service._crown_source = Mock(return_value=([self.crown], None, self.now.isoformat()))

    def bind_leagues(self):
        self.service.store.map_leagues(
            {"id": "p1", "name": "Argentina League", "eventCount": 1},
            {"id": "100", "name": "Argentina League", "eventCount": 1})

    def test_orders_cannot_create_fixtures_or_overwrite_live_state(self):
        historical = {**self.ps, "id": "old", "source": "ps38", "orders": 99,
                      "kickoffUtc": (self.now - timedelta(days=1)).isoformat(), "isLive": True}
        current_order = {**self.ps, "source": "ps38", "orders": 4, "isLive": True}
        self.assertFalse(current_fixture(historical, self.now))
        rows = _merge_event_sources([historical, current_order], [self.ps])
        self.assertEqual([row["id"] for row in rows], ["pin:1"])
        self.assertEqual(rows[0]["orders"], 4)
        self.assertFalse(rows[0]["isLive"])
        self.assertEqual(_merge_event_sources([historical, current_order], []), [])

    def test_source_failure_never_falls_back_to_orders(self):
        self.service._pinnacle_source.return_value = ([], "实时快照失效", "")
        with patch("ps38app.fixture_merge.ps38_fixtures", return_value=[self.ps]):
            payload = self.service.payload()
        self.assertEqual(payload["ps38Events"], [])
        self.assertFalse(payload["sources"]["ps38"]["ok"])

    def test_provider_variant_reuses_confirmed_id_without_rewriting_binding(self):
        self.bind_leagues()
        self.service.bind_fixture(self.ps["id"], self.crown["id"])
        saved = self.service.store.list_mappings()[0]
        self.service._pinnacle_source.return_value = (
            [{**self.ps, "id": "pin:official-live", "pinnacleId": "official-live"}], None, "now"
        )
        payload = self.service.payload()
        self.assertEqual(payload["ps38Events"][0]["id"], self.ps["id"])
        self.assertEqual(payload["ps38Events"][0]["catalogId"], "pin:official-live")
        self.assertEqual(payload["ps38Events"][0]["pinnacleId"], "official-live")
        self.assertEqual(self.service._pinnacle_source.return_value[0][0]["id"], "pin:official-live")
        self.assertEqual(self.service.store.list_mappings(), [saved])
        self.assertFalse(payload["suggestions"])

    def test_provider_variant_never_reuses_binding_for_ambiguous_or_different_fixture(self):
        self.bind_leagues()
        self.service.bind_fixture(self.ps["id"], self.crown["id"])
        for rows in (
            [{**self.ps, "id": "pin:new", "home": self.ps["away"], "away": self.ps["home"]}],
            [{**self.ps, "id": "pin:new", "kickoffUtc": (self.kickoff + timedelta(hours=1)).isoformat()}],
            [{**self.ps, "id": "pin:new", "home": "River Plate U19"}],
            [{**self.ps, "id": "pin:new1"}, {**self.ps, "id": "pin:new2"}],
        ):
            with self.subTest(rows=rows):
                self.service._pinnacle_source.return_value = (rows, None, "now")
                payload = self.service.payload()
                self.assertNotIn(self.ps["id"], [e["id"] for e in payload["ps38Events"]])

    def test_crown_enters_matching_after_league_binding_then_merged(self):
        payload = self.service.payload()
        self.assertEqual(payload["matchEvents"], [])
        self.assertEqual([e["id"] for e in payload["crownEvents"]], ["c1"])
        self.assertEqual(self.service.auto_merge()["mappedFixtures"], [])
        result = self.service.bind_league("p1", "100")
        self.assertEqual(result["autoBound"], 1)
        payload = self.service.payload()
        self.assertEqual(len(payload["matchEvents"]), 1)
        self.assertEqual(len(payload["mappings"]), 1)
        self.assertEqual(payload["mappings"][0]["ps38Id"], "pin:1")
        self.assertEqual(self.service.auto_merge()["mappedFixtures"], [])

    def test_bound_crown_games_remain_matchable_when_pinnacle_has_no_games(self):
        self.bind_leagues()
        self.service._pinnacle_source.return_value = ([], None, "now")
        self.service._crown_source.return_value = ([self.crown, {**self.crown, "id": "c2", "league_id": "unbound", "league": "Different League"}], None, "now")
        payload = self.service.payload()
        self.assertEqual({e["id"] for e in payload["matchEvents"]}, {"c1"})
        self.assertEqual(payload["matchCrownEvents"], payload["matchEvents"])
        self.assertEqual({e["id"] for e in payload["crownEvents"]}, {"c1", "c2"})
        self.assertEqual({e["id"] for e in payload["crownLeagues"]}, {"100", "unbound"})
        self.assertEqual(payload["suggestions"], [])

    def test_saved_league_remains_in_library_after_restart_without_current_games(self):
        self.service.bind_league("p1", "100")
        restarted = MergeService(self.service.config)
        restarted._pinnacle_source = Mock(return_value=([], None, "now"))
        restarted._crown_source = Mock(return_value=([], None, "now"))
        payload = restarted.payload()
        self.assertEqual(payload["crownLeagues"], [])
        self.assertEqual(payload["ps38Leagues"], [])
        self.assertEqual([row["crownId"] for row in payload["leagueMappings"]], ["100"])
        self.assertEqual(payload["counts"]["leagueMappings"], 1)

    def test_pinnacle_failure_does_not_hide_crown_games(self):
        self.bind_leagues()
        self.service._pinnacle_source.return_value = ([], "平博不可用", "")
        payload = self.service.payload()
        self.assertEqual([e["id"] for e in payload["matchEvents"]], ["c1"])
        self.assertFalse(payload["sources"]["ps38"]["ok"])

    def test_candidates_are_limited_per_crown_game(self):
        crown = {**self.ps, "id": "c1", "leagueId": "100"}
        crowns = [{**crown, "id": f"c{i}"} for i in range(5)]
        ps_events = [{**self.ps, "id": f"pin:{i}"} for i in range(5)]
        # All five Crown games must retain a candidate, even with one PS game.
        rows = rank_fixture_candidates([self.ps], crowns, league_mapping={"100": "p1"}, limit=3)
        self.assertEqual({r["crownId"] for r in rows}, {f"c{i}" for i in range(5)})
        # Each Crown game gets at most three PS candidates.
        rows = rank_fixture_candidates(ps_events, [crown], league_mapping={"100": "p1"}, limit=3)
        self.assertEqual(len(rows), 3)
        self.assertEqual({r["crownId"] for r in rows}, {"c1"})

    def test_already_merged_pinnacle_does_not_occupy_candidate_slots(self):
        self.bind_leagues()
        ps_events = [{**self.ps, "id": f"pin:{i}"} for i in range(4)]
        self.service._pinnacle_source.return_value = (ps_events, None, "now")
        for i in range(3):
            self.service.store.map_events(ps_events[i], {**ps_events[i], "id": f"mapped-c{i}"})
        payload = self.service.payload()
        self.assertEqual([r["ps38Id"] for r in payload["suggestions"]], ["pin:3"])
        self.assertEqual(payload["suggestions"][0]["crownId"], "c1")

    def test_finished_and_old_live_games_excluded(self):
        for extra in ({"isFinished": True}, {"matchStatus": "FT"},
                      {"isLive": True, "kickoffUtc": (self.now - timedelta(hours=8)).isoformat()}):
            with self.subTest(extra=extra):
                self.assertFalse(current_fixture({**self.ps, **extra}, self.now))
        self.assertTrue(current_fixture({**self.ps, "isLive": True,
                        "kickoffUtc": (self.now - timedelta(hours=1)).isoformat()}, self.now))
        self.service._crown_source.return_value = ([{**self.crown, "is_finished": True}], None, "")
        self.assertEqual(self.service.payload()["crownEvents"], [])

    def test_provider_today_is_not_cut_at_beijing_midnight(self):
        now = datetime(2026, 11, 3, 15, 55, tzinfo=timezone.utc)  # Beijing 23:55
        for kickoff, expected in (
            ("2026-11-03T15:59:59+00:00", True),
            ("2026-11-03T16:00:00+00:00", True),
            ("2026-11-04T02:00:00+00:00", True),
            ("2026-11-03T15:54:00+00:00", False),
        ):
            with self.subTest(kickoff=kickoff):
                self.assertEqual(current_fixture({**self.ps, "scope": "today", "kickoffUtc": kickoff}, now), expected)
        # An early-market entry is excluded even if its kickoff is today locally.
        self.assertFalse(current_fixture({**self.ps, "scope": "early",
                                         "kickoffUtc": "2026-11-03T15:59:59Z"}, now))
        crown = prepare_crown_event({**self.crown, "scope": "today",
                                     "start_label": "2026-11-03T22:00:00-04:00"})
        self.assertTrue(crown["kickoffLocal"].startswith("2026-11-04"))
        self.assertTrue(current_fixture(crown, now))

    def test_live_fixture_survives_midnight_but_finished_fixture_does_not(self):
        now = datetime(2026, 11, 3, 16, 5, tzinfo=timezone.utc)
        event = {**self.ps, "isLive": True, "kickoffUtc": "2026-11-03T15:30:00Z"}
        self.assertTrue(current_fixture(event, now))
        self.assertFalse(current_fixture({**event, "isFinished": True}, now))

    def test_pull_totals_and_matching_share_provider_scope(self):
        tomorrow = (self.now + timedelta(days=1)).isoformat()
        ps = [self.ps, {**self.ps, "id": "pin:tomorrow", "kickoffUtc": tomorrow, "scope": "today"},
              {**self.ps, "id": "pin:early", "scope": "early"},
              {**self.ps, "id": "pin:special", "league": "特别投注"}]
        crowns = [self.crown, {**self.crown, "id": "c:tomorrow", "start_label": tomorrow, "scope": "today"},
                  {**self.crown, "id": "c:early", "scope": "early"},
                  {**self.crown, "id": "c:special", "league": "奇幻赛事"}]
        self.service._pinnacle_source.return_value = (ps, None, "now")
        self.service._crown_source.return_value = (crowns, None, "now")
        self.bind_leagues()
        with patch("ps38app.fixture_merge.fetch_pinnacle_events", return_value=(ps, None, "now")), \
                patch("ps38app.fixture_merge.fetch_crown_events", return_value=(crowns, None, "now")):
            self.assertEqual(self.service.pull_crown()["count"], 2)
            self.assertEqual(self.service.pull_pinnacle()["count"], 2)
        payload = self.service.payload()
        self.assertEqual(payload["sources"]["crown"]["count"], 2)
        self.assertEqual(payload["sources"]["ps38"]["count"], 2)
        self.assertEqual({e["id"] for e in payload["matchEvents"]}, {"c1", "c:tomorrow"})
        self.assertEqual({e["crownId"] for e in payload["mappings"]}, {"c1", "c:tomorrow"})
        self.assertEqual(self.service.bind_fixture("pin:tomorrow", "c:tomorrow")["crownId"], "c:tomorrow")
        with self.assertRaisesRegex(ValueError, "赛事列表已更新"):
            self.service.bind_fixture("pin:early", "c:early")

    def test_ambiguous_candidates_never_auto_match_either_direction(self):
        self.bind_leagues()
        self.service._crown_source.return_value = ([self.crown, {**self.crown, "id": "c2"}], None, "")
        self.assertEqual(self.service.auto_merge()["mappedFixtures"], [])
        self.service._crown_source.return_value = ([self.crown], None, "")
        self.service._pinnacle_source.return_value = ([self.ps, {**self.ps, "id": "pin:2"}], None, "")
        self.assertEqual(self.service.auto_merge()["mappedFixtures"], [])

    def test_wrong_opponent_orientation_and_time_never_auto_match(self):
        base = {**self.ps, "id": "c1"}
        self.assertEqual(fixture_identity_score(base, self.ps), 100)
        for extra in ({"away": "Entirely Different"}, {"home": "Boca Juniors", "away": "River Plate"},
                      {"kickoffUtc": ""}, {"home": "River Plate U19"},
                      {"kickoffUtc": (self.kickoff + timedelta(minutes=16)).isoformat()}):
            with self.subTest(extra=extra):
                self.assertEqual(fixture_identity_score({**base, **extra}, self.ps), 0)

    def test_manual_binding_cannot_cross_leagues(self):
        self.bind_leagues()
        self.service._crown_source.return_value = ([{**self.crown, "league_id": "200"}], None, "")
        with self.assertRaisesRegex(ValueError, "不能跨联赛"):
            self.service.bind_fixture("pin:1", "c1")
        self.assertEqual(self.service.store.counts()[1], 0)

    def test_manual_confirmation_skips_catalog_ranking_and_saves_snapshot(self):
        self.bind_leagues()
        with patch("ps38app.fixture_merge.rank_league_candidates", side_effect=AssertionError("manual confirmation must not rank leagues")), \
                patch("ps38app.fixture_merge.rank_fixture_candidates", side_effect=AssertionError("manual confirmation must not rank fixtures")):
            saved = self.service.bind_fixture("pin:1", "c1")
        self.assertEqual(saved["ps38Id"], "pin:1")
        self.assertEqual(saved["crownId"], "c1")
        self.assertEqual(saved["ps38Snapshot"]["home"], self.ps["home"])
        self.assertEqual(saved["crownSnapshot"]["home"], self.crown["home"])
        self.assertEqual(self.service.store.list_mappings()[0]["crownId"], "c1")

    def test_manual_confirmation_still_rejects_finished_fixture(self):
        self.bind_leagues()
        self.service._crown_source.return_value = ([{**self.crown, "is_finished": True}], None, "")
        with self.assertRaisesRegex(ValueError, "赛事列表已更新"):
            self.service.bind_fixture("pin:1", "c1")
        self.assertEqual(self.service.store.counts()[1], 0)

    def test_league_unbound_during_confirmation_cannot_save_from_stale_catalog(self):
        self.bind_leagues()
        original_catalog = self.service._catalog

        def unbind_after_read():
            catalog = original_catalog()
            self.service.unbind_league("100")
            return catalog

        with patch.object(self.service, "_catalog", side_effect=unbind_after_read):
            with self.assertRaisesRegex(ValueError, "请先绑定对应联赛"):
                self.service.bind_fixture("pin:1", "c1")
        self.assertEqual(self.service.store.counts(), (0, 0))

    def test_unbound_league_stops_new_matches_but_preserves_confirmed_fixtures(self):
        self.service.bind_league("p1", "100")
        self.service.unbind_league("100")
        self.service._crown_source.return_value = ([self.crown, {**self.crown, "id": "c2"}], None, "now")
        self.service._pinnacle_source.return_value = ([self.ps, {**self.ps, "id": "pin:2"}], None, "now")
        payload = self.service.payload()
        self.assertEqual(payload["matchEvents"], [])
        self.assertEqual(payload["suggestions"], [])
        self.assertEqual([item["crownId"] for item in payload["mappings"]], ["c1"])
        self.assertEqual(self.service.auto_merge()["mappedFixtures"], [])
        with self.assertRaisesRegex(ValueError, "请先绑定对应联赛"):
            self.service.bind_fixture("pin:2", "c2")
        self.assertEqual(self.service.store.counts(), (0, 1))

    def test_saved_mappings_remain_visible_without_session_and_new_pairs_append(self):
        self.bind_leagues()
        for hours in (24, 49):
            event = {**self.ps, "id": f"pin:{hours}",
                     "kickoffUtc": (self.now - timedelta(hours=hours)).isoformat()}
            self.service.store.map_events(event, {**event, "id": f"c:{hours}"})

        self.service._crown_source.return_value = ([], "皇冠会员会话不可用", "")
        payload = self.service.payload()
        self.assertFalse(payload["sources"]["crown"]["ok"])
        self.assertEqual(
            {m["ps38Id"] for m in payload["mappings"]}, {"pin:24", "pin:49"}
        )
        self.assertEqual(payload["counts"]["fixtureMappings"], 2)
        self.assertEqual(self.service.store.counts()[1], 2)

        self.service._crown_source.return_value = ([self.crown], None, "now")
        result = self.service.auto_merge()
        self.assertEqual([item["crownId"] for item in result["mappedFixtures"]], ["c1"])
        payload = self.service.payload()
        self.assertEqual(payload["counts"]["fixtureMappings"], 3)
        self.assertEqual(
            {m["ps38Id"] for m in payload["mappings"]}, {"pin:24", "pin:49", "pin:1"}
        )

    def test_saved_provider_id_resolves_translation_but_not_different_provider_id(self):
        mappings = [{"ps38Id": "p1", "ps38Snapshot": {"name": "English League", "providerId": "123"}}]
        event = {**self.ps, "leagueId": "p38:中文名称", "providerLeagueId": "123"}
        self.assertEqual(align_league_ids([event], mappings, "ps38")[0]["leagueId"], "p1")
        event = {**self.ps, "leagueId": "p38:English League", "league": "English League", "providerLeagueId": "999"}
        self.assertNotEqual(align_league_ids([event], mappings, "ps38")[0]["leagueId"], "p1")

    def test_successful_pull_reports_other_source_failure_separately(self):
        self.service._crown_source.return_value = ([], "皇冠需重新登录", "")
        with patch("ps38app.fixture_merge.fetch_pinnacle_events", return_value=([self.ps], None, "now")):
            result = self.service.pull_pinnacle()
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["autoBound"], 0)
        self.assertIn("皇冠", result["autoError"])


class FeedTests(unittest.TestCase):
    def response(self, payload):
        response = Mock(status=200)
        response.read.return_value = json.dumps(payload).encode()
        conn = Mock()
        conn.getresponse.return_value = response
        return conn

    def test_expired_crown_cache_is_never_returned_even_without_cached_flag(self):
        payload = {"events": [{"id": "old"}], "updated_at": "2020-01-01T00:00:00Z"}
        with patch("http.client.HTTPConnection", return_value=self.response(payload)), patch(
            "ps38app.crown_fixtures.fetch_pool_crown_events", return_value=([], "需要重新登录", "")
        ) as pool:
            events, error, _ = fetch_crown_events({})
        self.assertEqual(events, [])
        self.assertIn("重新登录", error)
        pool.assert_called_once()

    def test_stale_pinnacle_snapshot_is_rejected(self):
        for payload in ({"events": [], "freshness": "stale"}, {"events": [], "lagSeconds": 121}):
            with patch("http.client.HTTPConnection", return_value=self.response(payload)):
                events, error, _ = fetch_pinnacle_events({"pinnacle_snapshot_url": "http://localhost/api/snapshot"})
            self.assertEqual(events, [])
            self.assertIn("过期", error)

    def test_xml_and_original_keep_dated_kickoff_not_match_clock(self):
        game = {"GID": "1", "TEAM_H": "A", "TEAM_C": "B", "LID": "2", "LEAGUE": "League",
                "GAME_DATE_TIME": "2026-11-03 08:00:00", "RETIMESET": "1H^24:06"}
        body = ('<serverresponse><original>' + json.dumps({"1": game}) + '</original></serverresponse>').encode()
        event = parse_games(body, live=True)[0]
        self.assertEqual(event["start_label"], "2026-11-03T08:00:00-04:00")
        self.assertEqual(event["scope"], "live")
        self.assertEqual(parse_games(body, live=False)[0]["scope"], "today")
        xml = b'<serverresponse><ec><game><gid>1</gid><retimeset>1H^24:06</retimeset></game></ec></serverresponse>'
        self.assertEqual(parse_games(xml, live=True)[0]["start_label"], "")

    def test_pinnacle_feed_preserves_source_scope_across_dates(self):
        now = datetime(2026, 11, 3, 15, 55, tzinfo=timezone.utc)
        raw = {"id": "1", "sportId": 29, "name": "A VS B", "league": "League",
               "kickoff": "2026-11-04 03:00:00", "isLive": False, "scope": "today"}
        with patch("http.client.HTTPConnection", return_value=self.response({"events": [raw]})):
            events, error, _ = fetch_pinnacle_events({"pinnacle_snapshot_url": "http://localhost/api/snapshot"})
        self.assertIsNone(error)
        self.assertEqual(events[0]["scope"], "today")
        self.assertTrue(current_fixture(events[0], now))

    def test_invalid_or_login_response_is_not_successful_empty_feed(self):
        for body in (b'<html>login</html>', b'<serverresponse><code>error</code><msg>doubleLogin</msg></serverresponse>',
                     b'<serverresponse><msg>CheckEMNU</msg></serverresponse>'):
            with self.subTest(body=body), self.assertRaises(CrownFeedError):
                parse_games(body, live=False)
        self.assertEqual(parse_games(b'<serverresponse><dataCount>0</dataCount></serverresponse>', live=False), [])

    def test_saved_session_with_transient_error_can_fetch_without_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / 'accounts.db'
            with closing(sqlite3.connect(db_path)) as db, db:
                db.execute('CREATE TABLE accounts (id,base_url,platform,enabled,status,last_check)')
                db.execute("INSERT INTO accounts VALUES ('test','https://member.test','crown_member',1,'error','now')")
            folder = root / 'test'
            (folder / 'profile').mkdir(parents=True)
            (folder / 'session_meta.json').write_text(json.dumps({'uid': 'fake', 'ver': 'fake', 'origin': 'https://member.test'}))
            (folder / 'profile/storage_state.json').write_text(json.dumps({'cookies': [
                {'name': 'fake', 'value': 'test', 'domain': 'member.test', 'path': '/', 'expires': -1}]}))
            xml = b'<serverresponse><game><GID>1</GID><GAME_DATE_TIME>2026-11-03 08:00:00</GAME_DATE_TIME></game></serverresponse>'
            with patch('ps38app.crown_fixtures._http_post', side_effect=[TimeoutError(), (200, xml), (200, xml)]) as post:
                events, error, fetched = fetch_pool_crown_events({'pool_db_path': db_path, 'pool_dir': root})
            self.assertIsNone(error)
            self.assertTrue(fetched)
            self.assertEqual(len(events), 1)
            self.assertTrue(events[0]['is_live'])
            self.assertEqual(post.call_count, 3)
            self.assertTrue(all(b'p=get_game_list' in call.args[1] for call in post.call_args_list))


if __name__ == '__main__':
    unittest.main()
