from __future__ import annotations

import importlib
import unittest
from unittest import mock


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
            now="2026-08-04T00:00:00+00:00",
            max_edges=10,
        )

        self.assertEqual(
            {asset for asset, _ in fetched},
            {"US:NVDA", "US:SOXX"},
        )
        self.assertEqual(repository.query_kwargs["hours"], 14 * 24)
        self.assertEqual(repository.query_kwargs["limit"], 5_000)
        self.assertEqual(
            repository.decision_relation_kwargs["event_max_age_hours"],
            14 * 24,
        )
        self.assertEqual(len(repository.price_batches), 2)
        self.assertEqual(len(repository.reaction_batches), 1)
        self.assertEqual(summary["processed"], 1)
        self.assertEqual(summary["reaction_rows"], 3)
        self.assertEqual(summary["business_status"], "ok")
        persisted = repository.reaction_batches[0][3]
        self.assertEqual(persisted["windows"]["1D"]["status"], "preliminary")
        self.assertEqual(persisted["windows"]["3D"]["status"], "pending")
        self.assertEqual(
            persisted["windows"]["3D"]["reason_code"], "window_not_due"
        )
        self.assertEqual(persisted["provider"], "yahoo")

    def test_market_collection_reports_business_degradation_safely(self) -> None:
        repository = FakeRepository()

        def transient_failure(*_args, **_kwargs):
            raise TimeoutError("temporary upstream timeout")

        summary = decision_collect.collect_market_reactions(
            repository=repository,
            history_fetcher=transient_failure,
            now="2026-08-04T00:00:00+00:00",
            max_edges=10,
        )

        self.assertEqual(summary["eligible"], 1)
        self.assertEqual(summary["attempted"], 1)
        self.assertEqual(summary["processed"], 0)
        self.assertEqual(summary["business_status"], "degraded")
        self.assertTrue(summary["degraded"])
        self.assertEqual(summary["reason_counts"], {"request_failed": 1})
        persisted = repository.reaction_batches[0][3]
        self.assertEqual(persisted["windows"]["1D"]["status"], "unavailable")
        self.assertEqual(
            persisted["windows"]["1D"]["reason_code"], "request_failed"
        )
        self.assertNotIn(
            "TimeoutError",
            str(persisted),
        )

    def test_market_collection_persists_fresh_windows_without_fetching(self) -> None:
        repository = FakeRepository()

        summary = decision_collect.collect_market_reactions(
            repository=repository,
            history_fetcher=lambda *_a, **_k: self.fail("must not fetch"),
            now="2026-08-03T00:00:00+00:00",
            max_edges=10,
        )

        self.assertEqual(summary["eligible"], 0)
        self.assertEqual(summary["skipped_not_due"], 1)
        self.assertEqual(summary["pending_scheduled"], 1)
        self.assertEqual(summary["business_status"], "pending")
        self.assertEqual(len(repository.reaction_batches), 1)
        persisted = repository.reaction_batches[0][3]
        self.assertEqual(
            {
                item["status"]
                for item in persisted["windows"].values()
            },
            {"pending"},
        )
        self.assertEqual(
            persisted["windows"]["1D"]["next_due_at"],
            "2026-08-03T12:00:00+00:00",
        )

    def test_weekend_follow_up_stays_pending_without_degradation(self) -> None:
        class WeekendRepository(FakeRepository):
            def query_events(self, **kwargs):
                self.query_kwargs = kwargs
                return [
                    {
                        "id": 7,
                        "dedup_key": "event-seven",
                        "published_at": "2026-08-07T20:00:00+00:00",
                        "time_status": "verified",
                    }
                ]

        repository = WeekendRepository()
        friday_close = 1_786_118_400  # 2026-08-07T16:00:00Z

        def history(asset_key, **_kwargs):
            return {
                "status": "available",
                "provider": "yahoo",
                "symbol": asset_key,
                "bars": [
                    {"timestamp": friday_close, "close": 100.0, "volume": 1}
                ],
            }

        summary = decision_collect.collect_market_reactions(
            repository=repository,
            history_fetcher=history,
            now="2026-08-09T20:00:00+00:00",
            max_edges=10,
        )

        self.assertEqual(summary["eligible"], 1)
        self.assertEqual(summary["attempted"], 1)
        self.assertEqual(summary["processed"], 0)
        self.assertEqual(summary["unavailable"], 0)
        self.assertEqual(summary["pending"], 1)
        self.assertEqual(summary["business_status"], "pending")
        self.assertFalse(summary["degraded"])
        persisted = repository.reaction_batches[0][3]
        self.assertEqual(persisted["windows"]["1D"]["status"], "pending")
        self.assertEqual(
            persisted["windows"]["1D"]["reason_code"],
            "follow_up_unavailable",
        )
        self.assertEqual(
            persisted["windows"]["1D"]["next_due_at"],
            "2026-08-10T02:00:00+00:00",
        )

    def test_due_unavailable_edges_receive_reserved_capacity(self) -> None:
        class RetryRepository(FakeRepository):
            def query_events(self, **kwargs):
                self.query_kwargs = kwargs
                return [
                    {
                        "id": index,
                        "dedup_key": f"event-{index}",
                        "published_at": "2026-08-01T00:00:00+00:00",
                        "time_status": "verified",
                    }
                    for index in range(1, 5)
                ]

            def query_market_validation_relations(self, **kwargs):
                self.decision_relation_kwargs = kwargs
                return [
                    {
                        "source_type": "event",
                        "source_id": str(index),
                        "topic_key": "general",
                        "asset_key": f"US:T{index}",
                        "direction": "positive",
                    }
                    for index in range(1, 5)
                ]

            def query_market_reactions(self, **kwargs):
                return [
                    {
                        "source_type": "event",
                        "source_id": "4",
                        "asset_key": "US:T4",
                        "window": window,
                        "status": "unavailable",
                        "reason_code": "request_failed",
                        "next_due_at": "2026-08-02T00:00:00+00:00",
                        "observed_at": "2026-08-02T00:00:00+00:00",
                    }
                    for window in ("1D", "3D", "5D")
                ]

        repository = RetryRepository()

        def available(asset_key, **_kwargs):
            return {
                "status": "available",
                "provider": "yahoo",
                "symbol": asset_key,
                "bars": [{"timestamp": 1, "close": 100}],
            }

        def reaction(*_args, **_kwargs):
            return {
                "status": "preliminary",
                "method_version": "test",
                "windows": {
                    label: {
                        "window": label,
                        "status": "preliminary",
                        "sample_count": 1,
                        "data_timestamps": {},
                    }
                    for label in ("1D", "3D", "5D")
                },
            }

        summary = decision_collect.collect_market_reactions(
            repository=repository,
            history_fetcher=available,
            reaction_computer=reaction,
            now="2026-08-08T00:00:00+00:00",
            max_edges=2,
        )

        selected_sources = {
            batch[1] for batch in repository.reaction_batches
        }
        self.assertEqual(summary["eligible"], 4)
        self.assertEqual(summary["reserved_unavailable_retries"], 1)
        self.assertIn("4", selected_sources)

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

    def test_public_input_commands_refresh_exactly_one_snapshot(self) -> None:
        repository = mock.Mock()
        with (
            mock.patch.object(
                decision_collect, "collect_relations", return_value={"ok": 1}
            ),
            mock.patch.object(
                decision_collect,
                "collect_market_reactions",
                return_value={"ok": 1},
            ),
            mock.patch.object(
                decision_collect, "collect_portfolio", return_value={"ok": 1}
            ),
            mock.patch.object(
                decision_collect,
                "collect_snapshot",
                return_value={"snapshot_id": 7},
            ) as snapshot,
        ):
            all_result = decision_collect.run("all", repository=repository)
            self.assertIn("snapshot", all_result)
            snapshot.assert_called_once_with(repository=repository)

            snapshot.reset_mock()
            relations_result = decision_collect.run(
                "relations", repository=repository
            )
            self.assertIn("snapshot", relations_result)
            snapshot.assert_called_once_with(repository=repository)

            snapshot.reset_mock()
            market_result = decision_collect.run("market", repository=repository)
            self.assertIn("snapshot", market_result)
            snapshot.assert_called_once_with(repository=repository)

    def test_private_portfolio_refresh_does_not_rebuild_public_snapshot(self) -> None:
        repository = mock.Mock()
        with (
            mock.patch.object(
                decision_collect, "collect_portfolio", return_value={"ok": 1}
            ),
            mock.patch.object(decision_collect, "collect_snapshot") as snapshot,
        ):
            result = decision_collect.run("portfolio", repository=repository)

        self.assertEqual(result, {"portfolio": {"ok": 1}})
        snapshot.assert_not_called()

    def test_main_uses_dedicated_exit_only_for_market_degradation(self) -> None:
        degraded = {
            "market": {
                "eligible": 2,
                "attempted": 2,
                "processed": 0,
                "unavailable": 2,
                "degraded": True,
            }
        }
        pending = {
            "market": {
                "eligible": 2,
                "attempted": 2,
                "processed": 0,
                "unavailable": 0,
                "pending": 2,
                "degraded": False,
            }
        }
        cases = (
            ("market", degraded, decision_collect.MARKET_DEGRADED_EXIT_CODE),
            ("all", degraded, decision_collect.MARKET_DEGRADED_EXIT_CODE),
            ("market", pending, 0),
            ("snapshot", {"snapshot": {"snapshot_id": 1}}, 0),
        )
        for command, summary, expected in cases:
            with self.subTest(command=command, expected=expected), (
                mock.patch.object(
                    decision_collect.argparse.ArgumentParser,
                    "parse_args",
                    return_value=mock.Mock(command=command),
                )
            ), mock.patch.object(
                decision_collect, "run", return_value=summary
            ), mock.patch("builtins.print") as output:
                exit_code = decision_collect.main()

            self.assertEqual(exit_code, expected)
            output.assert_called_once()


if __name__ == "__main__":
    unittest.main()
