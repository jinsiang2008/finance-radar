from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


LIB = Path(__file__).resolve().parents[2] / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import risk_radar  # noqa: E402


NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


class CreditSpreadTests(unittest.TestCase):
    def test_fred_oas_percent_values_are_normalized_to_basis_points(self) -> None:
        responses = iter(
            [
                "DATE,BAMLH0A0HYM2\n2026-08-01,5.50\n",
                "DATE,BAMLC0A0CM\n2026-08-01,1.20\n",
            ]
        )

        with mock.patch.object(
            risk_radar,
            "http_get",
            side_effect=lambda *args, **kwargs: next(responses),
        ):
            result = risk_radar.fetch_credit_spreads()

        self.assertEqual(result["hy_oas"], 550.0)
        self.assertEqual(result["ig_oas"], 120.0)
        self.assertEqual(result["spread_diff"], 430.0)
        self.assertEqual(result["unit"], "basis_points")
        self.assertEqual(result["status"], "critical")

    def test_credit_stress_scoring_uses_basis_point_thresholds(self) -> None:
        recession = risk_radar.score_recession_risk(
            {},
            {},
            {"hy_oas": 550.0, "unit": "basis_points"},
        )
        stress = risk_radar.score_market_stress(
            {},
            {},
            {"hy_oas": 450.0, "unit": "basis_points"},
        )

        self.assertIn("高收益债利差飙升", recession["signals"])
        self.assertIn("信用利差显著扩大", stress["signals"])


class AssetTagTests(unittest.TestCase):
    def test_tradeable_symbols_are_separated_from_sector_themes(self) -> None:
        tags = risk_radar.split_asset_tags(
            ["NVDA", "区域银行ETF (KRE)", "商业地产REITs", "全球科技股"]
        )

        self.assertEqual(tags["tickers"], ["NVDA", "KRE"])
        self.assertEqual(
            tags["sectors"], ["区域银行ETF", "商业地产REITs", "全球科技股"]
        )

    def test_tags_are_deduplicated_and_broad_phrases_stay_sectors(self) -> None:
        tags = risk_radar.split_asset_tags(
            ["TSM", "TSM", "几乎所有资产", "A股市场", "", None]
        )

        self.assertEqual(tags["tickers"], ["TSM"])
        self.assertEqual(tags["sectors"], ["几乎所有资产", "A股市场"])

    def test_scenarios_and_rhinos_expose_split_tags(self) -> None:
        swans = risk_radar.generate_black_swan_scenarios({}, {}, [], [])
        rhinos = risk_radar.identify_gray_rhinos()

        for entry in swans + rhinos:
            self.assertIsInstance(entry.get("tickers"), list)
            self.assertIsInstance(entry.get("sectors"), list)

        ai_bubble = next(s for s in swans if s["id"] == "bs_ai_bubble")
        self.assertIn("NVDA", ai_bubble["tickers"])
        self.assertNotIn("NVDA", ai_bubble["sectors"])


class MonitoredEventTests(unittest.TestCase):
    def test_policy_events_normalize_time_and_flag_unverified(self) -> None:
        events = risk_radar.build_policy_events(
            [
                {
                    "title": "FOMC statement on rate policy",
                    "url": "https://federalreserve.gov/a",
                    "source": "FOMC",
                    "date": "Mon, 03 Aug 2026 09:00:00 GMT",
                },
                {
                    "title": "央行公告",
                    "url": "http://pbc.gov.cn/b",
                    "source": "中国人民银行",
                },
            ],
            now=NOW,
        )

        self.assertEqual(events[0]["published_at"], "2026-08-03T09:00:00+00:00")
        self.assertEqual(events[0]["time_status"], "verified")
        self.assertIsNone(events[1]["published_at"])
        self.assertEqual(events[1]["time_status"], "unknown")
        for event in events:
            self.assertEqual(event["kind"], "policy")

    def test_pboc_timestamp_is_recovered_from_the_announcement_url(self) -> None:
        """These pages carry no feed date, only a timestamped path."""
        events = risk_radar.build_policy_events(
            [
                {
                    "title": "中国人民银行公开市场业务公告",
                    "url": (
                        "http://www.pbc.gov.cn/zhengcehuobisi/125207/125213/"
                        "125431/125469/2026080114300012345/index.html"
                    ),
                    "source": "中国人民银行",
                }
            ],
            now=NOW,
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["time_status"], "verified")
        # 2026-08-01 14:30:00 Beijing time is 06:30 UTC.
        self.assertEqual(events[0]["published_at"], "2026-08-01T06:30:00+00:00")

    def test_policy_event_urls_are_html_unescaped(self) -> None:
        events = risk_radar.build_policy_events(
            [
                {
                    "title": "央行讲话",
                    "url": "https://example.com/a?ref=x&amp;aid=1",
                    "source": "央行行长讲话",
                    "date": "Mon, 03 Aug 2026 09:00:00 GMT",
                }
            ],
            now=NOW,
        )

        self.assertEqual(events[0]["url"], "https://example.com/a?ref=x&aid=1")

    def test_policy_events_drop_stale_and_future_records(self) -> None:
        events = risk_radar.build_policy_events(
            [
                {
                    "title": "Ancient release",
                    "url": "https://example.com/old",
                    "source": "Federal Reserve",
                    "date": "Mon, 03 Aug 2020 09:00:00 GMT",
                },
                {
                    "title": "Impossible future release",
                    "url": "https://example.com/future",
                    "source": "Federal Reserve",
                    "date": "Tue, 03 Nov 2026 09:00:00 GMT",
                },
            ],
            now=NOW,
        )

        self.assertEqual(events, [])

    def test_indicator_events_detect_material_moves_only(self) -> None:
        previous = {
            "vix": {"value": 15.0},
            "credit_spreads": {"hy_oas": 300.0, "unit": "basis_points"},
        }
        current = {
            "vix": {"value": 27.0},
            "credit_spreads": {"hy_oas": 305.0, "unit": "basis_points"},
        }

        events = risk_radar.detect_indicator_events(current, previous, now=NOW)

        keys = [event["id"] for event in events]
        self.assertIn("ind_vix_spike", keys)
        self.assertNotIn("ind_credit_widening", keys)

        spike = next(e for e in events if e["id"] == "ind_vix_spike")
        self.assertEqual(spike["kind"], "indicator")
        self.assertEqual(spike["time_status"], "verified")
        self.assertEqual(spike["published_at"], NOW.isoformat())
        self.assertEqual(spike["previous_value"], 15.0)
        self.assertEqual(spike["current_value"], 27.0)
        self.assertTrue(spike["tickers"] or spike["sectors"])

    def test_indicator_events_need_a_previous_snapshot(self) -> None:
        current = {"vix": {"value": 40.0}}

        self.assertEqual(
            risk_radar.detect_indicator_events(current, None, now=NOW), []
        )
        self.assertEqual(
            risk_radar.detect_indicator_events(current, {}, now=NOW), []
        )

    def test_report_exposes_monitored_events_sorted_by_recency(self) -> None:
        report = risk_radar.assemble_monitored_events(
            policy_events=[
                {
                    "id": "p1",
                    "kind": "policy",
                    "title": "older",
                    "published_at": "2026-08-03T01:00:00+00:00",
                    "time_status": "verified",
                    "severity": "low",
                    "tickers": [],
                    "sectors": [],
                },
                {
                    "id": "p2",
                    "kind": "policy",
                    "title": "undated",
                    "published_at": None,
                    "time_status": "unknown",
                    "severity": "low",
                    "tickers": [],
                    "sectors": [],
                },
            ],
            indicator_events=[
                {
                    "id": "i1",
                    "kind": "indicator",
                    "title": "newer",
                    "published_at": "2026-08-03T11:00:00+00:00",
                    "time_status": "verified",
                    "severity": "high",
                    "tickers": [],
                    "sectors": [],
                }
            ],
        )

        self.assertEqual([e["id"] for e in report], ["i1", "p1", "p2"])


if __name__ == "__main__":
    unittest.main()
