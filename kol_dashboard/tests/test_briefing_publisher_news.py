from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
import json
import unittest

from kol_dashboard import briefing_service as subject
from kol_dashboard.tests.test_briefing_service import FakeRepository, NOW


class PublisherBriefingTests(unittest.TestCase):
    def item(self, ident="a", **kwargs):
        return {
            "id": ident, "publisher": "WSJ", "title": "Stock market outlook",
            "snippet": "Equities and company earnings remain in focus.",
            "url": "https://wsj.com/markets/article-" + ident,
            "published_at": (NOW - timedelta(minutes=5)).isoformat(),
            "published_at_status": "provided", "first_seen_at": NOW.isoformat(),
            "evidence_scope": "title_and_snippet", "source_ids": ["wsj_markets", "wsj_business"],
            **kwargs,
        }

    def analysis(self, ident="a", **kwargs):
        return {"status": "ready", "evidence_scope": "title_and_snippet", "evidence_ids": [ident],
                "headline_zh": "股票市场展望", "summary_zh": "报道关注股票市场及企业盈利。",
                "why_it_matters_zh": "影响仍需结合后续数据核验。", **kwargs}

    def sources(self):
        return [{"id": ident, "status": "ok", "stale": False,
                 "last_checked_at": NOW.isoformat(), "last_success_at": NOW.isoformat(),
                 "etag": "private-validator", "secret": "must-not-leak"}
                for ident in sorted(subject._PUBLISHER_FEEDS)]

    def build(self, items, *, events=None, **kwargs):
        return subject.build_latest_briefing(repository=FakeRepository(events), public_macro=None,
            decision_record=None, publisher_news={"items": items, "sources": self.sources()}, now=NOW, **kwargs)

    @staticmethod
    def stories(result):
        return [item for section in result["sections"] for item in section["items"]]

    def event(self, ident, *, impact="medium", **kwargs):
        return {"id": ident, "title": f"Stock market report {ident}", "snippet": "Equity investors review earnings.",
                "published_at": (NOW - timedelta(minutes=15)).isoformat(), "time_status": "verified",
                "source": "Bing News", "source_url": f"https://example.com/report/{ident}",
                "impact": impact, "source_count": 1, **kwargs}

    def test_optional_argument_preserves_exact_legacy_payload(self):
        args = dict(public_macro=None, decision_record=None, now=NOW)
        self.assertEqual(subject.build_latest_briefing(repository=FakeRepository(), **args),
                         subject.build_latest_briefing(repository=FakeRepository(), publisher_news=None, **args))

    def test_publisher_enters_real_section_without_kol_or_fake_firsthand(self):
        raw = self.item(analysis=self.analysis(), kol_name="Injected person", impact="critical", holdings="private")
        before = deepcopy(raw)
        result = self.build([raw])
        story = self.stories(result)[0]
        self.assertEqual(story["kind"], "publisher_news")
        self.assertEqual(story["title"], "股票市场展望")
        self.assertEqual(story["source_tier"], "reporting")
        self.assertEqual(story["source_label"], "WSJ")
        self.assertEqual(story["source_count"], 1)
        self.assertEqual(story["impact"], "unknown")
        self.assertNotIn("kol_name", story)
        self.assertTrue(story["ai_summary_used"])
        self.assertEqual(result["firsthand"], [])
        self.assertEqual(result["major_count"], 0)
        self.assertEqual(result["coverage"]["reporting"], 1)
        self.assertEqual(raw, before)
        self.assertNotIn("must-not-leak", json.dumps(result))
        self.assertNotIn("private-validator", json.dumps(result))

    def test_original_fact_title_controls_section_not_ai_commentary(self):
        story = self.stories(self.build([self.item(title="OpenAI releases a new AI model",
            analysis=self.analysis(headline_zh="央行降息展望", summary_zh="央行政策可能影响股票。"))]))[0]
        self.assertEqual(story["primary_section"], "ai")
        self.assertIn("macro", story["cross_tags"])

    def test_bad_dates_unknown_publishers_and_mismatched_urls_are_excluded(self):
        cases = [self.item(published_at=None), self.item(published_at_status="future"),
                 self.item(published_at=(NOW + timedelta(seconds=1)).isoformat()),
                 self.item(published_at=(NOW - timedelta(hours=25)).isoformat()),
                 self.item(publisher="Unknown"), self.item(url="https://bloomberg.com/a"),
                 self.item(url="https://localhost/a"), self.item(url="https://wsj.com/a?token=private")]
        for raw in cases:
            with self.subTest(raw=raw):
                self.assertEqual(self.stories(self.build([raw])), [])

    def test_title_only_and_unbound_analysis_do_not_claim_excerpt_summary(self):
        for analysis in (self.analysis(evidence_ids=["other"]), self.analysis(evidence_scope="official_body"), self.analysis()):
            raw = self.item(snippet="", evidence_scope="title_only", analysis=analysis)
            story = self.stories(self.build([raw]))[0]
            self.assertEqual(story["evidence_basis"], "title_only")
            self.assertFalse(story["ai_summary_used"])
            self.assertEqual(story["title"], raw["title"])
        raw = self.item(snippet="", evidence_scope="title_only", analysis=self.analysis(evidence_scope="title_only"))
        self.assertTrue(self.stories(self.build([raw]))[0]["ai_summary_used"])

    def test_same_article_in_two_feeds_and_kol_search_is_one_media_source(self):
        raw = self.item()
        event = self.event("kol-one", title=raw["title"], source="WSJ", snippet=raw["snippet"],
                           published_at=raw["published_at"], source_url=raw["url"].replace("https://", "https://www.") + "?mod=search")
        result = self.build([raw, deepcopy(raw)], events=[event])
        stories = self.stories(result)
        self.assertEqual(len(stories), 1)
        self.assertEqual(stories[0]["source_count"], 1)
        self.assertEqual(result["dedup_stats"]["output_count"], 1)

    def test_empty_publisher_poll_does_not_freshen_hourly_coverage_or_content(self):
        imported = {"schema_version": 1, "source_as_of": (NOW - timedelta(hours=3)).isoformat(),
                    "sections": {key: [] for key in subject.SECTION_KEYS}}
        result = self.build([], imported_snapshot=imported, refresh_schedule="hourly")
        self.assertEqual(result["source_coverage_as_of"], imported["source_as_of"])
        self.assertEqual(result["refresh_schedule_status"], "delayed")
        self.assertEqual(result["publisher_source_coverage_as_of"], NOW.isoformat())
        self.assertFalse(result["publisher_source_coverage_stale"])
        self.assertIsNone(result["content_as_of"])
        self.assertTrue(result["stale"])

    def test_publisher_coverage_requires_each_feed_and_uses_oldest_success(self):
        sources = self.sources()
        sources[0]["last_success_at"] = (NOW - timedelta(minutes=10)).isoformat()
        projection = subject._publisher_coverage({"sources": sources}, now=NOW)
        self.assertEqual(projection["publisher_source_coverage_as_of"], sources[0]["last_success_at"])
        self.assertFalse(projection["publisher_source_coverage_stale"])
        sources[0]["status"] = "error"
        self.assertTrue(subject._publisher_coverage({"sources": sources}, now=NOW)["publisher_source_coverage_stale"])
        sources.pop()
        incomplete = subject._publisher_coverage({"sources": sources}, now=NOW)
        self.assertIsNone(incomplete["publisher_source_coverage_as_of"])
        self.assertTrue(incomplete["publisher_source_coverage_stale"])

    def test_recent_fetch_does_not_refresh_publication_or_evidence_time(self):
        published = (NOW - timedelta(hours=4)).isoformat()
        result = self.build([self.item(published_at=published)])
        self.assertEqual(result["content_as_of"], published)
        self.assertTrue(result["stale"])
        self.assertFalse(result["publisher_source_coverage_stale"])

    def test_crowded_ordinary_section_reserves_one_unknown_impact_media_story(self):
        events = [self.event(str(index)) for index in range(8)]
        result = self.build([self.item()], events=events)
        section = next(value for value in result["sections"] if value["key"] == "finance")
        self.assertEqual(len(section["items"]), subject.MAX_SECTION_ITEMS)
        self.assertEqual(sum(item["kind"] == "publisher_news" for item in section["items"]), 1)
        self.assertEqual(section["selection_source_diversity"]["policy"], "one_direct_publisher")
        self.assertEqual(next(item for item in section["items"] if item["kind"] == "publisher_news")["impact"], "unknown")

    def test_diversity_does_not_displace_high_impact_or_primary_evidence(self):
        events = [self.event(str(index), impact="high") for index in range(8)]
        result = self.build([self.item()], events=events)
        section = next(value for value in result["sections"] if value["key"] == "finance")
        self.assertTrue(all(item["impact"] == "high" for item in section["items"]))
        self.assertNotIn("selection_source_diversity", section)
        native = [subject._event_highlight(self.event(str(index))) for index in range(8)]
        for item in native:
            item["source_tier"] = "official"
        publisher = subject._publisher_highlight(self.item(), now=NOW)
        sections, _ = subject._section_payloads([*native, publisher], now=NOW)
        finance = next(value for value in sections if value["key"] == "finance")
        self.assertTrue(all(item["source_tier"] == "official" for item in finance["items"]))
        self.assertNotIn("selection_source_diversity", finance)


if __name__ == "__main__":
    unittest.main()
