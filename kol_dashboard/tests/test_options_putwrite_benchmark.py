from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from research import options_putwrite_benchmark as benchmark


class CsvLoadingTests(unittest.TestCase):
    def test_loads_cboe_style_csv_and_records_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "PUT_History.csv"
            path.write_text(
                "DATE,PUT\n01/03/2020,100.0\n01/06/2020,101.5\n",
                encoding="utf-8",
            )

            points, digest = benchmark.load_series(path, "PUT")

        self.assertEqual(
            points,
            [
                benchmark.Observation(date(2020, 1, 3), 100.0),
                benchmark.Observation(date(2020, 1, 6), 101.5),
            ],
        )
        self.assertEqual(len(digest), 64)

    def test_rejects_duplicate_dates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.csv"
            path.write_text(
                "Date,Close\n2020-01-03,100\n2020-01-03,101\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "duplicate date"):
                benchmark.load_series(path, "TEST")


class MetricsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.points = [
            benchmark.Observation(date(2020, 1, 2), 100.0),
            benchmark.Observation(date(2020, 1, 31), 120.0),
            benchmark.Observation(date(2020, 2, 28), 90.0),
            benchmark.Observation(date(2020, 3, 31), 108.0),
        ]

    def test_reports_true_peak_to_trough_drawdown(self) -> None:
        metrics = benchmark.compute_metrics("TEST", self.points, "abc")

        self.assertAlmostEqual(metrics.daily_max_drawdown, -0.25)
        self.assertEqual(metrics.daily_drawdown_peak, "2020-01-31")
        self.assertEqual(metrics.daily_drawdown_trough, "2020-02-28")
        self.assertEqual(metrics.worst_month, "2020-02")
        self.assertAlmostEqual(metrics.worst_month_return or 0.0, -0.25)
        self.assertAlmostEqual(metrics.positive_month_ratio or 0.0, 1 / 2)

    def test_stress_period_uses_first_and_last_available_observation(self) -> None:
        result = benchmark.period_return(
            self.points, date(2020, 1, 15), date(2020, 3, 15)
        )

        self.assertEqual(result["start"], "2020-01-31")
        self.assertEqual(result["end"], "2020-02-28")
        self.assertAlmostEqual(result["return"], -0.25)

    def test_report_uses_common_range_and_compares_with_benchmark(self) -> None:
        shifted = [
            benchmark.Observation(date(2020, 1, 31), 200.0),
            benchmark.Observation(date(2020, 2, 28), 190.0),
            benchmark.Observation(date(2020, 3, 31), 205.0),
            benchmark.Observation(date(2020, 4, 30), 210.0),
        ]

        report = benchmark.build_report(
            {"TEST": (self.points, "a"), "SPX": (shifted, "b")},
            benchmark_name="SPX",
        )

        self.assertEqual(report["series"]["TEST"]["start"], "2020-01-31")
        self.assertEqual(report["series"]["SPX"]["end"], "2020-03-31")
        self.assertIn("monthly_beta", report["monthly_comparison"]["TEST"])

    def test_markdown_preserves_limitations_and_hashes(self) -> None:
        report = benchmark.build_report({"TEST": (self.points, "digest")})

        rendered = benchmark.render_markdown(report)

        self.assertIn("Input hashes", rendered)
        self.assertIn("Trailing CAGR", rendered)
        self.assertIn("`TEST`: `digest`", rendered)
        self.assertIn("not executable fills", rendered)


if __name__ == "__main__":
    unittest.main()
