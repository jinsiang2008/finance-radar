from __future__ import annotations

from datetime import timedelta
from email.utils import format_datetime
import fcntl
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kol_dashboard import news_collect, news_service
from kol_dashboard.tests.test_news_service import NOW, feed


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = self.tmp.name + "/news.sqlite3"

    @staticmethod
    def successful(source, **_kwargs):
        host = "bloomberg.com" if source.publisher == "Bloomberg" else "wsj.com"
        return {"status": 200, "body": feed({"url": f"https://{host}/article"}), "etag": '"version1"'}

    def test_all_sources_succeed_without_a_model_and_wsj_deduplicates(self):
        result = news_collect.run_once(db_path=self.path, now=NOW, fetcher=self.successful)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["successful_sources"], 3)
        self.assertEqual(result["inserted"], 2)
        self.assertEqual(news_service.get_news_snapshot(db_path=self.path, now=NOW)["total"], 2)

    def test_one_failure_does_not_cancel_other_feeds_or_erase_old_content(self):
        news_collect.run_once(db_path=self.path, now=NOW, fetcher=self.successful)
        def partial(source, **kwargs):
            if source.publisher == "Bloomberg":
                raise RuntimeError("Do not expose /secret/path or token=private")
            return self.successful(source, **kwargs)
        result = news_collect.run_once(db_path=self.path, now=NOW + timedelta(minutes=5), fetcher=partial)
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["successful_sources"], 2)
        self.assertEqual(result["sources"][0]["error"], "collection_error")
        snapshot = news_service.get_news_snapshot(db_path=self.path, now=NOW + timedelta(minutes=5))
        self.assertEqual(snapshot["total"], 2)
        self.assertEqual(snapshot["sources"][0]["status"], "error")
        self.assertNotIn("secret", str(snapshot))

    def test_cached_validators_reach_fetcher_and_304_succeeds(self):
        news_collect.run_once(db_path=self.path, now=NOW, fetcher=self.successful)
        seen = []
        def unchanged(source, **kwargs):
            seen.append(kwargs)
            return {"status": 304}
        result = news_collect.run_once(db_path=self.path, now=NOW + timedelta(minutes=5), fetcher=unchanged)
        self.assertEqual(result["successful_sources"], 3)
        self.assertEqual(result["inserted"], 0)
        self.assertTrue(all(item["etag"] == '"version1"' for item in seen))

    def test_live_collection_uses_fetch_completion_time_and_explicit_now_is_fixed(self):
        actual_utc = news_service.utc_datetime
        clock = [NOW]
        completed = NOW + timedelta(seconds=10)
        published = NOW + timedelta(seconds=5)

        def tick(value=None):
            return actual_utc(value) if value is not None else clock[0]

        def fetch(source, **kwargs):
            clock[0] = completed
            host = "bloomberg.com" if source.publisher == "Bloomberg" else "wsj.com"
            return {"status": 200, "body": feed({"url": f"https://{host}/arrival", "date": format_datetime(published)})}

        # Patch the exact module used by the collector, also under flat imports.
        with patch.object(news_collect.news_service, "utc_datetime", side_effect=tick):
            news_collect.run_once(db_path=self.path, fetcher=fetch)
        payload = news_service.get_news_snapshot(db_path=self.path, now=completed)
        self.assertTrue(all(item["published_at_status"] == "provided" for item in payload["items"]))
        self.assertTrue(all(item["first_seen_at"] == news_service.timestamp(completed) for item in payload["items"]))
        explicit_path = self.tmp.name + "/explicit.sqlite3"
        with patch.object(news_collect.news_service, "utc_datetime", side_effect=tick):
            news_collect.run_once(db_path=explicit_path, now=NOW, fetcher=fetch)
        fixed = news_service.get_news_snapshot(db_path=explicit_path, now=completed)
        self.assertTrue(all(item["published_at_status"] == "future" for item in fixed["items"]))
        self.assertTrue(all(item["first_seen_at"] == news_service.timestamp(NOW) for item in fixed["items"]))

    def test_live_failure_health_is_stamped_after_request_completion(self):
        actual_utc = news_service.utc_datetime
        clock = [NOW]
        completed = NOW + timedelta(seconds=10)
        def tick(value=None):
            return actual_utc(value) if value is not None else clock[0]
        def fail(*args, **kwargs):
            clock[0] = completed
            raise RuntimeError("network failed")
        with patch.object(news_collect.news_service, "utc_datetime", side_effect=tick):
            news_collect.run_once(db_path=self.path, fetcher=fail)
        payload = news_service.get_news_snapshot(db_path=self.path, now=completed)
        self.assertTrue(all(source["last_checked_at"] == news_service.timestamp(completed) for source in payload["sources"]))

    def test_bad_xml_for_all_sources_is_a_failed_run(self):
        result = news_collect.run_once(db_path=self.path, now=NOW, fetcher=lambda *args, **kwargs: {"status": 200, "body": b"<html/>"})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["successful_sources"], 0)
        self.assertTrue(all(item["error"] == "not_a_feed" for item in result["sources"]))

    def test_process_lock_prevents_duplicate_work_and_is_released(self):
        lock_path = Path(self.path + ".news-collect.lock")
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(news_collect, "collect_news") as collect:
                self.assertEqual(news_collect.run_once(db_path=self.path)["status"], "already_running")
                collect.assert_not_called()
        self.assertEqual(news_collect.run_once(db_path=self.path, now=NOW, fetcher=self.successful)["status"], "ok")

    def test_cli_exit_status_distinguishes_total_failure(self):
        for status, expected in (("ok", 0), ("degraded", 0), ("already_running", 0), ("failed", 1)):
            with patch.object(news_collect, "run_once", return_value={"status": status}), patch("builtins.print"):
                self.assertEqual(news_collect.main(["--db", self.path]), expected)


if __name__ == "__main__":
    unittest.main()
