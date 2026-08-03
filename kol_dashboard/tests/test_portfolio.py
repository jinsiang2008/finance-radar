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
    portfolio = importlib.import_module("kol_dashboard.portfolio")
except ModuleNotFoundError:
    portfolio = None


HOLDINGS = """
# 我的投资组合

## A 股持仓 — 股票
> 现价快照：**2026-04-22**
| 代号 | 名称 | 持股数 | 均价(元) | 备注 |
|------|------|--------|----------|------|
| 600519 | 贵州茅台 | 10 | 1500.00 | private-note-cn |
| 002080 | ~~中材科技~~ | 0 | 56.951 | ❌ 已清仓 |

## A 股持仓 — ETF / 基金
> 现价快照：**2026-04-22**
| 代号 | 名称 | 持股数 | 均价(元) | 备注 |
|------|------|--------|----------|------|
| 588000 | 科创50ETF | 500 | 1.395 | private-note-etf |

## A 股持仓 — 可转债
> 现价快照：**2026-04-22**
| 代号 | 名称 | 持有(张) | 均价(元/张) | 备注 |
|------|------|----------|--------------|------|
| 110085 | 通22转债 | 1 | 97.800 | private-note-bond |

## 美股持仓 — 股票
> 数据来源：Robinhood MCP 实时同步 **2026-06-15**
| 代号 | 名称 | 持股数 | 均价($) | 备注 |
|------|------|--------|---------|------|
| META | Meta | 8.5 | 335.96 | private-note-rh |
| ZERO | Closed | 0 | 1.00 | 已清仓 |

## 美股持仓 — ETF / 杠杆
| 代号 | 名称 | 持股数 | 均价($) | 备注 |
|------|------|--------|---------|------|
| SPY | S&P 500 ETF | 2 | 334.32 | plain ETF |
| NVDL | NVIDIA 2x Long | 4 | 82.47 | 杠杆ETF |

## 美股持仓（Schwab 账户）— 股票
> 数据来源：Schwab 持仓页截图 **2026-06-09**
| 代号 | 名称 | 持股数 | 均价($) | 备注 |
|------|------|--------|---------|------|
| META | Meta (C类) | 10 | 243.69 | private-note-schwab |

## 美股持仓 — 加密货币
| 代号 | 名称 | 持有量 | 均价($) | 备注 |
|------|------|--------|---------|------|
| BTC | Bitcoin | 0.02 | 85928.65 | private-note-crypto |

|## 🏠 看房记录
| 项目 | 详情 |
|------|------|
| 600000 | 房产原始文本不应被解析 |

## 关注列表（未持仓）
| 市场 | 代号 | 名称 | 关注理由 |
| 美股 | AAPL | Apple | 不应进入持仓 |

## 近期操作记录
| 日期 | 操作 | 代号 | 名称 | 股数 | 价格 |
| 2026-07-01 | 买入 | TSLA | Tesla | 99 | 100 |
"""


class PortfolioParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(portfolio, "kol_dashboard.portfolio is required")

    def test_parses_supported_sections_and_keeps_accounts_separate(self) -> None:
        snapshot = portfolio.parse_holdings_markdown(HOLDINGS)
        positions = snapshot["positions"]
        identities = {(item["account"], item["asset_key"]) for item in positions}

        self.assertIn(("招商证券", "CN:600519"), identities)
        self.assertIn(("招商证券", "CN:588000"), identities)
        self.assertIn(("招商证券", "CN:110085"), identities)
        self.assertIn(("Robinhood", "US:META"), identities)
        self.assertIn(("Schwab", "US:META"), identities)
        self.assertIn(("Robinhood", "CRYPTO:BTC"), identities)
        self.assertEqual(
            sum(item["asset_key"] == "US:META" for item in positions), 2
        )
        self.assertNotIn(("Robinhood", "US:AAPL"), identities)
        self.assertNotIn(("Robinhood", "US:TSLA"), identities)

    def test_classifies_assets_leverage_and_ignores_closed_rows(self) -> None:
        positions = portfolio.parse_holdings_markdown(HOLDINGS)["positions"]
        by_asset_account = {
            (item["asset_key"], item["account"]): item for item in positions
        }

        self.assertEqual(
            by_asset_account[("CN:588000", "招商证券")]["asset_class"], "etf"
        )
        self.assertEqual(
            by_asset_account[("CN:110085", "招商证券")]["asset_class"],
            "convertible_bond",
        )
        self.assertFalse(
            by_asset_account[("US:SPY", "Robinhood")]["is_leveraged"]
        )
        self.assertTrue(
            by_asset_account[("US:NVDL", "Robinhood")]["is_leveraged"]
        )
        self.assertFalse(any(item["symbol"] == "002080" for item in positions))
        self.assertFalse(any(item["symbol"] == "ZERO" for item in positions))

    def test_snapshot_json_is_sanitized_and_round_trips_with_validation(self) -> None:
        snapshot = portfolio.parse_holdings_markdown(HOLDINGS)
        encoded = portfolio.snapshot_to_json(snapshot)
        decoded = portfolio.snapshot_from_json(encoded)

        self.assertEqual(decoded, snapshot)
        self.assertEqual(
            set(snapshot),
            {"schema_version", "source_hash", "as_of", "positions"},
        )
        required_position_fields = {
            "account",
            "asset_key",
            "symbol",
            "name",
            "quantity",
            "avg_cost",
            "currency",
            "asset_class",
            "is_leveraged",
            "as_of",
        }
        self.assertTrue(snapshot["positions"])
        for item in snapshot["positions"]:
            self.assertEqual(set(item), required_position_fields)
        for forbidden in (
            "private-note",
            "看房",
            "关注理由",
            "操作记录",
            "备注",
            "止损",
            "目标价",
            "raw",
        ):
            self.assertNotIn(forbidden, encoded)

        unsafe = json.loads(encoded)
        unsafe["positions"][0]["remarks"] = "must not survive"
        with self.assertRaises(ValueError):
            portfolio.snapshot_from_json(json.dumps(unsafe))
        with self.assertRaises(ValueError):
            portfolio.snapshot_from_json('{"positions": NaN}')

        forged_hash = json.loads(encoded)
        forged_hash["source_hash"] = "0" * 64
        with self.assertRaises(ValueError):
            portfolio.snapshot_from_json(json.dumps(forged_hash))

    def test_source_hash_is_deterministic_and_input_is_not_mutated(self) -> None:
        source = str(HOLDINGS)
        first = portfolio.parse_holdings_markdown(source)
        second = portfolio.parse_holdings_markdown(source)
        self.assertEqual(first["source_hash"], second["source_hash"])
        self.assertEqual(source, HOLDINGS)

    def test_unrelated_private_sections_do_not_change_sanitized_hash(self) -> None:
        first = portfolio.parse_holdings_markdown(HOLDINGS)
        second = portfolio.parse_holdings_markdown(
            HOLDINGS
            + "\n## 私人备注\n电话 13800000000\n### 房产\n总价 2000 万\n"
        )

        self.assertEqual(first["source_hash"], second["source_hash"])

    def test_unknown_holdings_heading_and_subheading_terminate_parsing(self) -> None:
        source = """
## 美股持仓 — 股票
> 数据来源 **2026-07-31**
| 代号 | 名称 | 持股数 | 均价($) |
|---|---|---|---|
| NVDA | NVIDIA | 1 | 100 |

### 房产与备注
| 代号 | 名称 | 持股数 | 均价($) |
|---|---|---|---|
| TSLA | 不应解析 | 9 | 99 |

## 美股持仓备注 — 股票
| 代号 | 名称 | 持股数 | 均价($) |
|---|---|---|---|
| META | 不应解析 | 8 | 88 |
"""

        positions = portfolio.parse_holdings_markdown(source)["positions"]

        self.assertEqual([item["asset_key"] for item in positions], ["US:NVDA"])

    def test_file_loader_rejects_paths_other_than_fixed_holdings_file(self) -> None:
        with self.assertRaises((PermissionError, ValueError)):
            portfolio.load_holdings("/tmp/../tmp/attacker.md")


class PortfolioDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(portfolio, "kol_dashboard.portfolio is required")
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = mock.patch.object(
            db, "DB_PATH", str(Path(self.tmp.name) / "portfolio.sqlite3")
        )
        self.db_patch.start()
        db.init()

    def tearDown(self) -> None:
        self.db_patch.stop()
        self.tmp.cleanup()

    def test_snapshot_save_is_idempotent_and_reports_staleness(self) -> None:
        snapshot = portfolio.parse_holdings_markdown(HOLDINGS)
        original = deepcopy(snapshot)

        first_id = db.save_portfolio_snapshot(snapshot)
        second_id = db.save_portfolio_snapshot(snapshot)
        latest = db.latest_portfolio_snapshot(
            now="2026-07-01T00:00:00+00:00",
            stale_after_seconds=7 * 24 * 60 * 60,
        )

        self.assertEqual(first_id, second_id)
        self.assertEqual(latest["source_hash"], snapshot["source_hash"])
        self.assertEqual(len(latest["positions"]), len(snapshot["positions"]))
        self.assertTrue(latest["staleness"]["is_stale"])
        self.assertGreater(latest["staleness"]["age_seconds"], 0)
        with db.conn() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) AS n FROM portfolio_snapshots"
                ).fetchone()["n"],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) AS n FROM portfolio_positions"
                ).fetchone()["n"],
                len(snapshot["positions"]),
            )
        self.assertEqual(snapshot, original)

    def test_future_snapshot_date_is_marked_stale(self) -> None:
        snapshot = portfolio.parse_holdings_markdown(
            HOLDINGS.replace("2026-06-15", "2026-09-15").replace(
                "2026-06-09", "2026-09-09"
            )
        )
        db.save_portfolio_snapshot(snapshot)

        latest = db.latest_portfolio_snapshot(
            now="2026-08-01T00:00:00+00:00",
            stale_after_seconds=7 * 24 * 60 * 60,
        )

        self.assertTrue(latest["staleness"]["is_stale"])
        self.assertTrue(latest["staleness"]["clock_skew"])


if __name__ == "__main__":
    unittest.main()
