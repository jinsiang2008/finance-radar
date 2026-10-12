from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import llm_enrichment
import news_enrichment as subject


class NewsEnrichmentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "news.db"
        self.now = datetime(2026, 10, 13, 12, 30, tzinfo=timezone.utc)
        self.config = llm_enrichment.DeepSeekConfig(api_key="test-placeholder")
        self.items = [self.item(str(i)) for i in range(6)]
        self.snapshot = patch.object(subject.news_service, "get_news_snapshot", return_value={"items": self.items})
        self.snapshot.start()
        self.clock = patch.object(subject, "_now", return_value=self.now)
        self.clock.start()
        self.provider = Mock(side_effect=self.respond)

    def tearDown(self):
        self.clock.stop()
        self.snapshot.stop()
        self.directory.cleanup()

    def item(self, ident):
        return {"id": ident, "title": "Copper rises", "snippet": "Supply remains uncertain.",
                "url": "https://www.bloomberg.com/news/" + ident, "publisher": "Bloomberg",
                "published_at": (self.now - timedelta(minutes=5)).isoformat(),
                "published_at_status": "provided", "commodity_tags": ["copper"]}

    @staticmethod
    def brief(ident):
        return {"headline_zh": "铜价上涨", "summary_zh": "来源简介称铜价上涨，供应情况仍不确定。",
                "why_it_matters_zh": "需核对生产商成本和产量，才能判断股票影响。",
                "watchpoints": ["核验供应变化是否持续"], "evidence_ids": [ident]}

    def respond(self, **kwargs):
        evidence = json.loads(kwargs["user_prompt"])
        result = kwargs["validator"](self.brief(str(evidence["id"])))
        return llm_enrichment.EnrichmentResponse(result=result, usage=llm_enrichment.ProviderUsage(prompt_tokens=200, completion_tokens=100))

    def run_worker(self, **kwargs):
        return subject.run(db_path=self.path, now=self.now, config=self.config, provider=self.provider, **kwargs)

    def test_batch_cap_and_repeated_timer_do_not_repeat_paid_calls(self):
        result = self.run_worker(limit=100)
        self.assertEqual(result["ready"], 4)
        self.assertEqual(self.run_worker()["status"], "not_due")
        self.assertEqual(self.provider.call_count, 4)
        later = subject.run(db_path=self.path, now=self.now + timedelta(minutes=15), config=self.config, provider=self.provider)
        self.assertEqual(later["ready"], 2)
        self.assertEqual(self.provider.call_count, 6)

    def test_cached_projection_is_read_only_and_evidence_change_invalidates_it(self):
        self.run_worker(limit=1)
        first = subject.decorate_snapshot({"items": self.items}, db_path=self.path, now=self.now)
        self.assertEqual(first["items"][0]["analysis"]["status"], "ready")
        self.assertEqual(first["items"][0]["analysis"]["evidence_ids"], ["0"])
        self.assertNotIn("test-placeholder", json.dumps(first))
        self.items[0]["snippet"] = "Corrected source evidence"
        updated = subject.decorate_snapshot({"items": self.items}, db_path=self.path, now=self.now)
        self.assertEqual(updated["items"][0]["analysis"]["status"], "pending")
        self.assertNotIn("summary_zh", updated["items"][0]["analysis"])
        self.assertEqual(self.provider.call_count, 1)

    def test_old_future_missing_dates_never_create_paid_backfill(self):
        self.items[0]["published_at"] = (self.now - timedelta(days=2)).isoformat()
        self.items[1]["published_at"] = (self.now + timedelta(hours=1)).isoformat()
        self.items[2]["published_at_status"] = "missing"
        result = self.run_worker()
        self.assertEqual(result["attempted"], 3)

    def test_provider_failure_stops_batch_and_redacts_details(self):
        self.provider.side_effect = llm_enrichment.EnrichmentError("authentication", retry_after_seconds=3600)
        result = self.run_worker()
        self.assertEqual(result["status"], "provider_backoff")
        self.assertEqual(result["attempted"], 1)
        result = subject.run(db_path=self.path, now=self.now + timedelta(minutes=15), config=self.config, provider=self.provider)
        self.assertEqual(result["status"], "not_due")
        self.assertEqual(self.provider.call_count, 1)
        with subject._connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT error_code FROM news_analysis").fetchone()[0], "authentication")

    def test_invalid_model_citation_is_not_published(self):
        self.provider.side_effect = lambda **kwargs: llm_enrichment.EnrichmentResponse(result=self.brief("invented"), usage=llm_enrichment.ProviderUsage())
        result = self.run_worker(limit=1)
        self.assertEqual(result["failed"], 1)
        public = subject.decorate_snapshot({"items": self.items}, db_path=self.path, now=self.now)
        self.assertEqual(public["items"][0]["analysis"], {"status": "error"})

    def test_corrupt_cached_model_output_is_not_rendered(self):
        self.run_worker(limit=1)
        with subject._connect(self.path) as connection:
            connection.execute("UPDATE news_analysis SET result_json='not-json'")
        self.assertEqual(subject.decorate_snapshot({"items": self.items}, db_path=self.path, now=self.now)["items"][0]["analysis"], {"status": "error"})

    def test_exclusive_lock_prevents_duplicate_calls(self):
        with subject._worker_lock(self.path) as acquired:
            self.assertTrue(acquired)
            self.assertEqual(self.run_worker()["status"], "already_running")
        self.provider.assert_not_called()

    def test_no_config_discloses_unavailability_without_model_calls(self):
        with patch.object(subject.llm_enrichment, "load_config", return_value=None):
            result = subject.run(db_path=self.path, now=self.now, provider=self.provider)
        self.assertEqual(result["status"], "unconfigured")
        projection = subject.decorate_snapshot({"items": self.items}, db_path=self.path, now=self.now)
        self.assertFalse(projection["analysis_schedule"]["configured"])
        self.assertEqual(projection["items"][0]["analysis"]["status"], "unavailable")
        self.provider.assert_not_called()

    def test_peak_offpeak_and_noon_transition(self):
        peak = datetime(2026, 10, 13, 3, 55, tzinfo=timezone.utc)
        state = {"configured": 1, "last_run_at": (peak - timedelta(minutes=25)).isoformat()}
        self.assertEqual(subject._schedule(state, peak)["interval_minutes"], 60)
        self.assertEqual(subject._schedule(state, peak)["next_check_at"], "2026-10-13T04:00:00+00:00")
        self.assertTrue(subject._schedule(state, peak + timedelta(minutes=5))["due"])
        holiday = datetime(2026, 10, 1, 3, 0, tzinfo=timezone.utc)
        self.assertEqual(subject._schedule(None, holiday)["interval_minutes"], 15)

    def test_max_attempts_stop_invalid_output_loop(self):
        self.provider.side_effect = ValueError("raw secret must not be stored")
        for count in range(4):
            later = self.now + timedelta(minutes=15 * count)
            subject.run(db_path=self.path, now=later, config=self.config, provider=self.provider, limit=1)
        with subject._connect(self.path) as connection:
            row = connection.execute("SELECT * FROM news_analysis WHERE item_id='0'").fetchone()
            self.assertEqual(row["attempts"], 3)
            self.assertNotIn("secret", str(dict(row)))

    def test_validator_blocks_html_and_wrong_evidence(self):
        value = self.brief("0")
        value["summary_zh"] = "<script>不可信代码</script>"
        with self.assertRaises(ValueError):
            subject.validate_brief(value, "0")

    def test_last_attempt_crash_recovers_to_error_not_eternal_pending(self):
        self.run_worker(limit=1)
        with subject._connect(self.path) as connection:
            connection.execute("UPDATE news_analysis SET status='running',attempts=3,result_json=NULL")
        subject.run(db_path=self.path, now=self.now + timedelta(minutes=15), config=self.config, provider=self.provider, limit=1)
        public = subject.decorate_snapshot({"items": self.items}, db_path=self.path, now=self.now)
        self.assertEqual(public["items"][0]["analysis"]["status"], "error")


if __name__ == "__main__":
    unittest.main()
