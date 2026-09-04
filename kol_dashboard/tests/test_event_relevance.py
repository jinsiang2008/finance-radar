from __future__ import annotations

import unittest

from kol_dashboard.event_relevance import assess_event_relevance


def event(
    title: str,
    *,
    kol_key: str = "zuckerberg",
    kol_name: str = "Mark Zuckerberg",
    kol_name_cn: str = "扎克伯格",
    snippet: str = "",
    source: str = "Bing News",
    url: str = "https://example.com/story",
) -> dict[str, str]:
    return {
        "title": title,
        "snippet": snippet,
        "source": source,
        "url": url,
        "kol_key": kol_key,
        "kol_name": kol_name,
        "kol_name_cn": kol_name_cn,
    }


class EventRelevanceTests(unittest.TestCase):
    def test_tullamore_crash_is_neither_attributable_nor_financial(self) -> None:
        result = assess_event_relevance(event(
            "Man hospitalised after train strikes tractor in Tullamore crash",
            snippet="A local road was closed after the collision.",
        ))

        self.assertFalse(result["eligible"])
        self.assertFalse(result["entity_match"])
        self.assertFalse(result["finance_relevant"])
        self.assertFalse(result["intelligence_eligible"])
        self.assertEqual(result["rule_impact"], "low")
        self.assertEqual(result["reason"], "entity_not_found")

    def test_named_person_car_crash_does_not_become_market_impact(self) -> None:
        result = assess_event_relevance(event(
            "Elon Musk unhurt after car crash on highway",
            kol_key="musk",
            kol_name="Elon Musk",
            kol_name_cn="马斯克",
        ))

        self.assertTrue(result["eligible"])
        self.assertFalse(result["finance_relevant"])
        self.assertEqual(result["rule_impact"], "low")

    def test_stock_market_crash_is_high_with_explicit_person_context(self) -> None:
        result = assess_event_relevance(event(
            "Zuckerberg warns a stock market crash is possible",
        ))

        self.assertTrue(result["intelligence_eligible"])
        self.assertEqual(result["rule_impact"], "high")

    def test_meta_shares_crash_is_company_signal_not_person_quote(self) -> None:
        result = assess_event_relevance(event(
            "Meta shares crash after guidance cut",
        ))

        self.assertTrue(result["eligible"])
        self.assertTrue(result["finance_relevant"])
        self.assertEqual(result["attribution_basis"], "company_mention")
        self.assertEqual(result["matched_alias"], "Meta")
        self.assertEqual(result["rule_impact"], "high")

    def test_zuckerberg_capex_statement_is_financial_but_not_a_crisis(self) -> None:
        result = assess_event_relevance(event(
            "Zuckerberg says Meta will raise AI capex next year",
        ))

        self.assertTrue(result["intelligence_eligible"])
        self.assertEqual(result["attribution_basis"], "person_mention")
        self.assertEqual(result["rule_impact"], "medium")

    def test_ai_bubble_requires_and_has_a_market_theme_context(self) -> None:
        result = assess_event_relevance(event(
            "Zuckerberg warns the AI bubble may collapse",
        ))

        self.assertTrue(result["intelligence_eligible"])
        self.assertEqual(result["rule_impact"], "high")

    def test_culture_war_is_not_treated_as_geopolitical_impact(self) -> None:
        result = assess_event_relevance(event(
            "Zuckerberg comments on the latest culture war",
        ))

        self.assertTrue(result["eligible"])
        self.assertFalse(result["finance_relevant"])
        self.assertEqual(result["rule_impact"], "low")

    def test_meta_analysis_is_not_a_meta_company_match(self) -> None:
        result = assess_event_relevance(event(
            "New meta-analysis reviews sleep patterns in adults",
        ))

        self.assertFalse(result["eligible"])
        self.assertFalse(result["entity_match"])
        self.assertFalse(result["finance_relevant"])

    def test_company_social_event_stays_low(self) -> None:
        result = assess_event_relevance(event("Meta hosts its annual holiday party"))

        self.assertTrue(result["eligible"])
        self.assertFalse(result["finance_relevant"])
        self.assertFalse(result["intelligence_eligible"])
        self.assertEqual(result["rule_impact"], "low")

    def test_ambiguous_social_and_legal_words_are_not_finance_signals(self) -> None:
        cases = (
            ("Elon Musk shares family photos from a Christmas market", "musk", "Elon Musk"),
            ("Court orders Elon Musk to attend custody hearing", "musk", "Elon Musk"),
            ("Mark Zuckerberg funds a local hospital charity drive", "zuckerberg", "Mark Zuckerberg"),
            ("Mark Zuckerberg visits a farmers market", "zuckerberg", "Mark Zuckerberg"),
            ("Elon Musk visits an oil painting exhibition", "musk", "Elon Musk"),
            ("Elon Musk lists personal assets during a divorce hearing", "musk", "Elon Musk"),
            ("Mark Zuckerberg book sales rise after a reading", "zuckerberg", "Mark Zuckerberg"),
            ("Mark Zuckerberg launches a charity platform", "zuckerberg", "Mark Zuckerberg"),
            ("Elon Musk announces the baby name Model X", "musk", "Elon Musk"),
            ("Jensen Huang opens a semiconductor museum", "huangrenxun", "Jensen Huang"),
            ("Warren Buffett buys Apple pie", "buffett", "Warren Buffett"),
            ("Warren Buffett buys an Apple Watch", "buffett", "Warren Buffett"),
            ("Cathie Wood buys a Tesla for her daughter", "cathiewood", "Cathie Wood"),
            ("Elon Musk bought lunch after a Tesla event", "musk", "Elon Musk"),
            ("Elon Musk announces a school teaching model", "musk", "Elon Musk"),
            ("Jerome Powell holds his heart rate steady", "powell", "Jerome Powell"),
            ("Mark Zuckerberg announces a Meta holiday video", "zuckerberg", "Mark Zuckerberg"),
            ("黄仁勋参观当地菜市场", "huangrenxun", "Jensen Huang"),
            ("任泽平出席人才市场", "renzeping", "任泽平"),
            ("鲍威尔乘坐经济舱", "powell", "Jerome Powell"),
            ("巴菲特基金会举办慈善晚宴", "buffett", "Warren Buffett"),
            (
                "Jerome Powell strengthens bond with students at school",
                "powell",
                "Jerome Powell",
            ),
            (
                "Warren Buffett stocks pantry shelves for charity",
                "buffett",
                "Warren Buffett",
            ),
            (
                "Warren Buffett shares a portfolio of watercolor paintings",
                "buffett",
                "Warren Buffett",
            ),
        )

        for title, kol_key, kol_name in cases:
            with self.subTest(title=title):
                result = assess_event_relevance(event(
                    title,
                    kol_key=kol_key,
                    kol_name=kol_name,
                    kol_name_cn=kol_name,
                ))
                self.assertTrue(result["eligible"])
                self.assertFalse(result["finance_relevant"])
                self.assertFalse(result["intelligence_eligible"])

    def test_surname_homonyms_do_not_create_kol_attribution(self) -> None:
        cases = (
            (
                "Stocks trump bonds after rate-cut expectations",
                "trump",
                "Donald Trump",
            ),
            (
                "Stocks Trump Bonds After Rate-Cut Expectations",
                "trump",
                "Donald Trump",
            ),
            (
                "Equities Trump Treasuries as Risk Appetite Rises",
                "trump",
                "Donald Trump",
            ),
            (
                "STOCKS TRUMP BONDS AFTER RATE CUT",
                "trump",
                "Donald Trump",
            ),
            ("Stocks May Trump Bonds in 2026", "trump", "Donald Trump"),
            ("Why Stocks Could Trump Bonds Again", "trump", "Donald Trump"),
            (
                "Equities Will Trump Treasuries as Rates Fall",
                "trump",
                "Donald Trump",
            ),
            (
                "Stocks Still Trump Bonds for Long-Term Returns",
                "trump",
                "Donald Trump",
            ),
            (
                "Growth Stocks Easily Trump Value Peers",
                "trump",
                "Donald Trump",
            ),
            (
                "Powell Industries stock rises after earnings",
                "powell",
                "Jerome Powell",
            ),
        )

        for title, kol_key, kol_name in cases:
            with self.subTest(title=title):
                result = assess_event_relevance(event(
                    title,
                    kol_key=kol_key,
                    kol_name=kol_name,
                    kol_name_cn=kol_name,
                ))
                self.assertFalse(result["entity_match"])
                self.assertFalse(result["intelligence_eligible"])
                self.assertEqual(result["reason"], "entity_not_found")

    def test_trump_person_phrases_survive_verb_disambiguation(self) -> None:
        for title in (
            "Trump says stocks could rise after tariff talks",
            "President Trump announces a tariff policy",
            "Donald Trump comments on the stock market",
        ):
            with self.subTest(title=title):
                result = assess_event_relevance(event(
                    title,
                    kol_key="trump",
                    kol_name="Donald Trump",
                    kol_name_cn="特朗普",
                ))
                self.assertTrue(result["entity_match"])
                self.assertTrue(result["intelligence_eligible"])

    def test_title_only_market_actions_and_business_metrics_are_kept(self) -> None:
        cases = (
            ("Powell holds rates steady", "powell", "Jerome Powell"),
            ("Michael Burry shorts Tesla", "burry", "Michael Burry"),
            ("Cathie Wood buys Tesla", "cathiewood", "Cathie Wood"),
            ("Warren Buffett trims Apple stake", "buffett", "Warren Buffett"),
            ("Ray Dalio adds China exposure", "dalio", "Ray Dalio"),
            ("Jensen Huang says Blackwell shipments ramp", "huangrenxun", "Jensen Huang"),
            ("Lisa Su says AMD margins improve", "suzifeng", "Lisa Su"),
            ("Elon Musk says Tesla deliveries rose", "musk", "Elon Musk"),
            ("Michael Burry 做空特斯拉", "burry", "Michael Burry"),
            ("Cathie Wood 加仓特斯拉", "cathiewood", "Cathie Wood"),
            ("Warren Buffett 减持苹果股份", "buffett", "Warren Buffett"),
            ("Jensen Huang 表示 Blackwell 出货加速", "huangrenxun", "Jensen Huang"),
        )

        for title, kol_key, kol_name in cases:
            with self.subTest(title=title):
                result = assess_event_relevance(event(
                    title,
                    kol_key=kol_key,
                    kol_name=kol_name,
                    kol_name_cn=kol_name,
                ))
                self.assertTrue(result["finance_relevant"])
                self.assertTrue(result["intelligence_eligible"])

    def test_central_bank_rate_hold_is_high_impact(self) -> None:
        result = assess_event_relevance(event(
            "Powell holds rates steady",
            kol_key="powell",
            kol_name="Jerome Powell",
            kol_name_cn="鲍威尔",
        ))

        self.assertTrue(result["intelligence_eligible"])
        self.assertEqual(result["rule_impact"], "high")

    def test_company_fundamentals_are_kept_without_generic_keyword_noise(self) -> None:
        cases = (
            ("NVIDIA gains AI market share", "huangrenxun", "Jensen Huang"),
            ("Tesla vehicle sales fall", "musk", "Elon Musk"),
            ("Tesla cuts prices", "musk", "Elon Musk"),
            ("NVIDIA delays Blackwell", "huangrenxun", "Jensen Huang"),
            ("Tesla recalls vehicles", "musk", "Elon Musk"),
            ("OpenAI funding round", "altman", "Sam Altman"),
            ("NVIDIA wins contract", "huangrenxun", "Jensen Huang"),
            ("NVIDIA wins a cloud GPU contract", "huangrenxun", "Jensen Huang"),
            ("Tesla misses delivery estimates", "musk", "Elon Musk"),
            ("特斯拉销量下滑", "musk", "Elon Musk"),
            ("特斯拉降价", "musk", "Elon Musk"),
            ("特斯拉召回车辆", "musk", "Elon Musk"),
            ("英伟达市占率提升", "huangrenxun", "Jensen Huang"),
            ("OpenAI融资", "altman", "Sam Altman"),
        )

        for title, kol_key, kol_name in cases:
            with self.subTest(title=title):
                result = assess_event_relevance(event(
                    title,
                    kol_key=kol_key,
                    kol_name=kol_name,
                    kol_name_cn=kol_name,
                ))
                self.assertTrue(result["intelligence_eligible"])
                self.assertEqual(result["rule_impact"], "medium")

    def test_named_kol_product_launches_are_business_signals(self) -> None:
        cases = (
            ("Jensen Huang unveils a new AI platform", "huangrenxun", "Jensen Huang"),
            ("Sam Altman announces GPT-6 model", "altman", "Sam Altman"),
            ("Mark Zuckerberg launches a new Meta product", "zuckerberg", "Mark Zuckerberg"),
            ("Lisa Su unveils a new AMD processor", "suzifeng", "Lisa Su"),
        )

        for title, kol_key, kol_name in cases:
            with self.subTest(title=title):
                result = assess_event_relevance(event(
                    title,
                    kol_key=kol_key,
                    kol_name=kol_name,
                    kol_name_cn=kol_name,
                ))
                self.assertTrue(result["intelligence_eligible"])
                self.assertEqual(result["rule_impact"], "medium")

    def test_owned_direct_product_post_is_a_business_signal(self) -> None:
        result = assess_event_relevance(
            event(
                kol_key="musk",
                kol_name="Elon Musk",
                kol_name_cn="马斯克",
                title="NVIDIA launches enterprise AI platform",
                snippet="The product supports enterprise AI deployments.",
                source="X @elonmusk",
                url="https://x.com/elonmusk/status/123",
            )
        )

        self.assertTrue(result["intelligence_eligible"])
        self.assertEqual(result["attribution_basis"], "direct_source")
        self.assertEqual(result["rule_impact"], "medium")

    def test_owned_direct_product_and_trading_posts_are_kept(self) -> None:
        cases = (
            event(
                "Launching Grok 5 next week",
                kol_key="musk",
                kol_name="Elon Musk",
                kol_name_cn="马斯克",
                source="X @elonmusk",
                url="https://x.com/elonmusk/status/grok-5",
            ),
            event(
                "Bought NVDA at the close",
                kol_key="serenity",
                kol_name="Serenity",
                kol_name_cn="Serenity",
                source="X @aleabitoreddit",
                url="https://x.com/aleabitoreddit/status/bought-nvda",
            ),
            event(
                "Long NVDA",
                kol_key="serenity",
                kol_name="Serenity",
                kol_name_cn="Serenity",
                source="X @aleabitoreddit",
                url="https://x.com/aleabitoreddit/status/long-nvda",
            ),
        )

        for item in cases:
            with self.subTest(title=item["title"]):
                result = assess_event_relevance(item)
                self.assertTrue(result["intelligence_eligible"])
                self.assertEqual(result["attribution_basis"], "direct_source")

    def test_nvidia_product_launch_remains_a_valid_company_signal(self) -> None:
        result = assess_event_relevance(event(
            "NVIDIA launches a new Blackwell platform",
            kol_key="huangrenxun",
            kol_name="Jensen Huang",
            kol_name_cn="黄仁勋",
        ))

        self.assertTrue(result["intelligence_eligible"])
        self.assertEqual(result["attribution_basis"], "company_mention")
        self.assertEqual(result["rule_impact"], "medium")

    def test_one_word_war_post_from_trump_is_policy_context(self) -> None:
        result = assess_event_relevance(event(
            "WAR",
            kol_key="trump",
            kol_name="Donald Trump",
            kol_name_cn="特朗普",
            source="Truth Social @realDonaldTrump",
            url="https://truthsocial.com/@realDonaldTrump/123",
        ))

        self.assertTrue(result["eligible"])
        self.assertTrue(result["intelligence_eligible"])
        self.assertEqual(result["attribution_basis"], "direct_source")
        self.assertEqual(result["rule_impact"], "high")

    def test_direct_source_bypass_requires_the_expected_handle(self) -> None:
        result = assess_event_relevance(event(
            "WAR",
            kol_key="zuckerberg",
            source="Truth Social @realDonaldTrump",
            url="https://truthsocial.com/@realDonaldTrump/123",
        ))

        self.assertFalse(result["eligible"])
        self.assertEqual(result["attribution_basis"], "missing")

    def test_musk_x_source_requires_and_accepts_the_official_handle(self) -> None:
        owned = assess_event_relevance(event(
            "AI investment demand remains strong",
            kol_key="musk",
            kol_name="Elon Musk",
            kol_name_cn="马斯克",
            source="X @elonmusk",
            url="https://x.com/elonmusk/status/123",
        ))
        impostor = assess_event_relevance(event(
            "AI investment demand remains strong",
            kol_key="musk",
            kol_name="Elon Musk",
            kol_name_cn="马斯克",
            source="X @notelonmusk",
            url="https://x.com/notelonmusk/status/123",
        ))

        self.assertEqual(owned["attribution_basis"], "direct_source")
        self.assertTrue(owned["intelligence_eligible"])
        self.assertNotEqual(impostor["attribution_basis"], "direct_source")


if __name__ == "__main__":
    unittest.main()
