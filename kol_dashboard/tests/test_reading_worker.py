import fcntl
from datetime import datetime
import json
import tempfile
import unittest
from unittest.mock import patch

from kol_dashboard import llm_enrichment, reading_service, reading_worker
from kol_dashboard.tests.test_reading_service import podcast


class ReadingWorkerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = llm_enrichment.DeepSeekConfig(api_key="test-key-never-sent")
        self.clock = lambda: datetime.fromisoformat("2026-10-13T20:00:00+08:00")
        self.worker = reading_worker.Worker(self.directory.name, config=self.config, clock=self.clock)
        self.worker.runtime.publish_configuration(model=self.config.model, enabled=True)

    def test_shared_client_uses_existing_transport_and_preserves_usage(self):
        client = reading_worker.SharedDeepSeekClient(self.config, clock=self.clock)
        response = llm_enrichment.EnrichmentResponse(result={"overview": "摘要"}, usage=llm_enrichment.ProviderUsage(prompt_tokens=12, completion_tokens=5, total_tokens=17))
        with patch.object(llm_enrichment, "complete_json_with_usage", return_value=response) as complete:
            value = client.generate([{"role": "system", "content": "Output JSON"}, {"role": "user", "content": "Evidence text"}], 1100)
        self.assertEqual(json.loads(value), response.result)
        self.assertEqual(complete.call_args.kwargs["config"].max_output_tokens, 1100)
        self.assertEqual(complete.call_args.kwargs["user_id"], "kol-reading-summary")
        self.assertEqual(client.stats()["usage"]["total_tokens"], 17)
        self.assertEqual(client.stats()["usage_unreported_calls"], 0)
        self.assertNotIn(self.config.api_key, json.dumps(client.stats()))

    def test_failed_provider_usage_remains_unknown_without_reported_tokens(self):
        client = reading_worker.SharedDeepSeekClient(self.config, clock=self.clock)
        failure = llm_enrichment.EnrichmentError("network", retry_after_seconds=60)
        with patch.object(llm_enrichment, "complete_json_with_usage", side_effect=failure):
            with self.assertRaises(llm_enrichment.EnrichmentError):
                client.generate([{"role": "system", "content": "JSON"}, {"role": "user", "content": "Text"}], 1100)
        self.assertEqual(client.stats()["paid_api_calls"], 1)
        self.assertEqual(client.stats()["usage_unreported_calls"], 1)

    def test_local_prompt_rejection_does_not_count_a_paid_api_attempt(self):
        client = reading_worker.SharedDeepSeekClient(self.config, clock=self.clock)
        with patch.object(llm_enrichment, "_default_transport", side_effect=AssertionError("No network")):
            with self.assertRaises(llm_enrichment.EnrichmentError) as caught:
                client.generate([{"role": "system", "content": "JSON"},
                                 {"role": "user", "content": "x" * 32_000}], 1100)
        self.assertEqual(caught.exception.code, "invalid_request")
        self.assertEqual(client.stats()["paid_api_calls"], 0)
        self.assertEqual(client.stats()["usage_unreported_calls"], 0)

    def test_peak_paid_call_is_blocked_before_transport_or_usage_counter(self):
        client = reading_worker.SharedDeepSeekClient(self.config,
            clock=lambda: datetime.fromisoformat("2026-10-13T09:00:00+08:00"))
        with patch.object(llm_enrichment, "complete_json_with_usage") as complete:
            with self.assertRaises(reading_worker.schedule_policy.OffPeakDeferred):
                client.generate([{"role": "system", "content": "JSON"}, {"role": "user", "content": "Text"}], 1100)
        complete.assert_not_called()
        self.assertEqual(client.stats()["paid_api_calls"], 0)
        self.assertEqual(client.stats()["usage_unreported_calls"], 0)

    def test_chunk_boundary_defers_and_next_window_reuses_paid_chunk(self):
        clock = [datetime.fromisoformat("2026-10-13T08:59:59+08:00")]
        worker = reading_worker.Worker(self.directory.name, config=self.config, clock=lambda: clock[0])
        value = podcast("podcast-boundary")
        value["segments"].append({"id": "s0002", "text": "A second distinct topic in the interview.", "start_seconds": 20, "end_seconds": 40})
        worker.runtime.poc.save_episode(value)
        job = worker.runtime.engine.enqueue(value["id"], manual=False)
        calls = []
        def respond(**kwargs):
            refs = (["s0001"], ["s0002"], ["s0001", "s0002"], ["s0001", "s0002"])[len(calls)]
            calls.append(kwargs["user_prompt"])
            if len(calls) == 1:
                clock[0] = datetime.fromisoformat("2026-10-13T09:00:00+08:00")
            return llm_enrichment.EnrichmentResponse(
                result={"schema_version": "detailed-v1", "scope": "仅据取得文字。", "impacts": [], "overview": "访谈中的观点。", "points": [{"text": "原文支持这一观点。", "segment_ids": refs}]},
                usage=llm_enrichment.ProviderUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15))
        chunks = [[value["segments"][0]], [value["segments"][1]]]
        with patch.object(worker.runtime.poc, "bounded_summary_chunks", return_value=chunks), \
             patch.object(llm_enrichment, "complete_json_with_usage", side_effect=respond):
            self.assertFalse(worker.runtime.engine.run_one())
            deferred = next(row for row in worker.runtime.engine.snapshot() if row["id"] == job["id"])
            self.assertEqual(deferred["state"], "queued")
            self.assertEqual(deferred["deferred_reason"], "off_peak_only")
            self.assertEqual(len(calls), 1)
            saved = worker.runtime.poc.load_episode(value["id"])
            self.assertEqual(saved["processing"]["paid_api_calls"], 1)
            self.assertEqual(saved["coverage"]["chunks_processed"], 1)
            self.assertFalse(worker.runtime.engine.run_one())
            self.assertEqual(len(calls), 1)
            clock[0] = datetime.fromisoformat("2026-10-13T12:00:00+08:00")
            self.assertTrue(worker.runtime.engine.run_one())
        result = worker.runtime.poc.load_episode(value["id"])
        self.assertEqual(result["status"], "summarized")
        self.assertEqual(len(calls), 4)  # two cached evidence chunks, merge, final impact stage
        self.assertEqual(result["processing"]["paid_api_calls"], 4)

    def test_due_check_is_bounded_and_resume_enqueues_only_one_refresh(self):
        with patch.object(self.worker.runtime, "status", return_value={"due": True}), \
             patch.object(self.worker.runtime.engine, "enqueue_refresh") as enqueue, \
             patch.object(self.worker.runtime.engine, "run_one", return_value=False):
            self.worker.tick(monotonic=1)
            self.worker.tick(monotonic=2)
            self.worker.tick(monotonic=36000)
        self.assertEqual(enqueue.call_count, 2)

    def test_configuration_or_balance_block_disables_automatic_consumption(self):
        self.worker.runtime.engine._runtime_update(provider_blocked="balance")
        with patch.object(self.worker.runtime, "status", return_value={"due": True}), \
             patch.object(self.worker.runtime.engine, "enqueue_refresh") as enqueue, \
             patch.object(self.worker.runtime.engine, "run_one", return_value=False) as consume:
            self.worker.tick(monotonic=1)
        enqueue.assert_not_called()
        consume.assert_called_once_with(allow_automatic=False)

    def test_first_heartbeat_follows_queue_initialization_and_shutdown_marks_stopped(self):
        observations = []
        original = self.worker.heartbeat
        def capture(state="running"):
            observations.append((state, self.worker.runtime.engine.database.exists()))
            original(state)
        def stop_after_one():
            self.worker.stop.set()
            return False
        with patch.object(self.worker, "heartbeat", side_effect=capture), patch.object(self.worker, "tick", side_effect=stop_after_one):
            self.worker.run()
        self.assertEqual(observations, [("running", True), ("stopped", True)])
        self.assertFalse(self.worker.runtime.status()["worker_alive"])

    def test_second_scheduler_is_rejected_without_overwriting_heartbeat(self):
        path = self.worker.runtime.data / "runtime" / "reading-scheduler.lock"
        with path.open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(reading_service.ReadingUnavailable):
                self.worker.run()
        self.assertFalse((self.worker.runtime.data / "runtime" / "worker-heartbeat.json").exists())

    def test_missing_configuration_never_falls_back_to_local_model(self):
        with self.assertRaises(reading_service.ReadingUnavailable):
            reading_worker.Worker(self.directory.name, config=None)


if __name__ == "__main__":
    unittest.main()
