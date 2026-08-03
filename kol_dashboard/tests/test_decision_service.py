from __future__ import annotations

import importlib
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

from kol_dashboard import db


try:
    decision_service = importlib.import_module("kol_dashboard.decision_service")
except ModuleNotFoundError:
    decision_service = None


TEST_NOW = "2026-08-01T00:00:00+00:00"


def _relation(
    source_id: str,
    direction: str,
    *,
    source_type: str = "event",
    strength: float = 0.8,
    confidence: float = 0.8,
    evidence: dict | None = None,
) -> dict:
    return {
        "source_type": source_type,
        "source_id": source_id,
        "topic_key": "ai_semiconductors",
        "asset_key": "US:NVDA",
        "relation_type": "view" if source_type == "event" else "opportunity",
        "direction": direction,
        "strength": strength,
        "confidence": confidence,
        "horizon": "medium",
        "method": "deterministic_rules:test",
        "rationale": (
            "文本与资产同现；这是相关性证据，不表示该陈述导致价格变化。"
        ),
        "evidence": evidence
        or {
            "title": f"source {source_id}",
            "published_at": "2026-07-31T10:00:00+00:00",
        },
        "created_at": "2026-07-31T10:00:00+00:00",
    }


def _reaction(
    source_id: str,
    *,
    direction_confirmed: bool = True,
    status: str = "complete",
) -> dict:
    return {
        "source_type": "event",
        "source_id": source_id,
        "asset_key": "US:NVDA",
        "window": "3D",
        "benchmark_asset_key": "US:SOXX",
        "asset_return": 0.12 if direction_confirmed else -0.02,
        "benchmark_return": 0.03,
        "abnormal_return": 0.09 if direction_confirmed else -0.05,
        "expected_direction": "positive",
        "observed_direction": "positive" if direction_confirmed else "negative",
        "direction_confirmed": direction_confirmed,
        "status": status,
        "sample_count": 4,
        "data_timestamps": {
            "start": "2026-07-30T00:00:00+00:00",
            "end": "2026-08-01T00:00:00+00:00",
        },
        "method_version": "common_trading_days:test",
        "observed_at": "2026-08-01T00:00:00+00:00",
    }


class PublicDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(
            decision_service, "kol_dashboard.decision_service is required"
        )

    def test_conflicting_sources_cluster_without_losing_duplicate_evidence(self) -> None:
        relations = [
            _relation("kol-a", "positive"),
            _relation(
                "kol-a",
                "positive",
                evidence={
                    "title": "second observation",
                    "published_at": "2026-07-31T11:00:00+00:00",
                },
            ),
            _relation(
                "macro-a",
                "negative",
                source_type="macro_snapshot",
                evidence={
                    "name": "demand shock",
                    "published_at": "2026-07-31T12:00:00+00:00",
                },
            ),
        ]
        original = deepcopy(relations)

        result = decision_service.build_public_decisions(
            relations,
            [_reaction("kol-a")],
            {"coverage": 0.8},
            now=TEST_NOW,
        )

        self.assertEqual(len(result["decisions"]), 1)
        card = result["decisions"][0]
        self.assertEqual(card["classification"], "conflict")
        self.assertEqual(card["direction"], "mixed")
        self.assertEqual(card["source_count"], 2)
        self.assertEqual(len(card["evidence"]), 3)
        self.assertEqual(len(card["mechanism_relations"]), 3)
        self.assertIn("market_validation", card)
        self.assertEqual(card["data_as_of"], "2026-08-01T00:00:00+00:00")
        self.assertEqual(card["confidence"], card["score_components"]["confidence"])
        self.assertTrue(card["human_review_required"])
        self.assertIn(
            card["action_stage"],
            {"observe", "verify", "reduce_or_hedge", "scale_in"},
        )
        self.assertEqual(
            set(card["score_components"]),
            {
                "strength",
                "confidence",
                "freshness",
                "corroboration",
                "market_confirmation",
                "coverage",
            },
        )
        self.assertIn("total_score", card)
        self.assertEqual(relations, original)

    def test_low_macro_coverage_lowers_confidence_transparently(self) -> None:
        relations = [_relation("macro-a", "positive", source_type="macro_snapshot")]

        full = decision_service.build_public_decisions(
            relations, [], 1.0, now=TEST_NOW
        )
        sparse = decision_service.build_public_decisions(
            relations, [], 0.2, now=TEST_NOW
        )

        full_card = full["decisions"][0]
        sparse_card = sparse["decisions"][0]
        self.assertLess(
            sparse_card["score_components"]["confidence"],
            full_card["score_components"]["confidence"],
        )
        self.assertLess(sparse_card["total_score"], full_card["total_score"])
        self.assertEqual(sparse_card["score_components"]["coverage"], 0.2)

    def test_public_json_excludes_all_private_position_vocabulary(self) -> None:
        relation = _relation(
            "kol-private",
            "positive",
            evidence={
                "title": "AI demand",
                "account": "secret-broker",
                "quantity": 99,
                "avg_cost": 123.45,
                "portfolio": "secret",
                "matched_positions": ["secret"],
                "published_at": "2026-07-31T10:00:00+00:00",
            },
        )

        result = decision_service.build_public_decisions(
            [relation], [_reaction("kol-private")], 1.0, now=TEST_NOW
        )
        encoded = json.dumps(result, ensure_ascii=False).lower()

        for forbidden in (
            "shares",
            "quantity",
            "account",
            "cost",
            "portfolio",
            "matched_positions",
        ):
            self.assertNotIn(forbidden, encoded)

    def test_public_projection_drops_unapproved_private_aliases(self) -> None:
        relation = _relation(
            "kol-secret",
            "positive",
            evidence={
                "title": "AI demand",
                "published_at": "2026-07-31T10:00:00+00:00",
                "broker": "hidden-broker",
                "qty": 88,
                "email": "private@example.com",
                "账户": "私密账户",
                "持仓数量": 77,
                "备注": "private-note",
                "nested": {"phone": "13800000000"},
            },
        )
        reaction = {
            **_reaction("kol-secret"),
            "broker": "reaction-secret",
            "metadata": {"account_name": "hidden"},
            "data_timestamps": {
                "start": 1,
                "end": 4,
                "账户": "nested-secret",
                "phone": "13900000000",
            },
        }
        relation["evidence"]["title"] = {
            "text": "AI demand",
            "账户": "evidence-secret",
        }
        relation["method"] = {"账户": "method-secret"}
        relation["rationale"] = {"phone": "13700000000"}
        relation["source_type"] = {"账户": "source-type-secret"}
        relation["source_id"] = {"phone": "source-id-secret"}
        reaction["source_type"] = {"账户": "source-type-secret"}
        reaction["source_id"] = {"phone": "source-id-secret"}
        reaction["direction_confirmed"] = False

        result = decision_service.build_public_decisions(
            [relation], [reaction], 1.0, now=TEST_NOW
        )
        encoded = json.dumps(result, ensure_ascii=False).lower()

        for secret in (
            "hidden-broker",
            "private@example.com",
            "私密账户",
            "private-note",
            "13800000000",
            "reaction-secret",
            "account_name",
            "nested-secret",
            "13900000000",
            "evidence-secret",
            "method-secret",
            "13700000000",
            "source-type-secret",
            "source-id-secret",
        ):
            self.assertNotIn(secret.lower(), encoded)

    def test_public_projection_helpers_fail_closed_for_direct_api_use(self) -> None:
        relation = _relation(
            "public-helper",
            "positive",
            evidence={
                "title": "Visible title",
                "published_at": "2026-07-31T10:00:00+00:00",
                "account": "hidden-account",
            },
        )
        relation["private_note"] = "hidden-note"
        reaction = {
            **_reaction("public-helper"),
            "metadata": {"账户": "hidden-reaction"},
        }

        relations = decision_service.project_public_relations([relation])
        reactions = decision_service.project_public_reactions([reaction])
        encoded = json.dumps(
            {"relations": relations, "reactions": reactions},
            ensure_ascii=False,
        )

        self.assertEqual(relations[0]["topic_key"], "ai_semiconductors")
        self.assertEqual(relations[0]["evidence"]["title"], "Visible title")
        self.assertNotIn("hidden-account", encoded)
        self.assertNotIn("hidden-note", encoded)
        self.assertNotIn("hidden-reaction", encoded)

    def test_incomplete_market_sample_abstains_from_scaled_action(self) -> None:
        preliminary = _reaction("kol-a", status="preliminary")

        result = decision_service.build_public_decisions(
            [_relation("kol-a", "positive")],
            [preliminary],
            1.0,
            now=TEST_NOW,
        )
        card = result["decisions"][0]

        self.assertTrue(card["market_validation"]["abstain"])
        self.assertIn(card["action_stage"], {"observe", "verify"})
        self.assertIn("样本", card["market_validation"]["note"])

    def test_complete_but_contrary_market_never_escalates_action(self) -> None:
        result = decision_service.build_public_decisions(
            [_relation("kol-a", "positive", strength=1.0, confidence=1.0)],
            [_reaction("kol-a", direction_confirmed=False)],
            1.0,
            now=TEST_NOW,
        )
        card = result["decisions"][0]

        self.assertTrue(card["market_validation"]["abstain"])
        self.assertFalse(card["market_validation"]["direction_confirmed"])
        self.assertIn(card["action_stage"], {"observe", "verify"})

    def test_stored_confirmation_is_rebound_to_current_relation_direction(
        self,
    ) -> None:
        stale_binding = {
            **_reaction("kol-a"),
            "expected_direction": "negative",
            "observed_direction": "negative",
            "direction_confirmed": True,
            "abnormal_return": -0.05,
        }

        card = decision_service.build_public_decisions(
            [_relation("kol-a", "positive")],
            [stale_binding],
            1.0,
            now=TEST_NOW,
        )["decisions"][0]

        self.assertTrue(card["market_validation"]["abstain"])
        self.assertFalse(card["market_validation"]["direction_confirmed"])
        self.assertFalse(
            card["market_validation"]["records"][0]["direction_confirmed"]
        )

    def test_neutral_complete_sample_keeps_market_validation_abstained(
        self,
    ) -> None:
        neutral = {
            **_reaction("kol-b"),
            "observed_direction": "neutral",
            "direction_confirmed": None,
            "abnormal_return": 0.0,
        }

        card = decision_service.build_public_decisions(
            [
                _relation("kol-a", "positive"),
                _relation("kol-b", "positive"),
            ],
            [_reaction("kol-a"), neutral],
            1.0,
            now=TEST_NOW,
        )["decisions"][0]

        self.assertTrue(card["market_validation"]["abstain"])
        self.assertIsNone(card["market_validation"]["direction_confirmed"])

    def test_missing_source_ids_do_not_inflate_or_cross_match(self) -> None:
        relations = [
            {**_relation("ignored-a", "positive"), "source_id": None},
            {**_relation("ignored-b", "positive"), "source_id": ""},
        ]
        reaction = {**_reaction("ignored"), "source_id": None}

        result = decision_service.build_public_decisions(
            relations, [reaction], 1.0, now=TEST_NOW
        )
        card = result["decisions"][0]

        self.assertEqual(card["source_count"], 0)
        self.assertEqual(card["score_components"]["corroboration"], 0.0)
        self.assertEqual(card["market_validation"]["sample_count"], 0)

    def test_future_relation_timestamp_is_excluded(self) -> None:
        relation = _relation(
            "future",
            "positive",
            evidence={
                "title": "future-dated",
                "published_at": "2026-09-01T00:00:00+00:00",
            },
        )

        result = decision_service.build_public_decisions(
            [relation],
            [],
            1.0,
            now="2026-08-01T00:00:00+00:00",
        )

        self.assertEqual(result["decisions"], [])
        self.assertEqual(result["impact_matrix"], {"columns": [], "rows": []})

    def test_stale_and_undated_event_relations_are_excluded(self) -> None:
        relations = [
            _relation(
                "recent",
                "positive",
                evidence={
                    "title": "recent evidence",
                    "published_at": "2026-08-02T12:00:00+00:00",
                },
            ),
            _relation(
                "stale",
                "positive",
                evidence={
                    "title": "stale evidence",
                    "published_at": "2026-07-01T12:00:00+00:00",
                },
            ),
            _relation(
                "undated",
                "positive",
                evidence={"title": "unverified evidence"},
            ),
        ]

        result = decision_service.build_public_decisions(
            relations,
            [],
            1.0,
            now="2026-08-03T00:00:00+00:00",
        )

        self.assertEqual(len(result["decisions"]), 1)
        self.assertEqual(
            [item["source_id"] for item in result["decisions"][0]["evidence"]],
            ["recent"],
        )

    def test_only_known_sources_and_nonfuture_event_evidence_are_allowed(
        self,
    ) -> None:
        relations = [
            {
                **_relation(
                    "case-bypass",
                    "positive",
                    evidence={"title": "missing event time"},
                ),
                "source_type": " Event ",
            },
            {
                **_relation("unknown-source", "positive"),
                "source_type": "other",
            },
            _relation(
                "near-future",
                "positive",
                evidence={
                    "title": "four minutes in the future",
                    "published_at": "2026-08-03T00:04:00+00:00",
                },
            ),
            _relation(
                "macro-safe",
                "positive",
                source_type="macro_snapshot",
                evidence={"name": "public macro scenario"},
            ),
        ]

        result = decision_service.build_public_decisions(
            relations,
            [],
            1.0,
            now="2026-08-03T00:00:00+00:00",
        )

        self.assertEqual(len(result["decisions"]), 1)
        self.assertEqual(
            [item["source_id"] for item in result["decisions"][0]["evidence"]],
            ["macro-safe"],
        )

    def test_outputs_complete_topic_asset_impact_matrix(self) -> None:
        relations = [
            _relation("a", "positive"),
            {
                **_relation("b", "negative"),
                "topic_key": "monetary_policy",
                "asset_key": "US:TLT",
            },
        ]

        result = decision_service.build_public_decisions(
            relations, [], 1.0, now=TEST_NOW
        )
        matrix = result["impact_matrix"]

        self.assertEqual(
            matrix["columns"],
            ["US:NVDA", "US:TLT"],
        )
        self.assertEqual(
            [row["topic_key"] for row in matrix["rows"]],
            ["ai_semiconductors", "monetary_policy"],
        )
        self.assertEqual(len(matrix["rows"][0]["cells"]), 2)
        self.assertIsNotNone(matrix["rows"][0]["cells"][0])
        self.assertIsNone(matrix["rows"][0]["cells"][1])

    def test_disclaimer_states_correlation_discovery_and_review_limits(self) -> None:
        result = decision_service.build_public_decisions(
            [_relation("a", "positive")], [], 1.0, now=TEST_NOW
        )
        policy = result["evidence_policy"]
        self.assertIn("相关", policy)
        self.assertIn("因果", policy)
        self.assertIn("KOL", policy)
        self.assertIn("发现", policy)
        self.assertIn("abstain", policy)

    def test_unversioned_macro_snapshot_drops_all_free_text(self) -> None:
        snapshot = {
            "timestamp": "2026-08-03T00:00:00+00:00",
            "composite_risk": {
                "score": 60,
                "level": "high",
                "label": "PRIVATE-LABEL",
            },
            "sub_scores": {
                "recession": {
                    "score": 55,
                    "level": "medium",
                    "interpretation": "PRIVATE-INTERPRETATION",
                    "signals": ["PRIVATE-SIGNAL NVDA"],
                }
            },
            "search_queries": ["PRIVATE-QUERY NVDA"],
            "black_swan_scenarios": [
                {
                    "id": "private",
                    "name": "PRIVATE-NAME",
                    "hedge": "私人账户持有 PRIVATE-NVDA",
                    "affected_assets": ["PRIVATE-NVDA"],
                }
            ],
            "gray_rhinos": [
                {"id": "private", "market_impact": "PRIVATE-IMPACT"}
            ],
            "opportunities": [
                {"id": "private", "signal": "PRIVATE-OPPORTUNITY"}
            ],
        }

        projected = decision_service.project_public_macro(snapshot)
        encoded = json.dumps(projected, ensure_ascii=False)

        self.assertEqual(
            projected["composite_risk"],
            {"score": 60, "level": "high"},
        )
        self.assertEqual(projected["search_queries"], [])
        self.assertEqual(projected["black_swan_scenarios"], [])
        self.assertEqual(projected["gray_rhinos"], [])
        self.assertEqual(projected["opportunities"], [])
        self.assertNotIn("PRIVATE-", encoded)
        self.assertNotIn("私人账户", encoded)

    def test_versioned_public_macro_snapshot_keeps_approved_text(self) -> None:
        projected = decision_service.project_public_macro(
            {
                "public_schema_version": 1,
                "timestamp": "2026-08-03T00:00:00+00:00",
                "sub_scores": {
                    "recession": {
                        "score": 30,
                        "signals": ["公开信号"],
                    }
                },
                "search_queries": ["公开查询"],
                "black_swan_scenarios": [
                    {
                        "id": "public",
                        "name": "公开场景",
                        "hedge": "公开对冲说明",
                    }
                ],
            }
        )

        self.assertEqual(projected["public_schema_version"], 1)
        self.assertEqual(
            projected["sub_scores"]["recession"]["signals"],
            ["公开信号"],
        )
        self.assertEqual(projected["search_queries"], ["公开查询"])
        self.assertEqual(
            projected["black_swan_scenarios"][0]["hedge"],
            "公开对冲说明",
        )


class SourceIngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(
            decision_service, "kol_dashboard.decision_service is required"
        )
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = mock.patch.object(
            db, "DB_PATH", str(Path(self.tmp.name) / "ingest.sqlite3")
        )
        self.db_patch.start()
        db.init()

    def tearDown(self) -> None:
        self.db_patch.stop()
        self.tmp.cleanup()

    def test_ingest_generates_and_optionally_persists_relations(self) -> None:
        events = [
            {
                "id": 7,
                "title": "Bullish on NVIDIA AI demand",
                "snippet": "buy $NVDA",
                "tickers": ["NVDA"],
                "impact": "medium",
                "published_at": "2026-08-02T12:00:00+00:00",
            }
        ]
        macro = {
            "snapshot_id": 3,
            "opportunities": [
                {
                    "id": "gold",
                    "name": "黄金回调",
                    "asset": "GLD",
                    "confidence": "medium",
                }
            ],
        }
        original_events = deepcopy(events)
        original_macro = deepcopy(macro)

        relations = decision_service.ingest_sources(
            events,
            macro,
            persist=True,
            repository=db,
            now="2026-08-03T00:00:00+00:00",
        )

        self.assertGreaterEqual(len(relations), 2)
        self.assertEqual(
            len(db.query_relations()),
            len(relations),
        )
        self.assertEqual(events, original_events)
        self.assertEqual(macro, original_macro)
        self.assertNotIn(
            "portfolio", json.dumps(relations, ensure_ascii=False).lower()
        )

    def test_ingest_skips_stale_undated_and_future_events(self) -> None:
        def event(event_id: str, published_at: str | None) -> dict:
            return {
                "id": event_id,
                "title": "Bullish on NVIDIA AI demand",
                "snippet": "buy $NVDA",
                "tickers": ["NVDA"],
                "impact": "medium",
                "published_at": published_at,
            }

        relations = decision_service.ingest_sources(
            [
                event("recent", "2026-08-02T12:00:00+00:00"),
                event("stale", "2026-07-01T12:00:00+00:00"),
                event("undated", None),
                event("future", "2026-08-03T02:00:00+00:00"),
            ],
            now="2026-08-03T00:00:00+00:00",
        )

        self.assertEqual(
            {relation["source_id"] for relation in relations},
            {"recent"},
        )


class PrivateOverlayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(
            decision_service, "kol_dashboard.decision_service is required"
        )

    def test_private_overlay_marks_stale_leverage_and_does_not_mutate_inputs(self) -> None:
        public = decision_service.build_public_decisions(
            [
                {
                    **_relation("lev", "positive"),
                    "asset_key": "US:NVDL",
                }
            ],
            [],
            1.0,
            now=TEST_NOW,
        )
        positions = [
            {
                "account": "Robinhood",
                "asset_key": "US:NVDL",
                "symbol": "NVDL",
                "name": "NVIDIA 2x Long",
                "quantity": 4.0,
                "avg_cost": 82.47,
                "currency": "USD",
                "asset_class": "etf",
                "is_leveraged": True,
                "as_of": "2026-06-01",
            }
        ]
        quotes = {}
        original_public = deepcopy(public)
        original_positions = deepcopy(positions)
        original_quotes = deepcopy(quotes)

        private = decision_service.build_private_overlay(
            public,
            positions,
            quotes,
            now="2026-08-01T00:00:00+00:00",
        )

        card = private["decisions"][0]
        self.assertEqual(len(card["matched_positions"]), 1)
        self.assertIsNone(card["estimated_exposure"])
        self.assertTrue(card["stale"])
        self.assertTrue(card["leverage_flag"])
        self.assertLess(
            card["score_components"]["confidence"],
            public["decisions"][0]["score_components"]["confidence"],
        )
        self.assertEqual(public, original_public)
        self.assertEqual(positions, original_positions)
        self.assertEqual(quotes, original_quotes)

    def test_private_overlay_estimates_exposure_from_fresh_quote(self) -> None:
        public = decision_service.build_public_decisions(
            [_relation("fresh", "positive")], [], 1.0, now=TEST_NOW
        )
        positions = [
            {
                "account": "Schwab",
                "asset_key": "US:NVDA",
                "symbol": "NVDA",
                "name": "NVIDIA",
                "quantity": 10.0,
                "avg_cost": 100.0,
                "currency": "USD",
                "asset_class": "stock",
                "is_leveraged": False,
                "as_of": "2026-07-31",
            }
        ]
        quotes = {
            "US:NVDA": {
                "price": 120.0,
                "currency": "USD",
                "observed_at": "2026-08-01T00:00:00+00:00",
            }
        }

        private = decision_service.build_private_overlay(
            public,
            positions,
            quotes,
            now="2026-08-01T00:05:00+00:00",
        )
        card = private["decisions"][0]

        self.assertEqual(card["estimated_exposure"]["value"], 1200.0)
        self.assertEqual(card["estimated_exposure"]["currency"], "USD")
        self.assertFalse(card["stale"])
        self.assertFalse(card["leverage_flag"])

    def test_private_overlay_recomputes_action_after_stale_confidence_drop(self) -> None:
        public = {
            "decisions": [
                {
                    "topic_key": "ai_semiconductors",
                    "asset_key": "US:NVDL",
                    "classification": "opportunity",
                    "direction": "positive",
                    "horizon": "medium",
                    "evidence": [],
                    "source_count": 2,
                    "mechanism_relations": [],
                    "market_validation": {
                        "status": "complete",
                        "abstain": False,
                        "direction_confirmed": True,
                    },
                    "score_components": {
                        "strength": 0.5,
                        "confidence": 0.8,
                        "freshness": 0.6,
                        "corroboration": 0.65,
                        "market_confirmation": 1.0,
                        "coverage": 0.6,
                    },
                    "total_score": 0.7075,
                    "action_stage": "scale_in",
                    "trigger": "verify",
                    "invalidation": "reverse",
                    "contrary_evidence": [],
                    "human_review_required": True,
                }
            ],
            "impact_matrix": {},
            "evidence_policy": "人工复核",
        }
        positions = [
            {
                "account": "Robinhood",
                "asset_key": "US:NVDL",
                "symbol": "NVDL",
                "name": "NVIDIA 2x Long",
                "quantity": 4,
                "avg_cost": 80,
                "currency": "USD",
                "asset_class": "etf",
                "is_leveraged": True,
                "as_of": "2026-06-01",
            }
        ]

        private = decision_service.build_private_overlay(
            public,
            positions,
            {},
            now="2026-08-01T00:00:00+00:00",
        )

        self.assertLess(private["decisions"][0]["total_score"], 0.7)
        self.assertIn(
            private["decisions"][0]["action_stage"], {"observe", "verify"}
        )

    def test_future_position_date_is_stale(self) -> None:
        public = decision_service.build_public_decisions(
            [_relation("future-position", "positive")],
            [],
            1.0,
            now=TEST_NOW,
        )
        positions = [
            {
                "account": "Schwab",
                "asset_key": "US:NVDA",
                "symbol": "NVDA",
                "name": "NVIDIA",
                "quantity": 1,
                "avg_cost": 100,
                "currency": "USD",
                "asset_class": "stock",
                "is_leveraged": False,
                "as_of": "2026-09-01",
            }
        ]

        private = decision_service.build_private_overlay(
            public,
            positions,
            {},
            now="2026-08-01T00:00:00+00:00",
        )

        self.assertTrue(private["decisions"][0]["stale"])


if __name__ == "__main__":
    unittest.main()
