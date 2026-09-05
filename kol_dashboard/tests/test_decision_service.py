from __future__ import annotations

import importlib
import json
import tempfile
import unittest
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
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
    topic_key: str = "ai_semiconductors",
    asset_key: str = "US:NVDA",
    strength: float = 0.8,
    confidence: float = 0.8,
    horizon: str = "medium",
    evidence: dict | None = None,
) -> dict:
    return {
        "source_type": source_type,
        "source_id": source_id,
        "topic_key": topic_key,
        "asset_key": asset_key,
        "relation_type": "view" if source_type == "event" else "opportunity",
        "direction": direction,
        "strength": strength,
        "confidence": confidence,
        "horizon": horizon,
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
    asset_key: str = "US:NVDA",
    direction_confirmed: bool = True,
    status: str = "complete",
    window: str = "3D",
    sample_count: int | None = None,
    reason_code: str | None = None,
    next_due_at: str | None = None,
) -> dict:
    reaction = {
        "source_type": "event",
        "source_id": source_id,
        "asset_key": asset_key,
        "window": window,
        "benchmark_asset_key": "US:SOXX",
        "asset_return": 0.12 if direction_confirmed else -0.02,
        "benchmark_return": 0.03,
        "abnormal_return": 0.09 if direction_confirmed else -0.05,
        "expected_direction": "positive",
        "observed_direction": "positive" if direction_confirmed else "negative",
        "direction_confirmed": direction_confirmed,
        "status": status,
        "sample_count": (
            sample_count
            if sample_count is not None
            else {"1D": 2, "3D": 4, "5D": 6}.get(window, 0)
        ),
        "data_timestamps": {
            "start": "2026-07-30T00:00:00+00:00",
            "end": "2026-08-01T00:00:00+00:00",
        },
        "method_version": "common_trading_days:test",
        "observed_at": "2026-08-01T00:00:00+00:00",
    }
    if reason_code is not None:
        reaction["reason_code"] = reason_code
    if next_due_at is not None:
        reaction["next_due_at"] = next_due_at
    return reaction


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
        self.assertEqual(card["data_as_of"], "2026-07-31T12:00:00+00:00")
        self.assertEqual(
            card["market_validation"]["applicability_reason"],
            "direction_missing",
        )
        self.assertEqual(card["market_validation"]["records"], [])
        self.assertIsNone(card["market_validation"]["next_review_at"])
        self.assertEqual(card["confidence"], card["score_components"]["confidence"])
        self.assertTrue(card["human_review_required"])
        self.assertIn(
            card["action_stage"],
            {
                "observe",
                "verify",
                "candidate_reduce_or_hedge",
                "candidate_scale_in",
            },
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

    def test_cost_ticker_and_public_cost_text_survive_without_private_fields(
        self,
    ) -> None:
        relation = _relation(
            "costco-event",
            "positive",
            asset_key="US:COST",
            evidence={
                "title": "$COST beats estimates while freight cost falls",
                "item_id": "costco-earnings",
                "matched_asset": "US:COST",
                "avg_cost": 123.45,
                "published_at": "2026-07-31T10:00:00+00:00",
            },
        )
        reaction = _reaction(
            "costco-event",
            asset_key="US:COST",
            window="3D",
        )

        public = decision_service.build_public_decisions(
            [relation],
            [reaction],
            1.0,
            now=TEST_NOW,
        )
        card = public["decisions"][0]
        detail = card["evidence"][0]["detail"]
        self.assertEqual(card["asset_key"], "US:COST")
        self.assertEqual(card["evidence"][0]["source_id"], "costco-event")
        self.assertEqual(
            detail["title"],
            "$COST beats estimates while freight cost falls",
        )
        self.assertEqual(detail["matched_asset"], "US:COST")
        self.assertNotIn("avg_cost", json.dumps(public, ensure_ascii=False))

        private = decision_service.build_private_overlay(
            public,
            [
                {
                    "account": "broker",
                    "asset_key": "US:COST",
                    "quantity": 2,
                    "avg_cost": 800,
                    "currency": "USD",
                    "as_of": "2026-08-04",
                }
            ],
            {},
            now=TEST_NOW,
        )
        self.assertEqual(private["portfolio_overview"]["matched_position_count"], 1)
        self.assertEqual(private["portfolio_overview"]["unmatched_position_count"], 0)

    def test_public_opaque_identifiers_fail_closed_on_private_vocabulary(
        self,
    ) -> None:
        relation = _relation(
            "account_123-robinhood",
            "positive",
            evidence={
                "title": "Public market update",
                "item_id": "portfolio_456-secret",
                "url": (
                    "https://example.com/update?account_123=secret-broker"
                    "&portfolio_456=retirement"
                ),
                "published_at": "2026-07-31T10:00:00+00:00",
            },
        )

        result = decision_service.build_public_decisions(
            [relation],
            [],
            1.0,
            now=TEST_NOW,
        )
        encoded = json.dumps(result, ensure_ascii=False).lower()
        for private_value in (
            "account_123-robinhood",
            "portfolio_456-secret",
            "secret-broker",
            "retirement",
        ):
            self.assertNotIn(private_value, encoded)
        self.assertIn("[redacted]", encoded)

        cost_relation = _relation(
            "cost_123-robinhood",
            "positive",
            evidence={
                "title": "$COST logistics costs improved",
                "item_id": "avg_cost_123.45",
                "url": "https://example.com/update?avg_cost=123.45",
                "published_at": "2026-07-31T10:00:00+00:00",
            },
        )
        cost_result = decision_service.build_public_decisions(
            [cost_relation],
            [],
            1.0,
            now=TEST_NOW,
        )
        cost_encoded = json.dumps(cost_result, ensure_ascii=False).lower()
        for private_value in (
            "cost_123-robinhood",
            "avg_cost_123.45",
            "avg_cost=123.45",
        ):
            self.assertNotIn(private_value, cost_encoded)
        self.assertIn("$cost logistics costs improved", cost_encoded)

    def test_public_projection_drops_private_topic_and_asset_identities(
        self,
    ) -> None:
        relation = _relation(
            "public-source",
            "positive",
            asset_key="US:ACCOUNT-SECRET",
        )
        relation["topic_key"] = "account-retirement"
        reaction = _reaction(
            "public-source",
            asset_key="US:ACCOUNT-SECRET",
            window="1D",
        )

        self.assertEqual(decision_service.project_public_relations([relation]), [])
        self.assertEqual(decision_service.project_public_reactions([reaction]), [])
        public = decision_service.build_public_decisions(
            [relation],
            [reaction],
            1.0,
            now=TEST_NOW,
        )
        encoded = json.dumps(public, ensure_ascii=False).lower()
        self.assertEqual(public["decisions"], [])
        self.assertNotIn("account-retirement", encoded)
        self.assertNotIn("us:account-secret", encoded)

        cost_relation = _relation(
            "public-source",
            "positive",
            asset_key="US:AVG_COST_123",
        )
        cost_relation["topic_key"] = "avg_cost_123"
        self.assertEqual(
            decision_service.project_public_relations([cost_relation]),
            [],
        )

    def test_redacted_source_ids_remain_distinct_for_market_matching(self) -> None:
        relation = _relation(
            "account-alpha",
            "positive",
            strength=1.0,
            confidence=1.0,
            horizon="short",
        )
        reaction = _reaction(
            "portfolio-beta",
            window="1D",
        )

        result = decision_service.build_public_decisions(
            [relation],
            [reaction],
            1.0,
            now=TEST_NOW,
        )

        card = result["decisions"][0]
        market = card["market_validation"]
        self.assertNotEqual(
            card["evidence"][0]["source_id"],
            decision_service.project_public_reactions([reaction])[0]["source_id"],
        )
        self.assertEqual(market["status"], "unavailable")
        self.assertEqual(market["records"], [])
        self.assertTrue(market["abstain"])
        self.assertTrue(market["veto"])
        self.assertNotIn("candidate", card["action_stage"])

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

    def test_recent_long_horizon_event_remains_reachable_while_5d_is_pending(
        self,
    ) -> None:
        relation = _relation(
            "recent-long",
            "positive",
            horizon="long",
            evidence={
                "title": "recent long-horizon evidence",
                "published_at": "2026-07-29T01:00:00+00:00",
            },
        )
        reactions = [
            _reaction("recent-long", window="1D"),
            _reaction(
                "recent-long",
                window="3D",
                status="pending",
                sample_count=2,
                reason_code="window_not_due",
                next_due_at="2026-08-03T00:00:00+00:00",
            ),
            _reaction(
                "recent-long",
                window="5D",
                status="pending",
                sample_count=2,
                reason_code="window_not_due",
                next_due_at="2026-08-05T00:00:00+00:00",
            ),
        ]

        result = decision_service.build_public_decisions(
            [relation],
            reactions,
            1.0,
            now=TEST_NOW,
        )

        self.assertEqual(len(result["decisions"]), 1)
        card = result["decisions"][0]
        market = card["market_validation"]
        self.assertEqual(market["required_window"], "5D")
        self.assertEqual(market["status"], "preliminary")
        self.assertEqual(market["phase"], "confirmed_1d")
        self.assertEqual(market["completed_windows"], ["1D"])
        self.assertEqual(market["pending_windows"], ["3D", "5D"])
        self.assertFalse(market["veto"])
        self.assertTrue(market["abstain"])
        self.assertFalse(market["required_window_complete"])
        self.assertTrue(market["direction_confirmed"])
        self.assertEqual(
            result["decisions"][0]["score_components"]["market_confirmation"],
            0.55,
        )
        self.assertIn(
            result["decisions"][0]["action_stage"],
            {"verify", "observe"},
        )
        self.assertEqual(result["decision_overview"]["market_confirmed"], 0)
        self.assertEqual(result["decision_overview"]["pending_window"], 1)

    def test_human_review_actions_are_explicit_candidates(self) -> None:
        card = decision_service.build_public_decisions(
            [
                _relation(
                    "candidate-a",
                    "positive",
                    strength=1.0,
                    confidence=1.0,
                )
            ],
            [_reaction("candidate-a")],
            1.0,
            now=TEST_NOW,
        )["decisions"][0]

        self.assertTrue(card["human_review_required"])
        self.assertEqual(card["decision_status"], "candidate")
        self.assertEqual(card["action_stage"], "candidate_scale_in")
        self.assertNotIn(
            card["action_stage"],
            {"scale_in", "reduce_or_hedge"},
        )

    def test_market_business_health_aggregates_only_bounded_reason_codes(
        self,
    ) -> None:
        failed = {
            **_reaction("health-a", status="unavailable"),
            "reason_code": "invalid_range",
            "reason": "request_failed:SecretProviderException(private-token)",
            "provider": "yahoo",
            "provider_symbol": "NVDA",
            "asset_status": "unavailable",
        }

        result = decision_service.build_public_decisions(
            [_relation("health-a", "positive")],
            [failed],
            1.0,
            now=TEST_NOW,
        )

        health = result["business_health"]["market_validation"]
        self.assertEqual(health["status"], "unavailable")
        self.assertTrue(health["degraded"])
        self.assertEqual(health["reason_counts"], {"invalid_range": 1})
        encoded = json.dumps(result, ensure_ascii=False)
        self.assertIn('"reason_code": "invalid_range"', encoded)
        self.assertNotIn("SecretProviderException", encoded)
        self.assertNotIn("private-token", encoded)

    def test_unrelated_old_unavailable_reactions_do_not_degrade_current_health(
        self,
    ) -> None:
        old_unrelated = {
            **_reaction("old-unrelated", status="unavailable"),
            "reason_code": "asset_unavailable",
            "observed_at": "2026-07-18T00:00:00+00:00",
        }

        result = decision_service.build_public_decisions(
            [_relation("current", "positive")],
            [_reaction("current"), old_unrelated],
            1.0,
            now=TEST_NOW,
        )

        health = result["business_health"]["market_validation"]
        self.assertEqual(health["status"], "healthy")
        self.assertFalse(health["degraded"])
        self.assertEqual(health["total_records"], 1)
        self.assertEqual(health["complete_records"], 1)
        self.assertEqual(health["unavailable_records"], 0)
        self.assertEqual(health["reason_counts"], {})

    def test_partial_current_decision_coverage_cannot_report_healthy(self) -> None:
        missing_relation = {
            **_relation("missing-current", "negative"),
            "topic_key": "rates",
            "asset_key": "US:TLT",
        }

        result = decision_service.build_public_decisions(
            [_relation("covered-current", "positive"), missing_relation],
            [_reaction("covered-current")],
            1.0,
            now=TEST_NOW,
        )

        health = result["business_health"]["market_validation"]
        self.assertEqual(health["status"], "degraded")
        self.assertTrue(health["degraded"])
        self.assertEqual(health["total_decisions"], 2)
        self.assertEqual(health["covered_decisions"], 1)
        self.assertEqual(health["pending_decisions"], 0)
        self.assertEqual(health["unavailable_decisions"], 1)
        self.assertEqual(health["reason_counts"], {})

    def test_pending_current_decision_is_not_a_business_health_failure(
        self,
    ) -> None:
        pending_relation = {
            **_relation("pending-current", "negative"),
            "topic_key": "rates",
            "asset_key": "US:TLT",
        }
        pending_reaction = {
            **_reaction(
                "pending-current",
                status="pending",
                reason_code="window_not_due",
                next_due_at="2026-08-03T00:00:00+00:00",
            ),
            "asset_key": "US:TLT",
        }

        result = decision_service.build_public_decisions(
            [_relation("covered-current", "positive"), pending_relation],
            [_reaction("covered-current"), pending_reaction],
            1.0,
            now=TEST_NOW,
        )

        health = result["business_health"]["market_validation"]
        self.assertEqual(health["status"], "healthy")
        self.assertFalse(health["degraded"])
        self.assertEqual(health["covered_decisions"], 1)
        self.assertEqual(health["pending_decisions"], 1)
        self.assertEqual(health["unavailable_decisions"], 0)

    def test_macro_only_decision_uses_no_event_anchor_semantics(self) -> None:
        result = decision_service.build_public_decisions(
            [
                _relation(
                    "macro-only",
                    "positive",
                    source_type="macro_snapshot",
                )
            ],
            [],
            1.0,
            now=TEST_NOW,
        )

        card = result["decisions"][0]
        market = card["market_validation"]
        self.assertEqual(market["applicability"], "not_applicable")
        self.assertEqual(market["applicability_reason"], "no_event_anchor")
        self.assertEqual(market["source_scope"], "macro_only")
        self.assertTrue(market["abstain"])
        self.assertTrue(market["veto"])
        self.assertFalse(market["degraded"])
        self.assertIsNone(market["next_review_at"])
        self.assertEqual(market["reason_counts"], {"no_event_anchor": 1})
        self.assertIn(card["action_stage"], {"observe", "verify"})

        health = result["business_health"]["market_validation"]
        self.assertEqual(health["status"], "not_applicable")
        self.assertFalse(health["degraded"])
        self.assertEqual(health["applicable_decisions"], 0)
        self.assertEqual(health["no_event_anchor_decisions"], 1)
        self.assertEqual(health["data_failure_decisions"], 0)
        self.assertEqual(result["decision_overview"]["scenario_monitoring"], 1)
        self.assertEqual(result["decision_overview"]["data_unavailable"], 0)

    def test_event_without_direction_is_not_reported_as_data_failure(self) -> None:
        result = decision_service.build_public_decisions(
            [_relation("directionless", "neutral")],
            [_reaction("directionless")],
            1.0,
            now=TEST_NOW,
        )

        card = result["decisions"][0]
        market = card["market_validation"]
        self.assertEqual(market["applicability"], "not_applicable")
        self.assertEqual(market["applicability_reason"], "direction_missing")
        self.assertEqual(market["source_scope"], "event_only")
        self.assertEqual(market["phase"], "direction_unavailable")
        self.assertTrue(market["abstain"])
        self.assertTrue(market["veto"])
        self.assertFalse(market["degraded"])
        self.assertIn("事件预期尚未明确", market["note"])
        self.assertNotIn("机制方向", market["note"])
        self.assertIn(card["action_stage"], {"observe", "verify"})

        health = result["business_health"]["market_validation"]
        self.assertEqual(health["status"], "not_applicable")
        self.assertFalse(health["degraded"])
        self.assertEqual(health["direction_missing_decisions"], 1)
        self.assertEqual(health["data_failure_decisions"], 0)
        self.assertEqual(result["decision_overview"]["direction_missing"], 1)
        self.assertEqual(result["decision_overview"]["data_unavailable"], 0)

    def test_macro_direction_cannot_borrow_a_neutral_event_market_anchor(
        self,
    ) -> None:
        relations = [
            _relation("neutral-event", "neutral"),
            _relation(
                "positive-macro",
                "positive",
                source_type="macro_snapshot",
            ),
        ]
        reactions = [_reaction("neutral-event", window="1D")]

        result = decision_service.build_public_decisions(
            relations,
            reactions,
            1.0,
            now=TEST_NOW,
        )

        card = result["decisions"][0]
        market = card["market_validation"]
        self.assertEqual(card["classification"], "opportunity")
        self.assertEqual(market["applicability"], "not_applicable")
        self.assertEqual(market["applicability_reason"], "direction_missing")
        self.assertEqual(market["source_scope"], "mixed")
        self.assertEqual(market["records"], [])
        self.assertIsNone(market["direction_confirmed"])
        self.assertFalse(market["required_window_complete"])
        self.assertTrue(market["abstain"])
        self.assertTrue(market["veto"])
        self.assertNotIn("candidate", card["action_stage"])
        self.assertEqual(result["decision_overview"]["market_confirmed"], 0)

    def test_direction_missing_discards_pending_market_schedule(self) -> None:
        pending = _reaction(
            "neutral-event",
            status="pending",
            window="1D",
            sample_count=0,
            reason_code="window_not_due",
            next_due_at="2026-08-10T12:00:00+00:00",
        )
        result = decision_service.build_public_decisions(
            [
                _relation("neutral-event", "neutral"),
                _relation(
                    "positive-macro",
                    "positive",
                    source_type="macro_snapshot",
                ),
            ],
            [pending],
            1.0,
            now=TEST_NOW,
        )

        market = result["decisions"][0]["market_validation"]
        self.assertEqual(market["applicability_reason"], "direction_missing")
        self.assertEqual(market["pending_windows"], [])
        self.assertIsNone(market["next_review_at"])
        self.assertEqual(result["decision_overview"]["pending_window"], 0)

    def test_applicable_event_data_failure_remains_degraded(self) -> None:
        failed = _reaction(
            "data-failure",
            status="unavailable",
            window="1D",
            sample_count=0,
            reason_code="request_failed",
        )

        result = decision_service.build_public_decisions(
            [
                _relation(
                    "data-failure",
                    "positive",
                    horizon="short",
                )
            ],
            [failed],
            1.0,
            now=TEST_NOW,
        )

        card = result["decisions"][0]
        market = card["market_validation"]
        self.assertEqual(market["applicability"], "applicable")
        self.assertIsNone(market["applicability_reason"])
        self.assertEqual(market["source_scope"], "event_only")
        self.assertEqual(market["status"], "unavailable")
        self.assertTrue(market["degraded"])
        self.assertEqual(market["reason_counts"], {"request_failed": 1})
        self.assertIn(card["action_stage"], {"observe", "verify"})

        health = result["business_health"]["market_validation"]
        self.assertEqual(health["status"], "unavailable")
        self.assertTrue(health["degraded"])
        self.assertEqual(health["applicable_decisions"], 1)
        self.assertEqual(health["data_failure_decisions"], 1)
        self.assertEqual(health["not_applicable_decisions"], 0)
        self.assertEqual(result["decision_overview"]["data_unavailable"], 1)

    def test_mixed_source_scope_preserves_earliest_next_review(self) -> None:
        relations = [
            _relation("mixed-event", "positive"),
            _relation(
                "mixed-macro",
                "positive",
                source_type="macro_snapshot",
            ),
        ]
        reactions = [
            _reaction(
                "mixed-event",
                status="pending",
                window="1D",
                sample_count=0,
                reason_code="window_not_due",
                next_due_at="2026-08-01T12:00:00+00:00",
            ),
            _reaction(
                "mixed-event",
                status="pending",
                window="3D",
                sample_count=2,
                reason_code="window_not_due",
                next_due_at="2026-08-03T12:00:00+00:00",
            ),
        ]

        result = decision_service.build_public_decisions(
            relations,
            reactions,
            1.0,
            now=TEST_NOW,
        )

        market = result["decisions"][0]["market_validation"]
        self.assertEqual(market["applicability"], "applicable")
        self.assertIsNone(market["applicability_reason"])
        self.assertEqual(market["source_scope"], "mixed")
        self.assertEqual(market["status"], "pending")
        self.assertEqual(market["pending_windows"], ["1D", "3D"])
        self.assertEqual(
            market["next_review_at"],
            "2026-08-01T12:00:00+00:00",
        )

        summary = decision_service.project_decision_summary(result)
        summary_market = summary["decisions"][0]["market_validation"]
        self.assertEqual(summary_market["source_scope"], "mixed")
        self.assertEqual(
            summary_market["next_review_at"],
            "2026-08-01T12:00:00+00:00",
        )
        self.assertNotIn("records", summary_market)

    def test_decision_overview_separates_each_market_semantic(self) -> None:
        relations = [
            _relation(
                "confirmed",
                "positive",
                topic_key="confirmed",
                asset_key="US:CONF",
                strength=1.0,
                confidence=1.0,
                horizon="short",
            ),
            _relation(
                "contrary",
                "positive",
                topic_key="contrary",
                asset_key="US:CONT",
                horizon="short",
            ),
            _relation(
                "pending",
                "positive",
                topic_key="pending",
                asset_key="US:PEND",
                horizon="short",
            ),
            _relation(
                "scenario",
                "positive",
                source_type="macro_snapshot",
                topic_key="scenario",
                asset_key="US:SCEN",
                horizon="short",
            ),
            _relation(
                "direction",
                "neutral",
                topic_key="direction",
                asset_key="US:DIR",
                horizon="short",
            ),
            _relation(
                "failure",
                "positive",
                topic_key="failure",
                asset_key="US:FAIL",
                horizon="short",
            ),
        ]
        reactions = [
            _reaction(
                "confirmed",
                asset_key="US:CONF",
                window="1D",
            ),
            _reaction(
                "contrary",
                asset_key="US:CONT",
                direction_confirmed=False,
                window="1D",
            ),
            _reaction(
                "pending",
                asset_key="US:PEND",
                status="pending",
                window="1D",
                sample_count=0,
                reason_code="window_not_due",
                next_due_at="2026-08-02T00:00:00+00:00",
            ),
            _reaction(
                "failure",
                asset_key="US:FAIL",
                status="unavailable",
                window="1D",
                sample_count=0,
                reason_code="provider_error",
            ),
        ]

        result = decision_service.build_public_decisions(
            relations,
            reactions,
            1.0,
            now=TEST_NOW,
        )

        self.assertEqual(
            result["decision_overview"],
            {
                "total": 6,
                "candidate": 1,
                "market_confirmed": 1,
                "contrary": 1,
                "pending_window": 1,
                "scenario_monitoring": 1,
                "direction_missing": 1,
                "data_unavailable": 1,
            },
        )
        summary = decision_service.project_decision_summary(
            result,
            decision_limit=10,
        )
        self.assertEqual(summary["decision_overview"], result["decision_overview"])
        self.assertNotIn("portfolio_matched", summary["decision_overview"])

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
        self.assertEqual(
            card["market_validation"]["note"],
            "已有共同交易日样本未支持事件预期，候选必须停止并复核。",
        )
        self.assertIn(
            "完整观察窗口内，资产相对基准的表现未支持事件预期。",
            [item["detail"] for item in card["contrary_evidence"]],
        )
        self.assertNotIn(
            "机制方向",
            " ".join(item["detail"] for item in card["contrary_evidence"]),
        )
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

    def test_event_retention_is_horizon_aware(self) -> None:
        def relation(
            source_id: str,
            horizon: str,
            published_at: str,
            asset_key: str,
        ) -> dict:
            return {
                **_relation(
                    source_id,
                    "positive",
                    horizon=horizon,
                    evidence={
                        "title": source_id,
                        "published_at": published_at,
                    },
                ),
                "topic_key": source_id,
                "asset_key": asset_key,
            }

        result = decision_service.build_public_decisions(
            [
                relation(
                    "short-expired",
                    "short",
                    "2026-08-03T00:00:00+00:00",
                    "US:S1",
                ),
                relation(
                    "medium-current",
                    "medium",
                    "2026-08-02T00:00:00+00:00",
                    "US:M1",
                ),
                relation(
                    "medium-expired",
                    "medium",
                    "2026-07-30T00:00:00+00:00",
                    "US:M2",
                ),
                relation(
                    "long-current",
                    "long",
                    "2026-07-26T00:00:00+00:00",
                    "US:L1",
                ),
                relation(
                    "long-expired",
                    "long",
                    "2026-07-24T00:00:00+00:00",
                    "US:L2",
                ),
            ],
            [],
            1.0,
            now="2026-08-08T00:00:00+00:00",
        )

        self.assertEqual(
            {card["topic_key"] for card in result["decisions"]},
            {"medium-current", "long-current"},
        )

    def test_friday_long_event_survives_until_5d_window_completes(self) -> None:
        relation = _relation(
            "friday-long",
            "positive",
            horizon="long",
            strength=1.0,
            confidence=1.0,
            evidence={
                "title": "Friday long-horizon event",
                "published_at": "2026-07-24T20:00:00+00:00",
            },
        )
        before = decision_service.build_public_decisions(
            [relation],
            [
                _reaction("friday-long", window="1D"),
                _reaction("friday-long", window="3D"),
                _reaction(
                    "friday-long",
                    window="5D",
                    status="pending",
                    sample_count=4,
                    reason_code="window_not_due",
                    next_due_at="2026-07-31T20:00:00+00:00",
                ),
            ],
            1.0,
            now="2026-07-29T20:00:00+00:00",
        )
        after = decision_service.build_public_decisions(
            [relation],
            [
                _reaction("friday-long", window="1D"),
                _reaction("friday-long", window="3D"),
                _reaction("friday-long", window="5D"),
            ],
            1.0,
            now="2026-08-06T20:00:00+00:00",
        )

        self.assertEqual(len(before["decisions"]), 1)
        before_card = before["decisions"][0]
        self.assertTrue(before_card["market_validation"]["abstain"])
        self.assertFalse(before_card["market_validation"]["veto"])
        self.assertEqual(
            before_card["market_validation"]["phase"],
            "confirmed_3d",
        )
        self.assertIn(before_card["action_stage"], {"verify", "observe"})

        self.assertEqual(len(after["decisions"]), 1)
        after_card = after["decisions"][0]
        self.assertTrue(
            after_card["market_validation"]["required_window_complete"]
        )
        self.assertFalse(after_card["market_validation"]["abstain"])
        self.assertEqual(after_card["market_validation"]["phase"], "confirmed_5d")
        self.assertEqual(after_card["action_stage"], "candidate_scale_in")

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
        self.assertIn("不能用来否定事件预期", policy)
        self.assertNotIn("反向证据", policy)

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

    def test_public_macro_projects_alerts_through_a_strict_fail_closed_schema(self) -> None:
        projected = decision_service.project_public_macro(
            {
                "public_schema_version": 1,
                "market_data": {
                    "alert_inputs": {
                        "us_equity": {
                            "status": "available",
                            "data_status": "ok",
                            "stale": False,
                            "asset_key": "US:SPY",
                            "source": "Yahoo Finance",
                            "source_url": "https://finance.yahoo.com/quote/SPY/history/",
                            "observed_at": "2026-09-05T20:00:00Z",
                            "market_date": "2026-09-05",
                            "close": 650.0,
                            "sma20": 655.0,
                            "bars": [{"close": 650.0}],
                            "account": "private-account",
                        },
                        "cn_equity": {
                            "status": "available",
                            "data_status": "ok",
                            "stale": False,
                            "asset_key": "INDEX:CSI300",
                            "provider": "tencent",
                            "source": "腾讯行情",
                            "source_url": "https://gu.qq.com/sh000300",
                            "observed_at": "2026-09-05T07:00:00Z",
                            "market_date": "2026-09-05",
                            "close": 4548.05,
                        }
                    }
                },
                "market_alerts": {
                    "schema_version": 1,
                    "method_version": "macro-de-risk-trial-v1",
                    "generated_at": "2026-09-06T01:00:00Z",
                    "mode": "trial",
                    "human_review_required": True,
                    "automatic_execution": False,
                    "markets": [
                        {
                            "market": "US",
                            "label": "forged label",
                            "action": "reduce_candidate",
                            "action_label": "立即清仓",
                            "risk_level": "high",
                            "abstain": False,
                            "data_status": "ok",
                            "data_as_of": "2026-09-04T00:00:00Z",
                            "next_evaluation_at": "2026-09-06T02:00:00Z",
                            "summary": "趋势与压力共振",
                            "gates": [
                                {
                                    "key": "price_confirmation",
                                    "label": "价格确认",
                                    "status": "met",
                                    "detail": "完成日线",
                                    "shares": 999,
                                },
                                {
                                    "key": "unknown",
                                    "label": "unknown",
                                    "status": "forged",
                                },
                            ],
                            "triggered_signals": [
                                {
                                    "key": "vix_strong",
                                    "pillar": "volatility",
                                    "label": "VIX 抬升",
                                    "severity": "strong",
                                    "value": 30.0,
                                    "unit": "index",
                                    "threshold": "VIX >= 28",
                                    "observed_at": "2026-09-05T20:00:00Z",
                                    "time_basis": "completed_market_close",
                                    "source": "Yahoo Finance",
                                    "source_url": "https://finance.yahoo.com/quote/%5EVIX/history/",
                                    "detail": "public evidence",
                                    "portfolio": "must-not-leak",
                                },
                                {
                                    "key": "evil",
                                    "pillar": "made_up",
                                    "label": "forged",
                                    "severity": "strong",
                                },
                            ],
                            "counter_signals": [],
                            "upgrade_conditions": ["等待下一收盘"],
                            "invalidation_conditions": ["连续两日解除"],
                            "missing_sources": [],
                            "rule_version": "forged",
                            "confirmation": {"reduce_dates": ["private"]},
                            "account": "must-not-leak",
                        }
                    ],
                },
            }
        )

        alert_input = projected["market_data"]["alert_inputs"]["us_equity"]
        self.assertNotIn("bars", alert_input)
        self.assertEqual(
            projected["market_data"]["alert_inputs"]["cn_equity"]["source_url"],
            "https://gu.qq.com/sh000300",
        )
        alert = projected["market_alerts"]["markets"][0]
        self.assertEqual(alert["label"], "美股")
        self.assertEqual(alert["action_label"], "减仓候选")
        self.assertEqual(alert["gate_progress"], {"met": 1, "total": 1})
        self.assertEqual(len(alert["triggered_signals"]), 1)
        self.assertNotIn("confirmation", alert)
        encoded = json.dumps(projected, ensure_ascii=False)
        self.assertNotIn("must-not-leak", encoded)
        self.assertNotIn("private-account", encoded)
        self.assertNotIn("shares", encoded)

    def test_public_macro_alerts_reject_invalid_contract_and_force_abstain(self) -> None:
        base = {
            "schema_version": 1,
            "method_version": "macro-de-risk-trial-v1",
            "generated_at": "2026-09-06T01:00:00Z",
            "mode": "trial",
            "human_review_required": True,
            "automatic_execution": False,
            "markets": [
                {
                    "market": "CN",
                    "action": "exit_candidate",
                    "risk_level": "critical",
                    "abstain": True,
                    "data_status": "insufficient",
                    "summary": "信息不足",
                    "gates": [],
                }
            ],
        }
        projected = decision_service.project_public_macro(
            {"public_schema_version": 1, "market_alerts": base}
        )
        alert = projected["market_alerts"]["markets"][0]
        self.assertEqual(alert["action"], "observe")
        self.assertEqual(alert["risk_level"], "insufficient")

        forged = dict(base)
        forged["automatic_execution"] = True
        rejected = decision_service.project_public_macro(
            {"public_schema_version": 1, "market_alerts": forged}
        )
        self.assertNotIn("market_alerts", rejected)

    def test_public_macro_alerts_expire_after_collection_stops(self) -> None:
        projected = decision_service.project_public_macro(
            {
                "public_schema_version": 1,
                "market_alerts": {
                    "schema_version": 1,
                    "method_version": "macro-de-risk-trial-v1",
                    "generated_at": "2026-09-06T01:00:00Z",
                    "mode": "trial",
                    "human_review_required": True,
                    "automatic_execution": False,
                    "markets": [
                        {
                            "market": "US",
                            "action": "exit_candidate",
                            "risk_level": "critical",
                            "abstain": False,
                            "data_status": "ok",
                            "summary": "old action",
                            "gates": [
                                {
                                    "key": "data_freshness",
                                    "label": "核心数据新鲜度",
                                    "status": "met",
                                }
                            ],
                        }
                    ],
                },
            },
            now=datetime(2026, 9, 6, 3, 0, tzinfo=timezone.utc),
        )

        alert = projected["market_alerts"]["markets"][0]
        self.assertEqual(alert["action"], "observe")
        self.assertTrue(alert["abstain"])
        self.assertEqual(alert["risk_level"], "insufficient")
        self.assertIn("超过90分钟", alert["summary"])
        self.assertIn("宏观采集快照已过期", alert["missing_sources"])
        self.assertEqual(alert["gates"][0]["status"], "unavailable")

    def test_public_macro_projects_sanitized_official_body_evidence(self) -> None:
        projected = decision_service.project_public_macro(
            {
                "public_schema_version": 1,
                "monitored_events": [
                    {
                        "id": "fomc-statement",
                        "kind": "policy",
                        "title": "Federal Reserve issues FOMC statement",
                        "url": "https://www.federalreserve.gov/newsevents/pressreleases/monetary20260801a.htm",
                        "source": "Federal Reserve",
                        "severity": "high",
                        "category": "fomc_statement",
                        "content_status": "ready",
                        "content_excerpt": (
                            "<p>The Committee maintained the target range.</p>"
                            "<script>raw-provider-response</script>"
                        ),
                        "content_source_url": (
                            "https://www.federalreserve.gov/newsevents/"
                            "pressreleases/monetary20260801a.htm"
                        ),
                        "evidence_sections": [
                            {
                                "kind": "paragraph",
                                "text": "<strong>Voting</strong> was 9 to 3.",
                            },
                            {
                                "kind": "table_row",
                                "text": "Target range | 3.50%-3.75%",
                                "raw_html": "<tr>must-never-be-public</tr>",
                            },
                            {"kind": "raw_html", "text": "private-html"},
                        ],
                        "content_html": "<main>must-never-be-public</main>",
                        "raw_response": "provider-private",
                    }
                ],
            },
            {
                "fomc-statement": {
                    "status": "ready",
                    "ai_enrichment": {
                        "headline_zh": "美联储维持目标利率区间",
                        "summary_zh": "委员会维持目标区间不变。",
                        "why_it_matters_zh": "政策路径会影响利率敏感资产。",
                        "impact_level": "high",
                        "confidence": 0.82,
                        "evidence_basis": "official_body",
                    },
                }
            },
        )

        event = projected["monitored_events"][0]
        self.assertEqual(event["category"], "fomc_statement")
        self.assertEqual(event["content_status"], "ready")
        self.assertEqual(
            event["content_excerpt"],
            "The Committee maintained the target range.",
        )
        self.assertEqual(
            event["content_source_url"],
            "https://www.federalreserve.gov/newsevents/pressreleases/monetary20260801a.htm",
        )
        self.assertEqual(
            event["evidence_sections"],
            [
                {"kind": "paragraph", "text": "Voting was 9 to 3."},
                {
                    "kind": "table_row",
                    "text": "Target range | 3.50%-3.75%",
                },
            ],
        )
        self.assertEqual(
            event["ai_enrichment"]["evidence_basis"],
            "official_body",
        )
        encoded = json.dumps(event, ensure_ascii=False)
        self.assertNotIn("<", encoded)
        self.assertNotIn("raw-provider-response", encoded)
        self.assertNotIn("must-never-be-public", encoded)
        self.assertNotIn("provider-private", encoded)

    def test_public_macro_never_exposes_nonready_body_payload(self) -> None:
        projected = decision_service.project_public_macro(
            {
                "public_schema_version": 1,
                "monitored_events": [
                    {
                        "id": "unsupported-source",
                        "kind": "policy",
                        "title": "Policy update",
                        "category": "policy_update",
                        "content_status": "unsupported",
                        "content_excerpt": "must-never-be-public",
                        "content_source_url": "https://evil.example/policy",
                        "evidence_sections": [
                            {"kind": "paragraph", "text": "private section"}
                        ],
                    },
                    {
                        "id": "empty-body",
                        "kind": "policy",
                        "title": "Empty body",
                        "content_status": "ready",
                        "content_excerpt": "   ",
                    },
                    {
                        "id": "forged-ready-body",
                        "kind": "policy",
                        "title": "Forged body",
                        "content_status": "ready",
                        "content_excerpt": "Untrusted body text",
                        "content_source_url": "https://evil.example/policy",
                    },
                    {
                        "id": "wrong-official-path",
                        "kind": "policy",
                        "title": "Wrong official path",
                        "url": "https://www.federalreserve.gov/not-an-article",
                        "content_status": "ready",
                        "content_excerpt": "Must not be trusted by host alone",
                        "content_source_url": (
                            "https://www.federalreserve.gov:4444/not-an-article"
                        ),
                    },
                    {
                        "id": "mismatched-official-article",
                        "kind": "policy",
                        "title": "Mismatched official article",
                        "url": (
                            "https://www.federalreserve.gov/newsevents/"
                            "pressreleases/monetary20260801a.htm"
                        ),
                        "content_status": "ready",
                        "content_excerpt": "Body belongs to another release",
                        "content_source_url": (
                            "https://www.federalreserve.gov/newsevents/"
                            "pressreleases/monetary20260701a.htm"
                        ),
                    },
                ],
            }
        )

        unsupported, empty, forged, wrong_path, mismatch = projected[
            "monitored_events"
        ]
        self.assertEqual(unsupported["content_status"], "unsupported")
        self.assertNotIn("content_excerpt", unsupported)
        self.assertNotIn("content_source_url", unsupported)
        self.assertNotIn("evidence_sections", unsupported)
        self.assertEqual(empty["content_status"], "unavailable")
        self.assertNotIn("content_excerpt", empty)
        self.assertEqual(forged["content_status"], "unavailable")
        self.assertNotIn("content_excerpt", forged)
        self.assertNotIn("content_source_url", forged)
        self.assertEqual(wrong_path["content_status"], "unavailable")
        self.assertNotIn("content_excerpt", wrong_path)
        self.assertNotIn("content_source_url", wrong_path)
        self.assertEqual(mismatch["content_status"], "unavailable")
        self.assertNotIn("content_excerpt", mismatch)

    def test_public_macro_projects_ofr_stress_and_coverage_metadata(self) -> None:
        projected = decision_service.project_public_macro(
            {
                "public_schema_version": 1,
                "market_data": {
                    "financial_stress": {
                        "ofr_fsi": 1.25,
                        "credit": 0.8,
                        "funding": 0.4,
                        "volatility": 1.1,
                        "equity_valuation": -0.2,
                        "safe_assets": 0.3,
                        "observed_at": "2026-08-04",
                        "data_status": "ok",
                        "status": "elevated",
                        "source": "Office of Financial Research",
                        "source_url": (
                            "https://www.financialresearch.gov/"
                            "financial-stress-index/"
                        ),
                        "unit": "index",
                        "stale": False,
                        "note": "Official daily observation",
                        "account": "must-never-be-public",
                    },
                    "credit_spreads": {
                        "hy_oas": 420.0,
                        "unit": "basis_points",
                    },
                },
                "data_coverage": {
                    "available": 1,
                    "total": 6,
                    "pct": 17,
                    "sources": [
                        {
                            "key": "financial_stress",
                            "label": "全球金融压力（OFR FSI）",
                            "available": True,
                            "status": "elevated",
                            "data_status": "ok",
                            "observed_at": "2026-08-04",
                            "source_url": (
                                "https://www.financialresearch.gov/"
                                "financial-stress-index/"
                            ),
                            "stale": False,
                            "note": "Official daily observation",
                            "portfolio": "must-never-be-public",
                        }
                    ],
                },
            }
        )

        self.assertEqual(
            projected["market_data"]["financial_stress"],
            {
                "ofr_fsi": 1.25,
                "credit": 0.8,
                "funding": 0.4,
                "volatility": 1.1,
                "equity_valuation": -0.2,
                "safe_assets": 0.3,
                "observed_at": "2026-08-04",
                "data_status": "ok",
                "status": "elevated",
                "source": "Office of Financial Research",
                "source_url": (
                    "https://www.financialresearch.gov/financial-stress-index/"
                ),
                "unit": "index",
                "stale": False,
                "note": "Official daily observation",
            },
        )
        self.assertEqual(
            projected["market_data"]["credit_spreads"],
            {"hy_oas": 420.0, "unit": "basis_points"},
        )
        self.assertEqual(
            projected["data_coverage"]["sources"],
            [
                {
                    "key": "financial_stress",
                    "label": "全球金融压力（OFR FSI）",
                    "available": True,
                    "status": "elevated",
                    "data_status": "ok",
                    "observed_at": "2026-08-04",
                    "source_url": (
                        "https://www.financialresearch.gov/"
                        "financial-stress-index/"
                    ),
                    "stale": False,
                    "note": "Official daily observation",
                }
            ],
        )
        self.assertNotIn(
            "must-never-be-public",
            json.dumps(projected, ensure_ascii=False),
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

    def test_private_overlay_adds_portfolio_overview_without_public_leakage(
        self,
    ) -> None:
        public = decision_service.build_public_decisions(
            [
                _relation(
                    "portfolio-nvda",
                    "positive",
                    topic_key="ai",
                    asset_key="US:NVDA",
                ),
                _relation(
                    "portfolio-nvdl",
                    "negative",
                    topic_key="leverage",
                    asset_key="US:NVDL",
                ),
            ],
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
                "quantity": 10.0,
                "avg_cost": 100.0,
                "currency": "USD",
                "asset_class": "stock",
                "is_leveraged": False,
                "as_of": "2026-07-31",
            },
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
            },
            {
                "account": "Robinhood",
                "asset_key": "US:AAPL",
                "symbol": "AAPL",
                "name": "Apple",
                "quantity": 2.0,
                "avg_cost": 150.0,
                "currency": "USD",
                "asset_class": "stock",
                "is_leveraged": False,
                "as_of": "2026-07-31",
            },
        ]
        quotes = {
            "US:NVDA": {
                "price": 120.0,
                "currency": "USD",
                "observed_at": "2026-08-01T00:00:00+00:00",
            },
            "US:NVDL": {
                "price": 90.0,
                "currency": "USD",
                "observed_at": "2026-08-01T00:00:00+00:00",
            },
        }
        original_public = deepcopy(public)

        private = decision_service.build_private_overlay(
            public,
            positions,
            quotes,
            now=TEST_NOW,
        )

        expected_overview = {
            "position_count": 3,
            "matched_position_count": 2,
            "unmatched_position_count": 1,
            "impacted_asset_count": 2,
            "leveraged_match_count": 1,
            "stale_position_count": 1,
            "oldest_as_of": "2026-06-01",
            "candidate_matched_decisions": 0,
            "matching_policy": "exact_asset_key_v1",
            "indirect_exposure_calculated": False,
            "trade_execution_available": False,
        }
        for field, expected in expected_overview.items():
            self.assertEqual(private["portfolio_overview"][field], expected)
        self.assertEqual(private["decision_overview"]["portfolio_matched"], 2)
        self.assertNotIn("portfolio_overview", public)
        self.assertNotIn("portfolio_matched", public["decision_overview"])
        self.assertNotIn("portfolio_overview", original_public)
        self.assertEqual(public, original_public)

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

    def test_decision_summary_is_small_allowlist_and_detail_is_exact(self) -> None:
        public = decision_service.build_public_decisions(
            [_relation("summary-event", "positive")],
            [],
            1.0,
            now=TEST_NOW,
        )

        summary = decision_service.project_decision_summary(public)
        card = summary["decisions"][0]

        self.assertTrue(summary["summary"])
        self.assertEqual(summary["total_decisions"], 1)
        self.assertTrue(card["detail_available"])
        self.assertNotIn("evidence", card)
        self.assertNotIn("mechanism_relations", card)
        self.assertNotIn("contrary_evidence", card)
        self.assertNotIn("records", card["market_validation"])
        detail = decision_service.find_decision(
            public,
            card["topic_key"],
            card["asset_key"],
        )
        self.assertIsNotNone(detail)
        self.assertIn("evidence", detail)
        self.assertIsNone(
            decision_service.find_decision(public, "missing", card["asset_key"])
        )

    def test_decision_summary_reserves_global_leaders_then_diversifies_topics(
        self,
    ) -> None:
        def card(topic: str, asset: str, score: float) -> dict:
            return {
                "topic_key": topic,
                "asset_key": asset,
                "classification": "risk",
                "direction": "negative",
                "horizon": "medium",
                "source_count": 2,
                "score_components": {},
                "confidence": score,
                "total_score": score,
                "action_stage": "candidate_reduce_or_hedge",
                "trigger": "review",
                "invalidation": "reverse",
                "human_review_required": True,
                "decision_status": "candidate",
                "market_validation": {
                    "status": "complete",
                    "abstain": False,
                },
            }

        concentrated = [
            card("ai_semiconductors", f"US:AI{index}", 1.0 - index / 100)
            for index in range(9)
        ]
        diverse = [
            card(f"topic_{index}", f"US:D{index}", 0.80 - index / 100)
            for index in range(10)
        ]
        payload = {
            "decisions": concentrated + diverse,
            "impact_matrix": {
                "columns": [
                    item["asset_key"] for item in concentrated + diverse
                ],
                "rows": [],
            },
            "business_health": {
                "market_validation": {"status": "healthy"}
            },
        }

        summary = decision_service.project_decision_summary(
            payload,
            decision_limit=12,
        )

        topics = Counter(
            item["topic_key"] for item in summary["decisions"]
        )
        self.assertEqual(len(summary["decisions"]), 12)
        self.assertLessEqual(topics["ai_semiconductors"], 3)
        self.assertIn("US:AI0", {
            item["asset_key"] for item in summary["decisions"]
        })
        self.assertEqual(
            summary["selection_policy"]["name"],
            "diversified_top_score_v1",
        )
        self.assertEqual(
            summary["business_health"]["market_validation"]["status"],
            "healthy",
        )


if __name__ == "__main__":
    unittest.main()
