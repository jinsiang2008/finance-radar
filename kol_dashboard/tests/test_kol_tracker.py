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

    def test_x_uses_only_reliably_parseable_absolute_dates(self) -> None:
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
                    "date": "2h",
                    "text": "A sufficiently long post with only a relative date.",
                },
            ],
        )

        with mock.patch.dict(sys.modules, {"serenity_tracker": fake_tracker}):
            items = kol_tracker.search_x("aleabitoreddit")

        self.assertEqual(items[0]["published_at"], "2026-07-31T08:15:30+00:00")
        self.assertIsNone(items[1]["published_at"])

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
