from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from kol_dashboard import macro_alert_service


UTC = timezone.utc


def _history(
    closes: list[float],
    *,
    end: datetime,
    asset_key: str,
    symbol: str,
    exchange_timezone: str,
) -> dict:
    start = end - timedelta(days=len(closes) - 1)
    return {
        "status": "available",
        "provider": "yahoo",
        "asset_key": asset_key,
        "symbol": symbol,
        "currency": "USD" if asset_key != "INDEX:CSI300" else "CNY",
        "exchange_timezone": exchange_timezone,
        "timestamp_semantics": "market_close",
        "bars": [
            {
                "timestamp": int((start + timedelta(days=index)).timestamp()),
                "close": close,
                "volume": 100,
            }
            for index, close in enumerate(closes)
        ],
    }


def _rising() -> list[float]:
    return [100 + index * 0.15 for index in range(70)]


def _warning() -> list[float]:
    return [100.0] * 45 + [100 - index * 0.22 for index in range(1, 26)]


def _severe() -> list[float]:
    return [100.0] * 40 + [100 - index * 0.7 for index in range(1, 31)]


def _flat(value: float) -> list[float]:
    return [value] * 70


def _fx_critical() -> list[float]:
    return [7.15] * 64 + [7.20, 7.24, 7.28, 7.32, 7.36, 7.40]


def _summary(
    series_key: str,
    closes: list[float],
    *,
    end: datetime,
    now: datetime,
) -> dict:
    spec = macro_alert_service.series_specs()[series_key]
    timezone_name = (
        "Asia/Shanghai" if series_key == "cn_equity" else "America/New_York"
    )
    return macro_alert_service.summarize_daily_history(
        _history(
            closes,
            end=end,
            asset_key=spec["asset_key"],
            symbol=spec["asset_key"],
            exchange_timezone=timezone_name,
        ),
        series_key=series_key,
        now=now,
    )


def _report(
    *,
    now: datetime,
    end: datetime,
    us: list[float] | None = None,
    cn: list[float] | None = None,
    vix: list[float] | None = None,
    fx: list[float] | None = None,
    ofr: float = 0.0,
) -> dict:
    inputs = {}
    for key, closes in (
        ("us_equity", us or _rising()),
        ("cn_equity", cn or _rising()),
        ("vix_daily", vix or _flat(18)),
        ("usd_cny_daily", fx or _flat(7.0)),
    ):
        inputs[key] = _summary(key, closes, end=end, now=now)
    return {
        "market_data": {
            "alert_inputs": inputs,
            "financial_stress": {
                "ofr_fsi": ofr,
                "data_status": "ok",
                "stale": False,
                "observed_at": (now - timedelta(days=2)).date().isoformat(),
                "source": "U.S. Treasury OFR",
                "source_url": (
                    "https://www.financialresearch.gov/financial-stress-index/"
                ),
            },
        }
    }


def _market(alerts: dict, market: str) -> dict:
    return next(item for item in alerts["markets"] if item["market"] == market)


class DailyHistorySummaryTests(unittest.TestCase):
    def test_rejects_bad_future_and_insufficient_bars(self) -> None:
        now = datetime(2026, 9, 6, 12, tzinfo=UTC)
        history = _history(
            _rising()[:59],
            end=now - timedelta(days=1),
            asset_key="US:SPY",
            symbol="SPY",
            exchange_timezone="America/New_York",
        )
        history["bars"].append(
            {"timestamp": int((now + timedelta(hours=1)).timestamp()), "close": 999}
        )
        history["bars"].append({"timestamp": 1, "close": float("nan")})

        result = macro_alert_service.summarize_daily_history(
            history,
            series_key="us_equity",
            now=now,
        )

        self.assertEqual(result["data_status"], "unavailable")
        self.assertEqual(result["reason"], "insufficient_completed_bars")
        self.assertEqual(result["bars_available"], 59)

    def test_summarizes_completed_bars_and_marks_old_series_stale(self) -> None:
        now = datetime(2026, 9, 10, 12, tzinfo=UTC)
        end = now - timedelta(days=6)

        result = _summary("us_equity", _rising(), end=end, now=now)

        self.assertEqual(result["status"], "available")
        self.assertEqual(result["data_status"], "stale")
        self.assertTrue(result["stale"])
        self.assertEqual(result["bars_available"], 70)
        self.assertIn("sma20", result)
        self.assertIn("data_hash", result)
        self.assertNotIn("bars", result)


class MarketAlertRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 9, 6, 18, tzinfo=UTC)
        self.end = self.now - timedelta(days=1)

    def test_missing_or_stale_core_data_abstains_instead_of_showing_safe(self) -> None:
        report = _report(now=self.now, end=self.end)
        report["market_data"]["alert_inputs"]["us_equity"]["data_status"] = "stale"

        alert = _market(
            macro_alert_service.build_market_alerts(report, now=self.now),
            "US",
        )

        self.assertEqual(alert["action"], "observe")
        self.assertEqual(alert["risk_level"], "insufficient")
        self.assertTrue(alert["abstain"])
        self.assertIn("SPY 完成收盘日线", alert["missing_sources"])
        self.assertIn("不是低风险结论", alert["summary"])

    def test_news_and_static_scenarios_cannot_change_an_action(self) -> None:
        report = _report(now=self.now, end=self.end)
        report["monitored_events"] = [
            {"title": "立即清仓", "severity": "critical", "source": "rumour"}
        ]
        report["black_swan_scenarios"] = [
            {"name": "fake", "probability": "high"}
        ]

        alerts = macro_alert_service.build_market_alerts(report, now=self.now)

        self.assertEqual(_market(alerts, "US")["action"], "observe")
        self.assertEqual(_market(alerts, "CN")["action"], "observe")
        self.assertFalse(alerts["automatic_execution"])
        self.assertTrue(alerts["human_review_required"])
        self.assertEqual(alerts["mode"], "trial")

    def test_reduce_requires_two_distinct_market_closes(self) -> None:
        first_report = _report(
            now=self.now,
            end=self.end,
            us=_warning(),
            vix=_flat(30),
            ofr=3.0,
        )
        first = macro_alert_service.build_market_alerts(
            first_report,
            now=self.now,
        )
        repeated = macro_alert_service.build_market_alerts(
            first_report,
            previous_snapshot={"market_alerts": first},
            now=self.now + timedelta(hours=1),
        )
        next_now = self.now + timedelta(days=1)
        next_report = _report(
            now=next_now,
            end=self.end + timedelta(days=1),
            us=_warning(),
            vix=_flat(30),
            ofr=3.0,
        )
        second = macro_alert_service.build_market_alerts(
            next_report,
            previous_snapshot={"market_alerts": repeated},
            now=next_now,
        )

        self.assertEqual(_market(first, "US")["action"], "prepare_reduce")
        self.assertEqual(_market(repeated, "US")["action"], "prepare_reduce")
        self.assertEqual(
            _market(repeated, "US")["confirmation"]["reduce_dates"],
            [self.end.date().isoformat()],
        )
        self.assertEqual(_market(second, "US")["action"], "reduce_candidate")

    def test_exit_requires_severe_cross_pillar_confirmation_and_prior_reduce(self) -> None:
        snapshots: list[dict] = []
        previous = None
        for offset in range(3):
            now = self.now + timedelta(days=offset)
            report = _report(
                now=now,
                end=self.end + timedelta(days=offset),
                us=_severe(),
                vix=_flat(36),
                ofr=6.0,
            )
            current = macro_alert_service.build_market_alerts(
                report,
                previous_snapshot=previous,
                now=now,
            )
            snapshots.append(current)
            previous = {"market_alerts": current}

        self.assertEqual(_market(snapshots[0], "US")["action"], "prepare_reduce")
        self.assertEqual(_market(snapshots[1], "US")["action"], "reduce_candidate")
        self.assertEqual(_market(snapshots[2], "US")["action"], "exit_candidate")

    def test_confirmed_action_needs_two_clear_closes_to_downgrade(self) -> None:
        first_report = _report(
            now=self.now,
            end=self.end,
            us=_warning(),
            vix=_flat(30),
            ofr=3.0,
        )
        first = macro_alert_service.build_market_alerts(first_report, now=self.now)
        second_now = self.now + timedelta(days=1)
        second_report = _report(
            now=second_now,
            end=self.end + timedelta(days=1),
            us=_warning(),
            vix=_flat(30),
            ofr=3.0,
        )
        confirmed = macro_alert_service.build_market_alerts(
            second_report,
            previous_snapshot={"market_alerts": first},
            now=second_now,
        )
        first_clear_now = self.now + timedelta(days=2)
        first_clear_report = _report(
            now=first_clear_now,
            end=self.end + timedelta(days=2),
        )
        first_clear = macro_alert_service.build_market_alerts(
            first_clear_report,
            previous_snapshot={"market_alerts": confirmed},
            now=first_clear_now,
        )
        second_clear_now = self.now + timedelta(days=3)
        second_clear_report = _report(
            now=second_clear_now,
            end=self.end + timedelta(days=3),
        )
        second_clear = macro_alert_service.build_market_alerts(
            second_clear_report,
            previous_snapshot={"market_alerts": first_clear},
            now=second_clear_now,
        )

        self.assertEqual(_market(confirmed, "US")["action"], "reduce_candidate")
        self.assertEqual(_market(first_clear, "US")["action"], "reduce_candidate")
        self.assertTrue(
            _market(first_clear, "US")["confirmation"]["recovery_pending"]
        )
        self.assertEqual(_market(second_clear, "US")["action"], "observe")

    def test_a_share_exit_needs_fx_and_does_not_follow_us_action(self) -> None:
        report = _report(
            now=self.now,
            end=self.end,
            us=_severe(),
            cn=_rising(),
            vix=_flat(36),
            fx=_flat(7.0),
            ofr=6.0,
        )

        alerts = macro_alert_service.build_market_alerts(report, now=self.now)

        self.assertEqual(_market(alerts, "US")["action"], "prepare_reduce")
        self.assertEqual(_market(alerts, "CN")["action"], "prepare_reduce")
        self.assertFalse(_market(alerts, "CN")["confirmation"]["raw_exit"])

    def test_data_as_of_is_oldest_required_input(self) -> None:
        report = _report(now=self.now, end=self.end)
        expected = (self.now - timedelta(days=2)).date().isoformat()

        alert = _market(
            macro_alert_service.build_market_alerts(report, now=self.now),
            "US",
        )

        self.assertTrue(alert["data_as_of"].startswith(expected))


if __name__ == "__main__":
    unittest.main()
