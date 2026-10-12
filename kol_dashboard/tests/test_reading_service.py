from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kol_dashboard import llm_enrichment, reading_service
from research.podcast_poc import poc


def article(content_id="wx-test"):
    return {"id": content_id, "content_kind": "wechat_article", "source_kind": "external_rss_body",
            "source_id": "source", "coverage": {"scope": "source_text"}, "status": "body_ready",
            "title": "测试文章", "account": "测试来源", "body_hash": "test-body",
            "processing": {"paid_api_calls": 0}, "segments": [{"id": "p0001", "text": "真实正文用于核对原始证据。" * 30}]}


def podcast(content_id="podcast-test", *, source_kind="official_transcript", scope="full"):
    return {"id": content_id, "source_kind": source_kind, "coverage": {"scope": scope}, "status": "transcribed",
            "title": "Test Podcast", "show": "Test Show", "language": "en", "processing": {"paid_api_calls": 0},
            "segments": [{"id": "s0001", "text": "This transcript records what the speaker actually said.",
                          "start_seconds": 0, "end_seconds": 20}]}


class FakeClient:
    def generate(self, messages, max_tokens):
        return json.dumps({"overview": "根据提供的文字稿总结。", "points": [{"text": "说话人表达了这个观点。", "segment_ids": ["s0001"]}]})

    def stats(self):
        return {"paid_api_calls": 0, "usage_unreported_calls": 0, "usage": {}}


class ReadingServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.runtime = reading_service.ReadingRuntime(self.directory.name)

    def configured(self):
        self.runtime.publish_configuration(model="deepseek-v4-flash", enabled=True)

    def test_unconfigured_web_has_no_model_fallback_and_cannot_enqueue(self):
        self.runtime.poc.save_episode(article())
        with patch.object(llm_enrichment, "load_config", side_effect=AssertionError("Web must not read the provider key")):
            payload = self.runtime.wechat_payload()
            self.assertEqual(payload["automation"]["provider"], "unconfigured")
            self.assertFalse(payload["automation"]["enabled"])
            self.assertNotIn("csrf_token", payload)
            with self.assertRaises(reading_service.ReadingUnavailable):
                self.runtime.enqueue_summary("wx-test")

    def test_server_runtime_does_not_mutate_local_poc_and_web_cannot_infer(self):
        self.assertNotEqual(poc.DATA, self.runtime.data)
        self.assertIsNone(poc._RUNTIME)
        self.configured()
        with self.assertRaises(reading_service.ReadingUnavailable):
            self.runtime.poc._RUNTIME["client_factory"]()

    def test_existing_article_enqueues_once_without_model_call(self):
        self.configured()
        self.runtime.poc.save_episode(article())
        with patch.object(llm_enrichment, "complete_json_with_usage", side_effect=AssertionError("Web must never infer")):
            first = self.runtime.enqueue_summary("wx-test")
            second = self.runtime.enqueue_summary("wx-test")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["state"], "queued")
        self.assertEqual(self.runtime.poc.load_episode("wx-test")["status"], "body_ready")

    def test_podcast_metadata_or_untrusted_kind_is_never_summarized(self):
        self.configured()
        for source_kind, scope in (("rss_metadata", "metadata_only"), ("rss_metadata", "full"), ("official_transcript", "metadata_only")):
            self.runtime.poc.save_episode(podcast(source_kind=source_kind, scope=scope))
            with self.assertRaises(ValueError):
                self.runtime.enqueue_summary("podcast-test")

    def test_existing_full_or_sample_podcast_can_be_summarized_idempotently(self):
        runtime = reading_service.ReadingRuntime(self.directory.name, client_factory=FakeClient)
        runtime.publish_configuration(model="deepseek-v4-flash", enabled=True)
        for index, (kind, scope) in enumerate((("official_transcript", "full"), ("official_edited_transcript", "full"), ("local_asr_sample", "sample"))):
            value = podcast("podcast-" + str(index), source_kind=kind, scope=scope)
            runtime.poc.save_episode(value)
            first = runtime.enqueue_summary(value["id"])
            self.assertTrue(runtime.engine.run_one())
            repeated = runtime.enqueue_summary(value["id"])
            self.assertEqual(first["id"], repeated["id"])
            self.assertEqual(repeated["state"], "completed")
            self.assertTrue(repeated["reused"])
            self.assertEqual(runtime.poc.load_episode(value["id"])["coverage"]["scope"], scope)

    def test_heartbeat_is_bounded_and_stopped_or_stale_worker_is_offline(self):
        self.configured()
        path = self.runtime.data / "runtime" / "worker-heartbeat.json"
        for state, age, expected in (("running", 1, True), ("running", 121, False), ("stopped", 0, False)):
            self.runtime.poc.write_json(path, {"state": state, "updated_at": (datetime.now(timezone.utc)-timedelta(seconds=age)).isoformat()})
            self.assertEqual(self.runtime.status()["worker_alive"], expected)

    def test_media_path_rejects_traversal_symlinks_and_missing_files(self):
        media = self.runtime.data / "media"
        media.mkdir()
        (media / "podcast-test.wav").write_bytes(b"RIFF")
        (media / "linked.wav").symlink_to(media / "podcast-test.wav")
        self.assertEqual(self.runtime.media_path("podcast-test"), (media / "podcast-test.wav").resolve())
        for value in ("../podcast-test", "linked", "absent", "/etc/passwd", None):
            self.assertIsNone(self.runtime.media_path(value))

    def test_feed_fetch_never_accepts_an_arbitrary_article_or_internal_url(self):
        with patch.object(reading_service, "build_opener", side_effect=AssertionError("No network expected")):
            for url in ("http://127.0.0.1/secret", "https://example.org/article", "https://api.deepseek.com/chat/completions"):
                with self.assertRaises(ValueError):
                    self.runtime.fetch_feed(url, limit=1000)

    def test_module_api_requires_explicit_production_directory(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(reading_service.ReadingUnavailable):
                reading_service.get_runtime()


if __name__ == "__main__":
    unittest.main()
