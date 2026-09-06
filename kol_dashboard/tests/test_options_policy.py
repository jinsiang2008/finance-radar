from __future__ import annotations

import unittest
from datetime import datetime, timezone

from kol_dashboard import options_policy


UTC = timezone.utc


def policy_request(**overrides):
    request = {
        "schema_version": 1,
        "expected_revision": 0,
        "strategy": "cash_secured_put",
        "limits": {
            "assignment_budget_ceiling_usd": "50000",
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
                "max_assignment_price_usd": "150.5",
            },
            {"asset_key": "US:TSLA", "decision": "exclude"},
        ],
        "acknowledgements": {
            "cash_secured_only": True,
            "assignment_risk_reviewed": True,
        },
    }
    request.update(overrides)
    return request


class OptionsPolicyContractTests(unittest.TestCase):
    def assert_policy_error(self, request, code: str) -> None:
        with self.assertRaises(options_policy.PolicyValidationError) as raised:
            options_policy.normalize_policy_request(request)
        self.assertEqual(raised.exception.code, code)

    def test_valid_contract_normalizes_money_and_underlying_order(self) -> None:
        expected_revision, canonical = options_policy.normalize_policy_request(
            policy_request(expected_revision=7)
        )

        self.assertEqual(expected_revision, 7)
        self.assertEqual(
            canonical["limits"]["assignment_budget_ceiling_usd"],
            "50000.00",
        )
        self.assertEqual(
            canonical["underlyings"][0]["max_assignment_price_usd"],
            "150.50",
        )
        self.assertEqual(
            [item["asset_key"] for item in canonical["underlyings"]],
            ["US:NVDA", "US:TSLA"],
        )
        encoded = options_policy.canonical_policy_json(canonical)
        self.assertEqual(len(options_policy.policy_hash(encoded)), 64)
        self.assertNotIn("expected_revision", encoded)

    def test_unknown_fields_and_wrong_nested_shapes_are_rejected(self) -> None:
        self.assert_policy_error(
            {**policy_request(), "account": "do-not-store"},
            "invalid_policy_fields",
        )
        invalid_limits = policy_request()["limits"] | {"margin": True}
        self.assert_policy_error(
            policy_request(limits=invalid_limits),
            "invalid_limits_fields",
        )
        invalid_acknowledgements = policy_request()["acknowledgements"] | {
            "accepted_terms": True
        }
        self.assert_policy_error(
            policy_request(acknowledgements=invalid_acknowledgements),
            "invalid_acknowledgement_fields",
        )

    def test_boolean_float_nan_and_invalid_bps_relationships_are_rejected(self) -> None:
        for field, value, code in (
            ("max_total_reserved_bps", True, "invalid_max_total_reserved_bps"),
            ("max_single_underlying_bps", 1.5, "invalid_max_single_underlying_bps"),
            ("minimum_cash_buffer_bps", float("nan"), "invalid_minimum_cash_buffer_bps"),
            ("max_new_contracts_per_week", False, "invalid_max_new_contracts_per_week"),
        ):
            limits = {**policy_request()["limits"], field: value}
            self.assert_policy_error(policy_request(limits=limits), code)

        limits = {
            **policy_request()["limits"],
            "max_single_underlying_bps": 4000,
        }
        self.assert_policy_error(
            policy_request(limits=limits),
            "single_underlying_exceeds_total_reserved",
        )
        limits = {
            **policy_request()["limits"],
            "max_total_reserved_bps": 9000,
            "max_single_underlying_bps": 1000,
            "minimum_cash_buffer_bps": 2000,
        }
        self.assert_policy_error(
            policy_request(limits=limits),
            "reserved_plus_buffer_exceeds_available_cash",
        )

    def test_money_rejects_exponent_sign_nan_whitespace_and_excess_precision(self) -> None:
        for value in ("5e4", "+50000", "NaN", " 50000", "50000.001", 50000):
            limits = {
                **policy_request()["limits"],
                "assignment_budget_ceiling_usd": value,
            }
            self.assert_policy_error(
                policy_request(limits=limits),
                "invalid_assignment_budget_ceiling_usd",
            )

    def test_underlying_contract_rejects_duplicates_unknowns_and_leverage(self) -> None:
        duplicate = [
            {
                "asset_key": "US:NVDA",
                "decision": "willing",
                "max_assignment_price_usd": "100",
            },
            {"asset_key": "US:NVDA", "decision": "exclude"},
        ]
        self.assert_policy_error(
            policy_request(underlyings=duplicate),
            "duplicate_underlying",
        )
        self.assert_policy_error(
            policy_request(
                underlyings=[{"asset_key": "US:NVDL", "decision": "exclude"}]
            ),
            "leveraged_underlying_not_allowed",
        )
        self.assert_policy_error(
            policy_request(
                underlyings=[{"asset_key": "US:SECRET", "decision": "exclude"}]
            ),
            "unknown_underlying",
        )

    def test_willing_requires_price_and_exclude_forbids_it(self) -> None:
        self.assert_policy_error(
            policy_request(
                underlyings=[{"asset_key": "US:NVDA", "decision": "willing"}]
            ),
            "invalid_underlying_fields",
        )
        self.assert_policy_error(
            policy_request(
                underlyings=[
                    {
                        "asset_key": "US:NVDA",
                        "decision": "exclude",
                        "max_assignment_price_usd": "100.00",
                    }
                ]
            ),
            "invalid_underlying_fields",
        )

    def test_underlying_count_assignment_plan_and_acknowledgements_are_bounded(self) -> None:
        too_many = [
            {"asset_key": key, "decision": "exclude"}
            for key in list(options_policy.UNDERLYING_DIRECTORY)[:9]
        ]
        self.assert_policy_error(
            policy_request(underlyings=too_many), "too_many_underlyings"
        )
        self.assert_policy_error(
            policy_request(assignment_plan="sell_immediately"),
            "invalid_assignment_plan",
        )
        acknowledgements = {
            **policy_request()["acknowledgements"],
            "cash_secured_only": 1,
        }
        self.assert_policy_error(
            policy_request(acknowledgements=acknowledgements),
            "invalid_cash_secured_only",
        )

    def test_projection_requires_both_acknowledgements_and_nonexpired_review(self) -> None:
        _, canonical = options_policy.normalize_policy_request(policy_request())
        record = {
            "revision": 3,
            "updated_at": "2026-09-01T00:00:00+00:00",
            "review_due_at": "2026-10-01T00:00:00+00:00",
            "payload": canonical,
        }
        ready = options_policy.project_policy_record(
            record,
            now=datetime(2026, 9, 15, tzinfo=UTC),
        )

        self.assertEqual(ready["status"], "ready")
        self.assertTrue(ready["ready"])
        self.assertEqual(ready["confirmed_count"], 1)
        self.assertEqual(ready["excluded_count"], 1)
        self.assertEqual(ready["underlyings"][0]["name_zh"], "英伟达")
        self.assertEqual(ready["evidence_basis"], "user_confirmed")

        expired = options_policy.project_policy_record(
            record,
            now=datetime(2026, 10, 1, tzinfo=UTC),
        )
        self.assertEqual(expired["status"], "review_due")
        self.assertFalse(expired["ready"])

        canonical["acknowledgements"]["assignment_risk_reviewed"] = False
        unacknowledged = options_policy.project_policy_record(
            record,
            now=datetime(2026, 9, 15, tzinfo=UTC),
        )
        self.assertEqual(
            unacknowledged["status"], "acknowledgement_required"
        )


if __name__ == "__main__":
    unittest.main()
