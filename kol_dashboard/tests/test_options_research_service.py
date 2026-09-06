from __future__ import annotations

import json
import re
import unittest
from datetime import datetime, timezone

from kol_dashboard import options_policy, options_research_service


UTC = timezone.utc


def _macro(action: str = "observe", *, abstain: bool = False) -> dict:
    return {
        "market_alerts": {
            "schema_version": 1,
            "method_version": "macro-de-risk-trial-v1",
            "generated_at": "2026-09-06T01:00:00+00:00",
            "mode": "trial",
            "human_review_required": True,
            "automatic_execution": False,
            "markets": [
                {
                    "market": "US",
                    "action": action,
                    "action_label": action,
                    "risk_level": "medium",
                    "abstain": abstain,
                    "data_status": "insufficient" if abstain else "ok",
                    "data_as_of": "2026-09-05T20:00:00+00:00",
                }
            ],
        }
    }


def _portfolio(*, stale: bool = False, clock_skew: bool = False) -> dict:
    return {
        "snapshot_id": 17,
        "source_hash": "private-source-hash",
        "as_of": "2026-09-05",
        "positions": [
            {
                "account": "private-broker-account",
                "asset_key": "US:SECRET",
                "symbol": "SECRET",
                "name": "private-company-name",
                "quantity": 999.0,
                "avg_cost": 12.34,
            }
        ],
        "staleness": {
            "is_stale": stale,
            "clock_skew": clock_skew,
            "age_seconds": 3600,
        },
    }


def _ready_policy() -> dict:
    _, canonical = options_policy.normalize_policy_request(
        {
            "schema_version": 1,
            "expected_revision": 0,
            "strategy": "cash_secured_put",
            "limits": {
                "assignment_budget_ceiling_usd": "50000.00",
                "max_total_reserved_bps": 3000,
                "max_single_underlying_bps": 1500,
                "minimum_cash_buffer_bps": 2000,
                "max_new_contracts_per_week": 2,
            },
            "assignment_plan": "hold_for_review",
            "underlyings": [
                {
                    "asset_key": "US:NVDA",
                    "decision": "willing",
                    "max_assignment_price_usd": "150.00",
                }
            ],
            "acknowledgements": {
                "cash_secured_only": True,
                "assignment_risk_reviewed": True,
            },
        }
    )
    return options_policy.project_policy_record(
        {
            "revision": 1,
            "updated_at": "2026-09-06T01:00:00+00:00",
            "review_due_at": "2026-10-06T01:00:00+00:00",
            "payload": canonical,
        },
        now=datetime(2026, 9, 6, 3, 0, tzinfo=UTC),
    )


class OptionsResearchServiceTests(unittest.TestCase):
    def test_default_overview_fails_closed_without_inventing_candidates(self) -> None:
        result = options_research_service.build_options_overview(
            now=datetime(2026, 9, 6, 3, 0, tzinfo=UTC)
        )

        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(result["method_version"], "options-policy-readiness-v1")
        self.assertTrue(result["available"])
        self.assertEqual(result["mode"], "research_only")
        self.assertEqual(result["data_status"], "insufficient")
        self.assertEqual(result["decision_state"], "abstain")
        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(result["candidates"], [])
        self.assertTrue(result["human_review_required"])
        self.assertFalse(result["automatic_execution"])
        self.assertFalse(result["trade_execution_available"])
        self.assertEqual(result["served_at"], "2026-09-06T03:00:00+00:00")
        self.assertTrue(result["market_gate"]["blocks_new_short_puts"])
        self.assertGreaterEqual(len(result["rejections"]), 7)
        self.assertEqual(result["policy"]["status"], "not_configured")
        self.assertTrue(result["capabilities"]["policy_configuration"])
        self.assertFalse(result["capabilities"]["live_option_chain"])
        self.assertFalse(result["capabilities"]["broker_capacity"])
        self.assertFalse(result["capabilities"]["event_calendar"])
        self.assertFalse(result["capabilities"]["candidate_generation"])
        self.assertFalse(result["capabilities"]["trade_execution"])

    def test_private_portfolio_values_never_enter_projection(self) -> None:
        result = options_research_service.build_options_overview(
            portfolio_snapshot=_portfolio(stale=True),
            public_macro=_macro(),
            now=datetime(2026, 9, 6, 3, 0, tzinfo=UTC),
        )
        encoded = json.dumps(result, ensure_ascii=False, sort_keys=True)

        for private_value in (
            "private-source-hash",
            "private-broker-account",
            "US:SECRET",
            "private-company-name",
            "999.0",
            "12.34",
        ):
            self.assertNotIn(private_value, encoded)
        for forbidden_field in (
            '"source_hash"',
            '"account"',
            '"quantity"',
            '"avg_cost"',
            '"positions"',
        ):
            self.assertNotIn(forbidden_field, encoded)

        portfolio_gate = next(
            item
            for item in result["readiness"]["items"]
            if item["key"] == "portfolio_freshness"
        )
        self.assertEqual(portfolio_gate["status"], "stale")
        self.assertTrue(portfolio_gate["blocking"])
        self.assertEqual(portfolio_gate["evidence_as_of"], "2026-09-05")

    def test_fresh_portfolio_only_clears_its_own_gate(self) -> None:
        result = options_research_service.build_options_overview(
            portfolio_snapshot=_portfolio(),
            public_macro=_macro(),
        )
        items = {item["key"]: item for item in result["readiness"]["items"]}

        self.assertEqual(items["portfolio_freshness"]["status"], "ready")
        self.assertFalse(items["portfolio_freshness"]["blocking"])
        self.assertEqual(items["macro_gate"]["status"], "ready")
        self.assertFalse(items["macro_gate"]["blocking"])
        self.assertEqual(result["readiness"]["met"], 2)
        self.assertEqual(result["decision_state"], "abstain")
        self.assertEqual(result["candidate_count"], 0)

    def test_future_dated_portfolio_clock_skew_is_blocking(self) -> None:
        result = options_research_service.build_options_overview(
            portfolio_snapshot=_portfolio(clock_skew=True),
            public_macro=_macro(),
        )
        portfolio_gate = next(
            item
            for item in result["readiness"]["items"]
            if item["key"] == "portfolio_freshness"
        )

        self.assertEqual(portfolio_gate["status"], "stale")
        self.assertTrue(portfolio_gate["blocking"])
        self.assertIn("时间异常", portfolio_gate["detail"])
        self.assertIn(
            "portfolio_freshness",
            {item["code"] for item in result["rejections"]},
        )
        self.assertEqual(result["candidate_count"], 0)

    def test_macro_reduce_and_insufficient_states_block_new_short_puts(self) -> None:
        reduced = options_research_service.build_options_overview(
            portfolio_snapshot=_portfolio(),
            public_macro=_macro("reduce_candidate"),
        )
        insufficient = options_research_service.build_options_overview(
            portfolio_snapshot=_portfolio(),
            public_macro=_macro(abstain=True),
        )

        self.assertEqual(reduced["market_gate"]["status"], "blocked")
        self.assertTrue(reduced["market_gate"]["blocks_new_short_puts"])
        self.assertEqual(insufficient["market_gate"]["status"], "unavailable")
        self.assertEqual(insufficient["market_gate"]["action"], "observe")
        self.assertTrue(insufficient["market_gate"]["abstain"])
        self.assertTrue(insufficient["market_gate"]["blocks_new_short_puts"])

        unknown = options_research_service.build_options_overview(
            portfolio_snapshot=_portfolio(),
            public_macro=_macro("unknown_action"),
        )
        self.assertEqual(unknown["market_gate"]["status"], "unavailable")
        self.assertTrue(unknown["market_gate"]["abstain"])
        self.assertTrue(unknown["market_gate"]["blocks_new_short_puts"])

    def test_prepare_reduce_is_visible_but_does_not_relax_other_blockers(self) -> None:
        result = options_research_service.build_options_overview(
            portfolio_snapshot=_portfolio(),
            public_macro=_macro("prepare_reduce"),
        )

        self.assertEqual(result["market_gate"]["status"], "constrained")
        self.assertFalse(result["market_gate"]["blocks_new_short_puts"])
        self.assertEqual(result["decision_state"], "abstain")
        self.assertEqual(result["candidates"], [])

    def test_ready_policy_only_clears_policy_and_familiar_universe_gates(self) -> None:
        result = options_research_service.build_options_overview(
            portfolio_snapshot=_portfolio(),
            public_macro=_macro(),
            policy=_ready_policy(),
            now=datetime(2026, 9, 6, 3, 0, tzinfo=UTC),
        )
        items = {item["key"]: item for item in result["readiness"]["items"]}

        self.assertEqual(items["underwriting_policy"]["status"], "ready")
        self.assertFalse(items["underwriting_policy"]["blocking"])
        self.assertEqual(items["familiar_universe"]["status"], "ready")
        self.assertFalse(items["familiar_universe"]["blocking"])
        self.assertEqual(items["portfolio_freshness"]["status"], "ready")
        self.assertEqual(items["macro_gate"]["status"], "ready")
        for still_blocked in (
            "option_market_data",
            "funding_capacity",
            "options_permission",
            "event_calendar",
        ):
            self.assertEqual(items[still_blocked]["status"], "unavailable")
            self.assertTrue(items[still_blocked]["blocking"])
        self.assertEqual(result["readiness"]["met"], 4)
        self.assertEqual(result["data_status"], "insufficient")
        self.assertEqual(result["decision_state"], "abstain")
        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(result["candidates"], [])
        self.assertFalse(result["trade_execution_available"])

    def test_policy_projection_is_revalidated_and_cannot_leak_extra_fields(self) -> None:
        hostile = {
            **_ready_policy(),
            "account": "private-broker-account",
            "positions": [{"quantity": 999}],
        }
        hostile["limits"] = {
            **hostile["limits"],
            "account_secret": "must-not-appear",
        }
        result = options_research_service.build_options_overview(
            policy=hostile,
            now=datetime(2026, 9, 6, 3, 0, tzinfo=UTC),
        )
        encoded = json.dumps(result, ensure_ascii=False)

        self.assertNotIn("private-broker-account", encoded)
        self.assertNotIn("must-not-appear", encoded)
        self.assertNotIn('"quantity"', encoded)
        self.assertEqual(result["policy"]["status"], "not_configured")
        self.assertEqual(result["candidate_count"], 0)

    def test_benchmark_is_versioned_bounded_and_copied_per_response(self) -> None:
        first = options_research_service.build_options_overview()
        first["benchmark"]["series"][0]["cagr"] = 99
        second = options_research_service.build_options_overview()

        benchmark = second["benchmark"]
        self.assertEqual(benchmark["calculation_version"], "putwrite-benchmark-v1")
        self.assertEqual(benchmark["source_as_of"], "2026-09-04")
        self.assertEqual(benchmark["series"][0]["cagr"], 0.072)
        self.assertEqual(len(benchmark["series"]), 5)
        self.assertEqual(len(benchmark["stress_windows"]), 3)
        hashes = benchmark["input_sha256"]
        self.assertEqual(set(hashes), {"PUT", "PUTY", "WPUT", "PUTD", "SPX"})
        self.assertTrue(
            all(re.fullmatch(r"[0-9a-f]{64}", value) for value in hashes.values())
        )
        self.assertEqual(
            hashes["PUT"],
            "de5e047788418441d6c65f3b5d55c80e2664c1406b9d017550b532d092209fdd",
        )
        self.assertLessEqual(len(second["research_universe"]), 20)
        self.assertTrue(
            all(
                item["status"] == "needs_user_confirmation"
                for item in second["research_universe"]
            )
        )
        self.assertTrue(
            all("通用研究池" in item["note"] for item in second["research_universe"])
        )


if __name__ == "__main__":
    unittest.main()
