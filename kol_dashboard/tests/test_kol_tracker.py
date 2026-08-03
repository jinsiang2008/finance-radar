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
