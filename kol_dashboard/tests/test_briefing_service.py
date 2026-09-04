from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from kol_dashboard import briefing_service


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
        self.assertEqual(
            briefing_service.classify_source({"source": "Unknown"}),
            "discovery",
        )


class BriefingBuildTests(unittest.TestCase):
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
                "source_as_of",
                "stale",
                "coverage",
                "lead",
                "highlights",
                "firsthand",
                "watchpoints",
                "disclaimer",
            },
        )
        self.assertFalse(result["available"])
        self.assertTrue(result["stale"])
        self.assertIsNone(result["source_as_of"])
        self.assertEqual(result["lead"], {})
        self.assertEqual(result["highlights"], [])
        self.assertEqual(result["firsthand"], [])
        self.assertEqual(result["watchpoints"], [])
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
        self.assertNotIn("source_count", first)
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


if __name__ == "__main__":
    unittest.main()
