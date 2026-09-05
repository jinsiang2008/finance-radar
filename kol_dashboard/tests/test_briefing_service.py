from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from time import perf_counter

from kol_dashboard import briefing_collect, briefing_service


NOW = datetime(2026, 9, 4, 2, 0, tzinfo=timezone.utc)  # 10:00 Beijing


def ready_ai(**overrides) -> dict:
    value = {
        "status": "ready",
        "headline_zh": "AI 标题",
        "summary_zh": "AI 摘要",
        "why_it_matters_zh": "AI 影响说明",
        "impact_level": "high",
        "evidence_basis": "title_and_snippet",
        "assets": [
            {
                "asset_key": "US:NVDA",
                "name_zh": "英伟达",
                "direction": "positive",
                "account": "must-not-leak",
            }
        ],
    }
    value.update(overrides)
    return value


class FakeRepository:
    def __init__(self, events=None, history=None) -> None:
        self.events = list(events or [])
        self.history = list(history or [])
        self.event_query = None
        self.history_limit = None

    def query_events(self, **kwargs):
        self.event_query = kwargs
        return list(self.events)

    def macro_history(self, *, limit):
        self.history_limit = limit
        return list(self.history)


class EditionTests(unittest.TestCase):
    def test_beijing_edition_boundaries(self) -> None:
        cases = (
            ("2026-09-04T01:29:59+00:00", "morning", "晨间版"),
            ("2026-09-04T01:30:00+00:00", "midday", "午间版"),
            ("2026-09-04T06:59:59+00:00", "midday", "午间版"),
            ("2026-09-04T07:00:00+00:00", "close", "收盘版"),
            ("2026-09-04T12:29:59+00:00", "close", "收盘版"),
            (
                "2026-09-04T12:30:00+00:00",
                "us_premarket",
                "美股盘前·夜间版",
            ),
        )
        for timestamp, code, label in cases:
            with self.subTest(timestamp=timestamp):
                self.assertEqual(
                    briefing_service.edition_for(timestamp),
                    (code, label),
                )

    def test_edition_rejects_naive_time(self) -> None:
        with self.assertRaises(ValueError):
            briefing_service.edition_for(datetime(2026, 9, 4, 9, 30))


class SourceTierTests(unittest.TestCase):
    def test_public_urls_reject_internal_or_secret_bearing_targets(self) -> None:
        unsafe = (
            "http://localhost/story",
            "http://api.localhost/story",
            "http://127.0.0.1/story",
            "http://10.0.0.8/story",
            "http://169.254.1.2/story",
            "http://224.0.0.1/story",
            "http://0.0.0.0/story",
            "http://[::1]/story",
            "http://[ff00::1]/story",
            "http://0177.0.0.1/story",
            "http://0x7f.0.0.1/story",
            "http://127.1/story",
            "http://2130706433/story",
            "http://0x7f000001/story",
            "http://127.0.0.1\\.example.com/story",
            "http://10.0.0.1\\foo",
            "https://example.com/story with space",
            "https://０x7f.0.0.1/status",
            "https://１２７.0.0.1/status",
            "https://127．0.0.1/status",
            "https://%31%32%37.0.0.1/status",
            "https://example.com:80/story",
            "http://example.com:443/story",
            "https://example.com/story?token=private",
            "https://example.com/story?api%5Fkey=private",
            "https://example.com/story?id_token=private",
            "https://example.com/story?refresh_token=private",
            "https://example.com/story?client_secret=private",
            "https://example.com/story?secret_key=private",
            "https://example.com/story?auth_token=private",
            "https://example.com/story?api-key=private",
            "https://example.com/story?x_api_key=private",
            "https://example.com/story?my-api-key=private",
            "https://example.com/story?jwt=private",
            "https://example.com/story?bearer=private",
            "https://example.com/story?api_key[]=private",
            "https://example.com/story?token[]=private",
            "https://example.com/story?credentials[token]=private",
            "https://example.com/story?auth[access_token]=private",
            "https://example.com/story?authorizationCode=private",
            "https://example.com/story?sessionKey=private",
            "https://example.com/story?credentialKey=private",
            "https://example.com/story?apiKeyValue=private",
            "https://example.com/story?APIKeyValue=private",
            "https://example.com/story?accessCodeValue=private",
            "https://example.com/story?tokenValue=private",
            "https://example.com/story?secretValue=private",
            "https://example.com/story?passwordValue=private",
            "https://example.com/story?refreshTokenValue=private",
            "https://example.com/story?X-Amz-Signature=private",
            "https://example.com/story?sessionid=private",
            "https://example.com/story?code=private",
            "https://example.com/story?X-Amz-Token=private",
        )
        for url in unsafe:
            with self.subTest(url=url):
                self.assertEqual(briefing_service._public_url(url), "")

        self.assertEqual(
            briefing_service._public_url(
                "HTTPS://Example.COM/story?utm_source=test&z=2&a=1#fragment"
            ),
            "https://example.com/story?a=1&z=2",
        )

    def test_specific_original_url_matches_producer_article_predicate(self) -> None:
        urls = (
            "https://openai.com/",
            "https://openai.com/company-announcements",
            "https://openai.com/index",
            "https://openai.com/en/company-announcements",
            "https://openai.com/product-update",
            "https://openai.com/team-announcements",
            "https://openai.com/newsroom?story=123",
            "https://arxiv.org/abs/2609.01234",
            "https://huggingface.co/papers/2609.01234",
            "https://openai.com/index/example-release",
            "https://openai.com/en/article/new-model",
            "https://openai.com/launch-model-2026",
            "https://openai.com/release.html",
            "https://openai.com/story?id=release",
        )
        for url in urls:
            with self.subTest(url=url):
                expected = briefing_collect._specific_original_url(url) is not None
                canonical = briefing_service._public_url(url)
                self.assertEqual(
                    briefing_service._specific_original_url(canonical), expected
                )

    def test_conservative_source_tiers(self) -> None:
        official = {
            "source": "Federal Reserve",
            "content_source_url": (
                "https://www.federalreserve.gov/newsevents/pressreleases/"
                "monetary20260904a.htm"
            ),
        }
        self.assertEqual(
            briefing_service.classify_source(
                official,
                evidence_basis="official_body",
            ),
            "official",
        )
        self.assertEqual(
            briefing_service.classify_source(
                official,
                evidence_basis="title_only",
            ),
            "discovery",
        )

        truth = {
            "source": "Truth Social @realDonaldTrump",
            "source_url": "https://truthsocial.com/@realDonaldTrump/posts/123",
            "attribution_basis": "direct_source",
        }
        self.assertEqual(
            briefing_service.classify_source(truth, evidence_basis="post_text"),
            "first_party",
        )
        x_post = {
            "source": "X @elonmusk",
            "source_url": "https://x.com/elonmusk/status/123",
            "attribution_basis": "self_post",
        }
        self.assertEqual(
            briefing_service.classify_source(x_post, evidence_basis="post_text"),
            "first_party",
        )

        # A profile URL or a claimed direct-source flag alone cannot establish
        # that the record is an original social post.
        impostor = {**x_post, "source_url": "https://x.com/elonmusk"}
        self.assertEqual(
            briefing_service.classify_source(impostor, evidence_basis="post_text"),
            "discovery",
        )
        mismatched_handle = {
            **x_post,
            "source_url": "https://x.com/not_elon/status/123",
        }
        self.assertEqual(
            briefing_service.classify_source(
                mismatched_handle,
                evidence_basis="post_text",
            ),
            "discovery",
        )
        self.assertEqual(
            briefing_service.classify_source(
                {
                    "source": "Bing News",
                    "source_url": "https://www.reuters.com/world/example",
                }
            ),
            "discovery",
        )
        self.assertEqual(
            briefing_service.classify_source(
                {
                    "source": "Reuters",
                    "source_url": "https://www.reuters.com/world/example",
                }
            ),
            "reporting",
        )
        for source, source_url in (
            (
                "Hacker News",
                "https://www.reuters.com/technology/community-discovery",
            ),
            (
                "AI Digest",
                "https://ai-digest.liziran.com/zh/2026-09-04-example",
            ),
            (
                "AI Brief",
                "https://ai-brief.liziran.com/zh/2026-09-04-example",
            ),
        ):
            with self.subTest(source=source):
                self.assertEqual(
                    briefing_service.classify_source(
                        {"source": source, "source_url": source_url}
                    ),
                    "discovery",
                )
        self.assertEqual(
            briefing_service.classify_source({"source": "Unknown"}),
            "discovery",
        )


class BriefingBuildTests(unittest.TestCase):
    @staticmethod
    def event(
        event_id: int,
        title: str,
        *,
        published_at: str = "2026-09-04T01:55:00+00:00",
        source: str = "Reuters",
        source_url: str | None = None,
        kol_key: str = "",
        kol_name: str = "",
        impact: str = "medium",
        ai_enrichment: dict | None = None,
    ) -> dict:
        return {
            "id": event_id,
            "title": title,
            "snippet": f"{title} 的已采集事实摘要",
            "source": source,
            "source_url": source_url
            or f"https://www.reuters.com/world/story-{event_id}",
            "canonical_url": source_url
            or f"https://www.reuters.com/world/story-{event_id}",
            "impact": impact,
            "published_at": published_at,
            "fetched_at": published_at,
            "last_seen_at": published_at,
            "time_status": "verified",
            "source_count": 1,
            "kol_key": kol_key,
            "kol_name_cn": kol_name,
            "ai_status": "ready" if ai_enrichment else "pending",
            "ai_enrichment": ai_enrichment,
        }

    def test_empty_database_has_stable_shape_and_no_synthetic_firsthand(self) -> None:
        repository = FakeRepository()

        result = briefing_service.build_latest_briefing(
            repository=repository,
            public_macro=None,
            decision_record=None,
            now=NOW,
        )

        self.assertEqual(
            set(result),
            {
                "available",
                "date",
                "edition",
                "edition_label",
                "generated_at",
                "coverage_window_hours",
                "next_refresh_at",
                "refresh_schedule_status",
                "source_as_of",
                "content_as_of",
                "source_coverage_as_of",
                "source_coverage_stale",
                "stale",
                "coverage",
                "dedup_stats",
                "lead",
                "highlights",
                "firsthand",
                "watchpoints",
                "sections",
                "disclaimer",
            },
        )
        self.assertFalse(result["available"])
        self.assertTrue(result["stale"])
        self.assertIsNone(result["source_as_of"])
        self.assertIsNone(result["content_as_of"])
        self.assertIsNone(result["source_coverage_as_of"])
        self.assertIsNone(result["source_coverage_stale"])
        self.assertEqual(result["lead"], {})
        self.assertEqual(result["highlights"], [])
        self.assertEqual(result["firsthand"], [])
        self.assertEqual(result["watchpoints"], [])
        self.assertEqual(result["coverage_window_hours"], 24)
        self.assertIsNone(result["next_refresh_at"])
        self.assertEqual(result["refresh_schedule_status"], "unconfigured")
        self.assertEqual(
            [section["key"] for section in result["sections"]],
            list(briefing_service.SECTION_KEYS),
        )
        for section in result["sections"]:
            self.assertEqual(
                set(section),
                {
                    "key",
                    "label",
                    "description",
                    "source_as_of",
                    "stale",
                    "status",
                    "verified_count",
                    "total_count",
                    "items",
                },
            )
            self.assertEqual(section["items"], [])
            self.assertEqual(section["status"], "empty")
            self.assertTrue(section["stale"])
        self.assertEqual(
            result["dedup_stats"],
            {
                "input_count": 0,
                "output_count": 0,
                "merged_count": 0,
                "stable_id_matches": 0,
                "canonical_url_matches": 0,
                "ai_cluster_matches": 0,
                "semantic_matches": 0,
            },
        )
        self.assertEqual(
            result["coverage"],
            {
                "total": 0,
                "official": 0,
                "first_party": 0,
                "reporting": 0,
                "discovery": 0,
                "ai_ready": 0,
                "time_verified": 0,
            },
        )
        self.assertEqual(result["date"], "2026-09-04")
        self.assertEqual(repository.event_query["hours"], 24)
        self.assertEqual(repository.event_query["time_status"], "verified")
        self.assertEqual(repository.event_query["use_ai_impact"], True)

    def test_current_empty_import_reports_scan_coverage_without_fake_news(self) -> None:
        snapshot = {
            "schema_version": 1,
            "generated_at": "2026-09-04T01:59:00+00:00",
            "source_as_of": "2026-09-04T01:58:00+00:00",
            "sections": {key: [] for key in briefing_service.SECTION_KEYS},
        }

        result = briefing_service.build_latest_briefing(
            repository=FakeRepository(),
            public_macro=None,
            decision_record=None,
            imported_snapshot=snapshot,
            now=NOW,
        )

        self.assertTrue(result["available"])
        self.assertIsNone(result["source_as_of"])
        self.assertIsNone(result["content_as_of"])
        self.assertEqual(
            result["source_coverage_as_of"],
            "2026-09-04T01:58:00+00:00",
        )
        self.assertFalse(result["source_coverage_stale"])
        self.assertTrue(result["stale"])
        self.assertEqual(result["coverage"]["total"], 0)

    def test_batch_coverage_does_not_freshen_old_displayed_evidence(self) -> None:
        snapshot = {
            "schema_version": 1,
            "generated_at": "2026-09-04T01:59:00+00:00",
            "source_as_of": "2026-09-04T01:58:00+00:00",
            "sections": {key: [] for key in briefing_service.SECTION_KEYS},
        }
        snapshot["sections"]["finance"].append(
            {
                "title": "Older verified bank report remains relevant",
                "source": "Reuters",
                "source_url": "https://www.reuters.com/markets/older-bank-report",
                "story_key": "older-bank-report",
                "published_at": "2026-09-03T22:00:00+00:00",
                "time_status": "verified",
                "source_tier": "reporting",
                "summary": "The report is still inside the 24-hour window.",
                "assets": [],
                "cross_tags": [],
            }
        )

        result = briefing_service.build_latest_briefing(
            repository=FakeRepository(),
            public_macro=None,
            decision_record=None,
            imported_snapshot=snapshot,
            now=NOW,
        )

        self.assertEqual(result["content_as_of"], "2026-09-03T22:00:00+00:00")
        self.assertEqual(result["source_as_of"], result["content_as_of"])
        self.assertTrue(result["stale"])
        self.assertEqual(
            result["source_coverage_as_of"],
            "2026-09-04T01:58:00+00:00",
        )
        self.assertFalse(result["source_coverage_stale"])

    def test_ranking_limits_ai_fallback_and_related_record_semantics(self) -> None:
        events = [
            {
                "id": index,
                "title": f"Reporting {index}",
                "snippet": f"Original summary {index}",
                "source": "Reuters",
                "source_url": f"https://www.reuters.com/world/{index}",
                "impact": "high" if index < 7 else "medium",
                "published_at": f"2026-09-04T01:{50-index:02d}:00+00:00",
                "fetched_at": f"2026-09-04T01:{51-index:02d}:00+00:00",
                "time_status": "verified",
                "source_count": index + 2,
                "kol_name_cn": "测试 KOL",
                "ai_status": "pending",
            }
            for index in range(8)
        ]
        events[0].update(
            {
                "ai_status": "ready",
                "ai_enrichment": ready_ai(),
            }
        )
        events[1].update(
            {
                "title": "必须回退的原始标题",
                "snippet": "必须回退的原始摘要",
                "ai_status": "ready",
                "ai_enrichment": ready_ai(
                    headline_zh="不得展示的不完整 AI 标题",
                    why_it_matters_zh="",
                ),
            }
        )
        events.append(
            {
                "id": 99,
                "title": "本人发布重要动态",
                "snippet": "本人社交原文",
                "source": "X @elonmusk",
                "source_url": "https://x.com/elonmusk/status/99",
                "attribution_basis": "direct_source",
                "impact": "high",
                "published_at": "2026-09-04T01:58:00+00:00",
                "fetched_at": "2026-09-04T01:59:00+00:00",
                "time_status": "verified",
                "source_count": 12,
                "ai_status": "pending",
            }
        )
        macro = {
            "timestamp": "2026-09-04T01:59:00+00:00",
            "created_at": "2026-09-04T01:59:10+00:00",
            "monitored_events": [
                {
                    "id": "fed-statement",
                    "kind": "policy",
                    "title": "Federal Reserve issues FOMC statement",
                    "source": "Federal Reserve",
                    "url": (
                        "https://www.federalreserve.gov/newsevents/pressreleases/"
                        "monetary20260904a.htm"
                    ),
                    "content_status": "ready",
                    "content_excerpt": "The Committee published its decision.",
                    "content_source_url": (
                        "https://www.federalreserve.gov/newsevents/pressreleases/"
                        "monetary20260904a.htm"
                    ),
                    "published_at": "2026-09-04T01:57:00+00:00",
                    "time_status": "verified",
                    "severity": "high",
                    "ai_status": "pending",
                }
            ],
        }

        result = briefing_service.build_latest_briefing(
            repository=FakeRepository(events),
            public_macro=macro,
            decision_record=None,
            now=NOW,
        )

        self.assertEqual(len(result["highlights"]), briefing_service.MAX_HIGHLIGHTS)
        self.assertEqual(result["highlights"][0]["id"], "99")
        self.assertEqual(result["highlights"][1]["id"], "fed-statement")
        fallback = next(item for item in result["highlights"] if item["id"] == "1")
        self.assertEqual(fallback["title"], "必须回退的原始标题")
        self.assertEqual(fallback["summary"], "必须回退的原始摘要")
        self.assertEqual(fallback["ai_status"], "ready")
        self.assertFalse(fallback["ai_summary_used"])
        first = next(item for item in result["highlights"] if item["id"] == "0")
        self.assertEqual(first["title"], "AI 标题")
        self.assertTrue(first["ai_summary_used"])
        self.assertEqual(first["assets"][0], {
            "asset_key": "US:NVDA",
            "name_zh": "英伟达",
            "direction": "positive",
        })
        self.assertEqual(first["related_records"], 2)
        self.assertEqual(first["source_count"], 2)
        self.assertIn("last_updated_at", first)
        self.assertEqual(first["primary_section"], "finance")
        self.assertIn("ai", first["cross_tags"])
        self.assertIn("story_key", first)
        self.assertEqual(
            {item["source_tier"] for item in result["firsthand"]},
            {"official", "first_party"},
        )
        self.assertLessEqual(len(result["firsthand"]), briefing_service.MAX_FIRSTHAND)
        self.assertEqual(result["coverage"]["total"], 10)
        self.assertEqual(result["coverage"]["official"], 1)
        self.assertEqual(result["coverage"]["first_party"], 1)
        self.assertEqual(result["coverage"]["ai_ready"], 1)

    def test_macro_history_builds_risk_delta_and_freshness(self) -> None:
        macro = {
            "timestamp": "2026-09-04T01:45:00+00:00",
            "created_at": "2026-09-04T01:46:00+00:00",
            "composite_risk": {"score": 65, "level": "high"},
            "monitored_events": [],
        }
        history = [
            {
                "created_at": "2026-09-03T01:40:00+00:00",
                "composite_score": 42,
                "composite_level": "medium",
            },
            {
                "created_at": "2026-09-04T01:40:00+00:00",
                "composite_score": 65,
                "composite_level": "high",
            },
        ]

        result = briefing_service.build_latest_briefing(
            repository=FakeRepository(history=history),
            public_macro=macro,
            decision_record=None,
            now=NOW,
        )

        self.assertTrue(result["available"])
        self.assertFalse(result["stale"])
        self.assertEqual(result["lead"]["risk_score"], 65.0)
        self.assertEqual(result["lead"]["risk_level"], "high")
        self.assertEqual(result["lead"]["risk_delta_24h"], 23.0)
        self.assertEqual(result["source_as_of"], "2026-09-04T01:46:00+00:00")

    def test_ai_summary_cannot_bypass_effective_event_impact_gate(self) -> None:
        event = {
            "id": 1,
            "title": "Original low-impact event",
            "snippet": "The persisted public impact gate kept this event low.",
            "source": "Reuters",
            "source_url": "https://www.reuters.com/world/low-impact",
            "impact": "low",
            "published_at": "2026-09-04T01:55:00+00:00",
            "time_status": "verified",
            "ai_status": "ready",
            "ai_enrichment": ready_ai(
                impact_level="high",
                confidence=0.1,
                evidence_basis="title_only",
            ),
        }

        result = briefing_service.build_latest_briefing(
            repository=FakeRepository([event]),
            public_macro=None,
            decision_record=None,
            now=NOW,
        )

        self.assertEqual(result["highlights"][0]["impact"], "low")
        self.assertTrue(result["highlights"][0]["ai_summary_used"])

    def test_macro_ai_summary_uses_public_evidence_and_confidence_gate(self) -> None:
        low_confidence = {
            "id": "low-confidence",
            "kind": "policy",
            "title": "Original low-confidence title",
            "snippet": "Original low-confidence summary",
            "source": "Reuters",
            "url": "https://www.reuters.com/world/low-confidence",
            "published_at": "2026-09-04T01:55:00+00:00",
            "time_status": "verified",
            "severity": "high",
            "ai_status": "ready",
            "ai_enrichment": ready_ai(confidence=0.64),
        }
        title_only = {
            **low_confidence,
            "id": "title-only",
            "title": "Original title-only title",
            "url": "https://www.reuters.com/world/title-only",
            "published_at": "2026-09-04T01:54:00+00:00",
            "ai_enrichment": ready_ai(
                confidence=0.99,
                evidence_basis="title_only",
            ),
        }
        eligible = {
            **low_confidence,
            "id": "eligible",
            "title": "Original eligible title",
            "url": "https://www.reuters.com/world/eligible",
            "published_at": "2026-09-04T01:53:00+00:00",
            "ai_enrichment": ready_ai(confidence=0.65),
        }

        result = briefing_service.build_latest_briefing(
            repository=FakeRepository(),
            public_macro={"monitored_events": [low_confidence, title_only, eligible]},
            decision_record=None,
            now=NOW,
        )

        by_id = {item["id"]: item for item in result["highlights"]}
        self.assertEqual(
            by_id["low-confidence"]["title"],
            "Original low-confidence title",
        )
        self.assertFalse(by_id["low-confidence"]["ai_summary_used"])
        self.assertEqual(by_id["title-only"]["title"], "Original title-only title")
        self.assertFalse(by_id["title-only"]["ai_summary_used"])
        self.assertEqual(by_id["eligible"]["title"], "AI 标题")
        self.assertTrue(by_id["eligible"]["ai_summary_used"])
        self.assertEqual(result["coverage"]["ai_ready"], 1)

    def test_macro_highlights_require_verified_past_24_hours(self) -> None:
        current = {
            "id": "current",
            "kind": "policy",
            "title": "Current policy report",
            "source": "Reuters",
            "url": "https://www.reuters.com/world/current-policy",
            "published_at": "2026-09-04T01:55:00+00:00",
            "time_status": "verified",
            "severity": "medium",
            "ai_status": "pending",
        }
        old_official = {
            "id": "old-official",
            "kind": "policy",
            "title": "Old official decision",
            "source": "Federal Reserve",
            "url": (
                "https://www.federalreserve.gov/newsevents/pressreleases/"
                "monetary20260822a.htm"
            ),
            "content_status": "ready",
            "content_excerpt": "An old decision.",
            "content_source_url": (
                "https://www.federalreserve.gov/newsevents/pressreleases/"
                "monetary20260822a.htm"
            ),
            "published_at": "2026-08-22T02:00:00+00:00",
            "time_status": "verified",
            "severity": "high",
            "ai_status": "pending",
        }
        future_official = {
            **old_official,
            "id": "future-official",
            "title": "Future-dated official decision",
            "published_at": "2026-09-04T03:00:00+00:00",
        }
        unverified = {
            **current,
            "id": "unverified",
            "title": "Unverified policy report",
            "time_status": "unknown",
        }
        macro = {
            "monitored_events": [
                old_official,
                future_official,
                unverified,
                current,
            ]
        }

        result = briefing_service.build_latest_briefing(
            repository=FakeRepository(),
            public_macro=macro,
            decision_record=None,
            now=NOW,
        )

        self.assertEqual(
            [item["id"] for item in result["highlights"]],
            ["current"],
        )
        self.assertEqual(result["source_as_of"], "2026-09-04T01:55:00+00:00")
        self.assertFalse(result["stale"])

    def test_decision_generation_time_does_not_mask_stale_evidence(self) -> None:
        decision_record = {
            "generated_at": "2026-09-04T01:59:00+00:00",
            "source_as_of": "2026-09-04T01:59:00+00:00",
            "summary": {
                "decisions": [
                    {
                        "topic_key": "market_risk",
                        "asset_key": "US:SPY",
                        "direction": "negative",
                        "action_stage": "verify",
                        "data_as_of": "2026-09-03T22:00:00+00:00",
                    }
                ]
            },
        }

        result = briefing_service.build_latest_briefing(
            repository=FakeRepository(),
            public_macro=None,
            decision_record=decision_record,
            now=NOW,
        )

        self.assertTrue(result["available"])
        self.assertEqual(result["source_as_of"], "2026-09-03T22:00:00+00:00")
        self.assertTrue(result["stale"])
        self.assertEqual(
            result["watchpoints"][0]["data_as_of"],
            "2026-09-03T22:00:00+00:00",
        )

    def test_hidden_watchpoint_does_not_freshen_global_evidence_time(self) -> None:
        decisions = [
            {
                "topic_key": f"selected_{index}",
                "asset_key": f"US:S{index}",
                "action_stage": "verify",
                "total_score": 10 - index,
                "data_as_of": "2026-09-03T22:00:00+00:00",
            }
            for index in range(5)
        ]
        decisions.append(
            {
                "topic_key": "hidden_fresh",
                "asset_key": "US:HIDDEN",
                "action_stage": "observe",
                "total_score": 99,
                "data_as_of": "2026-09-04T01:59:00+00:00",
            }
        )

        result = briefing_service.build_latest_briefing(
            repository=FakeRepository(),
            public_macro=None,
            decision_record={"summary": {"decisions": decisions}},
            now=NOW,
        )

        self.assertEqual(len(result["watchpoints"]), 5)
        self.assertNotIn(
            "US:HIDDEN",
            {item["asset_key"] for item in result["watchpoints"]},
        )
        self.assertEqual(result["source_as_of"], "2026-09-03T22:00:00+00:00")
        self.assertTrue(result["stale"])

    def test_watchpoints_are_sorted_capped_and_public_allowlisted(self) -> None:
        decisions = []
        for index in range(8):
            decisions.append(
                {
                    "topic_key": "ai_semiconductors" if index == 0 else f"topic_{index}",
                    "asset_key": "US:NVDA" if index == 0 else f"US:T{index}",
                    "direction": "negative",
                    "action_stage": (
                        "candidate_reduce_or_hedge"
                        if index == 7
                        else "candidate_scale_in"
                        if index == 0
                        else "observe"
                    ),
                    "total_score": index,
                    "source_count": index + 1,
                    "market_validation": {
                        "next_review_at": "2026-09-05T00:00:00+00:00",
                        "applicability_reason": "window_pending",
                        "account": "private-account",
                    },
                    "positions": ["private-position"],
                }
            )
        decision_record = {
            "summary": {"decisions": decisions},
            "generated_at": "2026-09-04T01:50:00+00:00",
            "source_as_of": "2026-09-04T01:49:00+00:00",
        }

        result = briefing_service.build_latest_briefing(
            repository=FakeRepository(),
            public_macro=None,
            decision_record=decision_record,
            now=NOW,
        )

        self.assertEqual(len(result["watchpoints"]), briefing_service.MAX_WATCHPOINTS)
        self.assertEqual(result["watchpoints"][0]["asset_key"], "US:T7")
        known = next(
            item for item in result["watchpoints"] if item["asset_key"] == "US:NVDA"
        )
        self.assertEqual(known["topic_label"], "AI 与半导体")
        self.assertEqual(known["asset_label"], "英伟达")
        encoded = json.dumps(result, ensure_ascii=False).lower()
        self.assertNotIn("private-account", encoded)
        self.assertNotIn("private-position", encoded)
        self.assertNotIn("positions", encoded)

    def test_six_sections_are_stable_and_each_story_has_one_primary_home(self) -> None:
        events = [
            self.event(1, "美国与伊朗冲突升级牵动全球供应链"),
            self.event(2, "大型银行发布季度财报并上调利润指引"),
            self.event(3, "量子芯片与机器人平台取得工程进展"),
            self.event(4, "OpenAI 发布新一代推理模型"),
            self.event(
                5,
                "巴菲特披露伯克希尔最新持仓",
                kol_key="buffett",
                kol_name="巴菲特",
            ),
        ]
        macro = {
            "created_at": "2026-09-04T01:56:00+00:00",
            "monitored_events": [
                {
                    "id": "cpi-release",
                    "kind": "indicator",
                    "title": "官方公布最新 CPI 通胀数据",
                    "source": "Bureau of Labor Statistics",
                    "url": "https://www.bls.gov/news.release/cpi.htm",
                    "content_status": "ready",
                    "content_source_url": "https://www.bls.gov/news.release/cpi.htm",
                    "published_at": "2026-09-04T01:56:00+00:00",
                    "time_status": "verified",
                    "severity": "high",
                    "ai_status": "pending",
                }
            ],
        }

        result = briefing_service.build_latest_briefing(
            repository=FakeRepository(events),
            public_macro=macro,
            decision_record=None,
            now=NOW,
        )

        sections = {section["key"]: section for section in result["sections"]}
        self.assertEqual(set(sections), set(briefing_service.SECTION_KEYS))
        self.assertTrue(all(sections[key]["total_count"] >= 1 for key in sections))
        story_keys = []
        for key, section in sections.items():
            self.assertLessEqual(
                len(section["items"]), briefing_service.MAX_SECTION_ITEMS
            )
            for item in section["items"]:
                self.assertEqual(item["primary_section"], key)
                self.assertNotIn(key, item["cross_tags"])
                story_keys.append(item["story_key"])
        self.assertEqual(len(story_keys), len(set(story_keys)))

        # Top 5 is a quota-based view over real section items, not generated
        # copy. With six populated rails it represents five distinct rails.
        section_story_keys = {
            item["story_key"]
            for section in result["sections"]
            for item in section["items"]
        }
        self.assertEqual(len(result["highlights"]), 5)
        self.assertEqual(
            len({item["primary_section"] for item in result["highlights"]}),
            5,
        )
        self.assertTrue(
            all(
                item["story_key"] in section_story_keys
                for item in result["highlights"]
            )
        )

    def test_primary_section_follows_fact_title_not_ai_commentary(self) -> None:
        tariff = self.event(
            1,
            "美国宣布对华关税新措施",
            ai_enrichment=ready_ai(
                headline_zh="美国关税措施影响 AI 芯片供应链",
                why_it_matters_zh="AI 芯片与 GPU 供应链可能受到影响",
            ),
        )
        powell = self.event(
            2,
            "鲍威尔称美联储维持利率路径",
            kol_key="powell",
            kol_name="鲍威尔",
            ai_enrichment=ready_ai(
                headline_zh="鲍威尔利率表态影响 AI 估值",
                why_it_matters_zh="AI 成长股估值可能重新定价",
            ),
        )
        apple = self.event(
            3,
            "Apple reports quarterly earnings",
            ai_enrichment=ready_ai(
                headline_zh="美联储政策影响苹果财报",
                why_it_matters_zh="利率路径可能影响估值",
            ),
        )

        result = briefing_service.build_latest_briefing(
            repository=FakeRepository([tariff, powell, apple]),
            public_macro=None,
            decision_record=None,
            now=NOW,
        )
        by_id = {
            item["id"]: item
            for section in result["sections"]
            for item in section["items"]
        }

        self.assertEqual(by_id["1"]["primary_section"], "world")
        self.assertIn("ai", by_id["1"]["cross_tags"])
        self.assertIn("technology", by_id["1"]["cross_tags"])
        self.assertEqual(by_id["2"]["primary_section"], "macro")
        self.assertIn("ai", by_id["2"]["cross_tags"])
        self.assertEqual(by_id["3"]["primary_section"], "finance")
        self.assertIn("macro", by_id["3"]["cross_tags"])

    def test_ascii_ai_names_classify_next_to_chinese_text(self) -> None:
        titles = (
            "OpenAI发布GPT-6",
            "AI前沿突破",
            "ChatGPT推出新功能",
            "Claude发布新版",
        )
        result = briefing_service.build_latest_briefing(
            repository=FakeRepository(
                [
                    self.event(
                        index,
                        title,
                        source_url=f"https://example.com/mixed-ai-{index}",
                    )
                    for index, title in enumerate(titles, start=1)
                ]
            ),
            public_macro=None,
            decision_record=None,
            now=NOW,
        )

        ai = next(section for section in result["sections"] if section["key"] == "ai")
        self.assertEqual(ai["total_count"], len(titles))

    def test_top_five_prioritizes_validated_primary_source_without_v1_impact(self) -> None:
        section_titles = {
            "macro": "Official monetary policy filing",
            "world": "Global diplomatic talks update",
            "finance": "Bank funding market update",
            "technology": "Semiconductor process update",
            "ai": "OpenAI reasoning model update",
            "investors": "Investor portfolio disclosure update",
        }
        sections = {}
        for index, key in enumerate(briefing_service.SECTION_KEYS):
            official = key == "macro"
            sections[key] = [
                {
                    "section": key,
                    "title": section_titles[key],
                    "source": "SEC" if official else "Reuters",
                    "source_url": (
                        "https://www.sec.gov/newsroom/press-releases/official-top"
                        if official
                        else f"https://www.reuters.com/world/{key}-top"
                    ),
                    "canonical_url": (
                        "https://www.sec.gov/newsroom/press-releases/official-top"
                        if official
                        else f"https://www.reuters.com/world/{key}-top"
                    ),
                    "story_key": f"{key}-top",
                    "published_at": f"2026-09-04T01:{40 + index:02d}:00+00:00",
                    "time_status": "verified",
                    "source_tier": "official" if official else "reporting",
                    "source_count": 1,
                    "summary": section_titles[key],
                    "cross_tags": [],
                    "assets": [],
                    # v1 does not declare this field; the service must ignore it.
                    "impact": "low" if official else "high",
                }
            ]

        result = briefing_service.build_latest_briefing(
            repository=FakeRepository(),
            public_macro=None,
            decision_record=None,
            imported_snapshot={"schema_version": 1, "sections": sections},
            now=NOW,
        )

        self.assertEqual(result["highlights"][0]["id"], "macro-top")
        self.assertIn(
            "macro-top", {item["id"] for item in result["highlights"]}
        )
        self.assertTrue(
            all(item["impact"] == "unknown" for item in result["highlights"])
        )
        self.assertNotIn("本人原文", json.dumps(result, ensure_ascii=False))

    def test_section_freshness_is_independent(self) -> None:
        events = [
            self.event(
                1,
                "银行发布最新财报",
                published_at="2026-09-04T01:55:00+00:00",
            ),
            self.event(
                2,
                "全球停火谈判继续",
                published_at="2026-09-03T22:00:00+00:00",
            ),
        ]
        events[0]["fetched_at"] = "2026-09-04T01:59:00+00:00"
        events[0]["last_seen_at"] = "2026-09-04T01:59:00+00:00"

        result = briefing_service.build_latest_briefing(
            repository=FakeRepository(events),
            public_macro=None,
            decision_record=None,
            now=NOW,
        )

        sections = {section["key"]: section for section in result["sections"]}
        self.assertFalse(sections["finance"]["stale"])
        self.assertEqual(sections["finance"]["status"], "fresh")
        self.assertTrue(sections["world"]["stale"])
        self.assertEqual(sections["world"]["status"], "stale")
        self.assertFalse(result["stale"])

    def test_fresh_reporting_survives_stale_official_saturation(self) -> None:
        official = [
            {
                "section": "finance",
                "title": f"Regulator archive notice {index}",
                "source": "SEC",
                "source_url": (
                    "https://www.sec.gov/newsroom/press-releases/"
                    f"stale-official-{index}"
                ),
                "story_key": f"stale-official-{index}",
                "published_at": "2026-09-03T03:00:00+00:00",
                "time_status": "verified",
                "source_tier": "official",
                "summary": "A verified but older regulatory notice.",
                "assets": [],
                "cross_tags": [],
            }
            for index in range(6)
        ]
        fresh = {
            "section": "finance",
            "title": "Reuters reports a current bank funding development",
            "source": "Reuters",
            "source_url": "https://www.reuters.com/markets/current-bank-funding",
            "story_key": "fresh-reporting",
            "published_at": "2026-09-04T01:55:00+00:00",
            "time_status": "verified",
            "source_tier": "reporting",
            "summary": "Current verified reporting on bank funding.",
            "assets": [],
            "cross_tags": [],
        }
        snapshot = {
            "schema_version": 1,
            "sections": {
                key: (official + [fresh] if key == "finance" else [])
                for key in briefing_service.SECTION_KEYS
            },
        }

        result = briefing_service.build_latest_briefing(
            repository=FakeRepository(),
            public_macro=None,
            decision_record=None,
            imported_snapshot=snapshot,
            now=NOW,
        )
        finance = next(
            section for section in result["sections"] if section["key"] == "finance"
        )

        self.assertEqual(finance["total_count"], 7)
        self.assertEqual(len(finance["items"]), 6)
        self.assertEqual(finance["items"][0]["id"], "fresh-reporting")
        self.assertFalse(finance["stale"])
        self.assertEqual(result["highlights"][0]["id"], "fresh-reporting")

    def test_fresh_official_survives_stale_firsthand_saturation(self) -> None:
        events = []
        for index in range(7):
            fresh = index == 6
            events.append(
                {
                    "id": f"official-{index}",
                    "kind": "policy",
                    "title": f"Federal Reserve policy release {index}",
                    "source": "Federal Reserve",
                    "url": (
                        "https://www.federalreserve.gov/newsevents/pressreleases/"
                        f"monetary2026090{index + 1}a.htm"
                    ),
                    "content_status": "ready",
                    "content_excerpt": "The Committee published its decision.",
                    "content_source_url": (
                        "https://www.federalreserve.gov/newsevents/pressreleases/"
                        f"monetary2026090{index + 1}a.htm"
                    ),
                    "published_at": (
                        "2026-09-04T01:55:00+00:00"
                        if fresh
                        else "2026-09-03T03:00:00+00:00"
                    ),
                    "time_status": "verified",
                    "severity": "low" if fresh else "high",
                    "ai_status": "pending",
                }
            )

        result = briefing_service.build_latest_briefing(
            repository=FakeRepository(),
            public_macro={"monitored_events": events},
            decision_record=None,
            now=NOW,
        )

        self.assertEqual(len(result["firsthand"]), 6)
        self.assertEqual(result["firsthand"][0]["id"], "official-6")
        self.assertIn(
            "official-6", {item["id"] for item in result["firsthand"]}
        )

    def test_refetch_time_does_not_claim_the_story_was_updated(self) -> None:
        old = self.event(
            1,
            "Bank publishes quarterly earnings report",
            published_at="2026-09-03T03:00:00+00:00",
        )
        old["fetched_at"] = "2026-09-04T01:59:00+00:00"
        old["last_seen_at"] = "2026-09-04T01:59:00+00:00"
        macro_event = {
            "id": "old-policy",
            "kind": "policy",
            "title": "Federal Reserve policy statement remains available",
            "source": "Federal Reserve",
            "url": (
                "https://www.federalreserve.gov/newsevents/pressreleases/"
                "monetary20260903a.htm"
            ),
            "content_status": "ready",
            "content_excerpt": "The Committee statement remains unchanged.",
            "content_source_url": (
                "https://www.federalreserve.gov/newsevents/pressreleases/"
                "monetary20260903a.htm"
            ),
            "published_at": "2026-09-03T03:00:00+00:00",
            "time_status": "verified",
            "severity": "medium",
            "ai_status": "pending",
        }

        result = briefing_service.build_latest_briefing(
            repository=FakeRepository([old]),
            public_macro={
                "created_at": "2026-09-04T01:59:00+00:00",
                "monitored_events": [macro_event],
            },
            decision_record=None,
            now=NOW,
        )
        items = {
            item["id"]: item
            for section in result["sections"]
            for item in section["items"]
        }

        self.assertEqual(items["1"]["last_updated_at"], old["published_at"])
        self.assertEqual(
            items["old-policy"]["last_updated_at"],
            macro_event["published_at"],
        )

    def test_section_limit_preserves_official_source_ahead_of_discovery(self) -> None:
        discovery = [
            {
                "section": "finance",
                "title": f"市场聚合线索 {index}",
                "source_url": f"https://news.google.com/articles/{index}",
                "canonical_url": f"https://news.google.com/articles/{index}",
                "story_key": f"discovery-{index}",
                "published_at": f"2026-09-04T01:{50-index:02d}:00+00:00",
                "last_updated_at": f"2026-09-04T01:{50-index:02d}:00+00:00",
                "time_status": "verified",
                "source_tier": "discovery",
                "source_count": 1,
                "summary": "市场股票异动线索",
                "cross_tags": [],
                "assets": [],
                "impact": "high",
            }
            for index in range(8)
        ]
        official = {
            "section": "finance",
            "title": "监管机构发布正式市场公告",
            "source": "SEC",
            "source_url": "https://www.sec.gov/newsroom/press-releases/official-one",
            "canonical_url": "https://www.sec.gov/newsroom/press-releases/official-one",
            "story_key": "official-one",
            "published_at": "2026-09-04T01:10:00+00:00",
            "last_updated_at": "2026-09-04T01:10:00+00:00",
            "time_status": "verified",
            "source_tier": "official",
            "source_count": 1,
            "summary": "监管机构正式披露",
            "cross_tags": [],
            "assets": [],
            "impact": "low",
        }
        snapshot = {
            "schema_version": 1,
            "sections": {
                key: (discovery + [official] if key == "finance" else [])
                for key in briefing_service.SECTION_KEYS
            },
        }

        result = briefing_service.build_latest_briefing(
            repository=FakeRepository(),
            public_macro=None,
            decision_record=None,
            imported_snapshot=snapshot,
            now=NOW,
        )

        finance = next(
            section for section in result["sections"] if section["key"] == "finance"
        )
        self.assertEqual(finance["total_count"], 9)
        self.assertEqual(len(finance["items"]), 6)
        self.assertEqual(finance["items"][0]["source_tier"], "official")
        self.assertTrue(
            any(item["title"] == official["title"] for item in finance["items"])
        )

    def test_semantic_dedup_merges_vance_iran_wording_across_urls(self) -> None:
        events = [
            self.event(
                1,
                "万斯称对伊冲突并非战争",
                source="Reuters",
                source_url="https://www.reuters.com/world/vance-iran-one",
                kol_key="trump",
                kol_name="特朗普",
            ),
            self.event(
                2,
                "美国副总统万斯表示对伊军事行动不是战争",
                source="Bing News",
                source_url="https://www.bing.com/news/vance-iran-two",
                kol_key="trump",
                kol_name="特朗普",
                published_at="2026-09-04T01:50:00+00:00",
            ),
        ]

        result = briefing_service.build_latest_briefing(
            repository=FakeRepository(events),
            public_macro=None,
            decision_record=None,
            now=NOW,
        )

        world = next(
            section for section in result["sections"] if section["key"] == "world"
        )
        self.assertEqual(world["total_count"], 1)
        merged = world["items"][0]
        self.assertEqual(merged["source_tier"], "reporting")
        self.assertEqual(merged["source_count"], 2)
        self.assertEqual(merged["related_records"], 2)
        self.assertEqual(
            merged["last_updated_at"], "2026-09-04T01:55:00+00:00"
        )
        self.assertEqual(result["dedup_stats"]["semantic_matches"], 1)
        self.assertEqual(result["dedup_stats"]["merged_count"], 1)

    def test_same_url_conflicts_are_preserved_but_compatible_duplicates_merge(
        self,
    ) -> None:
        shared_url = "https://example.com/fed/live-statement"
        events = [
            self.event(
                1,
                "Fed cuts rates by 50 basis points",
                source_url=shared_url,
                published_at="2026-09-04T00:30:00+00:00",
                kol_key="powell",
                kol_name="鲍威尔",
                impact="high",
            ),
            self.event(
                2,
                "Fed cuts rates by 50 basis points",
                source_url=shared_url,
                published_at="2026-09-04T00:45:00+00:00",
                kol_key="powell",
                kol_name="鲍威尔",
                impact="medium",
            ),
            self.event(
                3,
                "Fed did not cut rates by 50 basis points",
                source_url=shared_url,
                published_at="2026-09-04T01:30:00+00:00",
                kol_key="powell",
                kol_name="鲍威尔",
                impact="low",
            ),
        ]

        result = briefing_service.build_latest_briefing(
            repository=FakeRepository(events),
            public_macro=None,
            decision_record=None,
            now=NOW,
        )
        macro = next(
            section for section in result["sections"] if section["key"] == "macro"
        )

        self.assertEqual(macro["total_count"], 2)
        self.assertEqual(result["dedup_stats"]["canonical_url_matches"], 1)
        self.assertEqual(len({item["story_key"] for item in macro["items"]}), 2)
        self.assertTrue(any("did not" in item["title"] for item in macro["items"]))
        affirmative = next(
            item for item in macro["items"] if "did not" not in item["title"]
        )
        self.assertEqual(affirmative["published_at"], "2026-09-04T00:45:00+00:00")
        self.assertEqual(affirmative["impact"], "medium")

    def test_later_import_cannot_replace_trusted_native_same_url_revision(
        self,
    ) -> None:
        shared_url = "https://www.reuters.com/world/fed-policy-update"
        trusted = self.event(
            1,
            "Fed cuts rates by 25 basis points",
            source_url=shared_url,
            published_at="2026-09-04T01:50:00+00:00",
            impact="high",
        )
        snapshot = {
            "schema_version": 1,
            "sections": {
                key: (
                    [
                        {
                            "title": "Fed cuts rates by 25 basis points",
                            "summary": "UNTRUSTED IMPORT MUST NOT WIN",
                            "source_label": "Unverified feed label",
                            "source_url": shared_url,
                            "story_key": "untrusted-later-revision",
                            "published_at": "2026-09-04T01:55:00+00:00",
                            "time_status": "verified",
                            "source_tier": "discovery",
                            "assets": [],
                            "cross_tags": [],
                        }
                    ]
                    if key == "macro"
                    else []
                )
                for key in briefing_service.SECTION_KEYS
            },
        }

        result = briefing_service.build_latest_briefing(
            repository=FakeRepository([trusted]),
            public_macro=None,
            decision_record=None,
            imported_snapshot=snapshot,
            now=NOW,
        )
        macro = next(
            section for section in result["sections"] if section["key"] == "macro"
        )

        self.assertEqual(macro["total_count"], 1)
        representative = macro["items"][0]
        self.assertEqual(representative["id"], "1")
        self.assertEqual(representative["source_tier"], "reporting")
        self.assertEqual(representative["source_label"], "Reuters")
        self.assertNotIn("UNTRUSTED", representative["summary"])
        self.assertEqual(representative["impact"], "high")
        self.assertEqual(representative["source_count"], 2)
        self.assertEqual(
            representative["last_updated_at"], "2026-09-04T01:50:00+00:00"
        )

    def test_same_tier_current_revision_wins_and_keeps_known_impact(self) -> None:
        shared_url = "https://www.reuters.com/markets/bank-funding-update"
        old_native = self.event(
            1,
            "Bank funding conditions improve",
            source_url=shared_url,
            published_at="2026-09-03T18:00:00+00:00",
            impact="high",
        )
        snapshot = {
            "schema_version": 1,
            "sections": {
                key: (
                    [
                        {
                            "title": "Bank funding conditions improve",
                            "summary": "Current Reuters revision.",
                            "source_label": "Reuters",
                            "source_url": shared_url,
                            "story_key": "current-bank-revision",
                            "published_at": "2026-09-04T01:55:00+00:00",
                            "time_status": "verified",
                            "source_tier": "reporting",
                            "assets": [],
                            "cross_tags": [],
                        }
                    ]
                    if key == "finance"
                    else []
                )
                for key in briefing_service.SECTION_KEYS
            },
        }

        result = briefing_service.build_latest_briefing(
            repository=FakeRepository([old_native]),
            public_macro=None,
            decision_record=None,
            imported_snapshot=snapshot,
            now=NOW,
        )
        finance = next(
            section for section in result["sections"] if section["key"] == "finance"
        )

        self.assertEqual(finance["total_count"], 1)
        representative = finance["items"][0]
        self.assertEqual(representative["id"], "current-bank-revision")
        self.assertEqual(representative["published_at"], "2026-09-04T01:55:00+00:00")
        self.assertEqual(representative["last_updated_at"], representative["published_at"])
        self.assertEqual(representative["impact"], "high")
        self.assertFalse(finance["stale"])

    def test_actionless_exact_titles_on_different_urls_are_not_merged(self) -> None:
        events = [
            self.event(
                1,
                "Market Update",
                source_url="https://example.com/market-update-one",
            ),
            self.event(
                2,
                "Market Update",
                source_url="https://example.com/market-update-two",
            ),
            self.event(
                3,
                "OpenAI Update",
                source_url="https://example.com/openai-update-one",
            ),
            self.event(
                4,
                "OpenAI Update",
                source_url="https://example.com/openai-update-two",
            ),
        ]

        result = briefing_service.build_latest_briefing(
            repository=FakeRepository(events),
            public_macro=None,
            decision_record=None,
            now=NOW,
        )

        self.assertEqual(result["coverage"]["total"], 4)
        self.assertEqual(result["dedup_stats"]["semantic_matches"], 0)

    def test_reused_landing_page_on_different_beijing_days_is_not_merged(self) -> None:
        shared_url = "https://example.com/daily/latest"
        events = [
            self.event(
                1,
                "Daily market briefing",
                source_url=shared_url,
                published_at="2026-09-03T15:30:00+00:00",
            ),
            self.event(
                2,
                "Daily market briefing",
                source_url=shared_url,
                published_at="2026-09-03T16:30:00+00:00",
            ),
        ]

        result = briefing_service.build_latest_briefing(
            repository=FakeRepository(events),
            public_macro=None,
            decision_record=None,
            now=NOW,
        )
        finance = next(
            section for section in result["sections"] if section["key"] == "finance"
        )

        self.assertEqual(finance["total_count"], 2)
        self.assertEqual(result["dedup_stats"]["canonical_url_matches"], 0)
        self.assertEqual(len({item["story_key"] for item in finance["items"]}), 2)

    def test_high_confidence_cluster_dedups_but_distinct_actions_do_not(self) -> None:
        clustered = ready_ai(
            confidence=0.91,
            cluster_key="openai-releases-reasoning-model",
            evidence_basis="title_and_snippet",
        )
        events = [
            self.event(
                1,
                "OpenAI 发布推理模型",
                source_url="https://www.reuters.com/technology/openai-one",
                ai_enrichment=clustered,
            ),
            self.event(
                2,
                "新推理模型由 OpenAI 正式推出",
                source_url="https://example.com/openai-two",
                ai_enrichment=clustered,
            ),
            self.event(
                8,
                "OpenAI recalls its reasoning model",
                source_url="https://example.com/openai-recall",
                ai_enrichment=clustered,
            ),
            self.event(
                9,
                "OpenAI releases GPT-6",
                source_url="https://example.com/openai-gpt-6",
                ai_enrichment=clustered,
            ),
            self.event(
                10,
                "OpenAI releases GPT-7",
                source_url="https://example.com/openai-gpt-7",
                ai_enrichment=clustered,
            ),
            self.event(
                3,
                "巴菲特增持日本商社",
                kol_key="buffett",
                kol_name="巴菲特",
            ),
            self.event(
                4,
                "巴菲特减持苹果股份",
                kol_key="buffett",
                kol_name="巴菲特",
            ),
        ]

        result = briefing_service.build_latest_briefing(
            repository=FakeRepository(events),
            public_macro=None,
            decision_record=None,
            now=NOW,
        )

        sections = {section["key"]: section for section in result["sections"]}
        self.assertEqual(sections["ai"]["total_count"], 4)
        self.assertEqual(sections["investors"]["total_count"], 2)
        self.assertEqual(result["dedup_stats"]["ai_cluster_matches"], 1)

    def test_english_semantic_dedup_keeps_different_openai_event_apart(self) -> None:
        events = [
            self.event(
                1,
                "OpenAI releases reasoning model for enterprise developers",
                source_url="https://www.reuters.com/technology/openai-model",
            ),
            self.event(
                2,
                "OpenAI launches a reasoning model aimed at enterprise developers",
                source_url="https://example.com/openai-model-launch",
            ),
            self.event(
                3,
                "OpenAI raises capital from global institutional investors",
                source_url="https://example.com/openai-fundraising",
            ),
        ]

        result = briefing_service.build_latest_briefing(
            repository=FakeRepository(events),
            public_macro=None,
            decision_record=None,
            now=NOW,
        )

        ai = next(section for section in result["sections"] if section["key"] == "ai")
        self.assertEqual(ai["total_count"], 2)
        self.assertEqual(result["dedup_stats"]["semantic_matches"], 1)
        self.assertTrue(
            any("raises capital" in item["title"] for item in ai["items"])
        )

    def test_semantic_signature_is_bilingual_and_direction_safe(self) -> None:
        events = [
            self.event(
                1,
                "美联储降息25个基点",
                kol_key="powell",
                kol_name="鲍威尔",
            ),
            self.event(
                2,
                "Fed cuts rates by 25 basis points",
                source_url="https://example.com/fed-cut-25bp",
                kol_key="powell",
                kol_name="鲍威尔",
            ),
            self.event(
                3,
                "美联储加息25个基点",
                source_url="https://example.com/fed-hike-25bp",
                kol_key="powell",
                kol_name="鲍威尔",
            ),
            self.event(
                4,
                "巴菲特增持苹果公司股份",
                kol_key="buffett",
                kol_name="巴菲特",
            ),
            self.event(
                5,
                "巴菲特减持苹果公司股份",
                source_url="https://example.com/buffett-cuts-apple",
                kol_key="buffett",
                kol_name="巴菲特",
            ),
            self.event(
                6,
                "OpenAI releases GPT-5.1 model",
                source_url="https://example.com/openai-release-gpt-5-1",
            ),
            self.event(
                7,
                "OpenAI recalls GPT-5.1 model",
                source_url="https://example.com/openai-recall-gpt-5-1",
            ),
            self.event(
                8,
                "美联储不会降息25个基点",
                source_url="https://example.com/fed-no-cut-25bp",
                kol_key="powell",
                kol_name="鲍威尔",
            ),
            self.event(
                9,
                "OpenAI will not release GPT-5.1 model",
                source_url="https://example.com/openai-no-release-gpt-5-1",
            ),
        ]

        result = briefing_service.build_latest_briefing(
            repository=FakeRepository(events),
            public_macro=None,
            decision_record=None,
            now=NOW,
        )

        sections = {section["key"]: section for section in result["sections"]}
        macro_titles = [item["title"] for item in sections["macro"]["items"]]
        self.assertEqual(sections["macro"]["total_count"], 3)
        self.assertTrue(
            any("降息" in title or "cuts rates" in title for title in macro_titles)
        )
        self.assertTrue(any("加息" in title for title in macro_titles))
        self.assertEqual(sections["investors"]["total_count"], 2)
        self.assertEqual(sections["ai"]["total_count"], 3)
        self.assertEqual(result["dedup_stats"]["semantic_matches"], 1)

    def test_rate_cut_negations_never_merge_with_positive_event(self) -> None:
        titles = (
            "Fed cuts rates by 25 basis points",
            "美联储未降息25个基点",
            "美联储没有降息25个基点",
            "美联储暂不降息25个基点",
            "Fed is not cutting rates by 25 basis points",
            "Fed will not in September cut rates by 25 basis points",
            "美联储未如预期降息25个基点",
            "Fed has not cut rates by 25 basis points",
            "美联储降息25个基点的传闻不实",
            "Fed unlikely to cut rates by 25 basis points",
            "Fed rules out cutting rates by 25 basis points",
            "Fed without cutting rates by 25 basis points",
            "Fed has no intention of cutting rates by 25 basis points",
            "Fed decides against cutting rates by 25 basis points",
            "美联储拒绝降息25个基点",
        )
        events = [
            self.event(
                index,
                title,
                source_url=f"https://example.com/rate-action-{index}",
                kol_key="powell",
                kol_name="鲍威尔",
            )
            for index, title in enumerate(titles, start=1)
        ]

        result = briefing_service.build_latest_briefing(
            repository=FakeRepository(events),
            public_macro=None,
            decision_record=None,
            now=NOW,
        )

        macro = next(
            section for section in result["sections"] if section["key"] == "macro"
        )
        self.assertEqual(macro["total_count"], len(titles))
        self.assertEqual(result["dedup_stats"]["semantic_matches"], 0)

    def test_product_release_expectation_denial_is_not_an_announcement(self) -> None:
        events = [
            self.event(
                1,
                "OpenAI releases GPT-6",
                source_url="https://example.com/openai-releases-gpt-6",
            ),
            self.event(
                2,
                "OpenAI is not expected to release GPT-6",
                source_url="https://example.com/openai-not-expected-gpt-6",
            ),
            self.event(
                3,
                "OpenAI has not released GPT-6",
                source_url="https://example.com/openai-has-not-gpt-6",
            ),
            self.event(
                4,
                "OpenAI cannot release GPT-6",
                source_url="https://example.com/openai-cannot-gpt-6",
            ),
            self.event(
                5,
                "OpenAI unlikely to release GPT-6",
                source_url="https://example.com/openai-unlikely-gpt-6",
            ),
        ]

        result = briefing_service.build_latest_briefing(
            repository=FakeRepository(events),
            public_macro=None,
            decision_record=None,
            now=NOW,
        )

        ai = next(section for section in result["sections"] if section["key"] == "ai")
        self.assertEqual(ai["total_count"], len(events))
        self.assertEqual(result["dedup_stats"]["semantic_matches"], 0)

    def test_negation_in_another_clause_never_merges_with_a_denial(self) -> None:
        titles = (
            "Fed cuts rates by 25 basis points, not ending QT",
            "Fed did not cut rates by 25 basis points",
            "美联储降息25个基点，未结束缩表",
            "美联储未降息25个基点",
        )
        result = briefing_service.build_latest_briefing(
            repository=FakeRepository(
                [
                    self.event(
                        index,
                        title,
                        source_url=f"https://example.com/negation-scope-{index}",
                        kol_key="powell",
                        kol_name="鲍威尔",
                    )
                    for index, title in enumerate(titles, start=1)
                ]
            ),
            public_macro=None,
            decision_record=None,
            now=NOW,
        )
        macro = next(
            section for section in result["sections"] if section["key"] == "macro"
        )

        self.assertEqual(macro["total_count"], len(titles))
        self.assertEqual(result["dedup_stats"]["semantic_matches"], 0)

    def test_predictions_and_rumours_never_merge_with_observed_events(self) -> None:
        events = [
            self.event(1, "Fed cuts rates by 25 basis points"),
            self.event(
                2,
                "Markets expect Fed to cut rates by 25 basis points in September",
                source_url="https://example.com/fed-expect-september",
            ),
            self.event(
                3,
                "Markets expect Fed to cut rates by 25 basis points in December",
                source_url="https://example.com/fed-expect-december",
            ),
            self.event(
                4,
                "OpenAI releases GPT-6",
                source_url="https://example.com/openai-gpt6-observed",
            ),
            self.event(
                5,
                "OpenAI may release GPT-6",
                source_url="https://example.com/openai-gpt6-maybe",
            ),
            self.event(
                6,
                "Fed to cut rates by 25 basis points",
                source_url="https://example.com/fed-to-cut",
            ),
            self.event(
                7,
                "Fed due to cut rates by 25 basis points",
                source_url="https://example.com/fed-due-to-cut",
            ),
            self.event(
                8,
                "美联储将降息25个基点",
                source_url="https://example.com/fed-future-cut-cn",
            ),
            self.event(
                9,
                "OpenAI to release GPT-6 next week",
                source_url="https://example.com/openai-to-release",
            ),
            self.event(
                10,
                "OpenAI 将发布 GPT-6",
                source_url="https://example.com/openai-future-cn",
            ),
        ]

        result = briefing_service.build_latest_briefing(
            repository=FakeRepository(events),
            public_macro=None,
            decision_record=None,
            now=NOW,
        )
        sections = {section["key"]: section for section in result["sections"]}

        self.assertEqual(sections["macro"]["total_count"], 6)
        self.assertEqual(sections["ai"]["total_count"], 4)
        self.assertEqual(result["dedup_stats"]["semantic_matches"], 0)

    def test_discussion_conditionals_and_questions_never_merge_as_facts(
        self,
    ) -> None:
        macro_titles = (
            "Fed cuts rates by 25 basis points",
            "Fed considers cutting rates by 25 basis points",
            "Fed debates cutting rates by 25 basis points",
            "Fed weighs cutting rates by 25 basis points",
            "Fed discusses cutting rates by 25 basis points",
            "Fed eyes cutting rates by 25 basis points",
            "Fed leaves the door open to cutting rates by 25 basis points",
            "美联储讨论降息25个基点",
            "美联储考虑降息25个基点",
            "美联储研究降息25个基点",
            "美联储将在下次会议评估是否降息25个基点",
            "If Fed cuts rates by 25 basis points",
            "Fed cuts rates by 25 basis points?",
            "美联储如果降息25个基点",
            "美联储若降息25个基点",
            "美联储降息25个基点？",
            "Fed cuts rates by 25 basis points next year",
            "美联储明年降息25个基点",
        )
        ai_titles = (
            "OpenAI releases GPT-6",
            "If OpenAI releases GPT-6",
            "OpenAI releases GPT-6?",
        )
        events = [
            self.event(
                index,
                title,
                source_url=f"https://example.com/ambiguous-macro-{index}",
                kol_key="powell",
                kol_name="鲍威尔",
            )
            for index, title in enumerate(macro_titles, start=1)
        ]
        events.extend(
            self.event(
                index,
                title,
                source_url=f"https://example.com/ambiguous-ai-{index}",
            )
            for index, title in enumerate(ai_titles, start=100)
        )

        result = briefing_service.build_latest_briefing(
            repository=FakeRepository(events),
            public_macro=None,
            decision_record=None,
            now=NOW,
        )
        sections = {section["key"]: section for section in result["sections"]}

        self.assertEqual(sections["macro"]["total_count"], len(macro_titles))
        self.assertEqual(sections["ai"]["total_count"], len(ai_titles))
        self.assertEqual(result["dedup_stats"]["semantic_matches"], 0)

    def test_multiple_actions_and_historical_clauses_never_semantic_merge(
        self,
    ) -> None:
        titles = (
            "Fed cuts rates by 25 basis points",
            "Fed hikes rates after cutting rates last year",
            "美联储加息，此前曾降息25个基点",
            "Fed keeps rates unchanged after cutting rates by 25 basis points in July",
            "美联储维持利率不变，此前曾降息25个基点",
            "Fed cut rates by 25 basis points in July",
            "Fed cut rates by 25 basis points at its previous meeting",
            "美联储7月降息25个基点",
            "美联储在上次会议降息25个基点",
        )
        result = briefing_service.build_latest_briefing(
            repository=FakeRepository(
                [
                    self.event(
                        index,
                        title,
                        source_url=f"https://example.com/multi-action-{index}",
                        kol_key="powell",
                        kol_name="鲍威尔",
                    )
                    for index, title in enumerate(titles, start=1)
                ]
            ),
            public_macro=None,
            decision_record=None,
            now=NOW,
        )
        macro = next(
            section for section in result["sections"] if section["key"] == "macro"
        )

        self.assertEqual(macro["total_count"], len(titles))
        self.assertEqual(result["dedup_stats"]["semantic_matches"], 0)

    def test_section_freshness_uses_only_displayed_verified_publication_time(self) -> None:
        old_items = []
        for index in range(6):
            item = self.event(
                index,
                f"银行季度财报事件 {index}",
                published_at="2026-09-03T22:00:00+00:00",
            )
            item["fetched_at"] = "2026-09-04T01:59:00+00:00"
            item["last_seen_at"] = "2026-09-04T01:59:00+00:00"
            old_items.append(item)
        hidden_fresh = self.event(
            99,
            "银行聚合市场线索 99",
            published_at="2026-09-04T01:58:00+00:00",
            source="Bing News",
            source_url="https://www.bing.com/news/hidden-fresh",
            impact="high",
        )

        result = briefing_service.build_latest_briefing(
            repository=FakeRepository(old_items + [hidden_fresh]),
            public_macro=None,
            decision_record=None,
            now=NOW,
        )

        finance = next(
            section for section in result["sections"] if section["key"] == "finance"
        )
        self.assertEqual(len(finance["items"]), 6)
        self.assertTrue(finance["stale"])
        self.assertEqual(finance["source_as_of"], "2026-09-03T22:00:00+00:00")
        self.assertTrue(result["stale"])
        self.assertEqual(result["source_as_of"], "2026-09-03T22:00:00+00:00")

        old_only = briefing_service.build_latest_briefing(
            repository=FakeRepository([old_items[0]]),
            public_macro=None,
            decision_record=None,
            now=NOW,
        )
        self.assertEqual(old_only["source_as_of"], "2026-09-03T22:00:00+00:00")
        self.assertTrue(old_only["stale"])

    def test_imported_hint_evidence_and_disclosure_dates_are_fail_closed(self) -> None:
        official = {
            "section": "world",
            "title": "OpenAI releases a new reasoning model",
            "source": "SEC",
            "source_url": "https://www.sec.gov/newsroom/press-releases/example",
            "canonical_url": "https://www.sec.gov/newsroom/press-releases/example",
            "story_key": "official-openai-release",
            "published_at": "2026-09-04T01:40:00+00:00",
            "last_updated_at": "2026-09-04T01:59:00+00:00",
            "fetched_at": "2026-09-04T01:59:00+00:00",
            "time_status": "verified",
            "source_tier": "official",
            "source_count": 1,
            "summary": "The filing describes the product release.",
            "cross_tags": [],
            "assets": [],
            "disclosed_at": "2026-09-04T01:30:00+00:00",
            "period_end": "2026-06-30",
            "account": "must-not-leak",
        }
        firsthand = {
            "section": "investors",
            "title": "投资人发布持仓观点",
            "source": "X @investor",
            "source_url": "https://x.com/investor/status/123",
            "canonical_url": "https://x.com/investor/status/123",
            "story_key": "investor-post",
            "published_at": "2026-09-04T01:42:00+00:00",
            "last_updated_at": "2026-09-04T01:42:00+00:00",
            "time_status": "verified",
            "source_tier": "first_party",
            "source_count": 1,
            "summary": "",
            "cross_tags": [],
            "assets": [],
        }
        snapshot = {
            "schema_version": 1,
            "sections": {
                key: (
                    [official]
                    if key == "world"
                    else [firsthand]
                    if key == "investors"
                    else []
                )
                for key in briefing_service.SECTION_KEYS
            },
        }

        result = briefing_service.build_latest_briefing(
            repository=FakeRepository(),
            public_macro=None,
            decision_record=None,
            imported_snapshot=snapshot,
            now=NOW,
        )

        sections = {section["key"]: section for section in result["sections"]}
        imported = sections["world"]["items"][0]
        self.assertEqual(imported["primary_section"], "world")
        self.assertIn("ai", imported["cross_tags"])
        self.assertEqual(imported["source_tier"], "official")
        self.assertEqual(imported["evidence_basis"], "title_and_snippet")
        self.assertEqual(imported["last_updated_at"], imported["published_at"])
        self.assertEqual(imported["disclosed_at"], "2026-09-04T01:30:00+00:00")
        self.assertEqual(imported["effective_at"], "2026-06-30")
        investor = sections["investors"]["items"][0]
        self.assertEqual(investor["source_tier"], "first_party")
        self.assertEqual(investor["evidence_basis"], "title_only")
        self.assertNotIn("disclosed_at", investor)
        self.assertNotIn("effective_at", investor)
        self.assertNotIn("account", json.dumps(result, ensure_ascii=False))

    def test_imported_snapshot_schema_and_bounds_are_revalidated(self) -> None:
        def payload() -> dict:
            return {
                "schema_version": 1,
                "sections": {key: [] for key in briefing_service.SECTION_KEYS},
            }

        invalid_payloads = []
        unsupported = payload()
        unsupported["schema_version"] = 2
        invalid_payloads.append(unsupported)
        missing_section = payload()
        missing_section["sections"].pop("macro")
        invalid_payloads.append(missing_section)
        too_many_in_one = payload()
        too_many_in_one["sections"]["world"] = [{}] * 81
        invalid_payloads.append(too_many_in_one)
        too_many_total = payload()
        for key in briefing_service.SECTION_KEYS:
            too_many_total["sections"][key] = [{}] * 51
        invalid_payloads.append(too_many_total)

        for snapshot in invalid_payloads:
            with self.subTest(snapshot=snapshot["schema_version"]):
                result = briefing_service.build_latest_briefing(
                    repository=FakeRepository(),
                    public_macro=None,
                    decision_record=None,
                    imported_snapshot=snapshot,
                    now=NOW,
                )
                self.assertFalse(result["available"])
                self.assertEqual(result["dedup_stats"]["input_count"], 0)

    def test_curated_paper_uses_featured_time_without_rewriting_publication(self) -> None:
        paper = {
            "section": "ai",
            "title": "A newly selected multimodal reasoning paper",
            "source": "AI Brief",
            "source_url": "https://ai-brief.liziran.com/zh/2026-09-04-paper",
            "original_url": "https://arxiv.org/abs/2609.01234",
            "canonical_url": "https://arxiv.org/abs/2609.01234",
            "discussion_url": "https://news.ycombinator.com/item?id=654321",
            "story_key": "paper-feature",
            "published_at": "2026-09-01T01:00:00+00:00",
            "fetched_at": "2026-09-04T01:58:00+00:00",
            "featured_at": "2026-09-04T01:55:00+00:00",
            "last_updated_at": "2026-09-01T01:00:00+00:00",
            "time_status": "verified",
            "publication_time_verified": True,
            "source_tier": "discovery",
            "source_count": 1,
            "summary": "Selected on T+3; the paper date remains unchanged.",
            "cross_tags": [],
            "assets": [],
            "kind": "paper_digest",
            "discovered_via": ["ai_brief_rss", "hacker_news_best"],
            "hn_id": 654321,
            "hn_score": 120,
            "hn_comments": 44,
            "hn_rank": 8,
            "heat_score": 66.0,
        }
        snapshot = {
            "schema_version": 1,
            "sections": {
                key: ([paper] if key == "ai" else [])
                for key in briefing_service.SECTION_KEYS
            },
        }

        result = briefing_service.build_latest_briefing(
            repository=FakeRepository(),
            public_macro=None,
            decision_record=None,
            imported_snapshot=snapshot,
            now=NOW,
        )

        ai = next(section for section in result["sections"] if section["key"] == "ai")
        item = ai["items"][0]
        self.assertEqual(item["kind"], "paper_digest")
        self.assertEqual(item["published_at"], "2026-09-01T01:00:00+00:00")
        self.assertEqual(item["featured_at"], "2026-09-04T01:55:00+00:00")
        self.assertEqual(item["original_url"], "https://arxiv.org/abs/2609.01234")
        self.assertEqual(item["hn_score"], 120)
        self.assertEqual(item["source_tier"], "discovery")
        self.assertTrue(ai["status"] == "fresh" and not ai["stale"])
        self.assertEqual(ai["source_as_of"], "2026-09-04T01:55:00+00:00")
        self.assertEqual(result["content_as_of"], "2026-09-04T01:55:00+00:00")
        self.assertIn("HN 120", item["rank_reason"])
        self.assertNotIn(item, result["firsthand"])

    def test_featured_only_digest_is_visible_but_not_time_verified(self) -> None:
        digest = {
            "section": "ai",
            "title": "Daily AI source roundup",
            "source": "AI Digest",
            "source_url": "https://ai-digest.liziran.com/zh/2026-09-04-digest",
            "canonical_url": "https://ai-digest.liziran.com/zh/2026-09-04-digest",
            "story_key": "digest-feature",
            "published_at": None,
            "fetched_at": "2026-09-04T01:58:00+00:00",
            "featured_at": "2026-09-04T01:54:00+00:00",
            "last_updated_at": "2026-09-04T01:54:00+00:00",
            "time_status": "featured_only",
            "publication_time_verified": False,
            "source_tier": "discovery",
            "source_count": 1,
            "summary": "The curation time is known; source publication is not.",
            "cross_tags": [],
            "assets": [],
            "kind": "ai_digest",
            "discovered_via": ["ai_digest_rss"],
        }
        snapshot = {
            "schema_version": 1,
            "sections": {
                key: ([digest] if key == "ai" else [])
                for key in briefing_service.SECTION_KEYS
            },
        }

        result = briefing_service.build_latest_briefing(
            repository=FakeRepository(),
            public_macro=None,
            decision_record=None,
            imported_snapshot=snapshot,
            now=NOW,
        )

        ai = next(section for section in result["sections"] if section["key"] == "ai")
        item = ai["items"][0]
        self.assertEqual(item["kind"], "ai_digest")
        self.assertNotIn("published_at", item)
        self.assertFalse(item["publication_time_verified"])
        self.assertEqual(item["time_status"], "featured_only")
        self.assertEqual(ai["verified_count"], 0)
        self.assertEqual(ai["status"], "fresh")

    def test_invalid_or_overheated_discovery_metadata_fails_closed(self) -> None:
        story = {
            "section": "technology",
            "title": "HN discovery story",
            "source": "Hacker News",
            "source_url": "https://example.com/hn-story",
            "original_url": "https://example.com/hn-story",
            "canonical_url": "https://example.com/hn-story",
            "discussion_url": "https://news.ycombinator.com/item?id=123",
            "story_key": "hn-story",
            "published_at": "2026-09-04T01:45:00+00:00",
            "fetched_at": "2026-09-04T01:58:00+00:00",
            "featured_at": "2026-09-04T01:55:00+00:00",
            "last_updated_at": "2026-09-04T01:45:00+00:00",
            "time_status": "verified",
            "publication_time_verified": True,
            "source_tier": "discovery",
            "source_count": 1,
            "summary": "A community-discovered story.",
            "cross_tags": [],
            "assets": [],
            "kind": "hn_story",
            "discovered_via": ["hacker_news_top"],
            "hn_id": 123,
            "hn_score": 500,
            "hn_comments": 200,
            "hn_rank": 1,
            "heat_score": 101.0,
        }
        snapshot = {
            "schema_version": 1,
            "sections": {
                key: ([story] if key == "technology" else [])
                for key in briefing_service.SECTION_KEYS
            },
        }

        result = briefing_service.build_latest_briefing(
            repository=FakeRepository(),
            public_macro=None,
            decision_record=None,
            imported_snapshot=snapshot,
            now=NOW,
        )

        self.assertFalse(result["available"])
        self.assertEqual(result["highlights"], [])

    def test_hn_heat_is_recomputed_and_current_discovery_beats_stale_reporting(
        self,
    ) -> None:
        def hn_story(
            item_id: int,
            *,
            producer_heat: float,
            rank: int,
            score: int,
            comments: int,
        ) -> dict:
            original = f"https://example.com/hn-{item_id}"
            published = "2026-09-04T01:20:00+00:00"
            return {
                "section": "technology",
                "title": f"Distinct Hacker News story {item_id}",
                "source": "Hacker News",
                "source_url": original,
                "original_url": original,
                "canonical_url": original,
                "discussion_url": (
                    f"https://news.ycombinator.com/item?id={item_id}"
                ),
                "story_key": f"hn-{item_id}",
                "published_at": published,
                "fetched_at": "2026-09-04T01:59:00+00:00",
                "featured_at": "2026-09-04T01:59:00+00:00",
                "last_updated_at": published,
                "time_status": "verified",
                "publication_time_verified": True,
                "source_tier": "discovery",
                "source_count": 1,
                "summary": "Community discovery metadata.",
                "cross_tags": [],
                "assets": [],
                "kind": "hn_story",
                "discovered_via": ["hacker_news_top"],
                "hn_id": item_id,
                "hn_score": score,
                "hn_comments": comments,
                "hn_rank": rank,
                "heat_score": producer_heat,
            }

        def reporting(story_key: str, *, published_at: str) -> dict:
            return {
                "section": "technology",
                "title": f"Verified semiconductor reporting {story_key}",
                "source": "Reuters",
                "source_url": f"https://www.reuters.com/technology/{story_key}",
                "canonical_url": f"https://www.reuters.com/technology/{story_key}",
                "story_key": story_key,
                "published_at": published_at,
                "time_status": "verified",
                "source_tier": "reporting",
                "source_count": 1,
                "summary": "Verified reporting remains higher trust.",
                "cross_tags": [],
                "assets": [],
            }

        stories = [
            reporting("fresh-reporting", published_at="2026-09-04T01:50:00+00:00"),
            *[
                reporting(
                    f"stale-reporting-{index}",
                    published_at="2026-09-03T06:00:00+00:00",
                )
                for index in range(6)
            ],
            hn_story(
                101,
                producer_heat=100.0,
                rank=50,
                score=1,
                comments=0,
            ),
            hn_story(
                102,
                producer_heat=0.0,
                rank=1,
                score=500,
                comments=250,
            ),
        ]
        strong_projection = briefing_service._imported_highlight(
            stories[-1], section_hint="technology", now=NOW
        )
        self.assertIsNotNone(strong_projection)
        assert strong_projection is not None
        self.assertEqual(
            briefing_service._trusted_hn_heat(strong_projection, now=NOW),
            briefing_collect.hn_heat_score(
                rank=1,
                score=500,
                comments=250,
                age_hours=40 / 60,
                appears_in_both=False,
            ),
        )
        snapshot = {
            "schema_version": 1,
            "sections": {
                key: (stories if key == "technology" else [])
                for key in briefing_service.SECTION_KEYS
            },
        }

        result = briefing_service.build_latest_briefing(
            repository=FakeRepository(),
            public_macro=None,
            decision_record=None,
            imported_snapshot=snapshot,
            now=NOW,
        )
        technology = next(
            section for section in result["sections"] if section["key"] == "technology"
        )

        item_ids = [item["id"] for item in technology["items"]]
        self.assertEqual(item_ids[:3], ["fresh-reporting", "hn-102", "hn-101"])
        self.assertEqual(len(item_ids), 6)
        self.assertEqual(sum(value.startswith("stale-reporting-") for value in item_ids), 3)

    def test_cross_source_curated_heat_without_hn_time_is_display_only(self) -> None:
        plain = {
            "section": "ai",
            "title": "Recent curated model release",
            "source": "AI Digest",
            "source_url": "https://ai-digest.liziran.com/zh/recent-release",
            "canonical_url": "https://example.com/news/recent-release",
            "original_url": "https://example.com/news/recent-release",
            "story_key": "recent-curated",
            "published_at": None,
            "fetched_at": "2026-09-04T01:58:00+00:00",
            "featured_at": "2026-09-04T01:55:00+00:00",
            "last_updated_at": "2026-09-04T01:55:00+00:00",
            "time_status": "featured_only",
            "publication_time_verified": False,
            "source_tier": "discovery",
            "source_count": 1,
            "summary": "A recent curated item.",
            "cross_tags": [],
            "assets": [],
            "kind": "ai_digest",
            "discovered_via": ["ai_digest_rss"],
        }
        cross_source = {
            **plain,
            "title": "Older curated agent discussion",
            "source_url": "https://ai-digest.liziran.com/zh/older-agent",
            "canonical_url": "https://example.com/news/older-agent",
            "original_url": "https://example.com/news/older-agent",
            "discussion_url": "https://news.ycombinator.com/item?id=777",
            "story_key": "older-cross-source",
            "featured_at": "2026-09-04T01:40:00+00:00",
            "last_updated_at": "2026-09-04T01:40:00+00:00",
            "discovered_via": ["ai_digest_rss", "hacker_news_top"],
            "hn_id": 777,
            "hn_score": 5_000,
            "hn_comments": 1_000,
            "hn_rank": 1,
            "heat_score": 100.0,
        }
        snapshot = {
            "schema_version": 1,
            "sections": {
                key: ([cross_source, plain] if key == "ai" else [])
                for key in briefing_service.SECTION_KEYS
            },
        }

        result = briefing_service.build_latest_briefing(
            repository=FakeRepository(),
            public_macro=None,
            decision_record=None,
            imported_snapshot=snapshot,
            now=NOW,
        )
        ai = next(section for section in result["sections"] if section["key"] == "ai")

        self.assertEqual(
            [item["id"] for item in ai["items"]],
            ["recent-curated", "older-cross-source"],
        )
        self.assertEqual(ai["items"][1]["heat_score"], 100.0)

    def test_generic_original_is_rejected_and_omitted_originals_do_not_merge(
        self,
    ) -> None:
        def digest(story_key: str) -> dict:
            return {
                "section": "ai",
                "title": f"Distinct digest evidence {story_key}",
                "source": "AI Digest",
                "source_url": f"https://ai-digest.liziran.com/zh/{story_key}",
                "canonical_url": f"https://ai-digest.liziran.com/zh/{story_key}",
                "story_key": story_key,
                "published_at": None,
                "fetched_at": "2026-09-04T01:58:00+00:00",
                "featured_at": "2026-09-04T01:55:00+00:00",
                "last_updated_at": "2026-09-04T01:55:00+00:00",
                "time_status": "featured_only",
                "publication_time_verified": False,
                "source_tier": "discovery",
                "source_count": 1,
                "summary": "No unique underlying original was claimed.",
                "cross_tags": [],
                "assets": [],
                "kind": "ai_digest",
                "discovered_via": ["ai_digest_rss"],
            }

        first = digest("digest-one")
        second = digest("digest-two")
        invalid = {
            **digest("digest-invalid"),
            "original_url": "https://openai.com/",
            "canonical_url": "https://openai.com/",
        }
        snapshot = {
            "schema_version": 1,
            "sections": {
                key: ([first, second, invalid] if key == "ai" else [])
                for key in briefing_service.SECTION_KEYS
            },
        }

        result = briefing_service.build_latest_briefing(
            repository=FakeRepository(),
            public_macro=None,
            decision_record=None,
            imported_snapshot=snapshot,
            now=NOW,
        )
        ai = next(section for section in result["sections"] if section["key"] == "ai")

        self.assertEqual(ai["total_count"], 2)
        self.assertEqual(
            {item["id"] for item in ai["items"]}, {"digest-one", "digest-two"}
        )

    def test_old_hn_submission_is_not_refreshed_by_a_new_fetch(self) -> None:
        old_submission = {
            "section": "technology",
            "title": "Old Hacker News submission fetched again",
            "source": "Hacker News",
            "source_url": "https://example.com/old-hn-story",
            "original_url": "https://example.com/old-hn-story",
            "canonical_url": "https://example.com/old-hn-story",
            "discussion_url": "https://news.ycombinator.com/item?id=999",
            "story_key": "old-hn-story",
            "published_at": "2026-09-02T15:00:00+00:00",
            "fetched_at": "2026-09-04T01:59:00+00:00",
            "featured_at": "2026-09-04T01:59:00+00:00",
            "last_updated_at": "2026-09-02T15:00:00+00:00",
            "time_status": "verified",
            "publication_time_verified": True,
            "source_tier": "discovery",
            "source_count": 1,
            "summary": "The HN submission is 35 hours old.",
            "cross_tags": [],
            "assets": [],
            "kind": "hn_story",
            "discovered_via": ["hacker_news_best"],
            "hn_id": 999,
            "hn_score": 800,
            "hn_comments": 500,
            "hn_rank": 1,
            "heat_score": 100.0,
        }
        snapshot = {
            "schema_version": 1,
            "sections": {
                key: ([old_submission] if key == "technology" else [])
                for key in briefing_service.SECTION_KEYS
            },
        }

        result = briefing_service.build_latest_briefing(
            repository=FakeRepository(),
            public_macro=None,
            decision_record=None,
            imported_snapshot=snapshot,
            now=NOW,
        )

        technology = next(
            section for section in result["sections"] if section["key"] == "technology"
        )
        self.assertEqual(technology["items"], [])
        self.assertEqual(result["highlights"], [])
        self.assertFalse(result["available"])

    def test_dedup_projection_is_bounded_for_large_candidate_page(self) -> None:
        events = [
            self.event(
                index,
                f"Company {index} reports distinct quarterly result {index}",
                source_url=f"https://example.com/distinct/{index}",
            )
            for index in range(564)
        ]

        started = perf_counter()
        result = briefing_service.build_latest_briefing(
            repository=FakeRepository(events),
            public_macro=None,
            decision_record=None,
            now=NOW,
        )
        elapsed = perf_counter() - started

        self.assertEqual(result["dedup_stats"]["input_count"], 564)
        self.assertEqual(result["dedup_stats"]["output_count"], 564)
        self.assertLess(elapsed, 1.0)

    def test_imported_fetched_only_item_cannot_enter_news_sections(self) -> None:
        item = {
            "section": "world",
            "title": "只有抓取时间的旧格式线索",
            "source_url": "https://example.com/fetched-only",
            "canonical_url": "https://example.com/fetched-only",
            "story_key": "fetched-only",
            "published_at": None,
            "fetched_at": "2026-09-04T01:58:00+00:00",
            "last_updated_at": "2026-09-04T01:58:00+00:00",
            "time_status": "fetched_only",
            "source_tier": "media",
            "source_count": 1,
            "summary": "不能将抓取时间冒充发布时间",
            "cross_tags": [],
            "assets": [],
        }
        snapshot = {
            "schema_version": 1,
            "sections": {
                key: ([item] if key == "world" else [])
                for key in briefing_service.SECTION_KEYS
            },
        }

        result = briefing_service.build_latest_briefing(
            repository=FakeRepository(),
            public_macro=None,
            decision_record=None,
            imported_snapshot=snapshot,
            now=NOW,
        )

        self.assertFalse(result["available"])
        self.assertEqual(result["highlights"], [])
        self.assertTrue(all(not section["items"] for section in result["sections"]))


if __name__ == "__main__":
    unittest.main()
