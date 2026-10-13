from __future__ import annotations

import unittest

from kol_dashboard import news_catalog as catalog


class NewsCatalogTests(unittest.TestCase):
    def test_spacex_and_its_brands_map_to_verified_spcx_not_spx(self):
        for title in ("SpaceX launches satellites", "Starlink signs deal", "Starship flight test", "星舰再试飞", "SPCX shares rise"):
            with self.subTest(title=title):
                item = catalog.classify(title)
                self.assertIn("space", item["topic_tags"])
                self.assertIn("US:SPCX", item["instrument_keys"])
                self.assertNotIn("US:SPY", item["instrument_keys"])
                direct = next(asset for asset in item["assets"] if asset["asset_key"] == "US:SPCX")
                self.assertEqual(direct["relation"], "direct_mention")
                self.assertTrue(direct["matched_terms"])
        self.assertNotIn("US:SPCX", catalog.classify("SPX climbs to a record")["instrument_keys"])

    def test_broad_sector_news_has_related_watchlist_not_invented_mentions(self):
        item = catalog.classify("AI chip demand accelerates", "Semiconductors power cloud growth")
        self.assertEqual(item["instrument_keys"], [])
        self.assertIn("ai_chips", item["topic_tags"])
        self.assertTrue(item["assets"])
        self.assertTrue(all(asset["relation"] == "topic_related" and not asset["matched_terms"] for asset in item["assets"]))
        self.assertTrue(all("不表示" in asset["note"] for asset in item["assets"]))

    def test_company_mentions_and_tickers_have_boundaries(self):
        self.assertIn("US:NVDA", catalog.classify("Nvidia earnings beat estimates")["instrument_keys"])
        self.assertIn("US:NVDA", catalog.classify("$NVDA rises")["instrument_keys"])
        self.assertIn("US:MSFT", catalog.classify("微软发布新产品")["instrument_keys"])
        item = catalog.classify("metadata and spies", "notNVDA and QQQextra and spcx")
        self.assertEqual(item["instrument_keys"], [])

    def test_titlecase_meta_requires_company_context(self):
        for title in ("Meta Reports Higher Earnings", "Meta is considering layoffs",
                      "Meta announces new AI products"):
            with self.subTest(title=title):
                item = catalog.classify(title)
                self.assertIn("US:META", item["instrument_keys"])
                asset = next(asset for asset in item["assets"] if asset["asset_key"] == "US:META")
                self.assertEqual(asset["relation"], "direct_mention")
                self.assertIn("Meta", asset["matched_terms"])

    def test_generic_meta_language_is_not_a_company_mention(self):
        for title in ("metadata reports show changes", "meta-analysis of company earnings",
                      "Meta-analysis Reports Higher Earnings", "Meta analysis reports higher earnings",
                      "meta announces new AI products"):
            with self.subTest(title=title):
                self.assertNotIn("US:META", catalog.classify(title)["instrument_keys"])

    def test_index_or_commodity_is_not_an_etf_mention(self):
        item = catalog.classify("Gold and copper rise; S&P 500 ends higher")
        self.assertEqual(item["instrument_keys"], [])
        assets = {asset["asset_key"]: asset for asset in item["assets"]}
        self.assertEqual(assets["US:GLD"]["relation"], "topic_related")
        self.assertEqual(assets["US:COPX"]["relation"], "topic_related")
        self.assertNotIn("US:SLV", assets)
        actual = catalog.classify("SPDR Gold Shares draws inflows")
        self.assertIn("US:GLD", actual["instrument_keys"])

    def test_topic_and_asset_filters_require_explicit_related_opt_in(self):
        self.assertTrue(catalog.matches_topic("铜价继续走高", "", "commodities"))
        self.assertFalse(catalog.matches_instrument("Copper rallies", "", "US:COPX"))
        self.assertTrue(catalog.matches_instrument("Copper rallies", "", "US:COPX", True))
        self.assertTrue(catalog.matches_instrument("COPX rallies", "", "US:COPX"))

    def test_catalog_counts_and_spacex_verification_are_explicit(self):
        result = catalog.catalog([{"title": "SpaceX satellite launch", "snippet": ""},
                                  {"title": "Blue Origin rocket launch", "snippet": ""}])
        space = next(topic for topic in result["topics"] if topic["key"] == "space")
        self.assertEqual(space["count"], 2)
        spacex = next(asset for asset in result["instruments"] if asset["key"] == "US:SPCX")
        self.assertEqual((spacex["direct_count"], spacex["related_count"]), (1, 1))
        self.assertEqual(spacex["ticker"], "SPCX")
        self.assertEqual(spacex["verified_at"], "2026-10-13")
        self.assertTrue(all(url.startswith("https://") for url in spacex["verification_sources"]))

    def test_caller_mutations_do_not_poison_cached_classification(self):
        first = catalog.classify("SpaceX launches Starship")
        first["topic_tags"].clear()
        first["assets"][0]["matched_terms"].clear()
        again = catalog.classify("SpaceX launches Starship")
        self.assertIn("space", again["topic_tags"])
        self.assertTrue(again["assets"][0]["matched_terms"])


if __name__ == "__main__":
    unittest.main()
