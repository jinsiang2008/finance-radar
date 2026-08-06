from __future__ import annotations

import hashlib
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


class FinancialStressTests(unittest.TestCase):
    HEADER = (
        "Date,OFR FSI,Credit,Equity valuation,Safe assets,Funding,Volatility,"
        "United States,Other advanced economies,Emerging markets\n"
    )

    def test_official_ofr_csv_uses_last_complete_finite_row(self) -> None:
        payload = self.HEADER + (
            "2026-08-01,1.25,0.30,-0.10,0.20,0.40,0.45,1.0,0.1,0.15\n"
            "2026-08-03,6.25,1.10,0.80,0.70,1.20,2.45,4.0,1.0,1.25\n"
        )

        with mock.patch.object(risk_radar, "http_get", return_value=payload) as get:
            result = risk_radar.fetch_financial_stress()

        get.assert_called_once()
        self.assertEqual(get.call_args.args[0], risk_radar.OFR_FSI_CSV_URL)
        self.assertEqual(result["ofr_fsi"], 6.25)
        self.assertEqual(result["credit"], 1.10)
        self.assertEqual(result["funding"], 1.20)
        self.assertEqual(result["volatility"], 2.45)
        self.assertEqual(result["equity_valuation"], 0.80)
        self.assertEqual(result["safe_assets"], 0.70)
        self.assertEqual(result["observed_at"], "2026-08-03")
        self.assertEqual(result["source"], "U.S. Treasury OFR")
        self.assertEqual(result["source_url"], risk_radar.OFR_FSI_SOURCE_URL)
        self.assertEqual(result["unit"], "index")
        self.assertEqual(result["status"], "critical")
        self.assertEqual(result["data_status"], "ok")

    def test_malformed_trailing_rows_do_not_replace_last_complete_row(self) -> None:
        payload = self.HEADER + (
            "2026-08-01,3.25,0.8,0.4,0.3,0.7,1.05,2.0,0.5,0.75\n"
            "2026-08-02,7.0,1.0,,0.5,1.0,2.0,4.0,1.0,2.0\n"
            "2026-08-03,nan,1.0,0.5,0.5,1.0,2.0,4.0,1.0,2.0\n"
        )

        with mock.patch.object(risk_radar, "http_get", return_value=payload):
            result = risk_radar.fetch_financial_stress()

        self.assertEqual(result["observed_at"], "2026-08-01")
        self.assertEqual(result["ofr_fsi"], 3.25)
        self.assertEqual(result["status"], "elevated")
        self.assertEqual(result["data_status"], "ok")

    def test_empty_response_is_explicitly_unavailable(self) -> None:
        with mock.patch.object(risk_radar, "http_get", return_value=""):
            result = risk_radar.fetch_financial_stress()

        for field in (
            "ofr_fsi",
            "credit",
            "funding",
            "volatility",
            "equity_valuation",
            "safe_assets",
            "observed_at",
        ):
            self.assertIsNone(result[field])
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["data_status"], "unavailable")

    def test_ofr_scoring_uses_controlled_weight_increments(self) -> None:
        financial_stress = {"ofr_fsi": 6.0, "data_status": "ok"}
        recession = risk_radar.score_recession_risk(
            {"value": 15.0},
            {"spread_2y10y": 0.5},
            financial_stress,
        )
        stress = risk_radar.score_market_stress(
            {"value": 15.0},
            {"value": 100.0, "change_pct": 0.1},
            financial_stress,
        )

        self.assertEqual(recession["score"], 55)
        self.assertEqual(stress["score"], 35)
        self.assertIn("OFR FSI > 5 → 系统性金融压力极高", recession["signals"])
        self.assertIn("OFR FSI > 5 → 系统性金融压力极高", stress["signals"])
        self.assertEqual(recession["data_status"], "ok")
        self.assertEqual(stress["data_status"], "ok")

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

    def test_missing_financial_stress_never_implies_normal_or_ample(self) -> None:
        recession = risk_radar.score_recession_risk(
            {"value": 15.0},
            {"spread_2y10y": 0.5},
            {},
        )
        stress = risk_radar.score_market_stress(
            {"value": 15.0},
            {"value": 100.0},
            {},
        )

        for score in (recession, stress):
            self.assertEqual(score["data_status"], "partial")
            self.assertIn("financial_stress", score["missing_inputs"])
            self.assertIn("数据不完整", score["interpretation"])
            self.assertIn("OFR 金融压力", score["interpretation"])
        self.assertNotIn("宏观环境正常", recession["interpretation"])
        self.assertNotIn("流动性充裕", stress["interpretation"])

    def test_recent_complete_ofr_point_can_be_reused_but_is_marked_stale(self) -> None:
        previous = {
            "financial_stress": {
                "ofr_fsi": -1.5,
                "credit": -0.5,
                "funding": -0.2,
                "volatility": -0.3,
                "equity_valuation": -0.1,
                "safe_assets": -0.4,
                "observed_at": "2026-08-01",
                "unit": "index",
                "data_status": "ok",
            }
        }

        result = risk_radar._reuse_recent_financial_stress(
            risk_radar._empty_financial_stress(),
            previous,
            now=NOW,
        )

        self.assertEqual(result["ofr_fsi"], -1.5)
        self.assertEqual(result["data_status"], "stale")
        self.assertTrue(result["stale"])
        self.assertEqual(result["status"], "low")

    def test_expired_ofr_point_is_not_reused(self) -> None:
        previous = {
            "financial_stress": {
                "ofr_fsi": -1.5,
                "credit": -0.5,
                "funding": -0.2,
                "volatility": -0.3,
                "equity_valuation": -0.1,
                "safe_assets": -0.4,
                "observed_at": "2026-07-20",
                "data_status": "ok",
            }
        }

        result = risk_radar._reuse_recent_financial_stress(
            risk_radar._empty_financial_stress(),
            previous,
            now=NOW,
        )

        self.assertIsNone(result["ofr_fsi"])
        self.assertEqual(result["data_status"], "unavailable")

    def test_new_reports_publish_ofr_instead_of_legacy_oas(self) -> None:
        financial_stress = {
            "ofr_fsi": 1.0,
            "credit": 0.2,
            "funding": 0.2,
            "volatility": 0.2,
            "equity_valuation": 0.2,
            "safe_assets": 0.2,
            "observed_at": "2026-08-03",
            "source": "U.S. Treasury OFR",
            "source_url": risk_radar.OFR_FSI_SOURCE_URL,
            "unit": "index",
            "status": "normal",
            "data_status": "ok",
        }
        payloads = {
            risk_radar.fetch_vix: {"value": 15.0},
            risk_radar.fetch_treasury_yields: {"2Y": 4.0, "10Y": 4.5},
            risk_radar.fetch_usd_cny: {"rate": 7.0},
            risk_radar.fetch_gold_oil: {},
            risk_radar.fetch_dxy: {"value": 100.0, "change_pct": 0.0},
            risk_radar.fetch_financial_stress: financial_stress,
            risk_radar.fetch_yield_curve_analysis: {"spread_2y10y": 0.5},
        }

        def fake_safe_fetch(fn, default=None):
            return payloads.get(fn, default if default is not None else {})

        with mock.patch.object(risk_radar, "_safe_fetch", side_effect=fake_safe_fetch):
            report = risk_radar.generate_risk_report()

        self.assertEqual(report["market_data"]["financial_stress"], financial_stress)
        self.assertNotIn("credit_spreads", report["market_data"])
        self.assertNotIn("US high yield bond spread today", report["search_queries"])


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
    def test_policy_event_id_is_a_stable_sha256_key(self) -> None:
        url = (
            "https://federalreserve.gov/newsevents/pressreleases/"
            "test.htm?section=policy&amp;view=full"
        )
        normalized_url = url.replace("&amp;", "&")

        first = risk_radar.build_policy_events(
            [
                {
                    "title": "Federal Reserve policy statement",
                    "url": url,
                    "source": "Federal Reserve",
                    "date": "Mon, 03 Aug 2026 09:00:00 GMT",
                }
            ],
            now=NOW,
        )[0]
        repeated = risk_radar.build_policy_events(
            [
                {
                    "title": "A revised feed title must not change identity",
                    "url": url,
                    "source": "Federal Reserve",
                    "date": "Mon, 03 Aug 2026 09:00:00 GMT",
                }
            ],
            now=NOW,
        )[0]

        digest = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()
        self.assertEqual(first["id"], repeated["id"])
        self.assertEqual(first["id"], f"pol_{digest[:12]}")

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

    def test_ofr_fsi_material_change_creates_an_indicator_event(self) -> None:
        previous = {
            "financial_stress": {"ofr_fsi": 1.25, "data_status": "ok"},
        }
        current = {
            "financial_stress": {"ofr_fsi": 3.10, "data_status": "ok"},
        }

        events = risk_radar.detect_indicator_events(current, previous, now=NOW)

        event = next(e for e in events if e["id"] == "ind_ofr_fsi_rise")
        self.assertEqual(event["source"], "U.S. Treasury OFR")
        self.assertEqual(event["previous_value"], 1.25)
        self.assertEqual(event["current_value"], 3.10)
        self.assertEqual(event["unit"], "index")
        self.assertEqual(event["severity"], "high")
        self.assertIn("OFR FSI 上升", event["title"])

    def test_legacy_credit_event_remains_available_for_old_snapshots(self) -> None:
        previous = {"credit_spreads": {"hy_oas": 300.0}}
        current = {"credit_spreads": {"hy_oas": 340.0}}

        events = risk_radar.detect_indicator_events(current, previous, now=NOW)

        self.assertIn("ind_credit_widening", [event["id"] for event in events])

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
