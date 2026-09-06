from __future__ import annotations

import copy
import hashlib
import inspect
import json
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Inexact, ROUND_UP, Rounded, localcontext

from kol_dashboard import options_capacity, options_policy


UTC = timezone.utc
DECISION_AT = datetime(2026, 9, 6, 15, 0, tzinfo=UTC)


def _sha256(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _policy_record(
    *,
    budget: str = "50000.00",
    total_bps: int = 3333,
    single_bps: int = 1250,
    buffer_bps: int = 1001,
    weekly_limit: int = 3,
) -> dict:
    _, payload = options_policy.normalize_policy_request(
        {
            "schema_version": 1,
            "expected_revision": 0,
            "strategy": "cash_secured_put",
            "limits": {
                "assignment_budget_ceiling_usd": budget,
                "max_total_reserved_bps": total_bps,
                "max_single_underlying_bps": single_bps,
                "minimum_cash_buffer_bps": buffer_bps,
                "max_new_contracts_per_week": weekly_limit,
            },
            "assignment_plan": "hold_for_review",
            "underlyings": [
                {
                    "asset_key": "US:MSFT",
                    "decision": "willing",
                    "max_assignment_price_usd": "400.00",
                }
            ],
            "acknowledgements": {
                "cash_secured_only": True,
                "assignment_risk_reviewed": True,
            },
        }
    )
    return {
        "revision": 7,
        "payload_sha256": options_policy.policy_hash(
            options_policy.canonical_policy_json(payload)
        ),
        "updated_at": "2026-09-01T15:00:00+00:00",
        "review_due_at": "2026-10-01T15:00:00+00:00",
        "payload": payload,
    }


def _account(
    *,
    available: str = "40000.00",
    buying_power: str = "35000.00",
    nlv: str = "100000.03",
    existing_reserved: str | None = "5000.00",
    opened_this_week: int = 1,
    as_of: datetime | None = None,
    received_at: datetime | None = None,
) -> dict:
    observed = as_of or DECISION_AT - timedelta(seconds=30)
    received = received_at or DECISION_AT - timedelta(seconds=5)
    existing_options = (
        {}
        if existing_reserved is None
        else {
            "US:SPY:2026-10-16:P:500.00:100": {
                "underlying": "SPY",
                "position_type": "cash_secured_short_put",
                "gross_cash_reserved_usd": existing_reserved,
            }
        }
    )
    payload = {
        "account_scope_id": "broker:individual:primary-usd-csp",
        "account_scope_type": "single_account",
        "collateral_pool_id": "broker:primary:usd-cash",
        "snapshot_id": "account-snapshot-0007",
        "status": "ready",
        "as_of": observed.isoformat(),
        "received_at": received.isoformat(),
        "currency": "USD",
        "cash_secured_put_permission": True,
        "available_cash_usd": available,
        "buying_power_usd": buying_power,
        "buying_power_semantics": "cash_secured_put",
        "net_liquidation_value_usd": nlv,
        "new_contracts_opened_this_week": opened_this_week,
        "positions_complete": True,
        "existing_options_complete": True,
        "positions": {
            "MSFT": {"market_value_usd": "10000.00"},
            "SPY": {"market_value_usd": "20000.00"},
        },
        "existing_options": existing_options,
    }
    return {**payload, "payload_sha256": _sha256(payload)}


def _rehash_account(account: dict) -> None:
    payload = {key: value for key, value in account.items() if key != "payload_sha256"}
    account["payload_sha256"] = _sha256(payload)


def _macro(
    action: str = "observe",
    *,
    generated_at: datetime | None = None,
    data_status: str = "ok",
    abstain: bool | None = None,
) -> dict:
    should_abstain = data_status != "ok" if abstain is None else abstain
    statuses = {
        "observe": "ready",
        "prepare_reduce": "constrained",
        "reduce_candidate": "blocked",
        "exit_candidate": "blocked",
    }
    payload = {
        "snapshot_id": "macro-snapshot-0021",
        "method_version": "macro-de-risk-trial-v1",
        "generated_at": (
            generated_at or DECISION_AT - timedelta(minutes=45)
        ).isoformat(),
        "markets": [
            {
                "market": "US",
                "status": "unavailable"
                if should_abstain
                else statuses.get(action, "ready"),
                "action": action,
                "data_status": data_status,
                "abstain": should_abstain,
                "abstain_reason": (
                    "core_market_data_insufficient" if should_abstain else None
                ),
            }
        ],
    }
    return {**payload, "projection_sha256": _sha256(payload)}


def _rehash_macro(macro: dict) -> None:
    payload = {
        key: value for key, value in macro.items() if key != "projection_sha256"
    }
    macro["projection_sha256"] = _sha256(payload)


def _derive(
    *,
    decision_at: datetime = DECISION_AT,
    policy: dict | None = None,
    account: dict | None = None,
    macro: dict | None = None,
) -> dict:
    return options_capacity.derive_effective_limits(
        decision_at=decision_at,
        policy_record=policy or _policy_record(),
        account_snapshot=account or _account(),
        macro_snapshot=macro or _macro(),
    )


def _codes(result: dict) -> set[str]:
    return {item["code"] for item in result["rejections"]}


class EffectiveLimitFormulaTests(unittest.TestCase):
    def test_formula_uses_minimum_policy_ceiling_and_existing_collateral(self) -> None:
        result = _derive()

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["method_version"], "options-capacity-v1")
        self.assertEqual(result["mode"], "research_only")
        self.assertEqual(result["decision_state"], "abstain")
        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(result["candidates"], [])
        self.assertTrue(result["human_review_required"])
        self.assertFalse(result["automatic_execution"])
        self.assertFalse(result["trade_execution_available"])
        self.assertEqual(result["rejections"], [])

        # NLV 100000.03: total=floor(33.33%)=33330.00,
        # single=floor(12.50%)=12500.00, buffer=ceil(10.01%)=10010.01.
        # E=5000, room=min(35000, 40000-buffer)=29989.99,
        # account total=34989.99, so policy total is the binding minimum.
        limits = result["risk_limits"]
        self.assertEqual(limits["minimum_cash_buffer_usd"], "10010.01")
        self.assertEqual(limits["max_total_cash_secured_usd"], "33330.00")
        self.assertEqual(limits["remaining_total_cash_secured_usd"], "28330.00")
        self.assertEqual(
            limits["max_single_underlying_assignment_usd"], "12500.00"
        )
        self.assertEqual(
            limits["max_single_underlying_assignment_semantics"],
            "cumulative_post_assignment_exposure_ceiling",
        )
        self.assertEqual(
            limits["max_post_assignment_underlying_ratio"], "0.125000"
        )
        self.assertEqual(limits["max_new_contracts_per_week"], 3)
        self.assertEqual(limits["remaining_new_contracts_this_week"], 2)
        self.assertEqual(limits["effective_for_macro_action"], "observe")

    def test_absolute_policy_and_account_minima_each_bind(self) -> None:
        cases = (
            (
                "absolute",
                _policy_record(budget="12000.00"),
                _account(),
                "12000.00",
                "7000.00",
            ),
            (
                "policy_percent",
                _policy_record(budget="50000.00"),
                _account(),
                "33330.00",
                "28330.00",
            ),
            (
                "account_room",
                _policy_record(budget="50000.00"),
                _account(available="30000.00", buying_power="8000.00"),
                "13000.00",
                "8000.00",
            ),
        )
        for name, policy, account, expected_total, expected_remaining in cases:
            with self.subTest(name=name):
                limits = _derive(policy=policy, account=account)["risk_limits"]
                self.assertEqual(
                    limits["max_total_cash_secured_usd"], expected_total
                )
                self.assertEqual(
                    limits["remaining_total_cash_secured_usd"],
                    expected_remaining,
                )

    def test_percentage_rounding_is_conservative_at_one_cent(self) -> None:
        policy = _policy_record(
            budget="50000.00",
            total_bps=3333,
            single_bps=1001,
            buffer_bps=1001,
        )
        account = _account(
            available="100.01",
            buying_power="100.01",
            nlv="100.01",
            existing_reserved=None,
        )

        limits = _derive(policy=policy, account=account)["risk_limits"]

        self.assertEqual(limits["max_total_cash_secured_usd"], "33.33")
        self.assertEqual(limits["remaining_total_cash_secured_usd"], "33.33")
        self.assertEqual(
            limits["max_single_underlying_assignment_usd"], "10.01"
        )
        self.assertEqual(limits["minimum_cash_buffer_usd"], "10.02")

    def test_existing_collateral_above_effective_ceiling_abstains(self) -> None:
        result = _derive(
            policy=_policy_record(budget="4000.00"),
            account=_account(existing_reserved="5000.00"),
        )

        self.assertIsNone(result["risk_limits"])
        self.assertIn("existing_collateral_limit_breached", _codes(result))

    def test_weekly_limit_already_reached_abstains(self) -> None:
        result = _derive(account=_account(opened_this_week=9))
        self.assertIsNone(result["risk_limits"])
        self.assertIn("weekly_new_contract_limit_reached", _codes(result))

    def test_cash_below_policy_buffer_abstains(self) -> None:
        result = _derive(account=_account(available="10000.00"))
        self.assertIsNone(result["risk_limits"])
        self.assertIn("cash_buffer_breached", _codes(result))

    def test_zero_new_capacity_abstains_instead_of_reporting_ready(self) -> None:
        cases = (
            _account(available="10010.01"),
            _account(buying_power="0.00"),
            _account(existing_reserved="5000.00"),
        )
        policies = (
            _policy_record(),
            _policy_record(),
            _policy_record(budget="5000.00"),
        )
        self.assertEqual(len(cases), len(policies))
        for account, policy in zip(cases, policies):
            with self.subTest(
                available=account["available_cash_usd"],
                buying_power=account["buying_power_usd"],
            ):
                result = _derive(account=account, policy=policy)
                self.assertIsNone(result["risk_limits"])
                self.assertIn("new_capacity_unavailable", _codes(result))


class TimeAndIntegrityGateTests(unittest.TestCase):
    def assert_abstains(self, result: dict, code: str) -> None:
        self.assertEqual(result["status"], "abstain")
        self.assertEqual(result["decision_state"], "abstain")
        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(result["candidates"], [])
        self.assertIsNone(result["risk_limits"])
        self.assertIn(code, _codes(result))

    def test_account_age_boundary_is_inclusive_then_stale(self) -> None:
        accepted = _account(
            as_of=DECISION_AT - timedelta(seconds=60),
            received_at=DECISION_AT,
        )
        rejected = _account(
            as_of=DECISION_AT - timedelta(seconds=60, microseconds=1),
            received_at=DECISION_AT,
        )

        self.assertEqual(_derive(account=accepted)["status"], "ready")
        self.assert_abstains(_derive(account=rejected), "account_stale")

    def test_account_future_and_inverted_times_fail_closed(self) -> None:
        future = _account(
            as_of=DECISION_AT,
            received_at=DECISION_AT + timedelta(microseconds=1),
        )
        inverted = _account(
            as_of=DECISION_AT - timedelta(seconds=2),
            received_at=DECISION_AT - timedelta(seconds=3),
        )

        self.assert_abstains(_derive(account=future), "account_future_time")
        self.assert_abstains(_derive(account=inverted), "account_time_order")

    def test_macro_age_boundary_is_inclusive_then_stale(self) -> None:
        accepted = _macro(generated_at=DECISION_AT - timedelta(minutes=90))
        rejected = _macro(
            generated_at=DECISION_AT - timedelta(minutes=90, microseconds=1)
        )

        self.assertEqual(_derive(macro=accepted)["status"], "ready")
        self.assert_abstains(_derive(macro=rejected), "macro_stale")

    def test_macro_future_time_and_naive_decision_fail_closed(self) -> None:
        future = _macro(generated_at=DECISION_AT + timedelta(microseconds=1))
        self.assert_abstains(_derive(macro=future), "macro_future_time")
        self.assert_abstains(
            _derive(decision_at=DECISION_AT.replace(tzinfo=None)),
            "decision_time_invalid",
        )

    def test_policy_expiry_is_exclusive_and_future_update_is_rejected(self) -> None:
        policy = _policy_record()
        due = datetime.fromisoformat(policy["review_due_at"])
        self.assert_abstains(
            _derive(
                decision_at=due,
                account=_account(
                    as_of=due - timedelta(seconds=30),
                    received_at=due - timedelta(seconds=5),
                ),
                macro=_macro(generated_at=due - timedelta(minutes=45)),
            ),
            "policy_review_due",
        )

        future = _policy_record()
        future["updated_at"] = (DECISION_AT + timedelta(seconds=1)).isoformat()
        future["review_due_at"] = (
            DECISION_AT + timedelta(days=30, seconds=1)
        ).isoformat()
        self.assert_abstains(_derive(policy=future), "policy_future_time")

    def test_hashes_and_exact_review_interval_are_mandatory(self) -> None:
        policy_hash = _policy_record()
        policy_hash["payload_sha256"] = "0" * 64
        self.assert_abstains(_derive(policy=policy_hash), "policy_hash_mismatch")

        policy_review = _policy_record()
        policy_review["review_due_at"] = "2026-10-02T15:00:00+00:00"
        self.assert_abstains(
            _derive(policy=policy_review), "policy_review_invalid"
        )

        account = _account()
        account["payload_sha256"] = "0" * 64
        self.assert_abstains(_derive(account=account), "account_hash_mismatch")

        macro = _macro()
        macro["projection_sha256"] = "0" * 64
        self.assert_abstains(_derive(macro=macro), "macro_hash_mismatch")


class ClosedWorldAndSemanticGateTests(unittest.TestCase):
    def assert_abstains(self, result: dict, code: str) -> None:
        self.assertIsNone(result["risk_limits"])
        self.assertIn(code, _codes(result))

    def test_top_level_and_nested_schemas_are_closed_world(self) -> None:
        policy = _policy_record()
        policy["id"] = 99
        self.assert_abstains(_derive(policy=policy), "invalid_policy_fields")

        account = _account()
        account["debug"] = True
        _rehash_account(account)
        self.assert_abstains(_derive(account=account), "invalid_account_fields")

        macro = _macro()
        macro["markets"][0]["risk_level"] = "low"
        _rehash_macro(macro)
        self.assert_abstains(_derive(macro=macro), "invalid_macro_market_fields")

        position = _account()
        position["positions"]["MSFT"]["quantity"] = "1.00"
        _rehash_account(position)
        self.assert_abstains(_derive(account=position), "positions_incomplete")

        option = _account()
        next(iter(option["existing_options"].values()))["account_scope_id"] = (
            "other-account"
        )
        _rehash_account(option)
        self.assert_abstains(
            _derive(account=option), "existing_options_incomplete"
        )

    def test_boolean_float_nonfinite_and_exponent_money_are_rejected(self) -> None:
        cases = (
            ("available_cash_usd", True),
            ("buying_power_usd", 35000.0),
            ("net_liquidation_value_usd", "NaN"),
            ("net_liquidation_value_usd", "Infinity"),
            ("available_cash_usd", "4e4"),
        )
        for field, value in cases:
            with self.subTest(field=field, value=repr(value)):
                account = _account()
                account[field] = value
                _rehash_account(account)
                self.assert_abstains(
                    _derive(account=account), "account_balances_invalid"
                )

    def test_account_scope_permission_currency_and_buying_power_semantics(self) -> None:
        cases = (
            ("account_scope_id", "", "account_identity_invalid"),
            ("currency", "CNY", "account_currency_not_usd"),
            (
                "cash_secured_put_permission",
                False,
                "options_permission_missing",
            ),
            (
                "buying_power_semantics",
                "margin",
                "buying_power_semantics_invalid",
            ),
            ("status", "partial", "account_not_ready"),
        )
        for field, value, code in cases:
            with self.subTest(field=field):
                account = _account()
                account[field] = value
                _rehash_account(account)
                self.assert_abstains(_derive(account=account), code)

    def test_multi_account_or_aggregate_shapes_are_explicitly_ambiguous(self) -> None:
        inputs = [
            [_account(), _account()],
            {**_account(), "accounts": ["primary", "secondary"]},
            {**_account(), "collateral_pool_ids": ["cash", "margin"]},
        ]
        for scope_type in ("household", "all_accounts", "combined"):
            account = _account()
            account["account_scope_type"] = scope_type
            _rehash_account(account)
            inputs.append(account)
        for scope_id in (
            "broker:household",
            "broker:all-accounts",
            "portfolio:combined",
        ):
            account = _account()
            account["account_scope_id"] = scope_id
            _rehash_account(account)
            inputs.append(account)
        multi_pool = _account()
        multi_pool["collateral_pool_id"] = ["cash", "margin"]
        inputs.append(multi_pool)

        for account in inputs:
            with self.subTest(shape=type(account).__name__):
                self.assert_abstains(
                    _derive(account=account), "account_scope_ambiguous"
                )

    def test_complete_positions_and_existing_options_are_required(self) -> None:
        for field, code in (
            ("positions_complete", "positions_incomplete"),
            ("existing_options_complete", "existing_options_incomplete"),
        ):
            with self.subTest(field=field):
                account = _account()
                account[field] = False
                _rehash_account(account)
                self.assert_abstains(_derive(account=account), code)

        bad_position = _account()
        bad_position["positions"]["MSFT"]["market_value_usd"] = "NaN"
        _rehash_account(bad_position)
        self.assert_abstains(
            _derive(account=bad_position), "positions_incomplete"
        )

        bad_option = _account()
        next(iter(bad_option["existing_options"].values()))[
            "position_type"
        ] = "naked_short_put"
        _rehash_account(bad_option)
        self.assert_abstains(
            _derive(account=bad_option), "existing_options_incomplete"
        )

    def test_macro_all_actions_and_readiness_semantics(self) -> None:
        observe = _derive(macro=_macro("observe"))
        self.assertEqual(observe["status"], "ready")
        self.assertEqual(
            observe["risk_limits"]["effective_for_macro_action"], "observe"
        )

        self.assert_abstains(
            _derive(macro=_macro("prepare_reduce")),
            "macro_constrained_limits_missing",
        )
        for action in ("reduce_candidate", "exit_candidate"):
            with self.subTest(action=action):
                self.assert_abstains(
                    _derive(macro=_macro(action)), "macro_risk_blocked"
                )
        self.assert_abstains(
            _derive(macro=_macro("unknown")), "macro_action_invalid"
        )
        self.assert_abstains(
            _derive(macro=_macro(data_status="insufficient")),
            "macro_not_ready",
        )
        self.assert_abstains(
            _derive(macro=_macro(data_status="stale")), "macro_not_ready"
        )

        inconsistent = _macro()
        inconsistent["markets"][0]["status"] = "blocked"
        _rehash_macro(inconsistent)
        self.assert_abstains(
            _derive(macro=inconsistent), "macro_state_inconsistent"
        )

        missing_reason = _macro(data_status="insufficient")
        missing_reason["markets"][0]["abstain_reason"] = None
        _rehash_macro(missing_reason)
        self.assert_abstains(
            _derive(macro=missing_reason), "macro_state_inconsistent"
        )

    def test_macro_requires_exact_method_and_one_us_market(self) -> None:
        method = _macro()
        method["method_version"] = "unreviewed-v2"
        _rehash_macro(method)
        self.assert_abstains(_derive(macro=method), "macro_method_invalid")

        duplicate = _macro()
        duplicate["markets"].append(copy.deepcopy(duplicate["markets"][0]))
        _rehash_macro(duplicate)
        self.assert_abstains(_derive(macro=duplicate), "macro_market_invalid")

        missing = _macro()
        missing["markets"][0]["market"] = "CN"
        _rehash_macro(missing)
        self.assert_abstains(_derive(macro=missing), "macro_market_invalid")

        with_cn = _macro()
        with_cn["markets"].append(
            {
                "market": "CN",
                "status": "unavailable",
                "action": "observe",
                "data_status": "insufficient",
                "abstain": True,
                "abstain_reason": "cn_data_insufficient",
            }
        )
        _rehash_macro(with_cn)
        self.assertEqual(_derive(macro=with_cn)["status"], "ready")

    def test_only_us_action_controls_us_options_capacity(self) -> None:
        us_blocked = _macro("reduce_candidate")
        us_blocked["markets"].append(
            {
                "market": "CN",
                "status": "ready",
                "action": "observe",
                "data_status": "ok",
                "abstain": False,
                "abstain_reason": None,
            }
        )
        _rehash_macro(us_blocked)
        self.assert_abstains(
            _derive(macro=us_blocked), "macro_risk_blocked"
        )

        cn_blocked = _macro("observe")
        cn_blocked["markets"].append(
            {
                "market": "CN",
                "status": "blocked",
                "action": "reduce_candidate",
                "data_status": "ok",
                "abstain": False,
                "abstain_reason": None,
            }
        )
        _rehash_macro(cn_blocked)
        self.assertEqual(_derive(macro=cn_blocked)["status"], "ready")

    def test_policy_requires_positive_revision_canonical_payload_and_ready_ack(self) -> None:
        revision = _policy_record()
        revision["revision"] = True
        self.assert_abstains(_derive(policy=revision), "policy_revision_invalid")

        noncanonical = _policy_record()
        noncanonical["payload"]["limits"]["assignment_budget_ceiling_usd"] = (
            "50000"
        )
        noncanonical["payload_sha256"] = options_policy.policy_hash(
            options_policy.canonical_policy_json(noncanonical["payload"])
        )
        self.assert_abstains(
            _derive(policy=noncanonical), "policy_payload_not_canonical"
        )

        unacknowledged = _policy_record()
        unacknowledged["payload"]["acknowledgements"][
            "assignment_risk_reviewed"
        ] = False
        unacknowledged["payload_sha256"] = options_policy.policy_hash(
            options_policy.canonical_policy_json(unacknowledged["payload"])
        )
        self.assert_abstains(_derive(policy=unacknowledged), "policy_not_ready")

    def test_errors_never_echo_account_values(self) -> None:
        account = _account()
        account["available_cash_usd"] = "DO-NOT-ECHO-PRIVATE-VALUE"
        _rehash_account(account)

        encoded = json.dumps(_derive(account=account), sort_keys=True)

        self.assertNotIn("DO-NOT-ECHO-PRIVATE-VALUE", encoded)
        self.assertIn("account_balances_invalid", encoded)


class IdentityAndIsolationTests(unittest.TestCase):
    def test_identity_and_input_hash_are_stable_and_bind_all_three_inputs(self) -> None:
        policy = _policy_record()
        account = _account()
        macro = _macro()
        first = _derive(policy=policy, account=account, macro=macro)
        repeated = _derive(policy=policy, account=account, macro=macro)

        self.assertEqual(first["identity"], repeated["identity"])
        self.assertEqual(first["input_sha256"], repeated["input_sha256"])
        self.assertRegex(first["input_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            first["identity"]["capacity_snapshot_id"],
            f"options-cap:{first['input_sha256']}",
        )
        self.assertLessEqual(len(first["identity"]["capacity_snapshot_id"]), 80)

        changed_inputs = []
        changed_policy = copy.deepcopy(policy)
        changed_policy["revision"] += 1
        changed_inputs.append((changed_policy, account, macro))

        renewed_policy = copy.deepcopy(policy)
        renewed_policy["updated_at"] = "2026-09-02T15:00:00+00:00"
        renewed_policy["review_due_at"] = "2026-10-02T15:00:00+00:00"
        changed_inputs.append((renewed_policy, account, macro))

        changed_account = copy.deepcopy(account)
        changed_account["snapshot_id"] = "account-snapshot-0008"
        _rehash_account(changed_account)
        changed_inputs.append((policy, changed_account, macro))

        changed_macro = copy.deepcopy(macro)
        changed_macro["snapshot_id"] = "macro-snapshot-0022"
        _rehash_macro(changed_macro)
        changed_inputs.append((policy, account, changed_macro))

        for changed in changed_inputs:
            with self.subTest(changed=changed[0]["revision"]):
                result = _derive(
                    policy=changed[0], account=changed[1], macro=changed[2]
                )
                self.assertNotEqual(result["input_sha256"], first["input_sha256"])

    def test_inputs_are_not_mutated(self) -> None:
        policy = _policy_record()
        account = _account()
        macro = _macro()
        originals = copy.deepcopy((policy, account, macro))

        _derive(policy=policy, account=account, macro=macro)

        self.assertEqual((policy, account, macro), originals)

    def test_decimal_context_cannot_change_or_break_capacity(self) -> None:
        policy = _policy_record()
        account = _account()
        macro = _macro()
        expected = _derive(policy=policy, account=account, macro=macro)

        with localcontext() as hostile_context:
            hostile_context.prec = 6
            hostile_context.rounding = ROUND_UP
            hostile_context.traps[Inexact] = True
            hostile_context.traps[Rounded] = True
            actual = _derive(policy=policy, account=account, macro=macro)

        self.assertEqual(actual, expected)

    def test_pathological_hash_payloads_abstain_instead_of_escaping(self) -> None:
        recursive_account = _account()
        nested: list = []
        cursor = nested
        for _ in range(2000):
            child: list = []
            cursor.append(child)
            cursor = child
        recursive_account["positions"] = nested
        result = _derive(account=recursive_account)
        self.assertIn("account_hash_invalid", _codes(result))
        self.assertIsNone(result["risk_limits"])

        invalid_unicode = _account()
        invalid_unicode["account_scope_id"] = "scope:\ud800"
        result = _derive(account=invalid_unicode)
        self.assertIn("account_hash_invalid", _codes(result))
        self.assertIsNone(result["risk_limits"])

    def test_public_api_is_keyword_only(self) -> None:
        with self.assertRaises(TypeError):
            options_capacity.derive_effective_limits(
                DECISION_AT,
                _policy_record(),
                _account(),
                _macro(),
            )

    def test_module_has_no_strategy_api_database_or_io_dependencies(self) -> None:
        source = inspect.getsource(options_capacity)

        for forbidden in (
            "options_strategy",
            "options_research_service",
            "kol_dashboard.app",
            "kol_dashboard.db",
            "requests",
            "urllib",
            "sqlite3",
            "open(",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
