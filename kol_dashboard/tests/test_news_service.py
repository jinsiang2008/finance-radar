from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.message import Message
from email.utils import format_datetime
from html import escape
import io
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import Request

from kol_dashboard import news_service as news


NOW = datetime(2026, 10, 13, 1, tzinfo=timezone.utc)


def feed(*items):
    nodes = []
    for index, item in enumerate(items):
        title = escape(item.get("title", "Copper supply declines"))
        url = escape(item.get("url", f"https://www.wsj.com/markets/article-{index}"))
        snippet = escape(item.get("snippet", "Gold and copper market news."))
        date = item.get("date", format_datetime(NOW - timedelta(minutes=8)))
        pub = "" if date is None else "<pubDate>" + escape(date) + "</pubDate>"
        nodes.append(f"<item><title>{title}</title><link>{url}</link><description>{snippet}</description>{pub}</item>")
    return ("<rss version='2.0'><channel><title>Markets</title>" + "".join(nodes) + "</channel></rss>").encode()


class FeedParsingTests(unittest.TestCase):
    def test_plaintext_and_commodity_labels_are_from_feed_excerpts(self):
        body = feed({"title": "<b>Copper & gold</b><script>steal()</script>",
                     "snippet": "<p>白银、铝、原油与 LNG</p><style>hidden</style>"})
        row = news.parse_feed(body, news.SOURCES[1], now=NOW)[0]
        self.assertEqual(row["title"], "Copper & gold")
        self.assertNotIn("hidden", row["snippet"])
        self.assertEqual(row["commodity_tags"], ["copper", "gold", "silver", "aluminum", "oil", "gas"])
        self.assertEqual(row["evidence_scope"], "title_and_snippet")

    def test_noncommodity_business_news_is_not_filtered_by_person_name(self):
        row = news.parse_feed(feed({"title": "Technology firms expand", "snippet": "Cloud demand increases"}), news.SOURCES[1], now=NOW)[0]
        self.assertEqual(row["commodity_tags"], [])

    def test_missing_description_is_marked_title_only(self):
        row = news.parse_feed(feed({"snippet": ""}), news.SOURCES[1], now=NOW)[0]
        self.assertEqual(row["evidence_scope"], "title_only")

    def test_url_normalization_deduplicates_tracking_and_www(self):
        source = news.SOURCES[1]
        one = "https://www.wsj.com/markets/a?mod=markets&x=1&utm_source=rss#section"
        two = "https://wsj.com/markets/a?x=1&mod=business"
        self.assertEqual(news.canonical_article_url(one, source), "https://wsj.com/markets/a?x=1")
        rows = news.parse_feed(feed({"url": one}, {"url": two}), source, now=NOW)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]["id"]), 32)

    def test_canonical_urls_keep_parameters_that_may_identify_distinct_facts(self):
        source = news.SOURCES[1]
        self.assertNotEqual(news.canonical_article_url("https://wsj.com/live?a=one", source),
                            news.canonical_article_url("https://wsj.com/live?a=two", source))

    def test_unsafe_urls_are_rejected(self):
        source = news.SOURCES[1]
        for url in ("javascript:alert(1)", "http://wsj.com/a", "https://127.0.0.1/a",
                    "https://wsj.com.evil.test/a", "https://wsj.com@evil.test/a", "https://u:pass@wsj.com/a",
                    "https://wsj.com:8888/a", "https://wsj.com\\@localhost/a", "https://wsj.com/a%0d%0aX:a",
                    "https://bloomberg.com/a", "https://wsj.com/a\n", "//wsj.com/a"):
            with self.subTest(url=url):
                # Surrounding whitespace is trimmed, but no embedded newline can
                # reach a request. The canonical result has no such characters.
                result = news.canonical_article_url(url, source)
                if url.endswith("\n"):
                    self.assertEqual(result, "https://wsj.com/a")
                else:
                    self.assertIsNone(result)

    def test_bad_xml_html_entity_declarations_and_oversize_fail(self):
        for body in (b"<rss>", b"<html><body>challenge</body></html>", b"<rss/>",
                     b'<!DOCTYPE rss [<!ENTITY x "secret">]><rss><channel/></rss>',
                     b"x" * (news.MAX_FEED_BYTES + 1), b"\xff\xfe<x>"):
            with self.subTest(body=body[:50]):
                with self.assertRaises(news.NewsFeedError):
                    news.parse_feed(body, news.SOURCES[1], now=NOW)

    def test_all_unusable_entries_fail_rather_than_mark_source_healthy(self):
        with self.assertRaisesRegex(news.NewsFeedError, "no_usable_items"):
            news.parse_feed(feed({"url": "https://evil.test/a"}), news.SOURCES[1], now=NOW)

    def test_missing_invalid_naive_and_future_dates_are_honest(self):
        dates = [(None, "missing"), ("nonsense", "invalid"), ("2026-10-13T00:00:00", "invalid"),
                 (format_datetime(NOW + timedelta(minutes=5)), "future")]
        for date, status in dates:
            with self.subTest(status=status, date=date):
                item = news.parse_feed(feed({"date": date}), news.SOURCES[1], now=NOW)[0]
                self.assertIsNone(item["published_at"])
                self.assertEqual(item["published_at_status"], status)
                self.assertEqual(item["reported_published_at"] is not None, status == "future")

    def test_atom_and_xml_namespaces_are_supported(self):
        body = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Oil</title>
          <link href="https://wsj.com/a"/><summary>Natural gas</summary>
          <published>2026-10-13T00:59:00Z</published></entry></feed>'''
        row = news.parse_feed(body, news.SOURCES[1], now=NOW)[0]
        self.assertEqual(row["commodity_tags"], ["oil", "gas"])
        self.assertEqual(row["published_at"], "2026-10-13T00:59:00+00:00")


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = self.tmp.name + "/news.sqlite3"

    def store(self, body=None, source=None, now=NOW, **response):
        news.init_news_db(self.path)
        return news.store_feed(source or news.SOURCES[1], {"status": 200, "body": body or feed({}), **response}, db_path=self.path, now=now)

    def snapshot(self, now=NOW, **kwargs):
        return news.get_news_snapshot(db_path=self.path, now=now, **kwargs)

    def test_snapshot_before_collector_is_read_only_and_explicitly_empty(self):
        payload = self.snapshot()
        self.assertFalse(news.database_path(self.path).exists())
        self.assertEqual(payload["items"], [])
        self.assertTrue(all(source["status"] == "not_checked" and source["stale"] for source in payload["sources"]))
        with sqlite3.connect(self.path) as con:
            con.execute("PRAGMA user_version=77")
            con.execute("CREATE TABLE existing (value TEXT)")
        self.assertEqual(self.snapshot()["items"], [])
        with sqlite3.connect(self.path) as con:
            self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], 77)
            self.assertEqual(con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [("existing",)])

    def test_schema_does_not_change_existing_database_version_or_tables(self):
        with sqlite3.connect(self.path) as con:
            con.execute("PRAGMA user_version=77")
            con.execute("CREATE TABLE events (id INTEGER)")
            con.execute("INSERT INTO events VALUES (19)")
        self.store()
        with sqlite3.connect(self.path) as con:
            self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], 77)
            self.assertEqual(con.execute("SELECT id FROM events").fetchone()[0], 19)

    def test_cross_feed_duplicate_preserves_first_seen_and_records_two_sources(self):
        self.store(feed({"url": "https://www.wsj.com/article?mod=markets"}))
        later = NOW + timedelta(minutes=5)
        result = self.store(feed({"url": "https://wsj.com/article?mod=business"}), source=news.SOURCES[2], now=later)
        self.assertEqual(result["inserted"], 0)
        snapshot = self.snapshot(now=later)
        self.assertEqual(snapshot["total"], 1)
        item = snapshot["items"][0]
        self.assertEqual(item["source_ids"], ["wsj_business", "wsj_markets"])
        self.assertEqual(item["first_seen_at"], news.timestamp(NOW))
        self.assertEqual(item["last_seen_at"], news.timestamp(later))
        self.assertEqual(item["discovery_delay_seconds"], 480)
        self.assertTrue(item["initial_import"])

    def test_distinct_article_urls_with_same_title_are_not_collapsed(self):
        self.store(feed({"url": "https://wsj.com/one"}, {"url": "https://wsj.com/two"}))
        self.assertEqual(self.snapshot()["total"], 2)

    def test_another_feed_without_date_does_not_erase_known_publication(self):
        for date in (None, "invalid", format_datetime(NOW + timedelta(days=1))):
            with self.subTest(date=date):
                self.store()
                self.store(feed({"date": date}), source=news.SOURCES[2], now=NOW + timedelta(minutes=5))
                item = self.snapshot()["items"][0]
                self.assertEqual(item["published_at_status"], "provided")
                self.assertEqual(item["published_at"], news.timestamp(NOW - timedelta(minutes=8)))

    def test_source_can_correct_a_previously_provided_publication_time(self):
        self.store()
        corrected = NOW - timedelta(minutes=25)
        self.store(feed({"date": format_datetime(corrected)}), now=NOW + timedelta(minutes=5))
        self.assertEqual(self.snapshot()["items"][0]["published_at"], news.timestamp(corrected))

    def test_headline_only_sibling_feed_preserves_excerpt_tags_and_scope(self):
        original = {"title": "Supply outlook", "snippet": "Copper and gold supply may decline"}
        self.store(feed(original))
        before = self.snapshot()["items"][0]
        # Repeating the sibling feed must not turn its earlier no-excerpt sighting
        # into a claim that it previously supplied the retained excerpt.
        for offset in (1, 2):
            self.store(feed({"title": original["title"], "snippet": ""}),
                       source=news.SOURCES[2], now=NOW + timedelta(minutes=offset))
            item = self.snapshot()["items"][0]
            self.assertEqual(item["snippet"], before["snippet"])
            self.assertEqual(item["commodity_tags"], ["copper", "gold"])
            self.assertEqual(item["evidence_scope"], "title_and_snippet")

    def test_same_feed_can_remove_its_previously_supplied_excerpt(self):
        self.store(feed({"title": "Supply outlook", "snippet": "Copper supply"}))
        self.store(feed({"title": "Supply outlook", "snippet": ""}), now=NOW + timedelta(minutes=1))
        item = self.snapshot()["items"][0]
        self.assertEqual(item["snippet"], "")
        self.assertEqual(item["commodity_tags"], [])
        self.assertEqual(item["evidence_scope"], "title_only")

    def test_existing_pre_provenance_database_migrates_idempotently(self):
        with sqlite3.connect(self.path) as con:
            con.execute("CREATE TABLE news_item_sources (item_id TEXT, source_id TEXT, first_seen_at TEXT, last_seen_at TEXT, PRIMARY KEY(item_id,source_id))")
            con.execute("INSERT INTO news_item_sources VALUES ('old','wsj_markets',?,?)", (news.timestamp(NOW), news.timestamp(NOW)))
        news.init_news_db(self.path)
        news.init_news_db(self.path)
        with sqlite3.connect(self.path) as con:
            row = con.execute("SELECT item_id,has_snippet FROM news_item_sources").fetchone()
            self.assertEqual(row, ("old", 0))

    def test_failures_preserve_items_last_success_and_increment_health(self):
        self.store(etag='"old"')
        news.record_source_failure(news.SOURCES[1], "http_403", db_path=self.path, now=NOW + timedelta(minutes=5))
        payload = self.snapshot(now=NOW + timedelta(minutes=5))
        source = payload["sources"][1]
        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(source["status"], "error")
        self.assertEqual(source["last_success_at"], news.timestamp(NOW))
        self.assertEqual(source["consecutive_failures"], 1)
        self.assertNotIn("etag", source)
        self.assertFalse(source["stale"])
        self.assertTrue(self.snapshot(now=NOW + timedelta(minutes=21))["sources"][1]["stale"])

    def test_304_preserves_content_validators_and_resets_failure(self):
        self.store(etag='"old"', last_modified="Mon, 12 Oct 2026 00:00:00 GMT")
        news.record_source_failure(news.SOURCES[1], "http_500", db_path=self.path, now=NOW)
        later = NOW + timedelta(minutes=5)
        news.store_feed(news.SOURCES[1], {"status": 304}, db_path=self.path, now=later)
        state = news.source_state(news.SOURCES[1].id, self.path)
        self.assertEqual(state["etag"], '"old"')
        self.assertIsNone(state["last_error"])
        self.assertEqual(state["last_success_at"], news.timestamp(later))
        self.assertEqual(self.snapshot()["items"][0]["last_seen_at"], news.timestamp(NOW))

    def test_304_without_success_is_not_accepted(self):
        news.init_news_db(self.path)
        with self.assertRaisesRegex(news.NewsFeedError, "unexpected_not_modified"):
            news.store_feed(news.SOURCES[1], {"status": 304}, db_path=self.path, now=NOW)

    def test_304_reclassifies_arrived_future_time_without_changing_first_seen(self):
        published = NOW + timedelta(minutes=2)
        far_future = NOW + timedelta(days=1)
        self.store(feed({"date": format_datetime(published)},
                        {"url": "https://wsj.com/still-future", "date": format_datetime(far_future)}), etag='"same"')
        self.assertTrue(all(item["published_at_status"] == "future" for item in self.snapshot()["items"]))
        later = NOW + timedelta(minutes=5)
        news.store_feed(news.SOURCES[1], {"status": 304}, db_path=self.path, now=later)
        payload = self.snapshot(now=later)
        item = next(item for item in payload["items"] if item["url"].endswith("article-0"))
        remaining = next(item for item in payload["items"] if item["url"].endswith("still-future"))
        self.assertEqual(item["published_at_status"], "provided")
        self.assertEqual(item["published_at"], news.timestamp(published))
        self.assertEqual(item["first_seen_at"], news.timestamp(NOW))
        self.assertEqual(item["last_seen_at"], news.timestamp(NOW))
        self.assertIsNone(item["discovery_delay_seconds"])
        self.assertEqual(remaining["published_at_status"], "future")
        self.assertEqual(payload["sources"][1]["latest_published_at"], news.timestamp(published))

    def test_invalid_feed_does_not_poison_cached_validators(self):
        self.store(etag='"old"')
        with self.assertRaises(news.NewsFeedError):
            self.store(b"<html/>", etag='"bad"')
        self.assertEqual(news.source_state(news.SOURCES[1].id, self.path)["etag"], '"old"')

    def test_initial_window_stays_bounded_on_second_poll(self):
        old = {"url": "https://wsj.com/old", "date": format_datetime(NOW - timedelta(days=3))}
        recent = {"url": "https://wsj.com/new"}
        self.store(feed(old, recent))
        self.store(feed(old, recent), now=NOW + timedelta(minutes=5))
        self.assertEqual(self.snapshot()["total"], 1)
        self.assertEqual(self.snapshot()["items"][0]["url"], "https://wsj.com/new")

    def test_first_poll_cap_unknown_dates_and_missing_delay(self):
        self.store(feed(*({"date": None, "url": f"https://wsj.com/a{i}"} for i in range(90))))
        self.assertEqual(self.snapshot(limit=200)["total"], news.MAX_INITIAL_ITEMS)
        self.assertTrue(all(item["discovery_delay_seconds"] is None for item in self.snapshot()["items"]))

    def test_filters_limits_and_seven_day_display_window(self):
        self.store(feed({"title": "Copper rises", "snippet": "Copper"},
                        {"title": "Equities", "snippet": "Stocks", "url": "https://wsj.com/equities"}))
        self.assertEqual(self.snapshot(commodity="copper", publisher="WSJ")["total"], 1)
        self.assertEqual(self.snapshot(publisher="Bloomberg")["total"], 0)
        self.assertEqual(len(self.snapshot(limit=1)["items"]), 1)
        self.assertEqual(self.snapshot(now=NOW + timedelta(days=8))["total"], 0)
        for filters in ({"limit": 201}, {"limit": 0}, {"limit": True}, {"commodity": "x' OR 1=1"}, {"publisher": "evil"}):
            with self.assertRaises(ValueError):
                self.snapshot(**filters)

    def test_changed_evidence_resets_analysis_marker_but_not_first_seen(self):
        self.store()
        with sqlite3.connect(self.path) as con:
            con.execute("UPDATE news_items SET analysis_status='ready'")
        self.store(now=NOW + timedelta(minutes=1))
        self.assertEqual(self.snapshot()["items"][0]["analysis_status"], "ready")
        self.store(feed({"snippet": "Revised: copper supply is unchanged"}), now=NOW + timedelta(minutes=2))
        item = self.snapshot()["items"][0]
        self.assertEqual(item["analysis_status"], "not_requested")
        self.assertEqual(item["first_seen_at"], news.timestamp(NOW))

    def test_retention_prunes_sightings_without_deleting_source_health(self):
        self.store()
        self.assertEqual(news.prune_news(db_path=self.path, now=NOW + timedelta(days=15)), 1)
        with sqlite3.connect(self.path) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM news_item_sources").fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM news_sources").fetchone()[0], 3)


class FetchTests(unittest.TestCase):
    def test_request_is_conditional_bounded_and_does_not_use_environment_proxy(self):
        response = MagicMock()
        response.status = 200
        response.headers = {"ETag": '"next"'}
        response.read1.side_effect = [feed({}), b""]
        response.__enter__.return_value = response
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(news, "build_opener", return_value=opener) as build:
            result = news.fetch_feed(news.SOURCES[1], etag='"old"', last_modified="Mon, 12 Oct 2026 00:00:00 GMT")
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["etag"], '"next"')
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header("If-none-match"), '"old"')
        self.assertEqual(request.get_header("If-modified-since"), "Mon, 12 Oct 2026 00:00:00 GMT")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 20)
        self.assertEqual(build.call_args.args[0].proxies, {})

    def test_304_handling_and_cache_header_injection(self):
        headers = Message()
        headers["ETag"] = '"next"'
        opener = MagicMock()
        opener.open.side_effect = HTTPError(news.SOURCES[1].feed_url, 304, "not modified", headers, io.BytesIO())
        with patch.object(news, "build_opener", return_value=opener):
            result = news.fetch_feed(news.SOURCES[1], etag="bad\r\nHeader: secret")
        self.assertEqual(result["status"], 304)
        self.assertIsNone(opener.open.call_args.args[0].get_header("If-none-match"))

    def test_redirect_and_unknown_sources_never_reach_arbitrary_hosts(self):
        handler = news._FeedRedirect(news.SOURCES[1].feed_url)
        for url in ("https://127.0.0.1/", "http://wsj.com/", "https://evil.test/", "https://www.wsj.com/"):
            with self.assertRaises(news.NewsFeedError):
                handler.redirect_request(Request(news.SOURCES[1].feed_url), None, 302, "found", {}, url)
        custom = news.NewsSource("custom", "Custom", "WSJ", "https://127.0.0.1/", ("localhost",))
        with self.assertRaises(news.NewsFeedError), patch.object(news, "build_opener") as build:
            news.fetch_feed(custom)
        build.assert_not_called()

    def test_large_body_or_content_length_is_rejected(self):
        for headers, chunks in (({"Content-Length": str(news.MAX_FEED_BYTES + 1)}, []),
                                ({}, [b"x" * (news.MAX_FEED_BYTES + 1)])):
            response = MagicMock()
            response.status, response.headers = 200, headers
            response.read1.side_effect = chunks
            response.__enter__.return_value = response
            opener = MagicMock()
            opener.open.return_value = response
            with patch.object(news, "build_opener", return_value=opener), self.assertRaisesRegex(news.NewsFeedError, "feed_too_large"):
                news.fetch_feed(news.SOURCES[1])


if __name__ == "__main__":
    unittest.main()
