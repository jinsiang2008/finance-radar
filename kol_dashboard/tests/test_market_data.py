from __future__ import annotations

import importlib
import json
import unittest
from datetime import datetime, timezone


try:
    market_data = importlib.import_module("kol_dashboard.market_data")
except ModuleNotFoundError:
    market_data = None


class _Response:
    def __init__(self, body: bytes):
        self.body = body

    def read(self) -> bytes:
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class ProviderSymbolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(market_data, "kol_dashboard.market_data is required")

    def test_maps_supported_assets_without_guessing_unknowns(self) -> None:
        cases = {
            ("US:BRK.B", "yahoo"): "BRK-B",
            ("CN:600519", "yahoo"): "600519.SS",
            ("CN:000001", "yahoo"): "000001.SZ",
            ("CN:430047", "yahoo"): "430047.BJ",
            ("CN:600519", "tencent"): "sh600519",
            ("CN:000001", "tencent"): "sz000001",
            ("CN:430047", "tencent"): "bj430047",
            ("CRYPTO:ETH", "yahoo"): "ETH-USD",
            ("COMMODITY:GOLD", "yahoo"): "GC=F",
            ("COMMODITY:OIL", "yahoo"): "CL=F",
            ("FX:DXY", "yahoo"): "DX-Y.NYB",
            ("FX:USD/CNY", "yahoo"): "CNY=X",
            ("INDEX:VIX", "yahoo"): "^VIX",
            ("BOND:UST_LONG", "yahoo"): "TLT",
        }
        for args, expected in cases.items():
            with self.subTest(args=args):
                self.assertEqual(market_data.provider_symbol(*args), expected)

        self.assertIsNone(market_data.provider_symbol("THEME:UNMAPPED", "yahoo"))
        self.assertIsNone(market_data.provider_symbol("US:SPY", "tencent"))
        self.assertIsNone(market_data.provider_symbol("not-an-asset", "yahoo"))

    def test_selects_explicit_benchmarks(self) -> None:
        self.assertEqual(market_data.benchmark_for("US:NVDA"), "US:SPY")
        self.assertEqual(
            market_data.benchmark_for("US:NVDA", "ai_semiconductors"),
            "US:SOXX",
        )
        self.assertEqual(market_data.benchmark_for("CN:600519"), "INDEX:CSI300")
        self.assertEqual(market_data.benchmark_for("CRYPTO:ETH"), "CRYPTO:BTC")
        self.assertIsNone(market_data.benchmark_for("CRYPTO:BTC"))
        self.assertIsNone(market_data.benchmark_for("THEME:UNMAPPED"))

    def test_theme_resolution_is_explicit_and_uses_independent_benchmark(
        self,
    ) -> None:
        resolution = market_data.resolve_provider_asset("THEME:AI", "yahoo")

        self.assertEqual(resolution["price_asset_key"], "US:SOXX")
        self.assertEqual(resolution["provider_symbol"], "SOXX")
        self.assertEqual(resolution["proxy_for"], "THEME:AI")
        self.assertEqual(market_data.benchmark_for("THEME:AI"), "US:SPY")
        self.assertNotEqual(
            resolution["price_asset_key"],
            market_data.benchmark_for("THEME:AI"),
        )
        self.assertIsNone(market_data.provider_symbol("THEME:AI", "yahoo"))


class MarketParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(market_data, "kol_dashboard.market_data is required")

    def test_parses_yahoo_chart_v8_bars_and_skips_bad_points(self) -> None:
        payload = {
            "chart": {
                "result": [
                    {
                        "meta": {"currency": "USD"},
                        "timestamp": [1704067200, 1704153600, 1704240000],
                        "indicators": {
                            "quote": [
                                {
                                    "close": [100.0, None, 102.5],
                                    "volume": [10, 20, 30],
                                }
                            ]
                        },
                    }
                ],
                "error": None,
            }
        }

        result = market_data.parse_yahoo_chart(payload, asset_key="US:TEST")

        self.assertEqual(result["status"], "available")
        self.assertEqual(result["currency"], "USD")
        self.assertEqual(
            [bar["timestamp"] for bar in result["bars"]],
            [1704067200, 1704240000],
        )
        self.assertEqual(result["bars"][1]["close"], 102.5)
        self.assertEqual(result["bars"][1]["volume"], 30.0)

    def test_csi300_daily_bar_uses_completed_shanghai_close_semantics(self) -> None:
        raw_timestamp = int(
            datetime(2026, 9, 4, 0, 0, tzinfo=timezone.utc).timestamp()
        )
        payload = {
            "chart": {
                "result": [
                    {
                        "meta": {
                            "currency": "CNY",
                            "exchangeTimezoneName": "Asia/Shanghai",
                        },
                        "timestamp": [raw_timestamp],
                        "indicators": {
                            "quote": [{"close": [4000.0], "volume": [5]}]
                        },
                    }
                ],
                "error": None,
            }
        }

        result = market_data.parse_yahoo_chart(
            payload,
            asset_key="INDEX:CSI300",
            symbol="000300.SS",
            interval="1d",
        )

        expected = int(
            datetime(2026, 9, 4, 7, 0, tzinfo=timezone.utc).timestamp()
        )
        self.assertEqual(result["bars"][0]["timestamp"], expected)
        self.assertEqual(result["timestamp_semantics"], "market_close")

    def test_parses_tencent_csi300_daily_history_with_close_timestamps(self) -> None:
        payload = {
            "code": 0,
            "data": {
                "sh000300": {
                    "day": [
                        ["2026-09-03", "4570", "4552.58", "4585", "4536", "10"],
                        ["bad-date", "1", "2", "3", "0", "4"],
                        ["2026-09-04", "4575", "4548.05", "4602", "4530", "20"],
                    ]
                }
            },
        }

        result = market_data.parse_tencent_daily_history(payload)

        self.assertEqual(result["status"], "available")
        self.assertEqual(result["provider"], "tencent")
        self.assertEqual(result["symbol"], "sh000300")
        self.assertEqual(result["currency"], "CNY")
        self.assertEqual(result["timestamp_semantics"], "market_close")
        self.assertEqual([bar["close"] for bar in result["bars"]], [4552.58, 4548.05])
        self.assertEqual(
            result["bars"][-1]["timestamp"],
            int(datetime(2026, 9, 4, 7, 0, tzinfo=timezone.utc).timestamp()),
        )

    def test_fetches_tencent_csi300_history_with_injected_opener(self) -> None:
        payload = {
            "code": 0,
            "data": {
                "sh000300": {
                    "day": [
                        ["2026-09-04", "4575", "4548.05", "4602", "4530", "20"]
                    ]
                }
            },
        }
        calls = []

        def opener(request, timeout):
            calls.append((request.full_url, timeout))
            return _Response(json.dumps(payload).encode("utf-8"))

        result = market_data.fetch_tencent_daily_history(
            "INDEX:CSI300",
            count=120,
            opener=opener,
            timeout=2.0,
        )

        self.assertEqual(result["status"], "available")
        self.assertIn("web.ifzq.gtimg.cn/appstock/app/fqkline/get", calls[0][0])
        self.assertIn("sh000300%2Cday%2C%2C%2C120%2Cqfq", calls[0][0])
        self.assertEqual(calls[0][1], 2.0)

        unsupported = market_data.fetch_tencent_daily_history(
            "CN:600519",
            opener=opener,
        )
        invalid_count = market_data.fetch_tencent_daily_history(
            "INDEX:CSI300",
            count=12,
            opener=opener,
        )
        self.assertEqual(unsupported["reason_code"], "unsupported_asset")
        self.assertEqual(invalid_count["reason_code"], "invalid_count")
        self.assertEqual(len(calls), 1)

    def test_yahoo_parser_prefers_adjusted_close_and_fails_closed(self) -> None:
        payload = {
            "chart": {
                "result": [
                    {
                        "meta": {"currency": "USD"},
                        "timestamp": [1704067200],
                        "indicators": {
                            "quote": [{"close": [100.0], "volume": [5]}],
                            "adjclose": [{"adjclose": [95.0]}],
                        },
                    }
                ],
                "error": None,
            }
        }

        adjusted = market_data.parse_yahoo_chart(payload, asset_key="US:TEST")
        malformed_payloads = (
            b"\xff\xfe",
            {"chart": []},
            {
                "chart": {
                    "result": [
                        {
                            "timestamp": [1704067200],
                            "indicators": {
                                "quote": [{"close": [100.0], "volume": 5}]
                            },
                        }
                    ],
                    "error": None,
                }
            },
        )

        self.assertEqual(adjusted["bars"][0]["close"], 95.0)
        for malformed in malformed_payloads:
            with self.subTest(malformed=type(malformed).__name__):
                result = market_data.parse_yahoo_chart(
                    malformed, asset_key="US:TEST"
                )
                self.assertEqual(result["status"], "unavailable")
                self.assertEqual(result["bars"], [])

    def test_yahoo_daily_bar_uses_exchange_close_timestamp(self) -> None:
        raw_timestamp = int(
            datetime(2026, 1, 2, 14, 30, tzinfo=timezone.utc).timestamp()
        )
        payload = {
            "chart": {
                "result": [
                    {
                        "meta": {
                            "currency": "USD",
                            "exchangeTimezoneName": "America/New_York",
                        },
                        "timestamp": [raw_timestamp],
                        "indicators": {
                            "quote": [{"close": [100.0], "volume": [5]}]
                        },
                    }
                ],
                "error": None,
            }
        }

        result = market_data.parse_yahoo_chart(payload, asset_key="US:TEST")

        self.assertEqual(
            result["bars"][0]["timestamp"],
            int(datetime(2026, 1, 2, 21, tzinfo=timezone.utc).timestamp()),
        )
        self.assertEqual(result["timestamp_semantics"], "market_close")

    def test_vix_daily_bar_uses_completed_chicago_close_semantics(self) -> None:
        raw_timestamp = int(
            datetime(2026, 9, 4, 7, 0, tzinfo=timezone.utc).timestamp()
        )
        payload = {
            "chart": {
                "result": [
                    {
                        "meta": {
                            "currency": "USD",
                            "exchangeTimezoneName": "America/Chicago",
                        },
                        "timestamp": [raw_timestamp],
                        "indicators": {
                            "quote": [{"close": [14.53], "volume": [0]}]
                        },
                    }
                ],
                "error": None,
            }
        }

        result = market_data.parse_yahoo_chart(
            payload,
            asset_key="INDEX:VIX",
            symbol="^VIX",
            interval="1d",
        )

        expected = int(
            datetime(2026, 9, 4, 20, 15, tzinfo=timezone.utc).timestamp()
        )
        self.assertEqual(result["bars"][0]["timestamp"], expected)
        self.assertEqual(result["timestamp_semantics"], "market_close")

    def test_fx_daily_bar_uses_new_york_close_and_drops_weekend_quote(self) -> None:
        friday_marker = int(
            datetime(2026, 9, 3, 23, 0, tzinfo=timezone.utc).timestamp()
        )
        weekend_quote = int(
            datetime(2026, 9, 5, 16, 4, tzinfo=timezone.utc).timestamp()
        )
        payload = {
            "chart": {
                "result": [
                    {
                        "meta": {
                            "currency": "CNY",
                            "exchangeTimezoneName": "Europe/London",
                        },
                        "timestamp": [friday_marker, weekend_quote],
                        "indicators": {
                            "quote": [
                                {"close": [6.72, 6.70], "volume": [0, 0]}
                            ]
                        },
                    }
                ],
                "error": None,
            }
        }

        result = market_data.parse_yahoo_chart(
            payload,
            asset_key="FX:USD/CNY",
            symbol="CNY=X",
            interval="1d",
        )

        self.assertEqual(len(result["bars"]), 1)
        self.assertEqual(
            result["bars"][0]["timestamp"],
            int(datetime(2026, 9, 4, 21, 0, tzinfo=timezone.utc).timestamp()),
        )
        self.assertEqual(result["timestamp_semantics"], "market_close")

    def test_yahoo_does_not_infer_equity_close_for_other_intervals_or_assets(
        self,
    ) -> None:
        raw_timestamp = int(
            datetime(2026, 1, 2, 14, 30, tzinfo=timezone.utc).timestamp()
        )
        payload = {
            "chart": {
                "result": [
                    {
                        "meta": {
                            "currency": "USD",
                            "exchangeTimezoneName": "America/New_York",
                        },
                        "timestamp": [raw_timestamp],
                        "indicators": {
                            "quote": [{"close": [100.0], "volume": [5]}]
                        },
                    }
                ],
                "error": None,
            }
        }

        weekly = market_data.parse_yahoo_chart(
            payload, asset_key="US:TEST", interval="1wk"
        )
        commodity = market_data.parse_yahoo_chart(
            payload, asset_key="COMMODITY:GOLD", interval="1d"
        )

        self.assertEqual(weekly["bars"][0]["timestamp"], raw_timestamp)
        self.assertEqual(weekly["timestamp_semantics"], "provider")
        self.assertEqual(commodity["bars"][0]["timestamp"], raw_timestamp)
        self.assertEqual(commodity["timestamp_semantics"], "provider")

    def test_network_and_bad_payloads_degrade_without_raising(self) -> None:
        def timeout(*_args, **_kwargs):
            raise TimeoutError("offline")

        timed_out = market_data.fetch_yahoo_history(
            "US:SPY", opener=timeout, timeout=0.01
        )
        malformed = market_data.fetch_yahoo_history(
            "US:SPY", opener=lambda *_a, **_k: _Response(b"not-json")
        )

        for result in (timed_out, malformed):
            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(result["bars"], [])
            self.assertIn("reason", result)

    def test_fetches_yahoo_with_injected_opener_only(self) -> None:
        payload = {
            "chart": {
                "result": [
                    {
                        "meta": {"currency": "USD"},
                        "timestamp": [1704067200],
                        "indicators": {
                            "quote": [{"close": [100.0], "volume": [5]}]
                        },
                    }
                ],
                "error": None,
            }
        }
        calls = []

        def opener(request, timeout):
            calls.append((request.full_url, timeout))
            return _Response(json.dumps(payload).encode("utf-8"))

        result = market_data.fetch_yahoo_history(
            "US:SPY", opener=opener, timeout=2.0
        )

        self.assertEqual(result["status"], "available")
        self.assertEqual(result["bars"][0]["close"], 100.0)
        self.assertEqual(len(calls), 1)
        self.assertIn("/v8/finance/chart/SPY", calls[0][0])

    def test_yahoo_accepts_month_ranges_and_rejects_ambiguous_m(self) -> None:
        payload = {
            "chart": {
                "result": [
                    {
                        "meta": {"currency": "USD"},
                        "timestamp": [1704067200],
                        "indicators": {
                            "quote": [{"close": [100.0], "volume": [5]}]
                        },
                    }
                ],
                "error": None,
            }
        }
        urls = []

        def opener(request, timeout):
            urls.append(request.full_url)
            return _Response(json.dumps(payload).encode("utf-8"))

        accepted = market_data.fetch_yahoo_history(
            "US:SPY", range_="3mo", opener=opener
        )
        rejected = market_data.fetch_yahoo_history(
            "US:SPY", range_="3m", opener=opener
        )

        self.assertEqual(accepted["status"], "available")
        self.assertIn("range=3mo", urls[0])
        self.assertEqual(rejected["status"], "unavailable")
        self.assertEqual(rejected["reason_code"], "invalid_range")
        self.assertEqual(len(urls), 1)

    def test_theme_history_exposes_proxy_lineage(self) -> None:
        payload = {
            "chart": {
                "result": [
                    {
                        "meta": {"currency": "USD"},
                        "timestamp": [1704067200],
                        "indicators": {
                            "quote": [{"close": [100.0], "volume": [5]}]
                        },
                    }
                ],
                "error": None,
            }
        }
        urls = []

        def opener(request, timeout):
            urls.append(request.full_url)
            return _Response(json.dumps(payload).encode("utf-8"))

        result = market_data.fetch_yahoo_history(
            "THEME:AI", range_="3mo", opener=opener
        )

        self.assertIn("/v8/finance/chart/SOXX", urls[0])
        self.assertEqual(result["asset_key"], "THEME:AI")
        self.assertEqual(result["price_asset_key"], "US:SOXX")
        self.assertEqual(result["proxy_for"], "THEME:AI")
        self.assertEqual(result["symbol"], "SOXX")

    def test_provider_exception_details_are_not_exposed(self) -> None:
        def timeout(*_args, **_kwargs):
            raise TimeoutError("secret upstream detail")

        result = market_data.fetch_yahoo_history("US:SPY", opener=timeout)

        self.assertEqual(result["reason"], "request_failed")
        self.assertEqual(result["reason_code"], "request_failed")
        self.assertNotIn("TimeoutError", json.dumps(result))

    def test_parses_tencent_a_share_quote(self) -> None:
        text = (
            'v_sh600519="1~贵州茅台~600519~1418.50~1400.00~1395.00~'
            '10~20~30~1418.50~1~1418.40~2~0~0~0~0~0~0~0~'
            '20260801150000~18.50~1.32";'
        )

        result = market_data.parse_tencent_quote(text, asset_key="CN:600519")

        self.assertEqual(result["status"], "available")
        self.assertEqual(result["symbol"], "sh600519")
        self.assertEqual(result["price"], 1418.5)
        self.assertEqual(result["currency"], "CNY")
        self.assertEqual(result["observed_at"], "2026-08-01T07:00:00+00:00")


def _bar(day: int, close: float) -> dict:
    timestamp = int(datetime(2026, 1, day, tzinfo=timezone.utc).timestamp())
    return {"timestamp": timestamp, "close": close, "volume": 100.0}


class EventReactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(market_data, "kol_dashboard.market_data is required")

    def test_uses_only_common_trading_dates_for_each_window(self) -> None:
        asset = [
            _bar(1, 100.0),
            _bar(2, 110.0),
            _bar(4, 121.0),
            _bar(5, 133.1),
            _bar(8, 146.41),
            _bar(9, 161.051),
        ]
        benchmark = [
            _bar(1, 200.0),
            _bar(2, 202.0),
            _bar(3, 204.0),  # Missing from asset; must not become a zero return.
            _bar(4, 208.0),
            _bar(5, 210.0),
            _bar(8, 220.0),
            _bar(9, 230.0),
        ]

        result = market_data.compute_event_reaction(
            asset,
            benchmark,
            datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
            expected_direction="positive",
        )

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["aligned_sample_count"], 6)
        self.assertAlmostEqual(result["windows"]["1D"]["asset_return"], 0.10)
        self.assertAlmostEqual(result["windows"]["3D"]["asset_return"], 0.331)
        self.assertAlmostEqual(
            result["windows"]["3D"]["benchmark_return"], 0.05
        )
        self.assertAlmostEqual(
            result["windows"]["3D"]["abnormal_return"], 0.281
        )
        self.assertEqual(
            result["windows"]["3D"]["data_timestamps"]["end"],
            _bar(5, 0)["timestamp"],
        )
        self.assertEqual(result["windows"]["3D"]["sample_count"], 4)
        self.assertTrue(result["windows"]["3D"]["direction_confirmed"])

    def test_after_close_event_uses_same_close_as_baseline(self) -> None:
        def close_bar(day: int, close: float) -> dict:
            timestamp = int(
                datetime(2026, 1, day, 16, tzinfo=timezone.utc).timestamp()
            )
            return {"timestamp": timestamp, "close": close, "volume": 100}

        asset = [
            close_bar(1, 100),
            close_bar(2, 110),
            close_bar(3, 121),
            close_bar(4, 133.1),
            close_bar(5, 146.41),
            close_bar(6, 161.051),
            close_bar(7, 177.1561),
        ]
        benchmark = [
            close_bar(1, 200),
            close_bar(2, 202),
            close_bar(3, 204),
            close_bar(4, 206),
            close_bar(5, 208),
            close_bar(6, 210),
            close_bar(7, 212),
        ]
        event_time = datetime(2026, 1, 2, 20, tzinfo=timezone.utc)

        result = market_data.compute_event_reaction(
            asset,
            benchmark,
            event_time,
            expected_direction="positive",
        )

        self.assertEqual(
            result["windows"]["1D"]["data_timestamps"]["start"],
            close_bar(2, 0)["timestamp"],
        )
        self.assertEqual(
            result["windows"]["1D"]["data_timestamps"]["end"],
            close_bar(3, 0)["timestamp"],
        )
        self.assertAlmostEqual(result["windows"]["1D"]["asset_return"], 0.10)

    def test_direction_confirmation_compares_with_expected_direction(self) -> None:
        asset = [_bar(day, close) for day, close in ((1, 100), (2, 90), (3, 80))]
        benchmark = [
            _bar(day, close) for day, close in ((1, 200), (2, 198), (3, 196))
        ]
        event_time = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)

        positive = market_data.compute_event_reaction(
            asset, benchmark, event_time, expected_direction="positive"
        )
        negative = market_data.compute_event_reaction(
            asset, benchmark, event_time, expected_direction="negative"
        )

        self.assertFalse(positive["windows"]["1D"]["direction_confirmed"])
        self.assertTrue(negative["windows"]["1D"]["direction_confirmed"])
        self.assertEqual(negative["expected_direction"], "negative")
        self.assertEqual(
            positive["windows"]["1D"]["observed_direction"], "negative"
        )

    def test_mixed_pre_and_post_event_pair_is_excluded(self) -> None:
        def timed(day: int, hour: int, close: float) -> dict:
            return {
                "timestamp": int(
                    datetime(
                        2026, 1, day, hour, tzinfo=timezone.utc
                    ).timestamp()
                ),
                "close": close,
                "volume": 100,
            }

        asset = [
            timed(1, 16, 100),
            timed(2, 16, 110),  # Before the event.
            timed(3, 16, 121),
        ]
        benchmark = [
            timed(1, 18, 200),
            timed(2, 18, 202),  # After the event: mixed pair must be excluded.
            timed(3, 18, 204),
        ]
        event_time = datetime(2026, 1, 2, 17, tzinfo=timezone.utc)

        result = market_data.compute_event_reaction(
            asset,
            benchmark,
            event_time,
            expected_direction="positive",
        )

        self.assertEqual(
            result["windows"]["1D"]["data_timestamps"]["end"],
            timed(3, 16, 0)["timestamp"],
        )
        self.assertAlmostEqual(result["windows"]["1D"]["asset_return"], 0.21)

    def test_marks_incomplete_follow_up_as_preliminary(self) -> None:
        result = market_data.compute_event_reaction(
            [_bar(1, 100), _bar(2, 101), _bar(3, 102)],
            [_bar(1, 200), _bar(2, 201), _bar(3, 202)],
            "2026-01-02T09:00:00+00:00",
        )

        self.assertEqual(result["status"], "preliminary")
        self.assertEqual(result["reason_code"], "insufficient_follow_up")
        self.assertEqual(result["windows"]["1D"]["status"], "complete")
        self.assertEqual(result["windows"]["3D"]["status"], "unavailable")
        self.assertEqual(result["windows"]["5D"]["status"], "unavailable")

    def test_bad_or_unaligned_bars_abstain_as_unavailable(self) -> None:
        result = market_data.compute_event_reaction(
            [_bar(2, 100)],
            [_bar(3, 200)],
            "2026-01-02T09:00:00+00:00",
        )

        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["aligned_sample_count"], 0)
        self.assertTrue(result["abstain"])


if __name__ == "__main__":
    unittest.main()
