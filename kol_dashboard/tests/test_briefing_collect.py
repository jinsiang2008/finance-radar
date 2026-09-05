from __future__ import annotations

import io
import inspect
import json
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from unittest import mock

from kol_dashboard import briefing_collect, briefing_import


class FakeResponse:
    def __init__(self, body: bytes, *, status: int = 200) -> None:
        self.body = body
        self.status = status
        self.closed = False

    def read(self, maximum: int) -> bytes:
        return self.body[:maximum]

    def close(self) -> None:
        self.closed = True


class FakeOpener:
    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, float]] = []
        self._lock = threading.Lock()

    def __call__(self, request, *, timeout: float):
        url = request.full_url
        with self._lock:
            self.calls.append((url, timeout))
            if url not in self.routes:
                raise AssertionError(f"unexpected URL: {url}")
            response = self.routes[url]
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, str):
            response = response.encode()
        if not isinstance(response, bytes):
            raise AssertionError(f"invalid fake response for {url}")
        return FakeResponse(response)


class RedirectSafetyTests(unittest.TestCase):
    def test_cross_origin_private_redirect_is_rejected_before_second_request(
        self,
    ) -> None:
        handler = briefing_collect.SameOriginRedirectHandler()
        request = briefing_collect.urllib.request.Request(
            briefing_collect.AI_DIGEST_FEED_URL
        )

        with self.assertRaises(briefing_collect.urllib.error.HTTPError) as caught:
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                Message(),
                "https://100.100.100.200/private",
            )

        self.assertIn("cross-origin redirect rejected", str(caught.exception))

    def test_same_origin_redirect_normalizes_default_https_port(self) -> None:
        handler = briefing_collect.SameOriginRedirectHandler()
        request = briefing_collect.urllib.request.Request(
            briefing_collect.AI_DIGEST_FEED_URL
        )

        redirected = handler.redirect_request(
            request,
            None,
            302,
            "Found",
            Message(),
            "https://ai-digest.liziran.com:443/zh/next.xml",
        )

        self.assertIsNotNone(redirected)
        self.assertEqual(
            redirected.full_url,
            "https://ai-digest.liziran.com:443/zh/next.xml",
        )

    def test_same_origin_redirect_chain_is_limited_to_three_hops(self) -> None:
        handler = briefing_collect.SameOriginRedirectHandler()
        request = briefing_collect.urllib.request.Request(
            briefing_collect.AI_DIGEST_FEED_URL
        )
        for hop in range(1, briefing_collect.MAX_REDIRECTS + 1):
            redirected = handler.redirect_request(
                request,
                None,
                302,
                "Found",
                Message(),
                f"https://ai-digest.liziran.com/zh/hop-{hop}.xml",
            )
            self.assertIsNotNone(redirected)
            request = redirected

        with self.assertRaisesRegex(
            briefing_collect.urllib.error.HTTPError,
            "redirect limit exceeded",
        ):
            handler.redirect_request(
                request,
                None,
                302,
                "Found",
                Message(),
                "https://ai-digest.liziran.com/zh/hop-4.xml",
            )

    def test_default_collector_builds_restricted_opener(self) -> None:
        fake = FakeOpener({})
        with mock.patch.object(
            briefing_collect,
            "_default_opener",
            return_value=fake,
        ) as factory:
            collector = briefing_collect.BriefingCollector()

        factory.assert_called_once_with()
        self.assertIs(collector.opener, fake)

    def test_public_entrypoints_leave_default_opener_selection_to_collector(
        self,
    ) -> None:
        self.assertIsNone(
            inspect.signature(briefing_collect.collect_briefing)
            .parameters["opener"]
            .default
        )
        self.assertIsNone(
            inspect.signature(briefing_collect.produce_briefing)
            .parameters["opener"]
            .default
        )


def json_bytes(value: object) -> bytes:
    return json.dumps(value).encode()


def rss_item(
    *,
    title: str,
    link: str,
    pub_date: str,
    description: str = "",
    content: str = "",
) -> str:
    return f"""
    <item>
      <title>{title}</title>
      <link>{link}</link>
      <guid isPermaLink="true">{link}</guid>
      <description><![CDATA[{description}]]></description>
      <content:encoded><![CDATA[{content}]]></content:encoded>
      <pubDate>{pub_date}</pubDate>
    </item>
    """


def rss_document(*items: str) -> bytes:
    return (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<rss version='2.0' "
        "xmlns:content='http://purl.org/rss/1.0/modules/content/'>"
        "<channel>"
        + "".join(items)
        + "</channel></rss>"
    ).encode()


def page_metadata(*, modified: str | None = None, published: str | None = None) -> bytes:
    tags = []
    if modified:
        tags.append(
            f'<meta property="article:modified_time" content="{modified}">'
        )
    if published:
        tags.append(
            f'<meta property="article:published_time" content="{published}">'
        )
    return ("<html><head>" + "".join(tags) + "</head></html>").encode()


class HackerNewsCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 9, 5, 3, 0, tzinfo=timezone.utc)

    def item(self, item_id: int, **overrides) -> dict:
        payload = {
            "id": item_id,
            "type": "story",
            "title": f"Story {item_id}",
            "url": f"https://example.com/story-{item_id}",
            "time": int((self.now - timedelta(hours=1)).timestamp()),
            "score": 100,
            "descendants": 30,
        }
        payload.update(overrides)
        return payload

    def test_heat_score_uses_every_auditable_signal_and_is_bounded(self) -> None:
        baseline = briefing_collect.hn_heat_score(
            rank=20,
            score=20,
            comments=5,
            age_hours=20,
            appears_in_both=False,
        )
        self.assertGreater(
            briefing_collect.hn_heat_score(
                rank=1,
                score=20,
                comments=5,
                age_hours=20,
                appears_in_both=False,
            ),
            baseline,
        )
        self.assertGreater(
            briefing_collect.hn_heat_score(
                rank=20,
                score=200,
                comments=5,
                age_hours=20,
                appears_in_both=False,
            ),
            baseline,
        )
        self.assertGreater(
            briefing_collect.hn_heat_score(
                rank=20,
                score=20,
                comments=100,
                age_hours=20,
                appears_in_both=False,
            ),
            baseline,
        )
        self.assertGreater(
            briefing_collect.hn_heat_score(
                rank=20,
                score=20,
                comments=5,
                age_hours=1,
                appears_in_both=False,
            ),
            baseline,
        )
        self.assertGreater(
            briefing_collect.hn_heat_score(
                rank=20,
                score=20,
                comments=5,
                age_hours=20,
                appears_in_both=True,
            ),
            baseline,
        )
        maximum = briefing_collect.hn_heat_score(
            rank=1,
            score=1_000_000,
            comments=1_000_000,
            age_hours=0,
            appears_in_both=True,
        )
        self.assertEqual(maximum, 100.0)
        at_zero = briefing_collect.hn_heat_score(
            rank=5,
            score=100,
            comments=40,
            age_hours=0,
            appears_in_both=False,
        )
        at_eight = briefing_collect.hn_heat_score(
            rank=5,
            score=100,
            comments=40,
            age_hours=8,
            appears_in_both=False,
        )
        at_sixteen = briefing_collect.hn_heat_score(
            rank=5,
            score=100,
            comments=40,
            age_hours=16,
            appears_in_both=False,
        )
        self.assertGreater(at_zero - at_eight, at_eight - at_sixteen)

    def test_hn_collection_is_partial_failure_safe_and_filters_invalid_items(
        self,
    ) -> None:
        routes: dict[str, object] = {
            briefing_collect.HN_TOP_URL: json_bytes([1, 2, 3, 4, 5, 6, 7]),
            briefing_collect.HN_BEST_URL: json_bytes([2, 1]),
            f"{briefing_collect.HN_API_BASE}/item/1.json": json_bytes(
                self.item(1, title="A fast Rust database")
            ),
            f"{briefing_collect.HN_API_BASE}/item/2.json": json_bytes(
                self.item(
                    2,
                    title="Ask HN: How do you read papers?",
                    url=None,
                    score=80,
                    descendants=120,
                )
            ),
            f"{briefing_collect.HN_API_BASE}/item/3.json": json_bytes(
                self.item(3, dead=True)
            ),
            f"{briefing_collect.HN_API_BASE}/item/4.json": json_bytes(
                self.item(4, type="job")
            ),
            f"{briefing_collect.HN_API_BASE}/item/5.json": json_bytes(
                self.item(
                    5,
                    time=int((self.now - timedelta(hours=25)).timestamp()),
                )
            ),
            f"{briefing_collect.HN_API_BASE}/item/6.json": json_bytes(
                self.item(
                    6,
                    time=int((self.now + timedelta(seconds=1)).timestamp()),
                )
            ),
            f"{briefing_collect.HN_API_BASE}/item/7.json": OSError("timeout"),
        }
        collector = briefing_collect.BriefingCollector(
            opener=FakeOpener(routes),
            clock=lambda: self.now,
        )

        stories, successful_lists = collector.collect_hacker_news(current=self.now)

        self.assertEqual(successful_lists, 2)
        self.assertEqual({story.hn_id for story in stories}, {1, 2})
        external = next(story for story in stories if story.hn_id == 1)
        ask = next(story for story in stories if story.hn_id == 2)
        self.assertEqual(external.source_url, "https://example.com/story-1")
        self.assertEqual(external.original_url, external.source_url)
        self.assertEqual(
            external.discussion_url,
            "https://news.ycombinator.com/item?id=1",
        )
        self.assertEqual(
            external.discovered_via,
            ("hacker_news_top", "hacker_news_best"),
        )
        self.assertIn("Hacker News 采集快照", external.summary)
        self.assertNotIn("距今", external.summary)
        self.assertIn("可审计热度分", external.why_it_matters)
        self.assertEqual(
            ask.source_url,
            "https://news.ycombinator.com/item?id=2",
        )
        self.assertIsNone(ask.original_url)
        self.assertTrue(any("item 7 failed" in error for error in collector.errors))

    def test_hn_scan_limit_is_one_global_item_request_budget(self) -> None:
        top_ids = list(range(1, 11))
        best_ids = list(range(101, 111))
        selected = (1, 101, 2, 102, 3)
        routes: dict[str, object] = {
            briefing_collect.HN_TOP_URL: json_bytes(top_ids),
            briefing_collect.HN_BEST_URL: json_bytes(best_ids),
        }
        for item_id in selected:
            routes[f"{briefing_collect.HN_API_BASE}/item/{item_id}.json"] = (
                json_bytes(self.item(item_id))
            )
        opener = FakeOpener(routes)
        collector = briefing_collect.BriefingCollector(
            opener=opener,
            clock=lambda: self.now,
            hn_scan_limit=5,
        )

        stories, successful_lists = collector.collect_hacker_news(current=self.now)

        self.assertEqual(successful_lists, 2)
        self.assertEqual({story.hn_id for story in stories}, set(selected))
        item_calls = [url for url, _ in opener.calls if "/item/" in url]
        self.assertEqual(len(item_calls), 5)

    def test_new_hn_story_without_descendants_is_collected_with_zero_comments(
        self,
    ) -> None:
        payload = self.item(8, title="A brand-new database release")
        payload.pop("descendants")
        routes = {
            briefing_collect.HN_TOP_URL: json_bytes([8]),
            briefing_collect.HN_BEST_URL: json_bytes([]),
            f"{briefing_collect.HN_API_BASE}/item/8.json": json_bytes(payload),
        }
        collector = briefing_collect.BriefingCollector(
            opener=FakeOpener(routes),
            clock=lambda: self.now,
        )

        stories, successful_lists = collector.collect_hacker_news(current=self.now)

        self.assertEqual(successful_lists, 2)
        self.assertEqual(len(stories), 1)
        self.assertEqual(stories[0].hn_comments, 0)
        self.assertIn("0 comments", stories[0].summary)

    def test_malformed_explicit_hn_comment_counts_fail_closed(self) -> None:
        for comments in (-1, "0"):
            with self.subTest(comments=comments):
                payload = self.item(8, descendants=comments)
                routes = {
                    briefing_collect.HN_TOP_URL: json_bytes([8]),
                    briefing_collect.HN_BEST_URL: json_bytes([]),
                    f"{briefing_collect.HN_API_BASE}/item/8.json": json_bytes(
                        payload
                    ),
                }
                collector = briefing_collect.BriefingCollector(
                    opener=FakeOpener(routes),
                    clock=lambda: self.now,
                )

                stories, successful_lists = collector.collect_hacker_news(
                    current=self.now
                )

                self.assertEqual(successful_lists, 2)
                self.assertEqual(stories, [])

    def test_generic_homepage_urls_do_not_merge_unrelated_hn_stories(self) -> None:
        routes = {
            briefing_collect.HN_TOP_URL: json_bytes([9, 10]),
            briefing_collect.HN_BEST_URL: json_bytes([]),
            f"{briefing_collect.HN_API_BASE}/item/9.json": json_bytes(
                self.item(9, title="OpenAI launches product Alpha", url="https://openai.com/")
            ),
            f"{briefing_collect.HN_API_BASE}/item/10.json": json_bytes(
                self.item(10, title="OpenAI recalls product Beta", url="https://openai.com/")
            ),
        }
        collector = briefing_collect.BriefingCollector(
            opener=FakeOpener(routes),
            clock=lambda: self.now,
        )

        stories, _ = collector.collect_hacker_news(current=self.now)
        deduplicated = briefing_collect.deduplicate_stories(stories)

        self.assertEqual(len(deduplicated), 2)
        self.assertEqual({story.hn_id for story in deduplicated}, {9, 10})
        self.assertTrue(all(story.original_url is None for story in stories))
        self.assertEqual(
            {story.source_url for story in stories},
            {
                "https://news.ycombinator.com/item?id=9",
                "https://news.ycombinator.com/item?id=10",
            },
        )
        for story in stories:
            payload = story.to_v1_item()
            self.assertEqual(payload["source_url"], payload["discussion_url"])


class RssCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 9, 5, 3, 0, tzinfo=timezone.utc)

    def collector(self, routes: dict[str, object]) -> briefing_collect.BriefingCollector:
        return briefing_collect.BriefingCollector(
            opener=FakeOpener(routes),
            clock=lambda: self.now,
        )

    def test_digest_splits_numbered_content_and_never_promotes_future_noon(
        self,
    ) -> None:
        issue_url = "https://ai-digest.liziran.com/zh/digest/today.html"
        content = """
          <h2>1. OpenAI publishes a safety report</h2>
          <p>The report describes a bounded evaluation.</p>
          <p><strong>为什么重要：</strong>开发者可以核验原始报告。</p>
          <blockquote><a href="https://openai.com/index/safety-report">Source</a></blockquote>
          <hr>
          <h2>2. A synthesis uses two reports</h2>
          <p>The two reports disagree on one metric.</p>
          <blockquote>
            <a href="https://example.com/one">One</a>
            <a href="https://example.org/two">Two</a>
          </blockquote>
        """
        feed = rss_document(
            rss_item(
                title="Daily issue",
                link=issue_url,
                pub_date="Sat, 05 Sep 2026 12:00:00 +0000",
                description="Daily summary",
                content=content,
            )
        )
        routes = {
            briefing_collect.AI_DIGEST_FEED_URL: feed,
            issue_url: page_metadata(
                modified="2026-09-05T10:30:00+08:00",
                published="2026-09-05T12:00:00+00:00",
            ),
        }
        collector = self.collector(routes)

        stories, succeeded = collector.collect_feed(
            briefing_collect._FEEDS[0],
            current=self.now,
        )

        self.assertTrue(succeeded)
        self.assertEqual(len(stories), 2)
        first, second = stories
        self.assertEqual(first.title, "OpenAI publishes a safety report")
        self.assertEqual(first.source_url, issue_url)
        self.assertEqual(
            first.original_url,
            "https://openai.com/index/safety-report",
        )
        self.assertIsNone(second.original_url)
        for story in stories:
            self.assertFalse(story.publication_time_verified)
            self.assertIsNone(story.published_at)
            self.assertEqual(
                story.featured_at,
                datetime(2026, 9, 5, 2, 30, tzinfo=timezone.utc),
            )
            self.assertIn("不写 published_at", story.why_it_matters)
            self.assertEqual(story.kind, "ai_digest")
            self.assertEqual(story.discovered_via, ("ai_digest_rss",))

    def test_adversarial_curated_content_is_bounded_and_deadline_checked(
        self,
    ) -> None:
        content = "".join(
            f"<h2>{index}. Story {index}</h2>"
            for index in range(1, 20_001)
        )
        started = time.monotonic()
        blocks = briefing_collect._curated_blocks(
            content,
            maximum=3,
            check_deadline=lambda: None,
        )

        self.assertEqual(len(blocks), 3)
        self.assertLess(time.monotonic() - started, 0.5)

        dense_block = "<h2>1. Dense story</h2>" + "".join(
            "<p>Paragraph <a href='https://example.com/articles/source-1'>"
            "source</a></p>"
            for _ in range(100)
        )
        dense = briefing_collect._curated_blocks(dense_block, maximum=1)[0]
        self.assertEqual(
            len(dense.paragraphs),
            briefing_collect.MAX_CURATED_PARAGRAPHS_PER_BLOCK,
        )
        self.assertEqual(
            len(dense.links),
            briefing_collect.MAX_CURATED_LINKS_PER_BLOCK,
        )

        issue_url = "https://ai-digest.liziran.com/zh/digest/adversarial.html"
        collector = briefing_collect.BriefingCollector(
            opener=FakeOpener(
                {
                    briefing_collect.AI_DIGEST_FEED_URL: rss_document(
                        rss_item(
                            title="Adversarial issue",
                            link=issue_url,
                            pub_date="Sat, 05 Sep 2026 01:00:00 +0000",
                            content=content,
                        )
                    ),
                    issue_url: page_metadata(
                        modified="2026-09-05T02:00:00+00:00"
                    ),
                }
            ),
            clock=lambda: self.now,
            deadline_seconds=0.2,
            rss_item_limit=3,
        )
        started = time.monotonic()
        stories, succeeded = collector.collect_feed(
            briefing_collect._FEEDS[0],
            current=self.now,
        )

        self.assertTrue(succeeded)
        self.assertEqual(len(stories), 3)
        self.assertLess(time.monotonic() - started, 0.5)

        tick = 0.0

        def advancing_monotonic() -> float:
            nonlocal tick
            tick += 0.01
            return tick

        expiring = briefing_collect.BriefingCollector(
            opener=FakeOpener({}),
            deadline_seconds=0.015,
            monotonic=advancing_monotonic,
        )
        expiring._start_deadline()
        with self.assertRaisesRegex(
            briefing_collect.FetchError,
            "deadline exceeded while parsing",
        ):
            briefing_collect._curated_blocks(
                content,
                maximum=briefing_collect.MAX_RSS_ITEM_LIMIT,
                check_deadline=expiring._ensure_deadline,
            )

    def test_ai_brief_prefers_one_arxiv_target_within_a_paper_block(self) -> None:
        issue_url = "https://ai-brief.liziran.com/zh/daily/today.html"
        content = """
          <h2>今日概览</h2>
          <p>Overview text is not a paper card.</p>
          <h2>重点关注</h2>
          <h3>Agent A routing paper improves protocol validity</h3>
          <p>The paper separates control flow from prompts.</p>
          <p><a href="https://github.com/example/code">Code</a></p>
          <blockquote><a href="https://arxiv.org/abs/2609.01234v2">Paper</a></blockquote>
          <h2>今日观察</h2>
          <p>This section must not become a paper card.</p>
        """
        routes = {
            briefing_collect.AI_BRIEF_FEED_URL: rss_document(
                rss_item(
                    title="Research issue",
                    link=issue_url,
                    pub_date="Sat, 05 Sep 2026 01:00:00 +0000",
                    content=content,
                )
            ),
            issue_url: page_metadata(modified="2026-09-05T02:00:00+00:00"),
        }
        collector = self.collector(routes)

        stories, succeeded = collector.collect_feed(
            briefing_collect._FEEDS[1],
            current=self.now,
        )

        self.assertTrue(succeeded)
        self.assertEqual(len(stories), 1)
        story = stories[0]
        self.assertEqual(story.kind, "paper_digest")
        self.assertEqual(story.source_url, issue_url)
        self.assertEqual(story.original_url, "https://arxiv.org/abs/2609.01234v2")
        self.assertIn("arxiv:2609.01234", story.aliases)
        self.assertIn("KIND:PAPER_DIGEST", story.assets)

    def test_title_only_curated_block_does_not_invent_a_summary(self) -> None:
        issue_url = "https://ai-brief.liziran.com/zh/daily/title-only.html"
        routes = {
            briefing_collect.AI_BRIEF_FEED_URL: rss_document(
                rss_item(
                    title="Research issue",
                    link=issue_url,
                    pub_date="Sat, 05 Sep 2026 01:00:00 +0000",
                    content=(
                        "<h2>重点关注</h2>"
                        "<h3>Title-only paper signal</h3>"
                        "<a href='https://arxiv.org/abs/2609.12345'>Paper</a>"
                    ),
                )
            ),
            issue_url: page_metadata(modified="2026-09-05T02:00:00+00:00"),
        }

        stories, succeeded = self.collector(routes).collect_feed(
            briefing_collect._FEEDS[1],
            current=self.now,
        )

        self.assertTrue(succeeded)
        self.assertEqual(len(stories), 1)
        self.assertEqual(stories[0].summary, "")

    def test_bad_xml_and_dtd_fail_closed(self) -> None:
        for body in (
            b"<rss><broken>",
            b"<!DOCTYPE rss [<!ENTITY x 'bad'>]><rss><channel/></rss>",
        ):
            collector = self.collector(
                {briefing_collect.AI_DIGEST_FEED_URL: body}
            )
            stories, succeeded = collector.collect_feed(
                briefing_collect._FEEDS[0],
                current=self.now,
            )
            self.assertFalse(succeeded)
            self.assertEqual(stories, [])
            self.assertTrue(collector.errors)

    def test_rss_scan_limit_bounds_detail_page_requests(self) -> None:
        items = []
        routes: dict[str, object] = {}
        for index in range(5):
            issue_url = f"https://ai-digest.liziran.com/zh/digest/{index}.html"
            items.append(
                rss_item(
                    title=f"Issue {index}",
                    link=issue_url,
                    pub_date="Sat, 05 Sep 2026 01:00:00 +0000",
                    content=(
                        f"<h2>1. Event {index}</h2><p>Summary.</p>"
                        f"<p><a href='https://example.com/{index}'>Source</a></p>"
                    ),
                )
            )
            if index < 2:
                routes[issue_url] = page_metadata(
                    modified="2026-09-05T02:00:00+00:00"
                )
        routes[briefing_collect.AI_DIGEST_FEED_URL] = rss_document(*items)
        opener = FakeOpener(routes)
        collector = briefing_collect.BriefingCollector(
            opener=opener,
            clock=lambda: self.now,
            rss_scan_limit=2,
        )

        stories, succeeded = collector.collect_feed(
            briefing_collect._FEEDS[0],
            current=self.now,
        )

        self.assertTrue(succeeded)
        self.assertEqual(len(stories), 2)
        page_calls = [
            url for url, _ in opener.calls if url != briefing_collect.AI_DIGEST_FEED_URL
        ]
        self.assertEqual(len(page_calls), 2)

    def test_future_or_unparseable_feed_time_without_page_time_is_skipped(
        self,
    ) -> None:
        for label, pub_date in (
            ("future", "Sat, 05 Sep 2026 12:00:00 +0000"),
            ("unparseable", "sometime today"),
        ):
            with self.subTest(label=label):
                issue_url = (
                    "https://ai-digest.liziran.com/zh/digest/"
                    f"no-time-{label}.html"
                )
                routes = {
                    briefing_collect.AI_DIGEST_FEED_URL: rss_document(
                        rss_item(
                            title="Unverified issue",
                            link=issue_url,
                            pub_date=pub_date,
                            content=(
                                "<h2>1. Event without a trustworthy time</h2>"
                                "<p>Summary.</p>"
                                "<p><a href='https://example.com/event'>Source</a></p>"
                            ),
                        )
                    ),
                    issue_url: page_metadata(),
                }
                collector = self.collector(routes)

                stories, succeeded = collector.collect_feed(
                    briefing_collect._FEEDS[0],
                    current=self.now,
                )

                self.assertTrue(succeeded)
                self.assertEqual(stories, [])

    def test_curated_generic_original_does_not_collapse_unrelated_cards(self) -> None:
        issue_url = "https://ai-digest.liziran.com/zh/digest/two-events.html"
        content = """
          <h2>1. Company launches product Alpha</h2>
          <p>Alpha launches.</p>
          <p><a href="https://example.com/company-announcements">Source</a></p>
          <hr>
          <h2>2. Company recalls product Beta</h2>
          <p>Beta is recalled.</p>
          <p><a href="https://example.com/company-announcements">Source</a></p>
          <hr>
          <h2>3. Company launches product Gamma</h2>
          <p>Gamma launches.</p><p><a href="https://example.org/index">Source</a></p>
          <hr>
          <h2>4. Company recalls product Delta</h2>
          <p>Delta is recalled.</p><p><a href="https://example.org/index">Source</a></p>
        """
        routes = {
            briefing_collect.AI_DIGEST_FEED_URL: rss_document(
                rss_item(
                    title="Two events",
                    link=issue_url,
                    pub_date="Sat, 05 Sep 2026 01:00:00 +0000",
                    content=content,
                )
            ),
            issue_url: page_metadata(modified="2026-09-05T02:00:00+00:00"),
        }
        collector = self.collector(routes)
        stories, succeeded = collector.collect_feed(
            briefing_collect._FEEDS[0],
            current=self.now,
        )
        self.assertTrue(succeeded)
        self.assertEqual(len(stories), 4)
        self.assertTrue(all(story.original_url is None for story in stories))
        payload = {
            "schema_version": 1,
            "snapshot_date": "2026-09-05",
            "generated_at": self.now.isoformat(),
            "source_as_of": self.now.isoformat(),
            "sections": {section: [] for section in briefing_import.SECTION_KEYS},
        }
        payload["sections"]["ai"] = [story.to_v1_item() for story in stories]

        normalized = briefing_import.validate_payload(payload, now=self.now)
        repository = mock.Mock()
        repository.upsert_daily_briefing_snapshot.return_value = 7
        imported = briefing_import.import_payload(
            payload,
            repository=repository,
            now=self.now,
        )

        self.assertEqual(len(normalized["sections"]["ai"]), 4)
        self.assertEqual(imported["item_count"], 4)
        persisted = repository.upsert_daily_briefing_snapshot.call_args.kwargs[
            "payload"
        ]
        self.assertEqual(len(persisted["sections"]["ai"]), 4)


class FullProducerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 9, 5, 3, 0, tzinfo=timezone.utc)

    def routes(self) -> dict[str, object]:
        original = "https://example.com/articles/shared-ai-story"
        digest_url = "https://ai-digest.liziran.com/zh/digest/shared.html"
        brief_url = "https://ai-brief.liziran.com/zh/daily/papers.html"
        hn_item = {
            "id": 101,
            "type": "story",
            "title": "Shared AI story on Hacker News",
            "url": original,
            "time": int((self.now - timedelta(hours=2)).timestamp()),
            "score": 180,
            "descendants": 90,
        }
        digest_content = f"""
          <h2>1. Shared AI event from a primary report</h2>
          <p>A concise, source-backed account of the event.</p>
          <blockquote><a href="{original}">Original report</a></blockquote>
        """
        brief_content = """
          <h2>1. A new paper evaluates agent routing</h2>
          <p>The evaluation measures protocol validity.</p>
          <blockquote><a href="https://arxiv.org/abs/2609.00001">Paper</a></blockquote>
        """
        return {
            briefing_collect.HN_TOP_URL: json_bytes([101]),
            briefing_collect.HN_BEST_URL: json_bytes([101]),
            f"{briefing_collect.HN_API_BASE}/item/101.json": json_bytes(hn_item),
            briefing_collect.AI_DIGEST_FEED_URL: rss_document(
                rss_item(
                    title="Digest issue",
                    link=digest_url,
                    pub_date="Sat, 05 Sep 2026 12:00:00 +0000",
                    content=digest_content,
                )
            ),
            digest_url: page_metadata(modified="2026-09-05T02:15:00+00:00"),
            briefing_collect.AI_BRIEF_FEED_URL: rss_document(
                rss_item(
                    title="Brief issue",
                    link=brief_url,
                    pub_date="Sat, 05 Sep 2026 01:00:00 +0000",
                    content=brief_content,
                )
            ),
            brief_url: page_metadata(modified="2026-09-05T02:10:00+00:00"),
        }

    def test_full_payload_has_six_sections_deduplicates_and_imports(self) -> None:
        result = briefing_collect.collect_briefing(
            opener=FakeOpener(self.routes()),
            now=self.now,
        )

        self.assertEqual(
            set(result.payload["sections"]),
            set(briefing_import.SECTION_KEYS),
        )
        self.assertEqual(result.payload["snapshot_date"], "2026-09-05")
        self.assertLessEqual(
            datetime.fromisoformat(result.payload["source_as_of"]),
            datetime.fromisoformat(result.payload["generated_at"]),
        )
        ai_items = result.payload["sections"]["ai"]
        self.assertEqual(len(ai_items), 2)
        shared = next(item for item in ai_items if item["kind"] == "ai_digest")
        self.assertEqual(
            shared["discovered_via"],
            ["hacker_news_top", "hacker_news_best", "ai_digest_rss"],
        )
        self.assertEqual(shared["hn_id"], 101)
        self.assertEqual(shared["hn_score"], 180)
        self.assertEqual(shared["hn_comments"], 90)
        self.assertGreater(shared["heat_score"], 0)
        self.assertEqual(shared["featured_at"], "2026-09-05T02:15:00+00:00")
        self.assertEqual(
            shared["discussion_url"],
            "https://news.ycombinator.com/item?id=101",
        )
        self.assertEqual(
            shared["original_url"],
            "https://example.com/articles/shared-ai-story",
        )
        self.assertEqual(result.payload["sections"]["technology"], [])
        normalized = briefing_import.validate_payload(result.payload, now=self.now)
        self.assertEqual(len(normalized["sections"]["ai"]), 2)
        self.assertEqual(result.errors, ())

    def test_generic_hn_links_survive_full_collection_as_distinct_discussions(
        self,
    ) -> None:
        routes = self.routes()
        routes[briefing_collect.HN_TOP_URL] = json_bytes([9, 10])
        routes[briefing_collect.HN_BEST_URL] = json_bytes([9, 10])
        for item_id, title in (
            (9, "OpenAI launches product Alpha"),
            (10, "OpenAI recalls product Beta"),
        ):
            routes[f"{briefing_collect.HN_API_BASE}/item/{item_id}.json"] = (
                json_bytes(
                    {
                        "id": item_id,
                        "type": "story",
                        "title": title,
                        "url": "https://openai.com/",
                        "time": int((self.now - timedelta(hours=1)).timestamp()),
                        "score": 50,
                        "descendants": 5,
                    }
                )
            )

        result = briefing_collect.collect_briefing(
            opener=FakeOpener(routes),
            now=self.now,
        )

        hn_items = [
            item
            for section in result.payload["sections"].values()
            for item in section
            if item["kind"] == "hn_story"
        ]
        self.assertEqual(len(hn_items), 2)
        self.assertEqual({item["hn_id"] for item in hn_items}, {9, 10})
        self.assertTrue(all(item.get("original_url") is None for item in hn_items))
        self.assertEqual(
            {item["source_url"] for item in hn_items},
            {
                "https://news.ycombinator.com/item?id=9",
                "https://news.ycombinator.com/item?id=10",
            },
        )
        briefing_import.validate_payload(result.payload, now=self.now)

    def test_all_network_failures_preserve_previous_atomic_output(self) -> None:
        routes = {
            briefing_collect.HN_TOP_URL: OSError("offline"),
            briefing_collect.HN_BEST_URL: OSError("offline"),
            briefing_collect.AI_DIGEST_FEED_URL: OSError("offline"),
            briefing_collect.AI_BRIEF_FEED_URL: OSError("offline"),
        }
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory, "daily.json")
            destination.write_text("last-valid-snapshot\n", encoding="utf-8")

            with self.assertRaises(briefing_collect.CollectionError):
                briefing_collect.produce_briefing(
                    output_path=destination,
                    opener=FakeOpener(routes),
                    now=self.now,
                )

            self.assertEqual(
                destination.read_text(encoding="utf-8"),
                "last-valid-snapshot\n",
            )

    def test_each_required_source_root_is_fail_closed_before_output(self) -> None:
        required_roots = (
            briefing_collect.HN_TOP_URL,
            briefing_collect.HN_BEST_URL,
            briefing_collect.AI_DIGEST_FEED_URL,
            briefing_collect.AI_BRIEF_FEED_URL,
        )
        for root_url in required_roots:
            with self.subTest(root_url=root_url), tempfile.TemporaryDirectory() as directory:
                routes = self.routes()
                routes[root_url] = OSError("required source unavailable")
                destination = Path(directory, "daily.json")
                destination.write_text("previous\n", encoding="utf-8")

                with self.assertRaises(briefing_collect.CollectionError):
                    briefing_collect.produce_briefing(
                        output_path=destination,
                        opener=FakeOpener(routes),
                        now=self.now,
                    )

                self.assertEqual(
                    destination.read_text(encoding="utf-8"),
                    "previous\n",
                )

    def test_each_ai_feed_must_produce_at_least_one_current_story(self) -> None:
        for feed_url in (
            briefing_collect.AI_DIGEST_FEED_URL,
            briefing_collect.AI_BRIEF_FEED_URL,
        ):
            with self.subTest(feed_url=feed_url), tempfile.TemporaryDirectory() as directory:
                routes = self.routes()
                routes[feed_url] = rss_document()
                destination = Path(directory, "daily.json")
                destination.write_text("previous\n", encoding="utf-8")

                with mock.patch.object(
                    briefing_collect.briefing_import,
                    "import_payload",
                ) as importer, self.assertRaises(briefing_collect.CollectionError):
                    briefing_collect.produce_briefing(
                        output_path=destination,
                        import_snapshot=True,
                        opener=FakeOpener(routes),
                        now=self.now,
                    )

                self.assertEqual(
                    destination.read_text(encoding="utf-8"),
                    "previous\n",
                )
                importer.assert_not_called()

    def test_empty_hn_roots_preserve_last_good_and_do_not_import(self) -> None:
        routes = self.routes()
        routes[briefing_collect.HN_TOP_URL] = json_bytes([])
        routes[briefing_collect.HN_BEST_URL] = json_bytes([])
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory, "daily.json")
            destination.write_text("last-good\n", encoding="utf-8")

            with mock.patch.object(
                briefing_collect.briefing_import,
                "import_payload",
            ) as importer, self.assertRaisesRegex(
                briefing_collect.CollectionError,
                "Hacker News must provide at least one current valid story",
            ):
                briefing_collect.produce_briefing(
                    output_path=destination,
                    import_snapshot=True,
                    opener=FakeOpener(routes),
                    now=self.now,
                )

            self.assertEqual(
                destination.read_text(encoding="utf-8"),
                "last-good\n",
            )
            importer.assert_not_called()

    def test_all_hn_item_failures_fail_completeness(self) -> None:
        routes = self.routes()
        routes[f"{briefing_collect.HN_API_BASE}/item/101.json"] = OSError(
            "item unavailable"
        )

        with self.assertRaisesRegex(
            briefing_collect.CollectionError,
            "Hacker News must provide at least one current valid story",
        ):
            briefing_collect.collect_briefing(
                opener=FakeOpener(routes),
                now=self.now,
            )

    def test_path_import_success_summary_uses_stdout_not_stderr(self) -> None:
        payload = {
            "schema_version": 1,
            "snapshot_date": "2026-09-05",
            "generated_at": self.now.isoformat(),
            "source_as_of": self.now.isoformat(),
            "sections": {section: [] for section in briefing_import.SECTION_KEYS},
        }
        result = briefing_collect.CollectionResult(payload=payload, errors=())
        imported = {"snapshot_id": 7, "item_count": 2}
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(
            briefing_collect,
            "produce_briefing",
            return_value=(result, imported),
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = briefing_collect.main(
                ["--output", "/tmp/daily.json", "--import"]
            )

        self.assertEqual(exit_code, 0)
        self.assertIn('"snapshot_id": 7', stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")


class CollectionBudgetTests(unittest.TestCase):
    def test_blocking_close_cannot_extend_the_wall_clock_deadline(self) -> None:
        read_started = threading.Event()
        close_started = threading.Event()
        release_close = threading.Event()
        release_read = threading.Event()

        class BlockingCloseResponse:
            status = 200

            def read(self, maximum: int) -> bytes:
                read_started.set()
                release_read.wait(2)
                return b"[]"

            def close(self) -> None:
                close_started.set()
                release_close.wait(2)
                release_read.set()

        collector = briefing_collect.BriefingCollector(
            opener=lambda request, *, timeout: BlockingCloseResponse(),
            timeout=1,
            deadline_seconds=0.05,
        )
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(
                briefing_collect.FetchError,
                "wall-clock deadline exceeded",
            ):
                collector._fetch(briefing_collect.HN_TOP_URL, maximum=100)
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertTrue(read_started.is_set())
            self.assertTrue(close_started.wait(0.2))
        finally:
            release_close.set()
            release_read.set()

    def test_blocking_read_is_closed_and_returns_within_wall_clock_deadline(
        self,
    ) -> None:
        read_started = threading.Event()
        closed = threading.Event()

        class BlockingResponse:
            status = 200

            def read(self, maximum: int) -> bytes:
                read_started.set()
                closed.wait(2)
                return b"[]"

            def close(self) -> None:
                closed.set()

        def opener(request, *, timeout: float):
            return BlockingResponse()

        collector = briefing_collect.BriefingCollector(
            opener=opener,
            timeout=1,
            deadline_seconds=0.05,
        )
        started = time.monotonic()

        with self.assertRaisesRegex(
            briefing_collect.FetchError,
            "wall-clock deadline exceeded",
        ):
            collector._fetch(briefing_collect.HN_TOP_URL, maximum=100)

        elapsed = time.monotonic() - started
        self.assertTrue(read_started.is_set())
        self.assertTrue(closed.is_set())
        self.assertLess(elapsed, 0.5)

    def test_request_timeout_is_clamped_to_whole_batch_deadline(self) -> None:
        ticks = iter((0.0, 1.0))
        opener = FakeOpener({briefing_collect.HN_TOP_URL: b"[]"})
        collector = briefing_collect.BriefingCollector(
            opener=opener,
            timeout=4,
            deadline_seconds=3,
            monotonic=lambda: next(ticks),
        )

        collector._fetch(briefing_collect.HN_TOP_URL, maximum=100)

        self.assertEqual(opener.calls[0][1], 2.0)

    def test_expired_batch_deadline_refuses_request_before_open(self) -> None:
        ticks = iter((0.0, 2.0))
        opener = FakeOpener({briefing_collect.HN_TOP_URL: b"[]"})
        collector = briefing_collect.BriefingCollector(
            opener=opener,
            deadline_seconds=1,
            monotonic=lambda: next(ticks),
        )

        with self.assertRaisesRegex(
            briefing_collect.FetchError,
            "deadline exceeded",
        ):
            collector._fetch(briefing_collect.HN_TOP_URL, maximum=100)

        self.assertEqual(opener.calls, [])

    def test_configurable_limits_are_hard_capped(self) -> None:
        collector = briefing_collect.BriefingCollector(
            opener=FakeOpener({}),
            hn_scan_limit=10_000,
            hn_item_limit=10_000,
            rss_scan_limit=10_000,
            rss_item_limit=10_000,
        )

        self.assertEqual(collector.hn_scan_limit, briefing_collect.MAX_HN_SCAN_LIMIT)
        self.assertEqual(collector.hn_item_limit, briefing_collect.MAX_HN_ITEM_LIMIT)
        self.assertEqual(collector.rss_scan_limit, briefing_collect.MAX_RSS_SCAN_LIMIT)
        self.assertEqual(collector.rss_item_limit, briefing_collect.MAX_RSS_ITEM_LIMIT)


if __name__ == "__main__":
    unittest.main()
