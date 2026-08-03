from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock


LIB = Path(__file__).resolve().parents[2] / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import risk_radar  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
