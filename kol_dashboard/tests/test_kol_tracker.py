from __future__ import annotations

import sys
import types
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
LIB_DIR = ROOT / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

import kol_tracker  # noqa: E402


TRUTH_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:truth="https://truthsocial.com/ns"><channel>
  <item>
    <title><![CDATA[[No Title] - Post from August 5, 2026]]></title>
    <link>https://trumpstruth.org/statuses/40591</link>
    <description><![CDATA[<p></p>]]></description>
    <pubDate>Wed, 05 Aug 2026 05:15:13 +0000</pubDate>
    <truth:originalUrl>https://truthsocial.com/@realDonaldTrump/117041195</truth:originalUrl>
  </item>
  <item>
    <title><![CDATA[Post from August 4, 2026]]></title>
    <link>https://trumpstruth.org/statuses/40560</link>
    <description><![CDATA[<p>We will impose a 50% TARIFF on all semiconductor
      imports unless companies build in America!</p>]]></description>
    <pubDate>Tue, 04 Aug 2026 22:52:51 +0000</pubDate>
    <truth:originalUrl>https://truthsocial.com/@realDonaldTrump/117039691</truth:originalUrl>
  </item>
</channel></rss>"""


class TruthSocialTests(unittest.TestCase):
    def test_posts_carry_real_timestamps_and_canonical_permalinks(self) -> None:
        with mock.patch.object(kol_tracker, "http_get", return_value=TRUTH_FEED):
            items = kol_tracker.search_truth_social("realDonaldTrump")

        self.assertEqual(len(items), 1)
        post = items[0]
        self.assertEqual(post["published_at"], "2026-08-04T22:52:51+00:00")
        self.assertEqual(
            post["url"], "https://truthsocial.com/@realDonaldTrump/117039691"
        )
        self.assertEqual(post["source"], "Truth Social @realDonaldTrump")
        self.assertIn("50% TARIFF", post["snippet"])
        self.assertNotIn("<p>", post["snippet"])

    def test_links_survive_mastodon_split_span_markup(self) -> None:
        """Mastodon splits long URLs across spans, so tag text is unusable."""
        feed = TRUTH_FEED.replace(
            "<description><![CDATA[<p>We will impose a 50% TARIFF on all semiconductor\n"
            "      imports unless companies build in America!</p>]]></description>",
            "<description><![CDATA[<p>Bessent interviewed by Kernen!</p>"
            '<p><a href="https://www.cnbc.com/video/full-interview">'
            '<span class="invisible">https://</span>'
            '<span class="ellipsis">www.cnbc.com/video</span>'
            '<span class="invisible">/full-interview</span>'
            "</a></p>]]></description>",
        )

        with mock.patch.object(kol_tracker, "http_get", return_value=feed):
            items = kol_tracker.search_truth_social("realDonaldTrump")

        snippet = items[0]["snippet"]
        self.assertIn(
            "Kernen! https://www.cnbc.com/video/full-interview", snippet
        )
        self.assertNotIn("www. cnbc", snippet)

    def test_reposts_without_text_are_skipped(self) -> None:
        with mock.patch.object(kol_tracker, "http_get", return_value=TRUTH_FEED):
            items = kol_tracker.search_truth_social("realDonaldTrump")

        self.assertTrue(
            all("40591" not in item["url"] for item in items),
            "media-only reposts carry no analysable text",
        )

    def test_long_posts_are_truncated_into_a_title(self) -> None:
        with mock.patch.object(kol_tracker, "http_get", return_value=TRUTH_FEED):
            items = kol_tracker.search_truth_social("realDonaldTrump")

        self.assertLessEqual(len(items[0]["title"]), 91)
        self.assertTrue(items[0]["title"])

    def test_unreachable_feed_degrades_without_raising(self) -> None:
        with mock.patch.object(kol_tracker, "http_get", return_value=""):
            self.assertEqual(kol_tracker.search_truth_social("realDonaldTrump"), [])

    def test_trump_scan_includes_truth_social_alongside_news(self) -> None:
        with mock.patch.object(
            kol_tracker, "search_truth_social",
            return_value=[{
                "title": "Tariff post",
                "snippet": "We will impose a 50% TARIFF on semiconductors",
                "url": "https://truthsocial.com/@realDonaldTrump/1",
                "source": "Truth Social @realDonaldTrump",
                "published_at": "2026-08-04T22:52:51+00:00",
            }],
        ), mock.patch.object(kol_tracker, "search_kol", return_value=[]):
            items = kol_tracker.scan_kol("trump", max_results=5)

        self.assertTrue(items)
        self.assertEqual(items[0]["kol_key"], "trump")
        self.assertEqual(items[0]["source"], "Truth Social @realDonaldTrump")
        self.assertIn("impact", items[0])
        self.assertIn("has_market_kw", items[0])

    def test_truth_social_never_crowds_out_news_coverage(self) -> None:
        truth_posts = [
            {
                "title": f"Truth post {index}",
                "snippet": f"A sufficiently long Truth Social post body {index}",
                "url": f"https://truthsocial.com/@realDonaldTrump/{index}",
                "source": "Truth Social @realDonaldTrump",
                "published_at": "2026-08-04T22:52:51+00:00",
            }
            for index in range(10)
        ]
        news = [
            {
                "title": f"News story {index}",
                "snippet": f"A sufficiently long news body {index}",
                "url": f"https://news.example.com/{index}",
                "source": "Bing News",
                "published_at": "2026-08-04T20:00:00+00:00",
            }
            for index in range(10)
        ]

        with mock.patch.object(
            kol_tracker, "search_truth_social",
            side_effect=lambda handle, limit=10, **kw: truth_posts[:limit],
        ), mock.patch.object(
            kol_tracker, "search_kol",
            side_effect=lambda key, term, limit=5: news[:limit],
        ):
            items = kol_tracker.scan_kol("trump", max_results=6)

        sources = [item["source"] for item in items]
        self.assertEqual(len(items), 6)
        self.assertIn("Truth Social @realDonaldTrump", sources)
        self.assertIn("Bing News", sources)
        self.assertEqual(sources.count("Truth Social @realDonaldTrump"), 3)

    def test_truth_social_is_configured_only_for_trump(self) -> None:
        handles = {
            key: kol.get("truth_handle")
            for key, kol in kol_tracker.KOLS.items()
            if kol.get("truth_handle")
        }

        self.assertEqual(handles, {"trump": "realDonaldTrump"})


class PublishedAtParsingTests(unittest.TestCase):
    def test_bing_rss_pubdate_is_normalized_to_utc_iso(self) -> None:
        rss = """<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0"><channel>
          <item>
            <title>NVIDIA announces new platform</title>
            <link>https://example.com/story</link>
            <pubDate>Fri, 31 Jul 2026 18:34:56 +0800</pubDate>
          </item>
        </channel></rss>"""

        with mock.patch.object(kol_tracker, "http_get", return_value=rss):
            items = kol_tracker.search_bing_rss("NVIDIA")

        self.assertEqual(items[0]["published_at"], "2026-07-31T10:34:56+00:00")

    def test_bing_rss_requests_date_sorted_english_market_results(self) -> None:
        """Relevance-ranked defaults returned months-old stories for some KOLs."""
        captured: dict[str, str] = {}

        def fake_get(url: str, **_kwargs: object) -> str:
            captured["url"] = url
            return ""

        with mock.patch.object(kol_tracker, "http_get", side_effect=fake_get):
            kol_tracker.search_bing_rss("Howard Marks")

        url = captured["url"]
        self.assertIn("setmkt=en-US", url)
        self.assertIn("setlang=en-US", url)
        self.assertIn("sortbydate", url)

    def test_bing_rss_falls_back_to_regex_when_xml_is_malformed(self) -> None:
        """A single bad character used to drop an entire KOL's feed."""
        rss = """<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0"><channel>
          <item>
            <title>Jensen Huang on AI demand</title>
            <link>https://example.com/huang</link>
            <pubDate>Mon, 03 Aug 2026 04:40:00 GMT</pubDate>
          </item>
        </channel></rss>""".replace("<channel>", "<channel><bad & tag>")

        with mock.patch.object(kol_tracker, "http_get", return_value=rss):
            items = kol_tracker.search_bing_rss("Jensen Huang")

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Jensen Huang on AI demand")
        self.assertEqual(items[0]["url"], "https://example.com/huang")
        self.assertEqual(items[0]["published_at"], "2026-08-03T04:40:00+00:00")

    def test_baidu_results_always_carry_a_published_at_key(self) -> None:
        """A missing key silently quarantined every Baidu sighting."""
        html = (
            '<h3 class="c-title"><a href="https://news.example.cn/a">'
            "央行发布公告</a></h3>"
            '<span class="c-color-gray2">2026年08月03日 09:15</span>'
        )

        with mock.patch.object(kol_tracker, "http_get", return_value=html):
            items = kol_tracker.search_baidu("央行")

        self.assertTrue(items)
        for item in items:
            self.assertIn("published_at", item)

    def test_bing_rss_does_not_fabricate_invalid_pubdate(self) -> None:
        rss = """<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0"><channel>
          <item>
            <title>Undated story</title>
            <link>https://example.com/undated</link>
            <pubDate>three hours ago</pubDate>
          </item>
        </channel></rss>"""

        with mock.patch.object(kol_tracker, "http_get", return_value=rss):
            items = kol_tracker.search_bing_rss("undated")

        self.assertIsNone(items[0]["published_at"])

    def test_publication_time_more_than_five_minutes_future_is_rejected(
        self,
    ) -> None:
        now = datetime(2026, 8, 3, 0, 0, tzinfo=timezone.utc)

        rejected = kol_tracker.normalize_published_at(
            (now + timedelta(minutes=6)).isoformat(),
            now=now,
        )
        tolerated = kol_tracker.normalize_published_at(
            (now + timedelta(minutes=4)).isoformat(),
            now=now,
        )

        self.assertIsNone(rejected)
        self.assertEqual(
            tolerated,
            "2026-08-03T00:04:00+00:00",
        )

    def test_x_prefers_absolute_dates_and_rejects_timeless_labels(self) -> None:
        fake_tracker = types.SimpleNamespace(
            fetch_page=lambda: "<html></html>",
            parse_tweets=lambda _html: [
                {
                    "tid": "100",
                    "date": "2026-07-31T08:15:30Z",
                    "text": "A sufficiently long post about $NVDA and AI demand.",
                },
                {
                    "tid": "101",
                    "date": "Jul 28",
                    "text": "A sufficiently long post with no time of day at all.",
                },
            ],
        )

        with mock.patch.dict(sys.modules, {"serenity_tracker": fake_tracker}):
            items = kol_tracker.search_x("aleabitoreddit")

        self.assertEqual(items[0]["published_at"], "2026-07-31T08:15:30+00:00")
        self.assertIsNone(items[1]["published_at"])

    def test_relative_offsets_resolve_to_the_older_window_edge(self) -> None:
        """Labels are floor-rounded, so "5h" means at least five hours ago."""
        now = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)

        self.assertEqual(
            kol_tracker.resolve_relative_time("5h", now=now),
            "2026-08-05T06:00:00+00:00",
        )
        self.assertEqual(
            kol_tracker.resolve_relative_time("30m", now=now),
            "2026-08-05T11:29:00+00:00",
        )
        self.assertEqual(
            kol_tracker.resolve_relative_time("45s", now=now),
            "2026-08-05T11:59:14+00:00",
        )

    def test_resolved_time_never_post_dates_an_earlier_observation(self) -> None:
        """A post visible at first fetch cannot have been published later."""
        first_seen = datetime(2026, 8, 5, 0, 24, 10, tzinfo=timezone.utc)
        now = datetime(2026, 8, 5, 6, 57, 51, tzinfo=timezone.utc)

        resolved = kol_tracker.resolve_relative_time("6h", now=now)

        self.assertIsNotNone(resolved)
        self.assertLess(datetime.fromisoformat(resolved), first_seen)

    def test_date_only_labels_stay_unresolved(self) -> None:
        """A bare "Aug 4" carries no time of day, so inventing one would lie."""
        now = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)

        for label in ("Aug 4", "Jul 31", "", "yesterday", None, "99h"):
            self.assertIsNone(
                kol_tracker.resolve_relative_time(label, now=now), label
            )

    def test_x_posts_with_relative_labels_become_time_verified(self) -> None:
        fake_tracker = types.SimpleNamespace(
            fetch_page=lambda: "<html></html>",
            parse_tweets=lambda _html: [
                {
                    "tid": "200",
                    "date": "5h",
                    "text": "A sufficiently long post about $RKLB winning a contract.",
                },
                {
                    "tid": "201",
                    "date": "Aug 4",
                    "text": "A sufficiently long post carrying only a bare date.",
                },
            ],
        )

        with mock.patch.dict(sys.modules, {"serenity_tracker": fake_tracker}):
            items = kol_tracker.search_x("aleabitoreddit")

        self.assertIsNotNone(items[0]["published_at"])
        self.assertIsNone(items[1]["published_at"])

    def test_x_fetch_retries_transient_server_errors(self) -> None:
        attempts: list[int] = []

        def flaky_fetch() -> str:
            attempts.append(1)
            if len(attempts) < 3:
                raise OSError("HTTP Error 500: Internal Server Error")
            return "<html></html>"

        fake_tracker = types.SimpleNamespace(
            fetch_page=flaky_fetch,
            parse_tweets=lambda _html: [
                {"tid": "1", "date": "1h", "text": "A long enough post body here."}
            ],
        )

        with mock.patch.dict(sys.modules, {"serenity_tracker": fake_tracker}):
            with mock.patch.object(kol_tracker.time, "sleep"):
                items = kol_tracker.search_x("aleabitoreddit")

        self.assertEqual(len(attempts), 3)
        self.assertEqual(len(items), 1)

    def test_x_tries_later_date_candidates_after_invalid_first_value(self) -> None:
        fake_tracker = types.SimpleNamespace(
            fetch_page=lambda: "<html></html>",
            parse_tweets=lambda _html: [
                {
                    "tid": "102",
                    "published_at": "2h",
                    "created_at": "2026-07-31T09:15:30Z",
                    "date": "Jul 31",
                    "text": "A sufficiently long post with a valid fallback timestamp.",
                }
            ],
        )

        with mock.patch.dict(sys.modules, {"serenity_tracker": fake_tracker}):
            items = kol_tracker.search_x("aleabitoreddit")

        self.assertEqual(items[0]["published_at"], "2026-07-31T09:15:30+00:00")


class CollectorFailureTests(unittest.TestCase):
    def test_required_database_write_failure_propagates(self) -> None:
        fake_db = types.SimpleNamespace(
            init=mock.Mock(side_effect=RuntimeError("db unavailable")),
            insert_events=mock.Mock(),
        )

        with mock.patch.dict(sys.modules, {"db": fake_db}):
            with mock.patch.dict(
                "os.environ",
                {"KOL_DB_WRITE_REQUIRED": "1"},
                clear=False,
            ):
                with self.assertRaises(RuntimeError):
                    kol_tracker._write_to_db([{"title": "test"}])

    def test_top_level_collector_exception_returns_nonzero(self) -> None:
        def fail(_args):
            raise RuntimeError("collector failed")

        stderr = StringIO()
        with mock.patch.object(kol_tracker, "COMMANDS", {"collect": fail}):
            with mock.patch.object(
                sys,
                "argv",
                ["kol_tracker.py", "collect"],
            ):
                with redirect_stderr(stderr):
                    result = kol_tracker.main()

        self.assertEqual(result, 1)
        self.assertIn("collector failed", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
