from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kol_dashboard import db


class DomainDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path_patch = mock.patch.object(
            db, "DB_PATH", str(Path(self.tmp.name) / "domain.sqlite3")
        )
        self.db_path_patch.start()
        db.init()

    def tearDown(self) -> None:
        self.db_path_patch.stop()
        self.tmp.cleanup()

    def test_market_price_upsert_is_idempotent_and_updates_same_key(self) -> None:
        first = {
            "timestamp": 1704067200,
            "close": 100.0,
            "volume": 10,
            "currency": "USD",
        }
        updated = {**first, "close": 101.5, "volume": 12}

        db.upsert_market_prices(
            "US:TEST",
            "yahoo",
            [first],
            provider_symbol="TEST",
            observed_at="2026-01-02T00:00:00+00:00",
        )
        db.upsert_market_prices(
            "US:TEST",
            "yahoo",
            [updated],
            provider_symbol="TEST",
            observed_at="2026-01-03T00:00:00+00:00",
        )

        rows = db.query_market_prices(asset_key="US:TEST", provider="yahoo")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["close"], 101.5)
        self.assertEqual(rows[0]["volume"], 12.0)
        self.assertEqual(rows[0]["observed_at"], "2026-01-03T00:00:00+00:00")

    def test_market_prices_reject_non_finite_numeric_data(self) -> None:
        with self.assertRaises(ValueError):
            db.upsert_market_prices(
                "US:TEST",
                "yahoo",
                [{"timestamp": 1704067200, "close": math.nan}],
            )
        self.assertEqual(db.query_market_prices(asset_key="US:TEST"), [])

    def test_market_reaction_upsert_is_idempotent_and_preserves_metadata(self) -> None:
        reaction = {
            "window": "1D",
            "status": "complete",
            "asset_return": 0.1,
            "benchmark_return": 0.03,
            "abnormal_return": 0.07,
            "expected_direction": "positive",
            "observed_direction": "positive",
            "direction_confirmed": True,
            "sample_count": 2,
            "data_timestamps": {"start": 1704067200, "end": 1704153600},
            "method_version": "common_trading_days:test",
        }

        db.upsert_market_reaction(
            "event",
            "42",
            "US:TEST",
            reaction,
            benchmark_asset_key="US:SPY",
            observed_at="2026-01-03T00:00:00+00:00",
        )
        db.upsert_market_reaction(
            "event",
            "42",
            "US:TEST",
            {**reaction, "abnormal_return": 0.08},
            benchmark_asset_key="US:SPY",
            observed_at="2026-01-04T00:00:00+00:00",
        )

        rows = db.query_market_reactions(
            source_type="event", source_id="42", asset_key="US:TEST"
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["benchmark_asset_key"], "US:SPY")
        self.assertEqual(rows[0]["method_version"], "common_trading_days:test")
        self.assertEqual(rows[0]["abnormal_return"], 0.08)
        self.assertEqual(rows[0]["expected_direction"], "positive")
        self.assertEqual(rows[0]["observed_direction"], "positive")
        self.assertEqual(
            rows[0]["data_timestamps"],
            {"end": 1704153600, "start": 1704067200},
        )
        self.assertEqual(rows[0]["observed_at"], "2026-01-04T00:00:00+00:00")

    def test_complete_market_reaction_is_not_downgraded_by_fetch_failure(
        self,
    ) -> None:
        complete = {
            "window": "5D",
            "status": "complete",
            "asset_return": 0.1,
            "benchmark_return": 0.03,
            "abnormal_return": 0.07,
            "expected_direction": "positive",
            "observed_direction": "positive",
            "direction_confirmed": True,
            "sample_count": 6,
            "data_timestamps": {"start": 1, "end": 6},
            "method_version": "common_trading_days:test",
        }
        unavailable = {
            "window": "5D",
            "status": "unavailable",
            "sample_count": 0,
            "data_timestamps": {},
            "method_version": "common_trading_days:test",
        }
        db.upsert_market_reaction(
            "event",
            "stable",
            "US:TEST",
            complete,
            observed_at="2026-01-03T00:00:00+00:00",
        )
        db.upsert_market_reaction(
            "event",
            "stable",
            "US:TEST",
            unavailable,
            observed_at="2026-01-04T00:00:00+00:00",
        )

        row = db.query_market_reactions(
            source_id="stable", asset_key="US:TEST"
        )[0]

        self.assertEqual(row["status"], "complete")
        self.assertEqual(row["abnormal_return"], 0.07)
        self.assertEqual(row["observed_at"], "2026-01-03T00:00:00+00:00")

    def test_market_reaction_rejects_unsafe_json_and_ranges(self) -> None:
        with self.assertRaises(ValueError):
            db.upsert_market_reaction(
                "event",
                "bad",
                "US:TEST",
                {
                    "window": "1D",
                    "status": "complete",
                    "asset_return": -1.1,
                    "benchmark_return": 0,
                    "abnormal_return": math.inf,
                    "direction_confirmed": True,
                    "sample_count": 2,
                    "data_timestamps": {"bad": math.nan},
                    "method_version": "test",
                },
            )
        with self.assertRaises(ValueError):
            db.upsert_market_reaction(
                "event",
                "private-json",
                "US:TEST",
                {
                    "window": "1D",
                    "status": "complete",
                    "asset_return": 0.1,
                    "benchmark_return": 0.02,
                    "abnormal_return": 0.08,
                    "direction_confirmed": True,
                    "sample_count": 2,
                    "data_timestamps": {
                        "start": 1,
                        "end": 2,
                        "账户": "must-not-persist",
                    },
                    "method_version": "test",
                },
            )

    def test_market_reaction_batch_is_atomic(self) -> None:
        result = {
            "method_version": "common_trading_days:test",
            "windows": {
                "1D": {
                    "status": "complete",
                    "asset_return": 0.1,
                    "benchmark_return": 0.03,
                    "abnormal_return": 0.07,
                    "direction_confirmed": True,
                    "sample_count": 2,
                    "data_timestamps": {"start": 1, "end": 2},
                },
                "3D": {
                    "status": "complete",
                    "asset_return": None,
                    "benchmark_return": 0.03,
                    "abnormal_return": 0.07,
                    "direction_confirmed": True,
                    "sample_count": 4,
                    "data_timestamps": {"start": 1, "end": 4},
                },
            },
        }

        with self.assertRaises(ValueError):
            db.upsert_market_reactions(
                "event",
                "atomic",
                "US:TEST",
                result,
                benchmark_asset_key="US:SPY",
            )

        self.assertEqual(
            db.query_market_reactions(source_id="atomic", asset_key="US:TEST"),
            [],
        )

    def test_observed_at_is_normalized_to_utc(self) -> None:
        db.upsert_market_prices(
            "US:TEST",
            "yahoo",
            [{"timestamp": 1704067200, "close": 100}],
            observed_at="2026-01-02T08:00:00+08:00",
        )

        row = db.query_market_prices(asset_key="US:TEST")[0]
        self.assertEqual(row["observed_at"], "2026-01-02T00:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
