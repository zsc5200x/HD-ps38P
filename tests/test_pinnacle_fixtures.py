import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from ps38app.fixture_merge import MergeService, current_fixture, fetch_pinnacle_events
from ps38app.pinnacle_fixtures import catalog_path, parse_catalog
from tests.support import isolated_config


def event_row(event_id, kickoff, period="", status="O"):
    row = [event_id, "Home", "Away", None, int(kickoff.timestamp() * 1000)]
    row += [None] * 13
    row[16], row[17] = period, status
    return row


def sport_rows(*rows, sport_id=29):
    return [[sport_id, "Football", [[101, "Test League", list(rows)]]]]


class PinnacleCatalogTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 3, 6, tzinfo=timezone.utc)
        self.tomorrow = datetime(2026, 11, 4, 1, 30, tzinfo=timezone.utc)
        self.url = "https://provider.test/sports-service/sv/compact/events"

    def test_provider_today_survives_local_midnight_and_early_is_excluded(self):
        payload = {
            "l": sport_rows(event_row(1, self.now)),
            "n": sport_rows(event_row(2, self.tomorrow)),
            "e": sport_rows(event_row(3, self.now)),
            "e1": sport_rows(event_row(4, self.now)),
        }
        events = parse_catalog(payload)
        self.assertEqual({e["id"] for e in events}, {"1", "2"})
        today = next(e for e in events if e["id"] == "2")
        self.assertEqual(today["scope"], "today")
        self.assertEqual(today["kickoff"], "2026-11-04T01:30:00+00:00")

    def test_live_wins_duplicate_id_and_nonfootball_is_excluded(self):
        payload = {
            "l": sport_rows(event_row(1, self.now)),
            "n": sport_rows(event_row(1, self.now))
            + sport_rows(event_row(2, self.tomorrow), sport_id=4),
        }
        events = parse_catalog(payload)
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["isLive"])
        self.assertEqual(events[0]["scope"], "live")

    def test_finished_period_is_preserved_for_validity_filter(self):
        events = parse_catalog({"l": sport_rows(event_row(1, self.now, "END")), "n": []})
        self.assertTrue(events[0]["isFinished"])

    def test_invalid_payload_does_not_look_like_successful_empty_catalog(self):
        for payload in (None, [], {}, {"l": []}, {"l": [], "n": None}, {"error": "login"}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                parse_catalog(payload)
        self.assertEqual(parse_catalog({"l": [], "n": []}), [])

    def test_path_uses_provider_catalog_without_client_date(self):
        path = catalog_path(self.url)
        params = parse_qs(urlsplit(path).query, keep_blank_values=True)
        self.assertEqual(params["me"], ["0"])
        self.assertEqual(params["sp"], ["29"])
        self.assertEqual(params["d"], [""])
        self.assertNotIn("date", params)
        self.assertNotIn("timezone", params)

    @staticmethod
    def connection(payload, status=200):
        response = Mock(status=status)
        response.read.return_value = json.dumps(payload).encode()
        connection = Mock()
        connection.getresponse.return_value = response
        return connection

    def test_official_catalog_flows_into_matching_without_local_snapshot(self):
        payload = {"l": [], "n": sport_rows(event_row(2, self.tomorrow))}
        connection = self.connection(payload)
        with (
            patch("http.client.HTTPSConnection", return_value=connection),
            patch("http.client.HTTPConnection") as local,
        ):
            events, error, fetched = fetch_pinnacle_events(
                {
                    "pinnacle_catalog_url": self.url,
                    "pinnacle_snapshot_url": "http://localhost/api/snapshot",
                }
            )
        local.assert_not_called()
        self.assertIsNone(error)
        self.assertTrue(fetched)
        self.assertEqual(events[0]["id"], "pin:2")
        self.assertEqual(events[0]["scope"], "today")
        self.assertTrue(current_fixture(events[0], self.now))
        connection.close.assert_called_once()

    def test_provider_error_never_silently_falls_back_to_date_filtered_snapshot(self):
        with (
            patch("http.client.HTTPSConnection", side_effect=OSError()),
            patch("http.client.HTTPConnection") as local,
        ):
            events, error, _ = fetch_pinnacle_events(
                {
                    "pinnacle_catalog_url": self.url,
                    "pinnacle_snapshot_url": "http://localhost/api/snapshot",
                }
            )
        self.assertEqual(events, [])
        self.assertIn("官方赛事清单", error)
        local.assert_not_called()

    def test_provider_cache_avoids_network_on_each_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = isolated_config(Path(tmp))
            config["pinnacle_catalog_url"] = self.url
            service = MergeService(config)
            with (
                patch("ps38app.fixture_merge.time.monotonic", return_value=100) as clock,
                patch(
                    "ps38app.fixture_merge.fetch_pinnacle_events", return_value=([], None, "now")
                ) as fetch,
            ):
                service._pinnacle_source()
                clock.return_value = 120
                service._pinnacle_source()
                self.assertEqual(fetch.call_count, 1)
                clock.return_value = 131
                service._pinnacle_source()
                self.assertEqual(fetch.call_count, 2)
