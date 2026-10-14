"""No-network proofs that only explicitly selected articles bypass peak hours."""
from datetime import datetime
import json
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from kol_dashboard import llm_enrichment, reading_service, reading_worker
from kol_dashboard.tests.test_reading_service import podcast
from research.podcast_poc import reading_jobs, schedule_policy


class OnDemandTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.clock = [datetime.fromisoformat("2026-10-14T10:00:00+08:00")]
        self.config = llm_enrichment.DeepSeekConfig(api_key="test-never-sent")
        self.worker = reading_worker.Worker(self.directory.name, config=self.config, clock=lambda: self.clock[0])
        self.runtime = self.worker.runtime
        self.runtime.publish_configuration(model=self.config.model, enabled=True)
        self.calls = []
        self.after_call = None
        self.transport = patch.object(llm_enrichment, "complete_json_with_usage", side_effect=self.response)
        self.transport.start()
        self.addCleanup(self.transport.stop)

    def response(self, **kwargs):
        prompt = kwargs["user_prompt"]
        self.calls.append(prompt)
        if self.after_call:
            self.after_call()
        decoded = json.loads(prompt)[0]["content"]
        refs = list(dict.fromkeys(re.findall(r'"(s[0-9]+)"', decoded))) or ["s0001"]
        return llm_enrichment.EnrichmentResponse(result={
            "schema_version": "detailed-v1", "scope": "仅有提供的短片段，缺乏进一步影响依据。", "impacts": [],
            "overview": "访谈中的观点。", "points": [{"text": "原文支持这一观点，但尚不能推导一般结论。", "segment_ids": refs}]},
            usage=llm_enrichment.ProviderUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15))

    def save(self, identity="podcast-demand"):
        value = podcast(identity)
        self.runtime.poc.save_episode(value)
        return value

    def job(self, identity):
        return next(job for job in self.runtime.engine.snapshot() if job["id"] == identity)

    def test_peak_explicit_summary_executes_all_stages_without_wait(self):
        value = self.save()
        job = self.runtime.enqueue_summary(value["id"])
        self.assertTrue(job["on_demand"])
        self.assertIsNone(job["not_before"])
        self.assertIsNone(job["deferred_reason"])
        self.assertTrue(self.runtime.engine.run_one())
        self.assertEqual(self.job(job["id"])["state"], "completed")
        self.assertEqual(len(self.calls), 2)
        self.assertFalse(schedule_policy.summary_is_on_demand())
        self.assertFalse(schedule_policy.summary_context_active())

    def test_peak_auto_and_legacy_manual_and_manual_refresh_never_start(self):
        auto = self.save("podcast-auto")
        legacy = self.save("podcast-legacy")
        jobs = [self.runtime.engine.enqueue(auto["id"], manual=False),
                self.runtime.engine.enqueue(legacy["id"], manual=True),
                self.runtime.enqueue_check(manual=True)]
        self.assertFalse(self.runtime.engine.run_one())
        self.assertEqual(self.calls, [])
        for original in jobs:
            job = self.job(original["id"])
            self.assertFalse(job["on_demand"])
            self.assertEqual(job["attempts"], 0)
            self.assertEqual(job["deferred_reason"], "off_peak_only")

    def test_click_promotes_existing_waiting_auto_job_without_downgrade(self):
        value = self.save()
        old = self.runtime.engine.enqueue(value["id"], manual=False)
        promoted = self.runtime.enqueue_summary(value["id"])
        again = self.runtime.engine.enqueue(value["id"], manual=False)
        self.assertEqual({old["id"], promoted["id"], again["id"]}, {old["id"]})
        self.assertTrue(again["on_demand"])
        self.assertTrue(again["manual"])
        self.assertIsNone(again["not_before"])
        self.assertTrue(self.runtime.engine.run_one(allow_automatic=False))
        self.assertEqual(len(self.calls), 2)

    def test_auto_cross_peak_defers_but_user_click_resumes_cached_stage(self):
        self.clock[0] = datetime.fromisoformat("2026-10-14T08:59:59+08:00")
        value = self.save()
        old = self.runtime.engine.enqueue(value["id"], manual=False)
        self.after_call = lambda: self.clock.__setitem__(0, datetime.fromisoformat("2026-10-14T09:00:00+08:00"))
        self.assertFalse(self.runtime.engine.run_one())
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.job(old["id"])["deferred_reason"], "off_peak_only")
        selected = self.runtime.enqueue_summary(value["id"])
        self.assertEqual(selected["id"], old["id"])
        self.assertTrue(self.runtime.engine.run_one())
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.runtime.poc.load_episode(value["id"])["processing"]["paid_api_calls"], 2)

    def test_user_promotes_running_auto_job_before_next_model_request(self):
        self.clock[0] = datetime.fromisoformat("2026-10-14T08:59:59+08:00")
        value = self.save()
        original = self.runtime.engine.enqueue(value["id"], manual=False)
        clicked = []
        def click():
            self.clock[0] = datetime.fromisoformat("2026-10-14T09:00:00+08:00")
            other_handler = reading_service.ReadingRuntime(self.directory.name, clock=lambda: self.clock[0])
            clicked.append(other_handler.enqueue_summary(value["id"]))
        self.after_call = click
        self.assertTrue(self.runtime.engine.run_one())
        self.assertEqual(len(self.calls), 2)
        self.assertEqual({item["id"] for item in clicked}, {original["id"]})
        self.assertTrue(all(item["on_demand"] for item in clicked))
        self.assertEqual(self.job(original["id"])["state"], "completed")

    def test_explicit_cross_peak_continues_but_next_auto_job_does_not_inherit(self):
        self.clock[0] = datetime.fromisoformat("2026-10-14T08:59:59+08:00")
        value, other = self.save(), self.save("podcast-other")
        self.runtime.enqueue_summary(value["id"])
        auto = self.runtime.engine.enqueue(other["id"], manual=False)
        self.after_call = lambda: self.clock.__setitem__(0, datetime.fromisoformat("2026-10-14T09:00:00+08:00"))
        self.assertTrue(self.runtime.engine.run_one())
        self.assertEqual(len(self.calls), 2)
        self.assertFalse(self.runtime.engine.run_one())
        self.assertEqual(self.job(auto["id"])["attempts"], 0)
        self.assertEqual(len(self.calls), 2)
        with self.assertRaises(schedule_policy.OffPeakDeferred):
            reading_worker.SharedDeepSeekClient(self.config, clock=lambda: self.clock[0]).generate(
                [{"role": "system", "content": "JSON"}, {"role": "user", "content": "text"}], 100)

    def test_recovery_keeps_explicit_permission_and_old_manual_does_not_gain_it(self):
        value, old = self.save(), self.save("podcast-old-manual")
        selected = self.runtime.enqueue_summary(value["id"])
        legacy = self.runtime.engine.enqueue(old["id"], manual=True)
        with self.runtime.engine._transaction() as connection:
            connection.execute("UPDATE jobs SET state='running' WHERE id=?", (selected["id"],))
        engine = reading_jobs.Engine(self.runtime.poc)
        self.assertTrue(engine.run_one())
        self.assertTrue(self.job(selected["id"])["on_demand"])
        self.assertEqual(self.job(selected["id"])["state"], "completed")
        self.assertFalse(engine.run_one())
        self.assertFalse(self.job(legacy["id"])["on_demand"])
        self.assertEqual(self.job(legacy["id"])["attempts"], 0)

    def test_defer_race_respects_permission_added_after_peak_check(self):
        value = self.save()
        job = self.runtime.engine.enqueue(value["id"], manual=False)
        with self.runtime.engine._transaction() as connection:
            connection.execute("UPDATE jobs SET state='running' WHERE id=?", (job["id"],))
        self.runtime.enqueue_summary(value["id"])
        self.runtime.engine._defer(job, schedule_policy.OffPeakDeferred(self.clock[0]))
        pending = self.job(job["id"])
        self.assertTrue(pending["on_demand"])
        self.assertIsNone(pending["not_before"])
        self.assertIsNone(pending["deferred_reason"])
        self.assertTrue(self.runtime.engine.run_one())

    def test_context_permission_is_bound_to_job_version_and_running_state(self):
        value = self.save()
        job = self.runtime.enqueue_summary(value["id"])
        self.assertFalse(self.runtime.engine._on_demand(job, running_only=True))
        with self.runtime.engine._transaction() as connection:
            connection.execute("UPDATE jobs SET state='running' WHERE id=?", (job["id"],))
        self.assertTrue(self.runtime.engine._on_demand(job, running_only=True))
        self.assertFalse(self.runtime.engine._on_demand({**job, "input_version": "other"}, running_only=True))
        self.assertFalse(self.runtime.engine._on_demand({**job, "article_id": "other"}, running_only=True))
        self.assertFalse(self.runtime.engine._on_demand({**job, "type": "refresh"}, running_only=True))


    def test_old_database_migration_never_promotes_ambiguous_manual_records(self):
        value = self.save()
        legacy = self.runtime.engine.enqueue(value["id"], manual=True)
        with self.runtime.engine._connection() as connection:
            connection.execute("ALTER TABLE jobs DROP COLUMN on_demand")
        migrated = reading_jobs.Engine(self.runtime.poc)
        self.assertFalse(next(j for j in migrated.snapshot() if j["id"] == legacy["id"])["on_demand"])
        self.assertFalse(migrated.run_one())
        self.assertEqual(self.calls, [])

    def test_failed_selected_job_keeps_permission_but_never_autoretries(self):
        value = self.save()
        selected = self.runtime.enqueue_summary(value["id"])
        with patch.object(llm_enrichment, "complete_json_with_usage", side_effect=RuntimeError("provider stopped")):
            self.assertTrue(self.runtime.engine.run_one())
        failed = self.job(selected["id"])
        self.assertEqual(failed["state"], "failed")
        self.assertTrue(failed["on_demand"])
        self.assertEqual(self.runtime.engine.enqueue(value["id"], manual=False)["state"], "failed")
        self.assertFalse(self.runtime.engine.run_one())
        retried = self.runtime.enqueue_summary(value["id"])
        self.assertEqual(retried["id"], selected["id"])
        self.assertIsNone(retried["not_before"])
        self.assertTrue(self.runtime.engine.run_one())
        self.assertEqual(self.job(selected["id"])["state"], "completed")

    def test_peak_explicit_upgrade_preserves_history_without_wait(self):
        value = self.save()
        value.update(status="summarized", summary={"overview": "旧摘要", "points": [{"text": "旧观点。", "segment_ids": ["s0001"]}]},
                     quality_review={"status": "assistant_reviewed"}, summary_signature="old-signature")
        self.runtime.poc.save_episode(value)
        selected = self.runtime.enqueue_summary(value["id"], upgrade=True)
        self.assertTrue(selected["upgrade"])
        self.assertTrue(selected["on_demand"])
        self.assertTrue(self.runtime.engine.run_one())
        result = self.runtime.poc.load_episode(value["id"])
        self.assertEqual(result["summary"]["schema_version"], "detailed-v1")
        self.assertEqual(result["summary_history"][0]["quality_review"], value["quality_review"])
        self.assertNotIn("quality_review", result)
