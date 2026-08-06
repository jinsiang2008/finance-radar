from __future__ import annotations

import unittest

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
        self.assertEqual(report["data_coverage"]["total"], 6)
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


if __name__ == "__main__":
    unittest.main()
