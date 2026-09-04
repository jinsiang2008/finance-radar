from __future__ import annotations

import json
import unittest

from kol_dashboard import relation_engine


class AssetExtractionTests(unittest.TestCase):
    def test_extracts_explicit_tickers_aliases_crypto_and_cn_codes(self) -> None:
        assets = relation_engine.extract_assets(
            "看好英伟达、台积电和 600519，关注 $TSLA 与比特币。",
            explicit_tickers=["AAPL"],
        )

        self.assertEqual(
            assets,
            [
                "US:AAPL",
                "US:TSLA",
                "CN:600519",
                "US:NVDA",
                "US:TSM",
                "CRYPTO:BTC",
            ],
        )

    def test_extractor_exposes_stable_identity_and_version(self) -> None:
        self.assertEqual(relation_engine.EXTRACTOR_NAME, "kol-relation-rules")
        self.assertRegex(relation_engine.EXTRACTOR_VERSION, r"^\d+\.\d+\.\d+$")

    def test_explicit_crypto_symbols_never_fall_back_to_us_tickers(self) -> None:
        assets = relation_engine.extract_assets(
            "", explicit_tickers=["BTC", "ETH", "DOGE", "SOL"]
        )
        self.assertEqual(
            assets,
            [
                "CRYPTO:BTC",
                "CRYPTO:ETH",
                "CRYPTO:DOGE",
                "CRYPTO:SOL",
            ],
        )


class EventRelationTests(unittest.TestCase):
    @staticmethod
    def event(title: str) -> dict:
        return {
            "id": 12,
            "title": title,
            "snippet": "NVIDIA discussed its next AI accelerator.",
            "tickers": ["NVDA"],
            "impact": "medium",
            "published_at": "2026-07-31T10:00:00+00:00",
            "url": "https://example.com/nvda",
        }

    def test_unknown_stance_is_neutral_and_lower_confidence(self) -> None:
        unknown = relation_engine.event_relations(
            self.event("NVIDIA presents its accelerator roadmap")
        )[0]
        bullish = relation_engine.event_relations(
            self.event("Bullish on NVIDIA; buy the AI leader")
        )[0]

        self.assertEqual(unknown["direction"], "neutral")
        self.assertEqual(unknown["stance"], "unknown")
        self.assertLess(unknown["confidence"], bullish["confidence"])
        self.assertEqual(
            unknown["evidence"]["extractor_version"],
            relation_engine.EXTRACTOR_VERSION,
        )

    def test_event_relations_are_pure_and_deterministic(self) -> None:
        event = self.event("Bullish on NVIDIA; buy the AI leader")
        self.assertEqual(
            relation_engine.event_relations(event),
            relation_engine.event_relations(event),
        )

    def test_relation_evidence_url_prefers_the_selected_sighting(self) -> None:
        event = {
            **self.event("Bullish on NVIDIA; buy the AI leader"),
            "source_url": "https://selected.example.com/nvda",
            "canonical_url": "https://canonical.example.com/nvda",
        }

        relation = relation_engine.event_relations(event)[0]

        self.assertEqual(
            relation["evidence"]["url"],
            "https://selected.example.com/nvda",
        )

    def test_negated_bullish_phrase_does_not_become_positive(self) -> None:
        relation = relation_engine.event_relations(
            self.event("Not bullish on NVIDIA at this valuation")
        )[0]
        self.assertEqual(relation["direction"], "neutral")
        self.assertEqual(relation["stance"], "unknown")

    def test_negated_bearish_and_sell_phrases_remain_neutral(self) -> None:
        for text in (
            "Not bearish on NVIDIA",
            "Do not sell $NVDA",
            "不要卖出英伟达",
        ):
            with self.subTest(text=text):
                relation = relation_engine.event_relations(self.event(text))[0]
                self.assertEqual(relation["direction"], "neutral")
                self.assertEqual(relation["stance"], "unknown")
                self.assertLessEqual(relation["confidence"], 0.45)

    def test_mixed_buy_sell_text_is_scored_per_nearby_asset_clause(self) -> None:
        event = {
            "id": 13,
            "title": "Buy $NVDA, but sell $TSLA",
            "snippet": "",
            "tickers": ["NVDA", "TSLA"],
            "impact": "medium",
        }

        relations = {
            edge["asset_key"]: edge for edge in relation_engine.event_relations(event)
        }

        self.assertEqual(relations["US:NVDA"]["direction"], "positive")
        self.assertEqual(relations["US:TSLA"]["direction"], "negative")


class MacroRelationTests(unittest.TestCase):
    def test_actual_opportunity_assets_map_to_tradeable_proxies(self) -> None:
        payload = {
            "snapshot_id": 13,
            "opportunities": [
                {
                    "type": "curve_normalization",
                    "asset": "金融股 / 周期股",
                    "signal": "收益率曲线接近正常化，银行利差改善",
                    "confidence": "medium",
                    "timeframe": "1-3个月",
                },
                {
                    "type": "currency_tailwind",
                    "asset": "出口型企业 / 美元计价资产",
                    "signal": "USD/CNY 7.25，人民币贬值利好出口和美元资产",
                    "confidence": "medium",
                    "timeframe": "1-3个月",
                },
            ],
        }

        edges = relation_engine.macro_relations(payload)
        assets = {edge["asset_key"] for edge in edges}

        self.assertTrue(
            {"US:XLF", "US:XLI", "FX:USD/CNY", "US:UUP"}.issubset(assets)
        )
        for edge in edges:
            if edge["asset_key"] in {
                "US:XLF",
                "US:XLI",
                "FX:USD/CNY",
                "US:UUP",
            }:
                self.assertIn("代理", edge["rationale"])
                self.assertIn("不代表个性化敞口", edge["rationale"])

    def test_macro_items_without_ids_use_distinct_deterministic_source_keys(self) -> None:
        payload = {
            "snapshot_id": 12,
            "opportunities": [
                {
                    "type": "dip_buying",
                    "asset": "NVDA",
                    "signal": "first signal",
                    "confidence": "medium",
                },
                {
                    "type": "dip_buying",
                    "asset": "NVDA",
                    "signal": "second signal",
                    "confidence": "medium",
                },
            ],
        }

        first = relation_engine.macro_relations(payload)
        second = relation_engine.macro_relations(payload)

        self.assertEqual(len(first), 2)
        self.assertEqual(len({edge["source_id"] for edge in first}), 2)
        self.assertEqual(
            [edge["source_id"] for edge in first],
            [edge["source_id"] for edge in second],
        )

    def test_same_snapshot_scenarios_keep_distinct_source_ids_and_evidence(self) -> None:
        payload = {
            "snapshot_id": 11,
            "black_swan_scenarios": [
                {
                    "id": "first",
                    "name": "AI demand shock",
                    "affected_assets": ["NVDA"],
                    "probability": "medium",
                    "impact": "high",
                },
                {
                    "id": "second",
                    "name": "AI supply shock",
                    "affected_assets": ["NVDA"],
                    "probability": "medium",
                    "impact": "high",
                },
            ],
        }

        edges = [
            edge
            for edge in relation_engine.macro_relations(payload)
            if edge["asset_key"] == "US:NVDA"
        ]

        self.assertEqual(len(edges), 2)
        self.assertEqual(len({edge["source_id"] for edge in edges}), 2)
        self.assertEqual(
            {edge["evidence"]["item_id"] for edge in edges},
            {"first", "second"},
        )

    def test_maps_common_macro_asset_proxies(self) -> None:
        payload = {
            "snapshot_id": 10,
            "black_swan_scenarios": [
                {
                    "id": "liquidity",
                    "name": "美元流动性危机",
                    "affected_assets": ["几乎所有风险资产"],
                    "probability": "low",
                    "impact": "severe",
                }
            ],
            "gray_rhinos": [
                {
                    "id": "carry",
                    "name": "日元套利交易平仓",
                    "affected_markets": ["日元", "日经", "新兴市场"],
                    "urgency": "approaching",
                }
            ],
        }

        assets = {
            edge["asset_key"] for edge in relation_engine.macro_relations(payload)
        }

        self.assertTrue(
            {
                "THEME:GLOBAL_RISK_ASSETS",
                "FX:JPY",
                "INDEX:NIKKEI",
                "THEME:EMERGING_MARKETS",
            }.issubset(assets)
        )

    def test_maps_macro_scenarios_and_opportunities_to_assets(self) -> None:
        payload = {
            "snapshot_id": 9,
            "black_swan_scenarios": [
                {
                    "id": "bs_ai",
                    "name": "AI 泡沫破裂",
                    "description": "AI 估值重置并冲击半导体。",
                    "affected_assets": ["NVDA", "BTC"],
                    "probability": "medium",
                    "impact": "severe",
                    "timeframe": "1-3个月",
                }
            ],
            "gray_rhinos": [
                {
                    "id": "gr_banks",
                    "name": "区域银行压力",
                    "description": "商业地产再融资风险上升。",
                    "affected_markets": ["区域银行ETF (KRE)"],
                    "urgency": "approaching",
                }
            ],
            "opportunities": [
                {
                    "type": "dip_buying",
                    "asset": "黄金 (GLD)",
                    "signal": "黄金回调，可逢低配置。",
                    "confidence": "medium",
                    "timeframe": "3-12个月",
                }
            ],
        }

        edges = relation_engine.macro_relations(payload)
        by_kind_asset = {
            (edge["relation_type"], edge["asset_key"]): edge for edge in edges
        }

        self.assertEqual(
            by_kind_asset[("risk_scenario", "US:NVDA")]["direction"], "negative"
        )
        self.assertEqual(
            by_kind_asset[("risk_scenario", "CRYPTO:BTC")]["direction"], "negative"
        )
        self.assertEqual(
            by_kind_asset[("structural_risk", "US:KRE")]["direction"], "negative"
        )
        self.assertEqual(
            by_kind_asset[("opportunity", "COMMODITY:GOLD")]["direction"],
            "positive",
        )
        for edge in edges:
            self.assertTrue(edge["method"].startswith("deterministic_rules:"))
            self.assertIn("evidence", edge)
            self.assertGreaterEqual(edge["confidence"], 0.0)
            self.assertLessEqual(edge["confidence"], 1.0)

    def test_legacy_portfolio_fields_are_not_relation_inputs(self) -> None:
        payload = {
            "snapshot_id": "legacy",
            "black_swan_scenarios": [
                {
                    "id": "private",
                    "name": "Legacy private scenario",
                    "affected_positions": ["PRIVATE"],
                    "probability": "medium",
                    "impact": "high",
                }
            ],
            "gray_rhinos": [
                {
                    "id": "private-rhino",
                    "name": "Legacy private rhino",
                    "portfolio_impact": "PRIVATE should fall",
                }
            ],
        }

        relations = relation_engine.macro_relations(payload)
        encoded = json.dumps(relations, ensure_ascii=False)

        self.assertNotIn("US:PRIVATE", encoded)
        self.assertNotIn("affected_positions", encoded)
        self.assertNotIn("portfolio_impact", encoded)


if __name__ == "__main__":
    unittest.main()
