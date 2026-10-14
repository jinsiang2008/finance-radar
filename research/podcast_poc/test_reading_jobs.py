"""Offline concurrency and recovery tests; never invoke inference or RSS services."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import fcntl
import hashlib
import json
import multiprocessing
from pathlib import Path
import tempfile
import threading
import types
import unittest
import uuid

from research.podcast_poc.reading_jobs import Engine, PUBLIC_FAILURE, MAX_INTERRUPTION_RECOVERIES
from research.podcast_poc import schedule_policy


class FakePoc:
    MODEL = "test-local-model"

    def __init__(self, directory):
        self.DATA = Path(directory)
        self.calls = []
        self.before_summary = None
        self.refresh_calls = []
        self.before_refresh = None
        self.sources = [{"status": "ok"}]

    def load_episode(self, article_id):
        return json.loads((self.DATA / "articles" / (article_id + ".json")).read_text())

    def save_episode(self, article):
        path = self.DATA / "articles" / (article["id"] + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix("." + uuid.uuid4().hex)
        temporary.write_text(json.dumps(article))
        temporary.replace(path)

    def summarize(self, article_id):
        self.calls.append(article_id)
        article = self.load_episode(article_id)
        article["status"] = "summarizing"
        self.save_episode(article)
        if self.before_summary:
            self.before_summary()
        article.update(status="summarized", summary=summary(), summary_signature="verified-input")
        self.save_episode(article)
        return article

    def refresh(self, per_source):
        self.refresh_calls.append(per_source)
        if self.before_refresh:
            self.before_refresh()
        return self.sources

    def wechat_module(self):
        return types.SimpleNamespace(refresh=self.refresh)


def summary():
    return {"overview": "测试摘要", "points": [{"text": "正文支持的要点", "segment_ids": ["p0001"]}]}


def article(article_id="wx-example", text=None, *, source_id="source-a", published_at="2026-10-01T00:00:00+00:00"):
    text = text or "真实正文需要足够信息并且不能把文章简介当成正文。" * 14
    return {"id": article_id, "content_kind": "wechat_article", "source_kind": "external_rss_body",
            "source_id": source_id, "published_at": published_at,
            "coverage": {"scope": "source_text"}, "status": "body_ready", "title": "测试文章", "account": "测试来源",
            "body_hash": hashlib.sha256(text.encode()).hexdigest(), "segments": [{"id": "p0001", "text": text}]}


def _hold_process_lock(lock_path, ready, release):
    with open(lock_path, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        ready.set()
        release.wait(10)


def _enqueue_process(directory, output):
    try:
        output.put(Engine(FakePoc(directory)).enqueue("wx-example")["id"])
    except Exception as exc:
        output.put("ERROR:" + type(exc).__name__)


class ReadingJobTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.poc = FakePoc(self.directory.name)
        self.poc.save_episode(article())
        self.engine = Engine(self.poc)

    def job(self, job_id):
        return next(job for job in self.engine.snapshot() if job["id"] == job_id)

    def test_concurrent_thread_requests_share_one_job(self):
        with ThreadPoolExecutor(max_workers=12) as executor:
            results = list(executor.map(lambda _: self.engine.enqueue("wx-example"), range(30)))
        self.assertEqual(len({job["id"] for job in results}), 1)
        self.assertEqual(len(self.engine.snapshot()), 1)
        self.assertEqual(self.poc.calls, [])

    def test_legacy_queue_migration_is_concurrent_and_preserves_pending_job(self):
        queued = self.engine.enqueue("wx-example")
        with self.engine._connection() as connection:
            connection.execute("ALTER TABLE jobs DROP COLUMN not_before")
            connection.execute("ALTER TABLE jobs DROP COLUMN deferred_reason")
        with ThreadPoolExecutor(max_workers=5) as executor:
            list(executor.map(lambda _index: Engine(self.poc), range(5)))
        result = self.job(queued["id"])
        self.assertEqual(result["state"], "queued")
        self.assertIsNone(result["not_before"])
        self.assertIsNone(result["deferred_reason"])

    def test_concurrent_process_requests_share_one_job(self):
        context = multiprocessing.get_context("spawn")
        output = context.Queue()
        processes = [context.Process(target=_enqueue_process, args=(self.directory.name, output)) for _ in range(3)]
        for process in processes:
            process.start()
        results = [output.get(timeout=15) for _ in processes]
        for process in processes:
            process.join(15)
            self.assertEqual(process.exitcode, 0)
        self.assertFalse(any(value.startswith("ERROR:") for value in results), results)
        self.assertEqual(len(set(results)), 1)

    def test_existing_review_is_reused_even_after_provider_change(self):
        value = article()
        value.update(status="summarized", summary=summary(), summary_signature="previous-provider",
                     quality_review={"status": "assistant_reviewed"}, model_draft_summary=summary())
        self.poc.save_episode(value)
        self.poc.MODEL = "new-provider"
        job = self.engine.enqueue(value["id"])
        self.assertEqual(job["state"], "completed")
        self.assertTrue(job["reused"])
        self.assertFalse(self.engine.run_one())
        self.assertEqual(self.poc.load_episode(value["id"]), value)

    def test_completed_input_and_duplicate_click_do_not_run_again(self):
        first = self.engine.enqueue("wx-example")
        self.assertTrue(self.engine.run_one())
        repeated = self.engine.enqueue("wx-example")
        self.assertEqual(first["id"], repeated["id"])
        self.assertEqual(repeated["state"], "completed")
        self.assertTrue(repeated["reused"])
        self.assertEqual(repeated["attempts"], 1)
        self.assertFalse(self.engine.run_one())
        self.assertEqual(len(self.poc.calls), 1)

    def test_metadata_short_text_and_invalid_ids_are_rejected(self):
        for update in ({"source_kind": "rss_metadata"}, {"coverage": {"scope": "metadata_only"}},
                       {"segments": [{"id": "p0001", "text": "x" * 199}]},
                       {"segments": [{"id": "p0001", "text": " " * 1000}]}, {"content_kind": "podcast"},
                       {"coverage": None}):
            value = article()
            value.update(update)
            self.poc.save_episode(value)
            with self.assertRaises(ValueError):
                self.engine.enqueue(value["id"])
        for invalid in ("../wx-example", "episode-example", None):
            with self.assertRaises(ValueError):
                self.engine.enqueue(invalid)
        self.assertEqual(self.engine.snapshot(), [])

    def test_changed_body_supersedes_queued_input(self):
        first = self.engine.enqueue("wx-example")
        self.poc.save_episode(article(text="正文的新版本包含完全不同的范围和事实。" * 18))
        self.assertTrue(self.engine.run_one())
        self.assertEqual(self.job(first["id"])["state"], "superseded")
        queued = [job for job in self.engine.snapshot() if job["state"] == "queued"]
        self.assertEqual(len(queued), 1)
        self.assertNotEqual(queued[0]["input_version"], first["input_version"])
        self.assertEqual(self.poc.calls, [])
        self.assertTrue(self.engine.run_one())
        self.assertEqual(self.poc.calls, ["wx-example"])

    def test_model_signature_hook_changes_unfinished_job_version(self):
        first = self.engine.enqueue("wx-example")
        self.poc.summary_signature = lambda item: "new-provider:" + item["body_hash"]
        second = self.engine.enqueue("wx-example")
        self.assertNotEqual(first["input_version"], second["input_version"])
        self.assertEqual(self.job(first["id"])["state"], "superseded")

    def test_failure_does_not_auto_retry_but_manual_request_can_retry(self):
        def fail():
            raise RuntimeError("https://user:SUPER_SECRET@example.org/path?token=SUPER_SECRET")
        self.poc.before_summary = fail
        first = self.engine.enqueue("wx-example", manual=False)
        self.assertTrue(self.engine.run_one())
        failed = self.job(first["id"])
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["error"], PUBLIC_FAILURE)
        self.assertEqual(self.engine.enqueue("wx-example", manual=False)["state"], "failed")
        self.assertFalse(self.engine.run_one())
        diagnostic = (self.engine.runtime / "reading-errors.jsonl").read_text()
        self.assertNotIn("SUPER_SECRET", diagnostic + json.dumps(failed) + json.dumps(self.poc.load_episode("wx-example")))
        self.assertIn("RuntimeError", diagnostic)
        self.poc.before_summary = None
        retried = self.engine.enqueue("wx-example", manual=True)
        self.assertEqual(retried["id"], first["id"])
        self.assertEqual(retried["state"], "queued")
        self.assertTrue(self.engine.run_one())
        self.assertEqual(self.job(first["id"])["attempts"], 2)
        self.assertEqual(self.job(first["id"])["state"], "completed")

    def test_safe_provider_failure_has_actionable_message_and_diagnostic(self):
        failure = RuntimeError("SUPER_SECRET provider-controlled text")
        failure.code, failure.http_status = "balance", 402
        def fail():
            raise failure
        self.poc.before_summary = fail
        first = self.engine.enqueue("wx-example")
        self.engine.run_one()
        self.assertIn("余额不足", self.job(first["id"])["error"])
        diagnostic = json.loads((self.engine.runtime / "reading-errors.jsonl").read_text().splitlines()[0])
        self.assertEqual(diagnostic["code"], "balance")
        self.assertEqual(diagnostic["http_status"], 402)
        self.assertNotIn("SUPER_SECRET", json.dumps(diagnostic) + json.dumps(self.job(first["id"])))

    def test_single_executor_does_not_reset_an_active_job(self):
        entered, release = threading.Event(), threading.Event()
        def block():
            entered.set()
            self.assertTrue(release.wait(5))
        self.poc.before_summary = block
        first = self.engine.enqueue("wx-example")
        other_engine = Engine(self.poc)
        with ThreadPoolExecutor(max_workers=1) as executor:
            running = executor.submit(self.engine.run_one)
            self.assertTrue(entered.wait(5))
            try:
                self.assertEqual(self.job(first["id"])["state"], "running")
                self.assertFalse(other_engine.run_one())
                self.assertEqual(self.job(first["id"])["state"], "running")
                self.assertEqual(self.engine.enqueue("wx-example")["id"], first["id"])
            finally:
                release.set()
            self.assertTrue(running.result(5))
        self.assertEqual(self.job(first["id"])["attempts"], 1)

    def test_other_process_lock_prevents_execution_and_recovery(self):
        first = self.engine.enqueue("wx-example")
        with self.engine._transaction() as connection:
            connection.execute("UPDATE jobs SET state='running' WHERE id=?", (first["id"],))
        context = multiprocessing.get_context("spawn")
        ready, release = context.Event(), context.Event()
        process = context.Process(target=_hold_process_lock, args=(str(self.engine.lock_path), ready, release))
        process.start()
        try:
            self.assertTrue(ready.wait(10))
            self.assertFalse(Engine(self.poc).run_one())
            self.assertEqual(self.job(first["id"])["state"], "running")
        finally:
            release.set()
            process.join(10)
        self.assertEqual(process.exitcode, 0)

    def test_interrupted_work_is_recovered_after_lock_release(self):
        def interrupt():
            raise KeyboardInterrupt()
        self.poc.before_summary = interrupt
        first = self.engine.enqueue("wx-example")
        with self.assertRaises(KeyboardInterrupt):
            self.engine.run_one()
        self.assertEqual(self.job(first["id"])["state"], "running")
        self.poc.before_summary = None
        self.assertTrue(Engine(self.poc).run_one())
        self.assertEqual(self.job(first["id"])["state"], "completed")
        self.assertEqual(self.job(first["id"])["attempts"], 2)

    def test_repeated_interruptions_stop_until_manual_retry(self):
        first = self.engine.enqueue("wx-example", manual=False)
        with self.engine._transaction() as connection:
            connection.execute("UPDATE jobs SET state='running', interruptions=? WHERE id=?",
                               (MAX_INTERRUPTION_RECOVERIES, first["id"]))
        self.assertFalse(self.engine.run_one())
        self.assertEqual(self.job(first["id"])["state"], "failed")
        self.assertEqual(self.engine.enqueue("wx-example", manual=False)["state"], "failed")
        self.engine.enqueue("wx-example", manual=True)
        self.assertTrue(self.engine.run_one())
        self.assertEqual(self.job(first["id"])["state"], "completed")

    def test_automatic_gate_allows_only_manual_summary_and_promotion(self):
        refresh = self.engine.enqueue_refresh()
        automatic = self.engine.enqueue("wx-example", manual=False)
        self.assertFalse(self.engine.run_one(allow_automatic=False))
        promoted = self.engine.enqueue("wx-example", manual=True)
        self.assertTrue(promoted["manual"])
        self.assertEqual(promoted["id"], automatic["id"])
        self.assertTrue(self.engine.run_one(allow_automatic=False))
        self.assertEqual(self.job(refresh["id"])["state"], "queued")
        self.assertEqual(self.job(automatic["id"])["state"], "completed")

    def test_peak_policy_defers_manual_and_automatic_work_without_attempt(self):
        clock = [datetime.fromisoformat("2026-10-13T10:00:00+08:00")]
        self.poc.reading_schedule = lambda: schedule_policy.snapshot(at=clock[0], off_peak_only=True)
        summary_job = self.engine.enqueue("wx-example", manual=True)
        refresh_job = self.engine.enqueue_refresh(manual=True)
        self.assertEqual(summary_job["deferred_reason"], "off_peak_only")
        self.assertEqual(refresh_job["deferred_reason"], "off_peak_only")
        self.assertFalse(self.engine.run_one(allow_automatic=False))
        self.assertEqual(self.poc.calls, [])
        self.assertEqual(self.poc.refresh_calls, [])
        self.assertTrue(all(job["attempts"] == 0 and job["state"] == "queued" for job in self.engine.snapshot()))
        clock[0] = datetime.fromisoformat("2026-10-13T12:00:00+08:00")
        self.assertTrue(self.engine.run_one(allow_automatic=False))
        self.assertEqual(self.job(refresh_job["id"])["state"], "completed")
        self.assertTrue(self.engine.run_one(allow_automatic=False))
        self.assertEqual(self.job(summary_job["id"])["state"], "completed")
        self.assertIsNone(self.job(summary_job["id"])["not_before"])

    def test_mid_job_peak_boundary_requeues_without_failure_or_interruption(self):
        clock = [datetime.fromisoformat("2026-10-13T08:59:59+08:00")]
        self.poc.reading_schedule = lambda: schedule_policy.snapshot(at=clock[0], off_peak_only=True)
        def boundary():
            clock[0] = datetime.fromisoformat("2026-10-13T09:00:00+08:00")
            raise schedule_policy.OffPeakDeferred(clock[0])
        self.poc.before_summary = boundary
        job = self.engine.enqueue("wx-example", manual=True)
        self.assertFalse(self.engine.run_one())
        deferred = self.job(job["id"])
        self.assertEqual(deferred["state"], "queued")
        self.assertIsNone(deferred["error"])
        self.assertEqual(deferred["deferred_reason"], "off_peak_only")
        self.assertEqual(self.poc.load_episode("wx-example")["status"], "body_ready")
        self.assertFalse((self.engine.runtime / "reading-errors.jsonl").exists())
        self.assertFalse(self.engine.run_one())
        self.assertEqual(self.job(job["id"])["attempts"], 1)
        self.poc.before_summary = None
        clock[0] = datetime.fromisoformat("2026-10-13T12:00:00+08:00")
        self.assertTrue(self.engine.run_one())
        self.assertEqual(self.job(job["id"])["state"], "completed")

    def test_manual_refresh_does_not_authorize_its_discovered_summary_batch(self):
        first = self.engine.enqueue_refresh()
        promoted = self.engine.enqueue_refresh(manual=True)
        self.assertEqual(first["id"], promoted["id"])
        self.assertTrue(promoted["manual"])
        self.assertTrue(self.engine.run_one(allow_automatic=False))
        downstream = next(job for job in self.engine.snapshot() if job["type"] == "summary")
        self.assertFalse(downstream["manual"])
        self.assertFalse(downstream["on_demand"])
        self.assertFalse(self.engine.run_one(allow_automatic=False))
        self.assertEqual(self.job(downstream["id"])["state"], "queued")
        selected = self.engine.enqueue(downstream["article_id"], on_demand=True)
        self.assertEqual(selected["id"], downstream["id"])
        self.assertTrue(self.engine.run_one(allow_automatic=False))
        self.assertEqual(self.job(downstream["id"])["state"], "completed")

    def test_refresh_dedupes_and_queues_pending_bodies_without_review_rewrites(self):
        reviewed = article("wx-reviewed")
        reviewed.update(status="summarized", summary=summary(), quality_review={"status": "assistant_reviewed"})
        self.poc.save_episode(reviewed)
        first = self.engine.enqueue_refresh()
        self.assertEqual(first["id"], self.engine.enqueue_refresh()["id"])
        self.assertTrue(self.engine.run_one())
        self.assertEqual(self.poc.refresh_calls, [20])
        self.assertEqual(self.poc.calls, [])
        pending = [job for job in self.engine.snapshot() if job["state"] == "queued"]
        self.assertEqual([job["article_id"] for job in pending], ["wx-example"])
        self.assertFalse(pending[0]["manual"])
        self.assertEqual(self.poc.load_episode("wx-reviewed"), reviewed)
        runtime = self.engine.runtime_snapshot()
        self.assertTrue(runtime["last_check_at"])
        self.assertTrue(runtime["last_success_at"])
        self.assertEqual(runtime["last_refresh_state"], "completed")
        self.assertEqual(runtime["successful_sources"], 1)

    def test_deeper_refresh_queues_all_new_articles_but_never_the_old_archive(self):
        imported = [article("wx-new-" + str(day), published_at=f"2026-10-{day:02d}T00:00:00+00:00")
                    for day in range(2, 6)]
        imported.extend(article("wx-archive-" + str(day), published_at=f"2026-09-{day:02d}T00:00:00+00:00")
                        for day in range(20, 28))
        self.poc.before_refresh = lambda: [self.poc.save_episode(value) for value in imported]
        for _ in range(2):
            self.engine.enqueue_refresh()
            self.assertTrue(self.engine.run_one())
            summary_ids = {job["article_id"] for job in self.engine.snapshot() if job["type"] == "summary"}
            self.assertEqual(summary_ids, {"wx-example", "wx-new-2", "wx-new-3", "wx-new-4", "wx-new-5"})
        self.assertEqual(self.poc.refresh_calls, [20, 20])
        self.assertNotIn("automatic_source_baselines", self.engine.runtime_snapshot())
        manual_archive = self.engine.enqueue("wx-archive-20", manual=True)
        self.assertEqual(manual_archive["state"], "queued")
        self.assertTrue(manual_archive["manual"])

    def test_new_source_only_seeds_two_latest_articles_on_every_refresh(self):
        imported = [article("wx-other-" + str(day), source_id="new-source",
                            published_at=f"2026-10-{day:02d}T00:00:00+00:00") for day in range(1, 8)]
        self.poc.before_refresh = lambda: [self.poc.save_episode(value) for value in imported]
        for _ in range(2):
            self.engine.enqueue_refresh()
            self.engine.run_one()
            summary_ids = {job["article_id"] for job in self.engine.snapshot() if job["type"] == "summary"}
            self.assertEqual(summary_ids, {"wx-example", "wx-other-6", "wx-other-7"})

    def test_interrupted_new_source_import_does_not_promote_all_archived_articles(self):
        imported = [article("wx-interrupted-" + str(day), source_id="new-source",
                            published_at=f"2026-10-{day:02d}T00:00:00+00:00") for day in range(1, 8)]
        def interrupt():
            for value in imported:
                self.poc.save_episode(value)
            raise KeyboardInterrupt()
        self.poc.before_refresh = interrupt
        self.engine.enqueue_refresh()
        with self.assertRaises(KeyboardInterrupt):
            self.engine.run_one()
        self.poc.before_refresh = None
        self.assertTrue(Engine(self.poc).run_one())
        summary_ids = {job["article_id"] for job in self.engine.snapshot() if job["type"] == "summary"}
        self.assertEqual(summary_ids, {"wx-example", "wx-interrupted-6", "wx-interrupted-7"})

    def test_edited_archived_article_previously_read_manually_can_be_processed(self):
        archived = article("wx-archive", published_at="2026-09-01T00:00:00+00:00")
        self.poc.before_refresh = lambda: self.poc.save_episode(archived)
        self.engine.enqueue_refresh()
        self.engine.run_one()
        self.assertFalse(any(job["article_id"] == archived["id"] for job in self.engine.snapshot()))
        manually_read = self.engine.enqueue(archived["id"], manual=True)
        self.engine.run_one()
        self.assertEqual(self.job(manually_read["id"])["state"], "completed")
        updated = article(archived["id"], text="文章发布后修订了此前陈述，需要根据新正文重新理解。" * 15,
                          published_at=archived["published_at"])
        self.poc.before_refresh = lambda: self.poc.save_episode(updated)
        self.engine.enqueue_refresh()
        self.engine.run_one()
        versions = [job for job in self.engine.snapshot() if job["article_id"] == archived["id"]]
        self.assertEqual(len(versions), 2)
        self.assertEqual({job["state"] for job in versions}, {"queued", "completed"})

    def test_failed_sources_preserve_last_success_and_pending_body(self):
        self.engine._runtime_update(last_success_at="previous-success")
        self.poc.sources = [{"status": "failed", "error": "https://user:secret@host"}]
        refresh = self.engine.enqueue_refresh()
        self.assertTrue(self.engine.run_one())
        self.assertEqual(self.job(refresh["id"])["state"], "failed")
        runtime = self.engine.runtime_snapshot()
        self.assertEqual(runtime["last_success_at"], "previous-success")
        self.assertEqual(runtime["failed_sources"], 1)
        self.assertNotIn("secret", json.dumps(runtime) + json.dumps(self.job(refresh["id"])))
        self.assertEqual(self.poc.load_episode("wx-example")["status"], "body_ready")
        self.assertTrue(any(job["state"] == "queued" and job["type"] == "summary" for job in self.engine.snapshot()))


if __name__ == "__main__":
    unittest.main()
