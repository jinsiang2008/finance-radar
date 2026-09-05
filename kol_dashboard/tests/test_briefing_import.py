from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from kol_dashboard import briefing_import, briefing_topics, db


class BriefingImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 9, 5, 2, 0, tzinfo=timezone.utc)

    def payload(self) -> dict:
        return {
            "schema_version": 1,
            "snapshot_date": "2026-09-05",
            "generated_at": self.now.isoformat(),
            "source_as_of": self.now.isoformat(),
            "sections": {
                section: [] for section in briefing_import.SECTION_KEYS
            },
        }

    def item(self, **overrides) -> dict:
        item = {
            "title": "Federal Reserve publishes September policy update",
            "source": "Federal Reserve",
            "source_url": (
                "HTTPS://www.federalreserve.gov/newsevents/pressreleases/"
                "monetary20260905a.htm?utm_source=newsletter"
            ),
            "published_at": (self.now - timedelta(hours=1)).isoformat(),
            "source_tier": "official",
            "summary": "The official policy statement was published.",
            "why": "The decision can change global funding conditions.",
            "assets": ["US:SPY", "USD", "us:spy"],
        }
        item.update(overrides)
        return item

    @staticmethod
    def enhancement(**overrides) -> dict:
        value = {
            "title_zh": "Python 发布新的并发运行时方案",
            "summary_basis": "title_only",
            "content_category": "software_dev",
            "content_tags": ["python", "engineering_practice"],
            "taxonomy_version": briefing_topics.TAXONOMY_VERSION,
            "translation_status": "translated",
        }
        value.update(overrides)
        return value

    def test_validation_canonicalizes_and_deduplicates_across_sections(self) -> None:
        payload = self.payload()
        payload["sections"]["macro"].append(self.item())
        payload["sections"]["world"].append(
            self.item(
                source="Federal Reserve",
                source_url=(
                    "https://www.federalreserve.gov/newsevents/pressreleases/"
                    "monetary20260905a.htm?utm_medium=syndication"
                ),
                source_tier="official",
                summary="A second source reports the same policy event.",
                assets=["US:SPY", "US:QQQ"],
            )
        )

        normalized = briefing_import.validate_payload(payload, now=self.now)

        self.assertEqual(len(normalized["sections"]["macro"]), 1)
        self.assertEqual(normalized["sections"]["world"], [])
        item = normalized["sections"]["macro"][0]
        self.assertEqual(item["section"], "macro")
        self.assertEqual(item["cross_tags"], ["world"])
        self.assertEqual(item["source_count"], 1)
        self.assertEqual(item["source_tier"], "official")
        self.assertEqual(item["time_status"], "verified")
        self.assertEqual(
            item["canonical_url"],
            "https://www.federalreserve.gov/newsevents/pressreleases/"
            "monetary20260905a.htm",
        )
        self.assertEqual(item["source_url"], item["canonical_url"])
        self.assertEqual(item["assets"], ["US:SPY", "USD", "US:QQQ"])
        self.assertEqual(len(item["story_key"]), 64)
        self.assertEqual(
            item["why_it_matters"],
            "The decision can change global funding conditions.",
        )

    def test_content_enhancement_is_normalized_without_changing_story_identity(
        self,
    ) -> None:
        plain = self.payload()
        plain["sections"]["technology"].append(
            self.item(
                title="Python publishes a new concurrency runtime design",
                source="Example Engineering",
                source_url="https://example.com/engineering/python-runtime-design",
                source_tier="media",
            )
        )
        enhanced = self.payload()
        enhanced["sections"]["technology"].append(
            self.item(
                title="Python publishes a new concurrency runtime design",
                source="Example Engineering",
                source_url="https://example.com/engineering/python-runtime-design",
                source_tier="media",
                **self.enhancement(
                    content_category=" SOFTWARE_DEV ",
                    content_tags=[" PYTHON ", "engineering_practice"],
                ),
            )
        )

        plain_item = briefing_import.validate_payload(plain, now=self.now)[
            "sections"
        ]["technology"][0]
        enhanced_item = briefing_import.validate_payload(enhanced, now=self.now)[
            "sections"
        ]["technology"][0]

        self.assertEqual(enhanced_item["story_key"], plain_item["story_key"])
        self.assertEqual(enhanced_item["title"], plain_item["title"])
        self.assertEqual(enhanced_item["title_zh"], "Python 发布新的并发运行时方案")
        self.assertNotIn("summary_zh", enhanced_item)
        self.assertEqual(enhanced_item["summary_basis"], "title_only")
        self.assertEqual(enhanced_item["content_category"], "software_dev")
        self.assertEqual(
            enhanced_item["content_tags"],
            ["python", "engineering_practice"],
        )
        self.assertEqual(
            enhanced_item["taxonomy_version"], briefing_topics.TAXONOMY_VERSION
        )
        self.assertEqual(enhanced_item["translation_status"], "translated")

    def test_curated_source_chinese_and_hn_self_post_bases_are_supported(self) -> None:
        payload = self.payload()
        payload["sections"]["ai"].append(
            self.item(
                title="A curated multimodal model release",
                source="AI Digest",
                source_url="https://ai-digest.liziran.com/zh/example",
                original_url="https://example.com/releases/multimodal-model-2026",
                published_at=None,
                fetched_at=self.now.isoformat(),
                featured_at=(self.now - timedelta(minutes=5)).isoformat(),
                source_tier="discovery",
                kind="ai_digest",
                discovered_via=["ai_digest_rss"],
                publication_time_verified=False,
                summary_zh="来源策展段落已用中文概括模型发布及其已知边界。",
                summary_basis="curated_excerpt",
                content_category="ai_ml",
                content_tags=["multimodal", "product_release"],
                taxonomy_version=briefing_topics.TAXONOMY_VERSION,
                translation_status="source_zh",
            )
        )
        payload["sections"]["technology"].append(
            self.item(
                title="Ask HN: How should teams review production incidents?",
                source="Hacker News",
                source_url="https://news.ycombinator.com/item?id=123456",
                original_url=None,
                discussion_url="https://news.ycombinator.com/item?id=123456",
                fetched_at=self.now.isoformat(),
                featured_at=(self.now - timedelta(minutes=2)).isoformat(),
                source_tier="discovery",
                kind="hn_story",
                discovered_via=["hacker_news_top"],
                publication_time_verified=True,
                hn_id=123456,
                hn_score=20,
                hn_comments=8,
                hn_rank=6,
                heat_score=30.0,
                title_zh="HN 讨论：团队应如何复盘生产事故",
                summary_zh="帖子讨论了事故复盘的组织方式、证据边界和后续行动。",
                summary_basis="self_post",
                content_category="org_management",
                content_tags=["incident_review", "engineering_management"],
                taxonomy_version=briefing_topics.TAXONOMY_VERSION,
                translation_status="translated",
            )
        )

        normalized = briefing_import.validate_payload(payload, now=self.now)

        curated = normalized["sections"]["ai"][0]
        self.assertEqual(curated["translation_status"], "source_zh")
        self.assertNotIn("title_zh", curated)
        self.assertIn("summary_zh", curated)
        self_post = normalized["sections"]["technology"][0]
        self.assertEqual(self_post["summary_basis"], "self_post")
        self.assertEqual(self_post["translation_status"], "translated")

    def test_content_enhancement_relationships_and_taxonomy_fail_closed(self) -> None:
        invalid_enhancements = (
            self.enhancement(summary_zh="不能把标题扩写成文章摘要。"),
            self.enhancement(summary_basis="curated_excerpt"),
            self.enhancement(summary_basis="self_post"),
            self.enhancement(title_zh=None),
            self.enhancement(translation_status="unavailable"),
            self.enhancement(
                title_zh=None,
                translation_status="source_zh",
            ),
            self.enhancement(translation_status="pending"),
            self.enhancement(taxonomy_version="daily-content-v0"),
            self.enhancement(content_category="unknown_topic"),
            self.enhancement(content_tags=["python", "python"]),
            self.enhancement(content_tags=["python", "rust", "go"]),
            self.enhancement(content_tags="python"),
            self.enhancement(title_zh="Python concurrency runtime"),
        )
        for enhancement in invalid_enhancements:
            with self.subTest(enhancement=enhancement):
                payload = self.payload()
                payload["sections"]["technology"].append(
                    self.item(
                        title="Python publishes a concurrency runtime design",
                        source="Example Engineering",
                        source_url="https://example.com/engineering/runtime-design",
                        source_tier="media",
                        **enhancement,
                    )
                )
                with self.assertRaises(briefing_import.BriefingValidationError):
                    briefing_import.validate_payload(payload, now=self.now)

    def test_dedup_preserves_one_atomic_same_trust_enhancement_bundle(self) -> None:
        payload = self.payload()
        common = {
            "title": "Python publishes a new concurrency runtime design",
            "source": "Example Engineering",
            "source_url": "https://example.com/engineering/python-runtime-design",
            "source_tier": "media",
        }
        payload["sections"]["technology"].extend(
            [
                self.item(
                    **common,
                    **self.enhancement(
                        title_zh=None,
                        translation_status="unavailable",
                        content_category="general_interest",
                        content_tags=[],
                    ),
                ),
                self.item(**common, **self.enhancement()),
            ]
        )

        item = briefing_import.validate_payload(payload, now=self.now)["sections"][
            "technology"
        ][0]

        self.assertEqual(item["translation_status"], "translated")
        self.assertEqual(item["title_zh"], "Python 发布新的并发运行时方案")
        self.assertEqual(item["content_category"], "software_dev")
        self.assertEqual(
            item["content_tags"], ["python", "engineering_practice"]
        )

        guarded = self.payload()
        official_common = {
            **common,
            "source": "Federal Reserve",
            "source_url": (
                "https://www.federalreserve.gov/newsevents/pressreleases/"
                "monetary20260905a.htm"
            ),
        }
        guarded["sections"]["technology"].extend(
            [
                self.item(**{**official_common, "source_tier": "official"}),
                self.item(
                    **{**official_common, "source_tier": "discovery"},
                    **self.enhancement(),
                ),
            ]
        )
        guarded_item = briefing_import.validate_payload(guarded, now=self.now)[
            "sections"
        ]["technology"][0]
        self.assertEqual(guarded_item["source_tier"], "official")
        self.assertNotIn("translation_status", guarded_item)

    def test_generic_same_day_titles_on_different_urls_stay_separate(self) -> None:
        payload = self.payload()
        payload["sections"]["macro"].append(
            self.item(
                title="Market Update",
                source="Reuters",
                source_url="https://www.reuters.com/world/market-update-one",
                source_tier="media",
            )
        )
        payload["sections"]["finance"].append(
            self.item(
                title="Market Update",
                source="Reuters",
                source_url="https://www.reuters.com/markets/market-update-two",
                source_tier="media",
            )
        )
        payload["sections"]["technology"].append(
            self.item(
                title="OpenAI Update",
                source="Reuters",
                source_url="https://www.reuters.com/technology/openai-update-one",
                source_tier="media",
            )
        )
        payload["sections"]["ai"].append(
            self.item(
                title="OpenAI Update",
                source="Reuters",
                source_url="https://www.reuters.com/technology/openai-update-two",
                source_tier="media",
            )
        )

        normalized = briefing_import.validate_payload(payload, now=self.now)

        self.assertEqual(len(normalized["sections"]["macro"]), 1)
        self.assertEqual(len(normalized["sections"]["finance"]), 1)
        self.assertEqual(len(normalized["sections"]["technology"]), 1)
        self.assertEqual(len(normalized["sections"]["ai"]), 1)
        self.assertEqual(normalized["sections"]["macro"][0]["source_count"], 1)
        self.assertEqual(normalized["sections"]["finance"][0]["source_count"], 1)

    def test_dedup_has_no_transitive_bridge_and_uses_specific_section_priority(self) -> None:
        payload = self.payload()
        payload["sections"]["technology"].append(
            self.item(
                title="Alpha platform update",
                source_url="https://example.com/shared",
                source_tier="media",
            )
        )
        payload["sections"]["technology"].append(
            self.item(
                title="OpenAI model update",
                source_url="https://example.com/shared",
                source_tier="media",
            )
        )
        payload["sections"]["ai"].append(
            self.item(
                title="OpenAI model update",
                source_url="https://example.com/shared",
                source_tier="media",
            )
        )
        payload["sections"]["technology"].append(
            self.item(
                title="OpenAI releases reasoning model",
                source_url="https://example.com/reasoning-story",
                source_tier="media",
            )
        )
        payload["sections"]["ai"].append(
            self.item(
                title="OpenAI releases reasoning model",
                source_url="https://example.com/reasoning-story",
                source_tier="media",
            )
        )
        payload["sections"]["finance"].append(
            self.item(
                title="Warren Buffett quarterly holding disclosure",
                source_url="https://example.com/investor-story",
                source_tier="media",
            )
        )
        payload["sections"]["investors"].append(
            self.item(
                title="Warren Buffett quarterly holding disclosure",
                source_url="https://example.com/investor-story",
                source_tier="media",
            )
        )

        normalized = briefing_import.validate_payload(payload, now=self.now)

        technology = normalized["sections"]["technology"]
        ai = normalized["sections"]["ai"]
        investors = normalized["sections"]["investors"]
        self.assertEqual(len(technology), 1)
        self.assertEqual(len(ai), 2)
        bridged = next(item for item in ai if item["title"] == "OpenAI model update")
        self.assertIn("technology", bridged["cross_tags"])
        prioritized = next(
            item for item in ai if item["title"] == "OpenAI releases reasoning model"
        )
        self.assertIn("technology", prioritized["cross_tags"])
        self.assertEqual(len(investors), 1)
        self.assertIn("finance", investors[0]["cross_tags"])

    def test_story_key_is_stable_when_title_changes_at_the_same_url(self) -> None:
        first = self.payload()
        first["sections"]["world"].append(
            self.item(
                title="Zeta headline",
                source_url="https://example.com/stable-story",
                source_tier="media",
            )
        )
        revised = self.payload()
        revised["sections"]["world"].append(
            self.item(
                title="Alpha corrected headline",
                source_url="https://example.com/stable-story",
                source_tier="media",
            )
        )

        first_key = briefing_import.validate_payload(first, now=self.now)["sections"][
            "world"
        ][0]["story_key"]
        revised_key = briefing_import.validate_payload(revised, now=self.now)[
            "sections"
        ]["world"][0]["story_key"]

        self.assertEqual(first_key, revised_key)

    def test_reused_landing_url_only_merges_same_day_compatible_titles(self) -> None:
        payload = self.payload()
        shared_url = "https://example.com/company-announcements"
        payload["sections"]["finance"].extend(
            [
                self.item(
                    title="Shared Landing: Earnings Update!",
                    source_url=shared_url,
                    source_tier="media",
                ),
                self.item(
                    title="shared landing earnings update",
                    source_url=shared_url,
                    source_tier="media",
                ),
                self.item(
                    title="Shared Landing Earnings Update",
                    source_url=shared_url,
                    published_at=(self.now - timedelta(days=1)).isoformat(),
                    source_tier="media",
                ),
                self.item(
                    title="Company releases Atlas model",
                    source_url=shared_url,
                    source_tier="media",
                ),
                self.item(
                    title="Company does not release Atlas model",
                    source_url=shared_url,
                    source_tier="media",
                ),
                self.item(
                    title="Company recalls Atlas model",
                    source_url=shared_url,
                    source_tier="media",
                ),
            ]
        )

        items = briefing_import.validate_payload(payload, now=self.now)["sections"][
            "finance"
        ]

        self.assertEqual(len(items), 5)
        self.assertEqual(len({item["story_key"] for item in items}), 5)
        self.assertEqual(
            sum(
                item["title"].casefold().startswith("shared landing")
                for item in items
            ),
            2,
        )
        self.assertEqual(
            {
                item["title"]
                for item in items
                if "Atlas model" in item["title"]
            },
            {
                "Company releases Atlas model",
                "Company does not release Atlas model",
                "Company recalls Atlas model",
            },
        )

    def test_fetched_only_item_is_not_mislabeled_as_verified_publication(self) -> None:
        payload = self.payload()
        payload["sections"]["technology"].append(
            self.item(
                published_at=None,
                fetched_at=self.now.isoformat(),
                source_tier="discovery",
            )
        )

        item = briefing_import.validate_payload(payload, now=self.now)["sections"][
            "technology"
        ][0]

        self.assertIsNone(item["published_at"])
        self.assertEqual(item["fetched_at"], self.now.isoformat())
        self.assertEqual(item["time_status"], "fetched_only")
        self.assertEqual(item["last_updated_at"], self.now.isoformat())

    def test_published_item_freshness_is_not_advanced_by_fetch_time(self) -> None:
        published = self.now - timedelta(hours=6)
        payload = self.payload()
        payload["sections"]["finance"].append(
            self.item(
                published_at=published.isoformat(),
                fetched_at=self.now.isoformat(),
            )
        )

        item = briefing_import.validate_payload(payload, now=self.now)["sections"][
            "finance"
        ][0]

        self.assertEqual(item["published_at"], published.isoformat())
        self.assertEqual(item["fetched_at"], self.now.isoformat())
        self.assertEqual(item["last_updated_at"], published.isoformat())

    def test_hacker_news_metadata_is_bounded_and_forced_to_discovery(self) -> None:
        payload = self.payload()
        original_url = "https://example.com/chromium-sandbox-rce"
        payload["sections"]["technology"].append(
            self.item(
                source="Hacker News",
                source_url=original_url,
                original_url=original_url,
                discussion_url="https://news.ycombinator.com/item?id=123456",
                source_tier="media",
                kind="hn_story",
                discovered_via=["hacker_news_top", "hacker_news_best"],
                publication_time_verified=True,
                featured_at=(self.now - timedelta(minutes=5)).isoformat(),
                fetched_at=self.now.isoformat(),
                hn_id=123456,
                hn_score=574,
                hn_comments=304,
                hn_rank=2,
                heat_score=88.74,
            )
        )

        item = briefing_import.validate_payload(payload, now=self.now)["sections"][
            "technology"
        ][0]

        self.assertEqual(item["kind"], "hn_story")
        self.assertEqual(
            item["discovered_via"],
            ["hacker_news_top", "hacker_news_best"],
        )
        self.assertEqual(item["source_tier"], "discovery")
        self.assertEqual(item["heat_score"], 88.7)
        self.assertEqual(item["canonical_url"], original_url)
        self.assertEqual(item["time_status"], "verified")

    def test_curated_feed_time_is_not_relabelled_as_publication(self) -> None:
        payload = self.payload()
        featured_at = self.now - timedelta(minutes=10)
        digest_url = "https://ai-digest.liziran.com/zh/2026-09-05-example"
        payload["sections"]["ai"].append(
            self.item(
                source="AI Digest",
                source_url=digest_url,
                original_url="https://openai.com/index/example-release",
                source_tier="official",
                kind="ai_digest",
                discovered_via=["ai_digest_rss"],
                publication_time_verified=False,
                published_at=None,
                featured_at=featured_at.isoformat(),
                fetched_at=self.now.isoformat(),
            )
        )

        item = briefing_import.validate_payload(payload, now=self.now)["sections"][
            "ai"
        ][0]

        self.assertIsNone(item["published_at"])
        self.assertFalse(item["publication_time_verified"])
        self.assertEqual(item["featured_at"], featured_at.isoformat())
        self.assertEqual(item["last_updated_at"], featured_at.isoformat())
        self.assertEqual(item["time_status"], "featured_only")
        self.assertEqual(item["source_url"], digest_url)
        self.assertEqual(
            item["canonical_url"], "https://openai.com/index/example-release"
        )
        self.assertEqual(item["source_tier"], "discovery")

    def test_paper_digest_keeps_t_plus_three_publication_and_cross_source_hn(self) -> None:
        payload = self.payload()
        paper_url = "https://arxiv.org/abs/2609.01234"
        brief_url = "https://ai-brief.liziran.com/zh/2026-09-05-example"
        payload["sections"]["ai"].append(
            self.item(
                source="AI Brief",
                source_url=brief_url,
                original_url=paper_url,
                discussion_url="https://news.ycombinator.com/item?id=654321",
                published_at=(self.now - timedelta(days=3)).isoformat(),
                fetched_at=self.now.isoformat(),
                source_tier="media",
                kind="paper_digest",
                discovered_via=["ai_brief_rss", "hacker_news_best"],
                publication_time_verified=True,
                featured_at=(self.now - timedelta(minutes=15)).isoformat(),
                hn_id=654321,
                hn_score=120,
                hn_comments=44,
                hn_rank=8,
                heat_score=66.0,
            )
        )

        item = briefing_import.validate_payload(payload, now=self.now)["sections"][
            "ai"
        ][0]

        self.assertEqual(
            item["published_at"], (self.now - timedelta(days=3)).isoformat()
        )
        self.assertEqual(item["featured_at"], (self.now - timedelta(minutes=15)).isoformat())
        self.assertEqual(item["canonical_url"], paper_url)
        self.assertEqual(item["hn_rank"], 8)

    def test_discovery_metadata_relationships_fail_closed(self) -> None:
        valid = self.item(
            source="Hacker News",
            source_url="https://example.com/story",
            original_url="https://example.com/story",
            discussion_url="https://news.ycombinator.com/item?id=123",
            source_tier="discovery",
            kind="hn_story",
            discovered_via=["hacker_news_top"],
            publication_time_verified=True,
            featured_at=(self.now - timedelta(minutes=5)).isoformat(),
            fetched_at=self.now.isoformat(),
            hn_id=123,
            hn_score=10,
            hn_comments=2,
            hn_rank=5,
            heat_score=20.0,
        )
        invalid_overrides = (
            {"publication_time_verified": False},
            {"publication_time_verified": False, "published_at": None},
            {"discovered_via": ["ai_digest_rss"]},
            {"discussion_url": "https://news.ycombinator.com/item?id=124"},
            {"hn_score": -1},
            {"hn_rank": 501},
            {"heat_score": 100.1},
        )
        for overrides in invalid_overrides:
            with self.subTest(overrides=overrides):
                payload = self.payload()
                payload["sections"]["technology"].append({**valid, **overrides})
                with self.assertRaises(briefing_import.BriefingValidationError):
                    briefing_import.validate_payload(payload, now=self.now)

        payload = self.payload()
        payload["sections"]["ai"].append(
            self.item(
                source="AI Digest",
                source_url="https://ai-digest.liziran.com/zh/example",
                original_url="https://ai-digest.liziran.com/zh/example",
                source_tier="discovery",
                kind="ai_digest",
                discovered_via=["ai_digest_rss"],
                publication_time_verified=False,
                published_at=None,
                featured_at=(self.now - timedelta(minutes=5)).isoformat(),
                fetched_at=self.now.isoformat(),
            )
        )
        with self.assertRaisesRegex(
            briefing_import.BriefingValidationError, "distinct underlying source"
        ):
            briefing_import.validate_payload(payload, now=self.now)

        payload = self.payload()
        payload["sections"]["ai"].append(
            self.item(
                source="AI Digest",
                source_url="https://ai-digest.liziran.com/zh/example",
                original_url="https://openai.com/",
                source_tier="discovery",
                kind="ai_digest",
                discovered_via=["ai_digest_rss"],
                publication_time_verified=False,
                published_at=None,
                featured_at=(self.now - timedelta(minutes=5)).isoformat(),
                fetched_at=self.now.isoformat(),
            )
        )
        with self.assertRaisesRegex(
            briefing_import.BriefingValidationError, "distinct underlying source"
        ):
            briefing_import.validate_payload(payload, now=self.now)

    def test_curated_items_without_a_specific_original_stay_distinct(self) -> None:
        payload = self.payload()
        for index in range(2):
            payload["sections"]["ai"].append(
                self.item(
                    title=f"Distinct curated story {index}",
                    source="AI Digest",
                    source_url=(
                        f"https://ai-digest.liziran.com/zh/distinct-{index}"
                    ),
                    source_tier="discovery",
                    kind="ai_digest",
                    discovered_via=["ai_digest_rss"],
                    publication_time_verified=False,
                    published_at=None,
                    featured_at=(self.now - timedelta(minutes=5)).isoformat(),
                    fetched_at=self.now.isoformat(),
                )
            )

        items = briefing_import.validate_payload(payload, now=self.now)["sections"][
            "ai"
        ]

        self.assertEqual(len(items), 2)
        self.assertEqual(len({item["canonical_url"] for item in items}), 2)
        self.assertEqual(len({item["story_key"] for item in items}), 2)

    def test_specific_original_url_requires_article_like_identity(self) -> None:
        cases = {
            "https://openai.com/": False,
            "https://openai.com/company-announcements": False,
            "https://openai.com/index": False,
            "https://openai.com/en/company-announcements": False,
            "https://openai.com/product-update": False,
            "https://openai.com/team-announcements": False,
            "https://openai.com/newsroom?story=123": False,
            "https://arxiv.org/abs/2609.01234": True,
            "https://huggingface.co/papers/2609.01234": True,
            "https://openai.com/index/example-release": True,
            "https://openai.com/en/article/new-model": True,
            "https://openai.com/launch-model-2026": True,
            "https://openai.com/release.html": True,
            "https://openai.com/story?id=release": True,
        }

        for url, expected in cases.items():
            with self.subTest(url=url):
                canonical = briefing_import.canonicalize_source_url(url)
                self.assertEqual(
                    briefing_import._specific_original_url(canonical), expected
                )

    def test_investor_disclosure_timestamps_are_strict_and_not_invented(self) -> None:
        disclosed = self.now - timedelta(hours=2)
        effective = (self.now - timedelta(days=30)).date().isoformat()
        payload = self.payload()
        payload["sections"]["investors"].append(
            self.item(
                disclosed_at=disclosed.isoformat(),
                period_end=effective,
            )
        )

        item = briefing_import.validate_payload(payload, now=self.now)["sections"][
            "investors"
        ][0]
        self.assertEqual(item["disclosed_at"], disclosed.isoformat())
        self.assertEqual(item["effective_at"], effective)

        payload = self.payload()
        payload["sections"]["investors"].append(
            self.item(
                effective_at=effective,
                data_as_of=effective,
            )
        )
        item = briefing_import.validate_payload(payload, now=self.now)["sections"][
            "investors"
        ][0]
        self.assertEqual(item["effective_at"], effective)

        timestamp_effective = self.now - timedelta(days=7, hours=3)
        payload = self.payload()
        payload["sections"]["investors"].append(
            self.item(effective_at=timestamp_effective.isoformat())
        )
        item = briefing_import.validate_payload(payload, now=self.now)["sections"][
            "investors"
        ][0]
        self.assertEqual(item["effective_at"], timestamp_effective.isoformat())

        payload = self.payload()
        payload["sections"]["investors"].append(
            self.item(
                disclosed_at=(self.now - timedelta(days=5)).isoformat(),
                period_end=(self.now - timedelta(days=4)).date().isoformat(),
            )
        )
        with self.assertRaisesRegex(
            briefing_import.BriefingValidationError,
            "cannot be after disclosed_at",
        ):
            briefing_import.validate_payload(payload, now=self.now)

        payload = self.payload()
        payload["sections"]["investors"].append(self.item())
        item = briefing_import.validate_payload(payload, now=self.now)["sections"][
            "investors"
        ][0]
        self.assertIsNone(item["disclosed_at"])
        self.assertIsNone(item["effective_at"])

        for overrides in (
            {"disclosed_at": "2026-09-05T01:00:00"},
            {"data_as_of": "2026-02-30"},
            {"effective_at": "2026-06-30T00:00:00"},
            {
                "effective_at": effective,
                "period_end": (
                    datetime.fromisoformat(effective) - timedelta(days=1)
                ).date().isoformat(),
            },
        ):
            with self.subTest(overrides=overrides):
                payload = self.payload()
                payload["sections"]["investors"].append(self.item(**overrides))
                with self.assertRaises(briefing_import.BriefingValidationError):
                    briefing_import.validate_payload(payload, now=self.now)

    def test_rejects_unsafe_urls_unknown_sections_and_future_times(self) -> None:
        for url in (
            "javascript:alert(1)",
            "file:///tmp/story.txt",
            "https://user:password@example.com/story",
            r"https://example.com\redirect",
            "https://example.com/story path",
            "https://%31%32%37.0.0.1/status",
            "https://ｅｘａｍｐｌｅ.com/story",
            "http://localhost/admin",
            "http://news.localhost/story",
            "http://news.local/story",
            "http://intranet/story",
            "http://127.0.0.1/story",
            "http://10.0.0.1/story",
            "http://192.168.1.1/story",
            "http://100.64.0.1/story",
            "http://169.254.1.2/story",
            "http://224.0.0.1/story",
            "http://192.0.2.1/story",
            "http://0.0.0.0/story",
            "http://[::1]/story",
            "http://[fc00::1]/story",
            "http://[fe80::1]/story",
            "http://[ff00::1]/story",
            "http://[::]/story",
            "http://[2001:db8::1]/story",
            "http://127.1/story",
            "http://2130706433/story",
            "http://0x7f000001/story",
            "https://example.com:8443/story",
        ):
            with self.subTest(url=url):
                payload = self.payload()
                payload["sections"]["world"].append(self.item(source_url=url))
                with self.assertRaisesRegex(
                    briefing_import.BriefingValidationError,
                    "source_url",
                ):
                    briefing_import.validate_payload(payload, now=self.now)

        for key in (
            "token",
            "access_token",
            "api_key",
            "api-key",
            "apikey",
            "id_token",
            "refresh_token",
            "client_secret",
            "secret_key",
            "auth_token",
            "x_api_key",
            "my-api-key",
            "jwt",
            "bearer",
            "signature",
            "sig",
            "session",
            "sessionid",
            "auth",
            "authorization",
            "key",
            "password",
            "secret",
            "credential",
            "code",
            "X-Amz-Credential",
            "X-Amz-Signature",
            "X-Amz-Security-Token",
            "X-Amz-Token",
            "api_key[]",
            "token[]",
            "credentials[token]",
            "auth[access_token]",
            "authorizationCode",
            "sessionKey",
            "credentialKey",
            "apiKeyValue",
            "APIKeyValue",
            "accessCodeValue",
            "tokenValue",
            "secretValue",
            "passwordValue",
            "refreshTokenValue",
        ):
            with self.subTest(sensitive_query=key):
                payload = self.payload()
                payload["sections"]["world"].append(
                    self.item(source_url=f"https://example.com/story?{key}=private")
                )
                with self.assertRaisesRegex(
                    briefing_import.BriefingValidationError,
                    "sensitive query",
                ):
                    briefing_import.validate_payload(payload, now=self.now)

        payload = self.payload()
        payload["sections"]["sports"] = []
        with self.assertRaisesRegex(
            briefing_import.BriefingValidationError,
            "invalid sections",
        ):
            briefing_import.validate_payload(payload, now=self.now)

        payload = self.payload()
        payload["sections"]["ai"].append(
            self.item(published_at=(self.now + timedelta(minutes=6)).isoformat())
        )
        with self.assertRaisesRegex(
            briefing_import.BriefingValidationError,
            "too far in the future",
        ):
            briefing_import.validate_payload(payload, now=self.now)

        payload = self.payload()
        payload["source_as_of"] = (self.now - timedelta(seconds=1)).isoformat()
        payload["sections"]["world"].append(
            self.item(
                published_at=(self.now - timedelta(hours=2)).isoformat(),
                fetched_at=self.now.isoformat(),
            )
        )
        with self.assertRaisesRegex(
            briefing_import.BriefingValidationError,
            "fetched_at cannot be after source_as_of",
        ):
            briefing_import.validate_payload(payload, now=self.now)

    def test_source_tier_claims_are_verified_before_representative_selection(self) -> None:
        title = "OpenAI publishes the same material market update"
        shared_url = "https://www.reuters.com/markets/company-update"
        payload = self.payload()
        payload["sections"]["finance"].extend(
            [
                self.item(
                    title=title,
                    source="Definitely Official",
                    source_url=shared_url,
                    source_tier="official",
                ),
                self.item(
                    title=title,
                    source="Reuters",
                    source_url=shared_url,
                    source_tier="media",
                ),
            ]
        )

        item = briefing_import.validate_payload(payload, now=self.now)["sections"][
            "finance"
        ][0]
        self.assertEqual(item["source"], "Reuters")
        self.assertEqual(item["source_tier"], "media")

        cases = (
            (
                self.item(),
                "official",
            ),
            (
                self.item(
                    source="X @elonmusk",
                    source_url="https://x.com/elonmusk/status/123456",
                    source_tier="first_party",
                ),
                "first_party",
            ),
            (
                self.item(
                    source="Truth Social @realDonaldTrump",
                    source_url=(
                        "https://truthsocial.com/@realDonaldTrump/posts/123456"
                    ),
                    source_tier="first_party",
                ),
                "first_party",
            ),
            (
                self.item(
                    source="X @notelonmusk",
                    source_url="https://x.com/elonmusk/status/123456",
                    source_tier="first_party",
                ),
                "discovery",
            ),
            (
                self.item(
                    source="Bing News",
                    source_url="https://www.reuters.com/markets/company-update",
                    source_tier="media",
                ),
                "discovery",
            ),
        )
        for raw, expected in cases:
            with self.subTest(source=raw["source"]):
                payload = self.payload()
                payload["sections"]["finance"].append(raw)
                normalized = briefing_import.validate_payload(payload, now=self.now)
                self.assertEqual(
                    normalized["sections"]["finance"][0]["source_tier"],
                    expected,
                )

    def test_requires_bounded_known_item_fields(self) -> None:
        payload = self.payload()
        payload["sections"]["finance"].append(
            self.item(untrusted_html="<script>alert(1)</script>")
        )
        with self.assertRaisesRegex(
            briefing_import.BriefingValidationError,
            "unsupported fields",
        ):
            briefing_import.validate_payload(payload, now=self.now)

        payload = self.payload()
        payload["sections"]["finance"].append(
            self.item(title="x" * (briefing_import.MAX_TITLE_LENGTH + 1))
        )
        with self.assertRaisesRegex(
            briefing_import.BriefingValidationError,
            "at most",
        ):
            briefing_import.validate_payload(payload, now=self.now)

    def test_import_is_idempotent_and_latest_read_uses_source_freshness(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            db,
            "DB_PATH",
            str(Path(temp_dir) / "briefing.sqlite3"),
        ):
            payload = self.payload()
            payload["sections"]["macro"].append(self.item())
            first = briefing_import.import_payload(
                payload,
                now=self.now,
                imported_at=self.now,
            )
            updated = deepcopy(payload)
            updated["sections"]["macro"][0]["summary"] = "Updated source summary."
            updated["generated_at"] = (self.now + timedelta(seconds=30)).isoformat()
            updated["source_as_of"] = (self.now + timedelta(seconds=30)).isoformat()
            second = briefing_import.import_payload(
                updated,
                now=self.now,
                imported_at=self.now + timedelta(minutes=1),
            )

            self.assertEqual(first["snapshot_id"], second["snapshot_id"])

            regressed = deepcopy(payload)
            regressed["generated_at"] = (
                self.now - timedelta(minutes=1)
            ).isoformat()
            regressed["source_as_of"] = (
                self.now - timedelta(minutes=1)
            ).isoformat()
            with self.assertRaisesRegex(ValueError, "cannot regress"):
                briefing_import.import_payload(
                    regressed,
                    now=self.now,
                    imported_at=self.now + timedelta(minutes=2),
                )
            with db.conn() as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM daily_briefing_snapshots"
                ).fetchone()[0]
            self.assertEqual(count, 1)
            latest = db.load_latest_daily_briefing_snapshot(
                now=self.now + timedelta(hours=23),
            )
            self.assertIsNotNone(latest)
            self.assertEqual(
                latest["payload"]["sections"]["macro"][0]["summary"],
                "Updated source summary.",
            )
            self.assertIsNone(
                db.load_latest_daily_briefing_snapshot(
                    now=self.now + timedelta(hours=25),
                )
            )

    def test_malformed_latest_json_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            db,
            "DB_PATH",
            str(Path(temp_dir) / "briefing.sqlite3"),
        ):
            payload = self.payload()
            payload["sections"]["world"].append(self.item())
            briefing_import.import_payload(payload, now=self.now)
            with db.conn() as connection:
                connection.execute(
                    "UPDATE daily_briefing_snapshots SET payload_json='not json'"
                )

            self.assertIsNone(
                db.load_latest_daily_briefing_snapshot(now=self.now)
            )

    def test_disclosure_fields_survive_persistence_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            db,
            "DB_PATH",
            str(Path(temp_dir) / "briefing.sqlite3"),
        ):
            disclosed = self.now - timedelta(hours=2)
            payload = self.payload()
            payload["sections"]["investors"].append(
                self.item(
                    disclosed_at=disclosed.isoformat(),
                    data_as_of="2026-06-30",
                )
            )
            briefing_import.import_payload(payload, now=self.now)

            record = db.load_latest_daily_briefing_snapshot(now=self.now)

        self.assertIsNotNone(record)
        item = record["payload"]["sections"]["investors"][0]
        self.assertEqual(item["disclosed_at"], disclosed.isoformat())
        self.assertEqual(item["effective_at"], "2026-06-30")
        self.assertNotIn("period_end", item)
        self.assertNotIn("data_as_of", item)

    def test_file_import_rejects_duplicate_json_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "duplicate.json"
            source.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
            with self.assertRaisesRegex(
                briefing_import.BriefingValidationError,
                "duplicate JSON key",
            ):
                briefing_import.load_json_file(source)

    def test_file_import_uses_nofollow_and_bounded_reads(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            target = directory / "target.json"
            target.write_text("{}", encoding="utf-8")
            symlink = directory / "briefing.json"
            symlink.symlink_to(target)
            with self.assertRaises(briefing_import.BriefingValidationError):
                briefing_import.load_json_file(symlink)

            oversized = directory / "oversized.json"
            oversized.write_bytes(b"x" * (briefing_import.MAX_FILE_BYTES + 1))
            with self.assertRaisesRegex(
                briefing_import.BriefingValidationError,
                "exceeds",
            ):
                briefing_import.load_json_file(oversized)


if __name__ == "__main__":
    unittest.main()
