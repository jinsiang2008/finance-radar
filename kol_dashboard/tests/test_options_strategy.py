from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import unittest

from kol_dashboard import options_strategy


UTC = timezone.utc
DECISION_AT = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _contract_key(*, symbol: str, expiration: str, strike: object) -> str:
    return f"US:{symbol}:{expiration}:P:{Decimal(str(strike)):.2f}:100"


def _sync_identity(payload: dict, *, sync_coverage: bool = True) -> None:
    contract = payload["contract"]
    symbol = contract["underlying"]
    expiration = contract["expiration"]
    key = _contract_key(
        symbol=symbol,
        expiration=expiration,
        strike=contract["strike"],
    )
    contract["contract_key"] = key
    payload["quote"]["option"]["contract_key"] = key
    snapshot_id = payload["provenance"]["snapshot_id"]
    payload["quote"]["option"]["snapshot_id"] = snapshot_id
    payload["quote"]["underlying"]["symbol"] = symbol
    payload["quote"]["underlying"]["snapshot_id"] = snapshot_id
    payload["quote"]["derived"]["contract_key"] = key
    payload["quote"]["derived"]["snapshot_id"] = snapshot_id
    payload["events"]["underlying"] = symbol
    if sync_coverage:
        payload["events"]["coverage_through"] = expiration


def _retime(payload: dict, decision_at: datetime) -> None:
    event_ts = decision_at - timedelta(seconds=10)
    recv_ts = decision_at - timedelta(seconds=5)
    payload["decision"]["at"] = _iso(decision_at)
    payload["provenance"]["as_of"] = _iso(event_ts)
    payload["provenance"]["received_at"] = _iso(recv_ts)
    for key in ("option", "underlying"):
        payload["quote"][key]["event_ts"] = _iso(event_ts)
        payload["quote"][key]["recv_ts"] = _iso(recv_ts)
    payload["events"]["known_at"] = _iso(decision_at - timedelta(hours=1))
    payload["account"]["as_of"] = _iso(decision_at - timedelta(seconds=30))
    payload["account"]["received_at"] = _iso(recv_ts)


def _fixture(*, symbol: str = "MSFT") -> dict:
    event_ts = DECISION_AT - timedelta(seconds=10)
    recv_ts = DECISION_AT - timedelta(seconds=5)
    snapshot_id = "market-snapshot-001"
    contract_key = _contract_key(
        symbol=symbol,
        expiration="2026-10-15",
        strike="95.00",
    )
    return {
        "decision": {"at": _iso(DECISION_AT)},
        "provenance": {
            "provider": "licensed-feed",
            "dataset": "us-options-nbbo",
            "license_mode": "licensed",
            "permitted_use": "internal_research",
            "data_mode": "realtime",
            "snapshot_id": snapshot_id,
            "as_of": _iso(event_ts),
            "received_at": _iso(recv_ts),
            "source_hash": "a" * 64,
        },
        "contract": {
            "underlying": symbol,
            "option_type": "put",
            "standard": True,
            "adjusted": False,
            "multiplier": 100,
            "strike": "95.00",
            "expiration": "2026-10-15",
            "contract_key": contract_key,
        },
        "quote": {
            "option": {
                "contract_key": contract_key,
                "snapshot_id": snapshot_id,
                "bid": "2.00",
                "ask": "2.10",
                "mid": "999999.99",
                "bid_size": 10,
                "ask_size": 12,
                "open_interest": 500,
                "volume": 50,
                "event_ts": _iso(event_ts),
                "recv_ts": _iso(recv_ts),
            },
            "underlying": {
                "symbol": symbol,
                "snapshot_id": snapshot_id,
                "price": "100.00",
                "event_ts": _iso(event_ts),
                "recv_ts": _iso(recv_ts),
            },
            "derived": {
                "contract_key": contract_key,
                "snapshot_id": snapshot_id,
                "delta": "-0.20",
            },
        },
        "events": {
            "underlying": symbol,
            "calendar_status": "ready",
            "known_at": _iso(DECISION_AT - timedelta(hours=1)),
            "coverage_through": "2026-10-15",
            "earnings": {"status": "none"},
            "corporate_action": {"status": "clear"},
            "occ_adjustment": {"status": "none"},
        },
        "macro": {
            "status": "ready",
            "data_status": "ok",
            "abstain": False,
            "action": "observe",
        },
        "policy": {
            "status": "ready",
            # This manual planning figure must never substitute for broker cash.
            "manual_research_budget_usd": "1000000.00",
            "underlyings": {
                symbol: {
                    "whitelisted": True,
                    "product_type": "stock" if symbol != "SPY" else "etf",
                    "leveraged": False,
                    "inverse": False,
                    "volatility_linked": False,
                    "decision": "willing",
                    "max_assignment_price_usd": "100.00",
                }
            },
            "risk_limits": {
                "minimum_cash_buffer_usd": "5000.00",
                "max_total_cash_secured_usd": "30000.00",
                "max_post_assignment_underlying_ratio": "0.30",
                "max_new_contracts_per_week": 3,
                "effective_for_macro_action": "observe",
            },
        },
        "account": {
            "status": "ready",
            "as_of": _iso(DECISION_AT - timedelta(seconds=30)),
            "received_at": _iso(DECISION_AT - timedelta(seconds=5)),
            "currency": "USD",
            "cash_secured_put_permission": True,
            "available_cash_usd": "30000.00",
            "buying_power_usd": "30000.00",
            "net_liquidation_value_usd": "100000.00",
            "new_contracts_opened_this_week": 1,
            "positions_complete": True,
            "existing_options_complete": True,
            "positions": {symbol: {"market_value_usd": "10000.00"}},
            "existing_options": {
                "existing-contract": {
                    "underlying": "SPY",
                    "position_type": "cash_secured_short_put",
                    "gross_cash_reserved_usd": "5000.00",
                }
            },
        },
    }


def _codes(result: dict) -> set[str]:
    return {item["code"] for item in result["rejections"]}


def _evaluate(payload: dict) -> dict:
    return options_strategy.evaluate_cash_secured_put(payload)


class PassingContractTests(unittest.TestCase):
    def test_exact_one_contract_metrics_and_fixed_safety_flags(self) -> None:
        payload = _fixture()
        result = _evaluate(payload)

        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["method_version"], "cash-secured-put-gate-v1")
        self.assertEqual(result["status"], "research_candidate")
        self.assertEqual(result["mode"], "research_only")
        self.assertEqual(result["research_context"], "current_research")
        self.assertTrue(result["is_current_data"])
        self.assertTrue(result["human_review_required"])
        self.assertFalse(result["automatic_execution"])
        self.assertFalse(result["trade_execution_available"])
        self.assertEqual(result["rejections"], [])
        self.assertEqual(result["risk_budget"], "standard")
        self.assertEqual(result["contract"]["dte_calendar_days"], 44)
        self.assertEqual(
            result["contract"]["contract_key"],
            "US:MSFT:2026-10-15:P:95.00:100",
        )
        self.assertEqual(
            result["evidence"]["market_data"]["snapshot_id"],
            "market-snapshot-001",
        )
        self.assertEqual(result["evidence"]["market_data"]["source_hash"], "a" * 64)
        self.assertEqual(
            result["evidence"]["decision_at"], "2026-09-01T14:00:00+00:00"
        )

        metrics = result["one_contract_metrics"]
        self.assertEqual(metrics["gross_cash_reserved_usd"], "9500.00")
        self.assertEqual(metrics["premium_at_bid_usd"], "200.00")
        self.assertEqual(metrics["breakeven_usd_per_share"], "93.00")
        self.assertEqual(metrics["max_profit_usd"], "200.00")
        self.assertEqual(metrics["stock_zero_loss_usd"], "9300.00")
        self.assertEqual(metrics["breakeven_cushion_ratio"], "0.070000")
        self.assertEqual(metrics["breakeven_cushion_bps"], "700.00")
        self.assertEqual(
            metrics["simple_annualized_premium_yield_ratio"], "0.174641"
        )
        self.assertEqual(
            metrics["simple_annualized_premium_yield_bps"], "1746.41"
        )
        self.assertEqual(metrics["spread_ratio"], "0.050000")
        self.assertEqual(metrics["spread_bps"], "500.00")
        self.assertEqual(metrics["post_assignment_underlying_ratio"], "0.195000")
        self.assertEqual(result["derived"]["delta"], "-0.200000")
        self.assertEqual(
            result["derived"]["contract_key"],
            "US:MSFT:2026-10-15:P:95.00:100",
        )
        self.assertEqual(result["derived"]["snapshot_id"], "market-snapshot-001")
        self.assertFalse(result["derived"]["delta_is_probability"])
        self.assertTrue(any("不是 CAGR" in note for note in result["metric_notes"]))
        self.assertTrue(any("不是到期获利概率" in note for note in result["metric_notes"]))

    def test_mid_is_never_used_and_output_has_no_execution_or_size_advice(self) -> None:
        low_mid = _fixture()
        high_mid = _fixture()
        low_mid["quote"]["option"]["mid"] = "0.01"
        high_mid["quote"]["option"]["mid"] = "999999999.00"

        low = _evaluate(low_mid)
        high = _evaluate(high_mid)

        self.assertEqual(low["one_contract_metrics"], high["one_contract_metrics"])
        encoded = json.dumps(low, ensure_ascii=False, sort_keys=True).lower()
        self.assertNotIn('"mid"', encoded)
        self.assertNotIn('"order"', encoded)
        self.assertNotIn('"quantity"', encoded)
        self.assertNotIn('"contracts"', encoded)
        self.assertNotIn("market order", encoded)

    def test_input_is_not_mutated(self) -> None:
        payload = _fixture()
        original = deepcopy(payload)
        _evaluate(payload)
        self.assertEqual(payload, original)

    def test_delta_is_required_and_never_inferred(self) -> None:
        payload = _fixture()
        del payload["quote"]["derived"]["delta"]
        result = _evaluate(payload)
        self.assertEqual(result["status"], "rejected")
        self.assertIn("delta_invalid", _codes(result))
        self.assertNotIn("one_contract_metrics", result)

    def test_delayed_and_eod_are_never_described_as_current(self) -> None:
        for mode, event_age in (("delayed", 15 * 60), ("eod", 18 * 60 * 60)):
            with self.subTest(mode=mode):
                payload = _fixture()
                event_ts = DECISION_AT - timedelta(seconds=event_age)
                recv_ts = DECISION_AT - timedelta(seconds=5)
                payload["provenance"]["data_mode"] = mode
                payload["provenance"]["as_of"] = _iso(event_ts)
                payload["provenance"]["received_at"] = _iso(recv_ts)
                for key in ("option", "underlying"):
                    payload["quote"][key]["event_ts"] = _iso(event_ts)
                    payload["quote"][key]["recv_ts"] = _iso(recv_ts)

                result = _evaluate(payload)

                self.assertEqual(result["status"], "research_candidate")
                self.assertEqual(result["research_context"], "non_current_research")
                self.assertFalse(result["is_current_data"])
                self.assertNotIn("当前机会", json.dumps(result, ensure_ascii=False))


class ProvenanceGateTests(unittest.TestCase):
    def test_every_provenance_field_is_required(self) -> None:
        for field in (
            "provider",
            "dataset",
            "license_mode",
            "permitted_use",
            "data_mode",
            "snapshot_id",
            "as_of",
            "received_at",
            "source_hash",
        ):
            with self.subTest(field=field):
                payload = _fixture()
                del payload["provenance"][field]
                result = _evaluate(payload)
                self.assertEqual(result["status"], "rejected")
                self.assertIn("provenance_fields_missing", _codes(result))
                self.assertNotIn("one_contract_metrics", result)

    def test_license_use_mode_identity_and_hash_fail_closed(self) -> None:
        cases = (
            ("license_mode", "scraped_unknown", "provenance_license_invalid"),
            ("permitted_use", "redistribution_only", "provenance_use_not_permitted"),
            ("data_mode", "live-ish", "data_mode_invalid"),
            ("provider", "<provider-secret>", "provenance_identity_invalid"),
            ("source_hash", "short", "source_hash_invalid"),
        )
        for field, value, expected in cases:
            with self.subTest(field=field):
                payload = _fixture()
                payload["provenance"][field] = value
                result = _evaluate(payload)
                self.assertIn(expected, _codes(result))
                self.assertNotIn(str(value), json.dumps(result, ensure_ascii=False))

    def test_snapshot_identity_is_required_and_safely_rejected(self) -> None:
        for value in (None, "<bad-snapshot>", "x" * 81):
            with self.subTest(value=value):
                payload = _fixture()
                payload["provenance"]["snapshot_id"] = value
                result = _evaluate(payload)
                self.assertIn("market_snapshot_identity_invalid", _codes(result))
                self.assertNotIn("one_contract_metrics", result)

    def test_future_reversed_and_naive_source_times_reject(self) -> None:
        reversed_times = _fixture()
        reversed_times["provenance"]["as_of"] = _iso(
            DECISION_AT - timedelta(seconds=1)
        )
        reversed_times["provenance"]["received_at"] = _iso(
            DECISION_AT - timedelta(seconds=2)
        )
        reversed_result = _evaluate(reversed_times)
        self.assertIn("provenance_time_order", _codes(reversed_result))
        self.assertFalse(reversed_result["is_current_data"])
        self.assertEqual(
            reversed_result["research_context"], "unavailable_research"
        )

        future = _fixture()
        future["provenance"]["received_at"] = _iso(
            DECISION_AT + timedelta(seconds=1)
        )
        future_result = _evaluate(future)
        self.assertIn("provenance_future_time", _codes(future_result))
        self.assertFalse(future_result["is_current_data"])

        naive = _fixture()
        naive["provenance"]["as_of"] = "2026-09-01T13:59:50"
        naive_result = _evaluate(naive)
        self.assertIn("provenance_time_invalid", _codes(naive_result))
        self.assertFalse(naive_result["is_current_data"])

    def test_non_mapping_and_unhashable_values_do_not_raise(self) -> None:
        self.assertEqual(_evaluate([])["status"], "rejected")  # type: ignore[arg-type]
        payload = _fixture()
        payload["provenance"]["data_mode"] = {"malicious": True}
        payload["provenance"]["license_mode"] = ["licensed"]
        payload["macro"]["action"] = {"action": "observe"}
        result = _evaluate(payload)
        self.assertEqual(result["status"], "rejected")
        self.assertIn("data_mode_invalid", _codes(result))
        self.assertIn("provenance_license_invalid", _codes(result))
        self.assertIn("macro_action_unknown", _codes(result))

    def test_missing_sections_fail_closed_without_computing_metrics(self) -> None:
        result = _evaluate({})
        self.assertEqual(result["status"], "rejected")
        self.assertIn("section_missing", _codes(result))
        self.assertNotIn("one_contract_metrics", result)


class IdentityBindingGateTests(unittest.TestCase):
    def test_contract_key_must_be_canonical_and_quote_must_match_it(self) -> None:
        contract_mismatch = _fixture()
        contract_mismatch["contract"]["contract_key"] = (
            "US:MSFT:2026-10-15:P:96.00:100"
        )
        result = _evaluate(contract_mismatch)
        self.assertIn("contract_identity_invalid", _codes(result))
        self.assertNotIn("one_contract_metrics", result)

        quote_mismatch = _fixture()
        quote_mismatch["quote"]["option"]["contract_key"] = (
            "US:MSFT:2026-10-15:P:94.00:100"
        )
        result = _evaluate(quote_mismatch)
        self.assertIn("option_quote_contract_mismatch", _codes(result))
        self.assertNotIn("one_contract_metrics", result)

    def test_underlying_and_event_symbols_must_match_contract(self) -> None:
        quote_mismatch = _fixture()
        quote_mismatch["quote"]["underlying"]["symbol"] = "AAPL"
        self.assertIn(
            "underlying_quote_symbol_mismatch",
            _codes(_evaluate(quote_mismatch)),
        )

        event_mismatch = _fixture()
        event_mismatch["events"]["underlying"] = "AAPL"
        self.assertIn(
            "event_underlying_mismatch",
            _codes(_evaluate(event_mismatch)),
        )

    def test_both_quotes_must_match_provenance_snapshot(self) -> None:
        for section in ("option", "underlying"):
            with self.subTest(section=section):
                payload = _fixture()
                payload["quote"][section]["snapshot_id"] = "other-snapshot"
                result = _evaluate(payload)
                self.assertIn("market_snapshot_mismatch", _codes(result))
                self.assertNotIn("one_contract_metrics", result)

        both_wrong = _fixture()
        for section in ("option", "underlying"):
            both_wrong["quote"][section]["snapshot_id"] = "shared-but-wrong"
        self.assertIn("market_snapshot_mismatch", _codes(_evaluate(both_wrong)))

    def test_derived_delta_must_match_contract_and_snapshot(self) -> None:
        for field, value in (
            ("contract_key", "US:MSFT:2026-10-15:P:94.00:100"),
            ("snapshot_id", "other-snapshot"),
        ):
            with self.subTest(field=field):
                payload = _fixture()
                payload["quote"]["derived"][field] = value
                result = _evaluate(payload)
                self.assertIn("derived_identity_mismatch", _codes(result))
                self.assertFalse(result["is_current_data"])
                self.assertEqual(
                    result["research_context"], "unavailable_research"
                )
                self.assertNotIn("one_contract_metrics", result)


class ContractAndPolicyGateTests(unittest.TestCase):
    def test_contract_type_standard_adjustment_and_multiplier(self) -> None:
        cases = (
            ("option_type", "call", "option_type_not_put"),
            ("standard", False, "non_standard_contract"),
            ("adjusted", True, "adjusted_contract"),
            ("multiplier", 99, "multiplier_not_100"),
            ("multiplier", True, "multiplier_not_100"),
            ("strike", "0", "strike_invalid"),
            ("strike", True, "strike_invalid"),
        )
        for field, value, expected in cases:
            with self.subTest(field=field, value=value):
                payload = _fixture()
                payload["contract"][field] = value
                result = _evaluate(payload)
                self.assertIn(expected, _codes(result))
                self.assertNotIn("one_contract_metrics", result)

    def test_calendar_dte_inclusive_boundaries_and_rejections(self) -> None:
        for expiration in ("2026-09-22", "2026-10-31"):
            with self.subTest(expiration=expiration):
                payload = _fixture()
                payload["contract"]["expiration"] = expiration
                _sync_identity(payload)
                self.assertEqual(_evaluate(payload)["status"], "research_candidate")
        for expiration in ("2026-09-21", "2026-11-01"):
            with self.subTest(expiration=expiration):
                payload = _fixture()
                payload["contract"]["expiration"] = expiration
                _sync_identity(payload)
                self.assertIn("dte_out_of_range", _codes(_evaluate(payload)))

        malformed = _fixture()
        malformed["contract"]["expiration"] = "2026/10/15"
        self.assertIn("expiration_invalid", _codes(_evaluate(malformed)))

    def test_dte_uses_new_york_calendar_date_near_utc_boundary(self) -> None:
        payload = _fixture()
        _retime(payload, datetime(2026, 9, 2, 0, 30, tzinfo=UTC))
        payload["contract"]["expiration"] = "2026-09-22"
        _sync_identity(payload)

        result = _evaluate(payload)

        self.assertEqual(result["status"], "research_candidate")
        self.assertEqual(result["contract"]["dte_calendar_days"], 21)

    def test_whitelist_and_product_profile_reject_unsafe_underlyings(self) -> None:
        not_listed = _fixture()
        not_listed["contract"]["underlying"] = "AAPL"
        _sync_identity(not_listed)
        self.assertIn("underlying_not_whitelisted", _codes(_evaluate(not_listed)))

        for flag in ("leveraged", "inverse", "volatility_linked"):
            with self.subTest(flag=flag):
                payload = _fixture()
                payload["policy"]["underlyings"]["MSFT"][flag] = True
                result = _evaluate(payload)
                self.assertIn("underlying_product_prohibited", _codes(result))

        known_vix_etp = _fixture(symbol="VXX")
        known_vix_etp["policy"]["underlyings"]["VXX"]["product_type"] = "etf"
        self.assertIn(
            "underlying_product_prohibited", _codes(_evaluate(known_vix_etp))
        )

    def test_willing_decision_and_assignment_price_are_explicit(self) -> None:
        for decision, expected in (
            ("exclude", "asset_policy_excluded"),
            ("unconfirmed", "asset_policy_unconfirmed"),
            (None, "asset_policy_unconfirmed"),
        ):
            with self.subTest(decision=decision):
                payload = _fixture()
                payload["policy"]["underlyings"]["MSFT"]["decision"] = decision
                self.assertIn(expected, _codes(_evaluate(payload)))

        above_limit = _fixture()
        above_limit["policy"]["underlyings"]["MSFT"][
            "max_assignment_price_usd"
        ] = "94.99"
        self.assertIn(
            "strike_above_assignment_price", _codes(_evaluate(above_limit))
        )

        invalid_limit = _fixture()
        invalid_limit["policy"]["underlyings"]["MSFT"][
            "max_assignment_price_usd"
        ] = float("nan")
        self.assertIn(
            "max_assignment_price_invalid", _codes(_evaluate(invalid_limit))
        )

        not_ready = _fixture()
        not_ready["policy"]["status"] = "draft"
        self.assertIn("policy_not_ready", _codes(_evaluate(not_ready)))

    def test_risk_limits_are_strict_and_boolean_is_not_numeric(self) -> None:
        for field, value in (
            ("minimum_cash_buffer_usd", "-0.01"),
            ("max_total_cash_secured_usd", "0"),
            ("max_post_assignment_underlying_ratio", "0"),
            ("max_post_assignment_underlying_ratio", "1.000001"),
            ("minimum_cash_buffer_usd", True),
            ("max_new_contracts_per_week", True),
        ):
            with self.subTest(field=field, value=value):
                payload = _fixture()
                payload["policy"]["risk_limits"][field] = value
                self.assertIn("risk_limits_invalid", _codes(_evaluate(payload)))


class QuoteGateTests(unittest.TestCase):
    def test_missing_or_identity_invalid_quotes_are_never_current(self) -> None:
        for section, expected in (
            ("option", "option_quote_invalid"),
            ("underlying", "underlying_quote_invalid"),
        ):
            with self.subTest(section=section):
                payload = _fixture()
                del payload["quote"][section]
                result = _evaluate(payload)
                self.assertIn(expected, _codes(result))
                self.assertFalse(result["is_current_data"])
                self.assertEqual(
                    result["research_context"], "unavailable_research"
                )

        identity_invalid = _fixture()
        identity_invalid["quote"]["underlying"]["symbol"] = "AAPL"
        result = _evaluate(identity_invalid)
        self.assertIn("underlying_quote_symbol_mismatch", _codes(result))
        self.assertFalse(result["is_current_data"])
        self.assertEqual(result["research_context"], "unavailable_research")

    def test_liquidity_threshold_boundaries_are_inclusive(self) -> None:
        payload = _fixture()
        option = payload["quote"]["option"]
        option.update(
            {
                "bid": "2.00",
                "ask": "2.16",
                "bid_size": 1,
                "ask_size": 1,
                "open_interest": 500,
                "volume": 50,
            }
        )
        result = _evaluate(payload)
        self.assertEqual(result["status"], "research_candidate")
        self.assertEqual(result["one_contract_metrics"]["spread_ratio"], "0.080000")

    def test_each_quote_threshold_fails_closed(self) -> None:
        cases = (
            ("bid", "0", "bid_not_positive"),
            ("ask", "1.99", "crossed_quote"),
            ("bid_size", 0, "bid_size_not_positive"),
            ("ask_size", 0, "ask_size_not_positive"),
            ("ask", "2.17", "spread_too_wide"),
            ("open_interest", 499, "open_interest_too_low"),
            ("volume", 49, "volume_too_low"),
        )
        for field, value, expected in cases:
            with self.subTest(field=field, value=value):
                payload = _fixture()
                payload["quote"]["option"][field] = value
                result = _evaluate(payload)
                self.assertIn(expected, _codes(result))
                self.assertNotIn("one_contract_metrics", result)

    def test_put_premium_cannot_consume_or_exceed_strike(self) -> None:
        cases = (
            ("95.00", "95.00"),
            ("94.50", "95.01"),
        )
        for bid, ask in cases:
            with self.subTest(bid=bid, ask=ask):
                payload = _fixture()
                payload["quote"]["option"]["bid"] = bid
                payload["quote"]["option"]["ask"] = ask
                result = _evaluate(payload)
                self.assertIn("put_premium_out_of_bounds", _codes(result))
                self.assertNotIn("one_contract_metrics", result)

        ask_at_strike = _fixture()
        ask_at_strike["quote"]["option"]["bid"] = "94.50"
        ask_at_strike["quote"]["option"]["ask"] = "95.00"
        self.assertEqual(_evaluate(ask_at_strike)["status"], "research_candidate")

    def test_nan_infinity_and_boolean_pseudo_numbers_are_rejected(self) -> None:
        cases = (
            ("option", "bid", float("nan"), "option_quote_invalid"),
            ("option", "ask", float("inf"), "ask_invalid"),
            ("option", "bid_size", True, "bid_size_not_positive"),
            ("option", "ask_size", False, "ask_size_not_positive"),
            ("option", "open_interest", True, "open_interest_too_low"),
            ("option", "volume", float("-inf"), "volume_too_low"),
            ("underlying", "price", True, "underlying_price_invalid"),
        )
        for section, field, value, expected in cases:
            with self.subTest(section=section, field=field):
                payload = _fixture()
                payload["quote"][section][field] = value
                self.assertIn(expected, _codes(_evaluate(payload)))

        oversized = _fixture()
        oversized["quote"]["option"]["bid"] = "9" * 1000
        self.assertIn("option_quote_invalid", _codes(_evaluate(oversized)))

        exponent_text = _fixture()
        exponent_text["quote"]["option"]["bid"] = "1e999999"
        self.assertIn("option_quote_invalid", _codes(_evaluate(exponent_text)))

    def test_realtime_freshness_is_15_seconds_for_both_quotes(self) -> None:
        for section in ("option", "underlying"):
            with self.subTest(section=section, age="boundary"):
                payload = _fixture()
                payload["quote"][section]["event_ts"] = _iso(
                    DECISION_AT - timedelta(seconds=15)
                )
                self.assertEqual(_evaluate(payload)["status"], "research_candidate")
            with self.subTest(section=section, age="stale"):
                payload = _fixture()
                payload["quote"][section]["event_ts"] = _iso(
                    DECISION_AT - timedelta(seconds=15, microseconds=1)
                )
                result = _evaluate(payload)
                self.assertIn("quote_stale", _codes(result))
                self.assertFalse(result["is_current_data"])
                self.assertEqual(result["research_context"], "unavailable_research")

    def test_future_reversed_and_old_received_quote_times_reject(self) -> None:
        reversed_times = _fixture()
        reversed_times["quote"]["option"]["event_ts"] = _iso(
            DECISION_AT - timedelta(seconds=1)
        )
        reversed_times["quote"]["option"]["recv_ts"] = _iso(
            DECISION_AT - timedelta(seconds=2)
        )
        self.assertIn("quote_time_order", _codes(_evaluate(reversed_times)))

        future = _fixture()
        future["quote"]["underlying"]["recv_ts"] = _iso(
            DECISION_AT + timedelta(microseconds=1)
        )
        self.assertIn("quote_future_time", _codes(_evaluate(future)))

        old_receive = _fixture()
        old_receive["quote"]["option"]["recv_ts"] = _iso(
            DECISION_AT - timedelta(seconds=15, microseconds=1)
        )
        self.assertIn("quote_stale", _codes(_evaluate(old_receive)))

    def test_put_delta_requires_negative_inclusive_range_not_probability(self) -> None:
        for delta in ("-0.10", "-0.30"):
            with self.subTest(delta=delta):
                payload = _fixture()
                payload["quote"]["derived"]["delta"] = delta
                result = _evaluate(payload)
                self.assertEqual(result["status"], "research_candidate")
                self.assertFalse(result["derived"]["delta_is_probability"])
        for delta in ("0.10", "0.30", "-0.099999", "-0.300001", "0"):
            with self.subTest(delta=delta):
                payload = _fixture()
                payload["quote"]["derived"]["delta"] = delta
                self.assertIn("delta_out_of_range", _codes(_evaluate(payload)))


class EventAndMacroGateTests(unittest.TestCase):
    def test_event_calendar_is_point_in_time_safe(self) -> None:
        future_known = _fixture()
        future_known["events"]["known_at"] = _iso(
            DECISION_AT + timedelta(microseconds=1)
        )
        self.assertIn("event_calendar_lookahead", _codes(_evaluate(future_known)))

        not_ready = _fixture()
        not_ready["events"]["calendar_status"] = "partial"
        self.assertIn("event_calendar_not_ready", _codes(_evaluate(not_ready)))

        unknown_earnings = _fixture()
        unknown_earnings["events"]["earnings"]["status"] = "unknown"
        self.assertIn("earnings_status_unknown", _codes(_evaluate(unknown_earnings)))

    def test_event_calendar_age_and_expiration_coverage_fail_closed(self) -> None:
        boundary = _fixture()
        boundary["events"]["known_at"] = _iso(
            DECISION_AT - timedelta(days=4)
        )
        self.assertEqual(_evaluate(boundary)["status"], "research_candidate")

        stale = _fixture()
        stale["events"]["known_at"] = _iso(
            DECISION_AT - timedelta(days=4, microseconds=1)
        )
        self.assertIn("event_calendar_stale", _codes(_evaluate(stale)))

        for coverage in (None, "2026/10/15"):
            with self.subTest(coverage=coverage):
                invalid = _fixture()
                invalid["events"]["coverage_through"] = coverage
                self.assertIn(
                    "event_calendar_coverage_invalid",
                    _codes(_evaluate(invalid)),
                )

        insufficient = _fixture()
        insufficient["events"]["coverage_through"] = "2026-10-14"
        self.assertIn(
            "event_calendar_coverage_insufficient",
            _codes(_evaluate(insufficient)),
        )

    def test_earnings_gate_uses_new_york_expiration_day_end(self) -> None:
        before = _fixture()
        before["events"]["earnings"] = {
            "status": "scheduled",
            "at": "2026-10-16T03:59:59+00:00",
        }
        self.assertIn("earnings_before_expiration", _codes(_evaluate(before)))

        after = _fixture()
        after["events"]["earnings"] = {
            "status": "scheduled",
            "at": "2026-10-16T04:00:00+00:00",
        }
        self.assertEqual(_evaluate(after)["status"], "research_candidate")

    def test_unresolved_corporate_action_and_occ_adjustment_reject(self) -> None:
        corporate = _fixture()
        corporate["events"]["corporate_action"]["status"] = "unresolved"
        self.assertIn("corporate_action_unresolved", _codes(_evaluate(corporate)))

        occ = _fixture()
        occ["events"]["occ_adjustment"]["status"] = "adjusted"
        self.assertIn("occ_adjustment_present", _codes(_evaluate(occ)))

    def test_macro_abstain_unknown_reduce_and_exit_all_reject(self) -> None:
        cases = (
            ("observe", "ready", True, "macro_abstain"),
            ("unknown", "ready", False, "macro_action_unknown"),
            ("reduce_candidate", "blocked", False, "macro_risk_blocked"),
            ("exit_candidate", "blocked", False, "macro_risk_blocked"),
        )
        for action, status, abstain, expected in cases:
            with self.subTest(action=action, status=status, abstain=abstain):
                payload = _fixture()
                payload["macro"]["action"] = action
                payload["macro"]["status"] = status
                payload["macro"]["abstain"] = abstain
                self.assertIn(expected, _codes(_evaluate(payload)))

        unavailable = _fixture()
        unavailable["macro"]["data_status"] = "stale"
        self.assertIn("macro_not_ready", _codes(_evaluate(unavailable)))

    def test_prepare_reduce_only_allows_spy_or_qqq_and_marks_constrained(self) -> None:
        blocked = _fixture()
        blocked["macro"]["action"] = "prepare_reduce"
        blocked["macro"]["status"] = "constrained"
        self.assertIn("macro_prepare_reduce_symbol", _codes(_evaluate(blocked)))

        for symbol in ("SPY", "QQQ"):
            with self.subTest(symbol=symbol):
                payload = _fixture(symbol=symbol)
                payload["policy"]["underlyings"][symbol]["product_type"] = "etf"
                payload["macro"]["action"] = "prepare_reduce"
                payload["macro"]["status"] = "constrained"
                payload["policy"]["risk_limits"][
                    "effective_for_macro_action"
                ] = "prepare_reduce"
                result = _evaluate(payload)
                self.assertEqual(result["status"], "research_candidate")
                self.assertEqual(result["risk_budget"], "constrained")

    def test_macro_status_must_exactly_match_action(self) -> None:
        cases = (
            ("observe", "constrained"),
            ("prepare_reduce", "ready"),
            ("reduce_candidate", "ready"),
            ("exit_candidate", "ready"),
        )
        for action, status in cases:
            with self.subTest(action=action, status=status):
                payload = _fixture(symbol="SPY" if action == "prepare_reduce" else "MSFT")
                if action == "prepare_reduce":
                    payload["policy"]["underlyings"]["SPY"]["product_type"] = "etf"
                    payload["policy"]["risk_limits"][
                        "effective_for_macro_action"
                    ] = "prepare_reduce"
                payload["macro"]["action"] = action
                payload["macro"]["status"] = status
                self.assertIn("macro_not_ready", _codes(_evaluate(payload)))


class AccountGateTests(unittest.TestCase):
    def test_account_time_must_be_ordered_nonfuture_and_at_most_60_seconds_old(self) -> None:
        boundary = _fixture()
        boundary["account"]["as_of"] = _iso(DECISION_AT - timedelta(seconds=60))
        self.assertEqual(_evaluate(boundary)["status"], "research_candidate")

        stale = _fixture()
        stale["account"]["as_of"] = _iso(
            DECISION_AT - timedelta(seconds=60, microseconds=1)
        )
        self.assertIn("account_stale", _codes(_evaluate(stale)))

        future = _fixture()
        future["account"]["received_at"] = _iso(
            DECISION_AT + timedelta(microseconds=1)
        )
        self.assertIn("account_future_time", _codes(_evaluate(future)))

        reversed_times = _fixture()
        reversed_times["account"]["as_of"] = _iso(
            DECISION_AT - timedelta(seconds=1)
        )
        reversed_times["account"]["received_at"] = _iso(
            DECISION_AT - timedelta(seconds=2)
        )
        self.assertIn("account_time_order", _codes(_evaluate(reversed_times)))

    def test_permission_positions_and_existing_options_are_semantically_complete(self) -> None:
        permission = _fixture()
        permission["account"]["cash_secured_put_permission"] = False
        self.assertIn("options_permission_missing", _codes(_evaluate(permission)))

        positions = _fixture()
        positions["account"]["positions_complete"] = False
        self.assertIn("positions_incomplete", _codes(_evaluate(positions)))

        bad_position = _fixture()
        bad_position["account"]["positions"]["MSFT"]["market_value_usd"] = True
        self.assertIn("positions_incomplete", _codes(_evaluate(bad_position)))

        existing = _fixture()
        existing["account"]["existing_options_complete"] = False
        self.assertIn("existing_options_incomplete", _codes(_evaluate(existing)))

        bad_existing = _fixture()
        bad_existing["account"]["existing_options"]["existing-contract"][
            "gross_cash_reserved_usd"
        ] = float("inf")
        self.assertIn("existing_options_incomplete", _codes(_evaluate(bad_existing)))

        ambiguous_existing = _fixture()
        item = ambiguous_existing["account"]["existing_options"][
            "existing-contract"
        ]
        item["strategy"] = "long_put"
        self.assertIn(
            "existing_options_incomplete",
            _codes(_evaluate(ambiguous_existing)),
        )

        not_ready = _fixture()
        not_ready["account"]["status"] = "partial"
        self.assertIn("account_not_ready", _codes(_evaluate(not_ready)))

        wrong_currency = _fixture()
        wrong_currency["account"]["currency"] = "CNY"
        self.assertIn("account_currency_not_usd", _codes(_evaluate(wrong_currency)))

    def test_full_gross_reserve_checks_cash_buying_power_buffer_and_total(self) -> None:
        cases = (
            ("available_cash_usd", "9499.99", "available_cash_insufficient"),
            ("buying_power_usd", "9499.99", "buying_power_insufficient"),
            ("available_cash_usd", "14499.99", "cash_buffer_breached"),
        )
        for field, value, expected in cases:
            with self.subTest(field=field, value=value):
                payload = _fixture()
                payload["account"][field] = value
                self.assertIn(expected, _codes(_evaluate(payload)))

        total = _fixture()
        total["account"]["existing_options"]["existing-contract"][
            "gross_cash_reserved_usd"
        ] = "20500.01"
        self.assertIn("total_collateral_limit_breached", _codes(_evaluate(total)))

        total_boundary = _fixture()
        total_boundary["account"]["existing_options"]["existing-contract"][
            "gross_cash_reserved_usd"
        ] = "20500.00"
        self.assertEqual(_evaluate(total_boundary)["status"], "research_candidate")

    def test_weekly_new_contract_limit_is_consumed_without_size_advice(self) -> None:
        boundary = _fixture()
        boundary["account"]["new_contracts_opened_this_week"] = 2
        self.assertEqual(_evaluate(boundary)["status"], "research_candidate")

        reached = _fixture()
        reached["account"]["new_contracts_opened_this_week"] = 3
        result = _evaluate(reached)
        self.assertIn("weekly_new_contract_limit_breached", _codes(result))
        self.assertNotIn("one_contract_metrics", result)

        invalid = _fixture()
        invalid["account"]["new_contracts_opened_this_week"] = True
        self.assertIn(
            "weekly_new_contract_count_invalid", _codes(_evaluate(invalid))
        )

    def test_effective_risk_limits_must_match_macro_action(self) -> None:
        payload = _fixture(symbol="SPY")
        payload["policy"]["underlyings"]["SPY"]["product_type"] = "etf"
        payload["macro"]["action"] = "prepare_reduce"
        payload["macro"]["status"] = "constrained"
        result = _evaluate(payload)
        self.assertIn("risk_limits_macro_mode_mismatch", _codes(result))
        self.assertNotIn("one_contract_metrics", result)

    def test_post_assignment_concentration_is_checked_at_inclusive_limit(self) -> None:
        boundary = _fixture()
        boundary["account"]["positions"]["MSFT"]["market_value_usd"] = "20500.00"
        self.assertEqual(_evaluate(boundary)["status"], "research_candidate")

        over = _fixture()
        over["account"]["positions"]["MSFT"]["market_value_usd"] = "20500.01"
        self.assertIn(
            "post_assignment_concentration_breached", _codes(_evaluate(over))
        )

    def test_existing_target_short_put_assignment_exposure_is_included(self) -> None:
        boundary = _fixture()
        existing = boundary["account"]["existing_options"]["existing-contract"]
        existing["underlying"] = "MSFT"
        boundary["account"]["positions"]["MSFT"][
            "market_value_usd"
        ] = "15500.00"
        result = _evaluate(boundary)
        self.assertEqual(result["status"], "research_candidate")
        self.assertEqual(
            result["one_contract_metrics"][
                "post_assignment_underlying_ratio"
            ],
            "0.300000",
        )

        over = _fixture()
        existing = over["account"]["existing_options"]["existing-contract"]
        existing["underlying"] = "MSFT"
        del existing["position_type"]
        over["account"]["positions"]["MSFT"][
            "market_value_usd"
        ] = "15500.01"
        self.assertIn(
            "post_assignment_concentration_breached",
            _codes(_evaluate(over)),
        )

    def test_manual_research_budget_never_substitutes_for_broker_cash(self) -> None:
        payload = _fixture()
        payload["policy"]["manual_research_budget_usd"] = "999999999.00"
        del payload["account"]["available_cash_usd"]
        result = _evaluate(payload)
        self.assertEqual(result["status"], "rejected")
        self.assertIn("account_balances_invalid", _codes(result))
        self.assertNotIn("one_contract_metrics", result)

    def test_account_nan_infinity_and_boolean_pseudo_numbers_reject(self) -> None:
        for field, value in (
            ("available_cash_usd", True),
            ("buying_power_usd", float("nan")),
            ("net_liquidation_value_usd", float("inf")),
        ):
            with self.subTest(field=field):
                payload = _fixture()
                payload["account"][field] = value
                self.assertIn("account_balances_invalid", _codes(_evaluate(payload)))


class FailurePrivacyTests(unittest.TestCase):
    def test_rejections_are_stable_and_do_not_echo_malicious_values(self) -> None:
        payload = _fixture()
        malicious = "<script>steal-account-SECRET-937451</script>"
        payload["provenance"]["provider"] = malicious
        payload["contract"]["underlying"] = malicious
        payload["account"]["positions"] = {
            malicious: {"market_value_usd": "123456789.12"}
        }
        result = _evaluate(payload)
        encoded = json.dumps(result, ensure_ascii=False, sort_keys=True)

        self.assertEqual(result["status"], "rejected")
        self.assertNotIn(malicious, encoded)
        self.assertNotIn("123456789.12", encoded)
        self.assertTrue(
            all(set(item) == {"code", "label", "detail"} for item in result["rejections"])
        )
        self.assertEqual(
            next(
                item for item in result["rejections"]
                if item["code"] == "provenance_identity_invalid"
            ),
            {
                "code": "provenance_identity_invalid",
                "label": "数据来源标识不可核验",
                "detail": "来源与数据集必须使用受限长度的可审计标识。",
            },
        )


if __name__ == "__main__":
    unittest.main()
