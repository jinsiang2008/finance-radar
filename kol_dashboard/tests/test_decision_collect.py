from __future__ import annotations

import importlib
import unittest


try:
    decision_collect = importlib.import_module("kol_dashboard.decision_collect")
except ModuleNotFoundError:
    decision_collect = None


class FakeRepository:
    def __init__(self) -> None:
        self.replacements = []
        self.price_batches = []
        self.reaction_batches = []
        self.saved_snapshots = []
        self.query_kwargs = None
        self.decision_relation_kwargs = None

    def query_events(self, **kwargs):
        self.query_kwargs = kwargs
        return [
            {
                "id": 7,
                "dedup_key": "event-seven",
                "title": "Bullish NVIDIA AI demand",
                "snippet": "buy $NVDA",
                "tickers": ["NVDA"],
                "impact": "medium",
                "published_at": "2026-08-02T12:00:00+00:00",
                "time_status": "verified",
            }
        ]

    def latest_macro(self):
        return {
            "snapshot_id": 9,
            "public_schema_version": 1,
            "opportunities": [
                {
                    "id": "gold",
                    "name": "黄金机会",
                    "asset": "GLD",
                    "confidence": "medium",
                }
            ],
        }

    def replace_relations(self, source_type, source_id, edges):
        self.replacements.append((source_type, str(source_id), edges))
        return len(edges)

    def query_market_validation_relations(self, **kwargs):
        self.decision_relation_kwargs = kwargs
        return [
            {
                "source_type": "event",
                "source_id": "7",
                "topic_key": "ai_semiconductors",
                "asset_key": "US:NVDA",
                "direction": "positive",
                "evidence_json": (
                    '{"published_at":"2026-08-02T12:00:00+00:00"}'
                ),
            }
        ]

    def query_market_reactions(self, **kwargs):
        return []

    def upsert_market_prices(
        self, asset_key, provider, bars, **kwargs
    ):
        self.price_batches.append((asset_key, provider, bars, kwargs))
        return len(bars)

    def upsert_market_reactions(
        self, source_type, source_id, asset_key, result, **kwargs
    ):
        self.reaction_batches.append(
            (source_type, str(source_id), asset_key, result, kwargs)
        )
        return len(result["windows"])

    def save_portfolio_snapshot(self, snapshot):
        self.saved_snapshots.append(snapshot)
        return 3


class DecisionCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(
            decision_collect, "kol_dashboard.decision_collect is required"
        )

    def test_relation_collection_uses_only_recent_verified_events(self) -> None:
        repository = FakeRepository()

        summary = decision_collect.collect_relations(
            repository=repository,
            now="2026-08-03T00:00:00+00:00",
        )

        self.assertEqual(repository.query_kwargs["hours"], 72)
        self.assertEqual(repository.query_kwargs["time_status"], "verified")
        self.assertEqual(repository.query_kwargs["limit"], 1_000)
        self.assertGreaterEqual(summary["event_relations"], 1)
        self.assertGreaterEqual(summary["macro_relations"], 1)
        self.assertTrue(repository.replacements)

    def test_market_collection_fetches_assets_and_persists_reactions(self) -> None:
        repository = FakeRepository()
        fetched = []

        def fetch_history(asset_key, **kwargs):
            fetched.append((asset_key, kwargs))
            return {
                "status": "complete",
                "provider": "yahoo",
                "symbol": asset_key,
                "bars": [
                    {
                        "timestamp": 1_775_174_400,
                        "close": 100.0,
                        "observed_at": "2026-08-03T00:00:00+00:00",
                    }
                ],
            }

        def compute_reaction(asset_bars, benchmark_bars, event_time, **kwargs):
            self.assertEqual(event_time, "2026-08-02T12:00:00+00:00")
            self.assertEqual(kwargs["expected_direction"], "positive")
            return {
                "method_version": "test",
                "expected_direction": "positive",
                "windows": {
                    "1D": {"window": "1D", "status": "preliminary"},
                    "3D": {"window": "3D", "status": "preliminary"},
                    "5D": {"window": "5D", "status": "preliminary"},
                },
            }

        summary = decision_collect.collect_market_reactions(
            repository=repository,
            history_fetcher=fetch_history,
            reaction_computer=compute_reaction,
            now="2026-08-03T00:00:00+00:00",
            max_edges=10,
        )

        self.assertEqual(
            {asset for asset, _ in fetched},
            {"US:NVDA", "US:SOXX"},
        )
        self.assertEqual(repository.query_kwargs["hours"], 14 * 24)
        self.assertEqual(
            repository.decision_relation_kwargs["event_max_age_hours"],
            14 * 24,
        )
        self.assertEqual(len(repository.price_batches), 2)
        self.assertEqual(len(repository.reaction_batches), 1)
        self.assertEqual(summary["processed"], 1)
        self.assertEqual(summary["reaction_rows"], 3)

    def test_missing_portfolio_is_reported_without_failing_collection(self) -> None:
        repository = FakeRepository()

        missing = decision_collect.collect_portfolio(
            repository=repository,
            holdings_loader=lambda: (_ for _ in ()).throw(
                FileNotFoundError("missing")
            ),
        )
        stored = decision_collect.collect_portfolio(
            repository=repository,
            holdings_loader=lambda: {
                "schema_version": 1,
                "source_hash": "a" * 64,
                "as_of": None,
                "positions": [],
            },
        )

        self.assertEqual(missing, {"available": False, "reason": "missing"})
        self.assertTrue(stored["available"])
        self.assertEqual(stored["snapshot_id"], 3)
        self.assertEqual(len(repository.saved_snapshots), 1)


if __name__ == "__main__":
    unittest.main()
