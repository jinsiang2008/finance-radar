from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from kol_dashboard import macro_collect


class CoverageAnnotationTests(unittest.TestCase):
    @staticmethod
    def _source(report: dict, key: str) -> dict:
        return next(
            source
            for source in report["data_coverage"]["sources"]
            if source["key"] == key
        )

    def test_ofr_financial_stress_replaces_credit_slot_and_keeps_metadata(
        self,
    ) -> None:
        report = macro_collect.annotate_coverage(
            {
                "market_data": {
                    "financial_stress": {
                        "ofr_fsi": 1.25,
                        "status": "elevated",
                        "data_status": "ok",
                        "observed_at": "2026-08-04",
                        "source_url": (
                            "https://www.financialresearch.gov/"
                            "financial-stress-index/"
                        ),
                        "stale": False,
                        "note": "Official daily observation",
                        "account": "must-not-be-copied",
                    }
                }
            }
        )

        source = self._source(report, "financial_stress")
        self.assertEqual(report["data_coverage"]["total"], 8)
        self.assertEqual(
            source,
            {
                "key": "financial_stress",
                "label": "全球金融压力（OFR FSI）",
                "available": True,
                "status": "elevated",
                "data_status": "ok",
                "observed_at": "2026-08-04",
                "source_url": (
                    "https://www.financialresearch.gov/financial-stress-index/"
                ),
                "stale": False,
                "note": "Official daily observation",
            },
        )

    def test_legacy_hy_oas_keeps_financial_stress_coverage_available(self) -> None:
        report = macro_collect.annotate_coverage(
            {
                "market_data": {
                    "credit_spreads": {
                        "hy_oas": 450.0,
                        "status": "elevated",
                        "observed_at": "2026-08-03",
                    }
                }
            }
        )

        source = self._source(report, "financial_stress")
        self.assertTrue(source["available"])
        self.assertEqual(source["status"], "elevated")
        self.assertEqual(source["observed_at"], "2026-08-03")
        self.assertNotIn(
            "credit_spreads",
            {item["key"] for item in report["data_coverage"]["sources"]},
        )

    def test_unavailable_ofr_value_does_not_count_as_coverage(self) -> None:
        report = macro_collect.annotate_coverage(
            {
                "market_data": {
                    "financial_stress": {
                        "ofr_fsi": 1.25,
                        "data_status": "unavailable",
                    }
                }
            }
        )

        self.assertFalse(self._source(report, "financial_stress")["available"])

    def test_reused_stale_ofr_does_not_count_as_fresh_coverage(self) -> None:
        report = macro_collect.annotate_coverage(
            {
                "market_data": {
                    "financial_stress": {
                        "ofr_fsi": 1.25,
                        "data_status": "stale",
                        "stale": True,
                        "observed_at": "2026-07-01",
                    }
                }
            }
        )

        source = self._source(report, "financial_stress")
        self.assertFalse(source["available"])
        self.assertTrue(source["stale"])

    def test_gold_and_oil_both_need_prices_for_coverage(self) -> None:
        partial = macro_collect.annotate_coverage(
            {
                "market_data": {
                    "gold_oil": {"gold": {"price": 2400.0}, "oil": {}}
                }
            }
        )
        complete = macro_collect.annotate_coverage(
            {
                "market_data": {
                    "gold_oil": {
                        "gold": {"price": 2400.0},
                        "oil": {"price": 75.0},
                    }
                }
            }
        )

        self.assertFalse(self._source(partial, "gold_oil")["available"])
        self.assertTrue(self._source(complete, "gold_oil")["available"])

    def test_equity_alert_inputs_extend_coverage_without_copying_raw_bars(self) -> None:
        now = datetime(2026, 9, 6, 18, tzinfo=timezone.utc)
        calls = []

        def fetcher(asset_key, **kwargs):
            calls.append((asset_key, kwargs))
            end = now - timedelta(days=1)
            start = end - timedelta(days=69)
            return {
                "status": "available",
                "symbol": asset_key,
                "currency": "USD",
                "exchange_timezone": "America/New_York",
                "timestamp_semantics": "market_close",
                "bars": [
                    {
                        "timestamp": int((start + timedelta(days=index)).timestamp()),
                        "close": 100 + index,
                    }
                    for index in range(70)
                ],
            }

        inputs = macro_collect.collect_alert_inputs(
            history_fetcher=fetcher,
            now=now,
        )
        report = macro_collect.annotate_coverage(
            {"market_data": {"alert_inputs": inputs}}
        )

        self.assertEqual(len(calls), 4)
        self.assertTrue(self._source(report, "us_equity_trend")["available"])
        self.assertTrue(self._source(report, "cn_equity_trend")["available"])
        self.assertNotIn("bars", inputs["us_equity"])
        self.assertEqual(
            {kwargs["range_"] for _, kwargs in calls},
            {"6mo"},
        )

    def test_one_alert_history_failure_does_not_abort_other_series(self) -> None:
        now = datetime(2026, 9, 6, 18, tzinfo=timezone.utc)

        def fetcher(asset_key, **_kwargs):
            if asset_key == "INDEX:VIX":
                raise TimeoutError("offline")
            end = now - timedelta(days=1)
            start = end - timedelta(days=69)
            return {
                "status": "available",
                "symbol": asset_key,
                "exchange_timezone": "UTC",
                "bars": [
                    {
                        "timestamp": int((start + timedelta(days=index)).timestamp()),
                        "close": 100 + index,
                    }
                    for index in range(70)
                ],
            }

        inputs = macro_collect.collect_alert_inputs(
            history_fetcher=fetcher,
            now=now,
        )

        self.assertEqual(inputs["vix_daily"]["data_status"], "unavailable")
        self.assertEqual(inputs["us_equity"]["data_status"], "ok")

    def test_fresh_tencent_history_replaces_stale_yahoo_csi300(self) -> None:
        now = datetime(2026, 9, 6, 18, tzinfo=timezone.utc)

        def make_history(asset_key, *, end, provider="yahoo"):
            if asset_key == "INDEX:CSI300":
                end = end.replace(hour=7, minute=0, second=0, microsecond=0)
            start = end - timedelta(days=69)
            return {
                "status": "available",
                "provider": provider,
                "symbol": "sh000300" if provider == "tencent" else asset_key,
                "currency": "CNY" if asset_key == "INDEX:CSI300" else "USD",
                "exchange_timezone": (
                    "Asia/Shanghai"
                    if asset_key == "INDEX:CSI300"
                    else "America/New_York"
                ),
                "timestamp_semantics": "market_close",
                "bars": [
                    {
                        "timestamp": int((start + timedelta(days=index)).timestamp()),
                        "close": 100 + index,
                    }
                    for index in range(70)
                ],
            }

        def yahoo(asset_key, **_kwargs):
            end = (
                now - timedelta(days=40)
                if asset_key == "INDEX:CSI300"
                else now - timedelta(days=1)
            )
            return make_history(asset_key, end=end)

        fallback_calls = []

        def tencent(asset_key, **kwargs):
            fallback_calls.append((asset_key, kwargs))
            return make_history(
                asset_key,
                end=now - timedelta(days=1),
                provider="tencent",
            )

        inputs = macro_collect.collect_alert_inputs(
            history_fetcher=yahoo,
            cn_history_fallback=tencent,
            now=now,
        )

        self.assertEqual(len(fallback_calls), 1)
        self.assertEqual(inputs["cn_equity"]["data_status"], "ok")
        self.assertEqual(inputs["cn_equity"]["provider"], "tencent")
        self.assertEqual(inputs["cn_equity"]["source"], "腾讯行情")
        self.assertEqual(inputs["cn_equity"]["market_date"], "2026-09-05")


if __name__ == "__main__":
    unittest.main()
