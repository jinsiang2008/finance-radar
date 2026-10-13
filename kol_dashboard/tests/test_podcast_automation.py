"""Offline behavior checks for automatic official podcast reading.

Synthetic transcripts stand in for publisher content. These tests exercise the
persistent queue and reservations without any publisher or inference requests.
"""
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timedelta
from email.message import Message
import hashlib
import io
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import BaseHandler, build_opener
from urllib.response import addinfourl

from kol_dashboard import reading_service


def parsed_transcript():
    return {
        "source_kind": "official_edited_transcript",
        "segments": [{"id": "s0001", "speaker": "Guest", "start_seconds": 0,
                      "end_seconds": 120, "text": "A synthetic discussion with verifiable source evidence."}],
        "coverage": {"scope": "full", "segments_total": 1, "segments_processed": 0,
                     "note": "Provided official text; audio has not been independently checked."},
    }


class PodcastAutomationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.moment = datetime.fromisoformat("2026-10-13T20:00:00+08:00")
        self.runtime = self.new_runtime()
        self.runtime.publish_configuration(model="deepseek-v4-flash", enabled=True)

    def new_runtime(self):
        return reading_service.ReadingRuntime(self.directory.name, clock=lambda: self.moment)

    def episode(self, source="dwarkesh", suffix="latest", *, published_at="2026-10-13T00:00:00+00:00"):
        source_urls = {"dwarkesh": "https://www.dwarkesh.com/p/test-guest",
                       "lex": "https://lexfridman.com/test-guest/",
                       "diary": "https://example.org/diary/test-guest"}
        value = {
            "id": source + "-" + suffix, "source_id": source,
            "title": "Synthetic interview " + suffix, "show": self.runtime.poc.FEEDS[source][0],
            "language": "en", "published_at": published_at,
            "source_url": source_urls.get(source, "https://example.org/podcast"),
            "feed_url": self.runtime.poc.FEEDS[source][2], "guid": "stable-guid-" + suffix,
            "audio_url": "https://example.org/audio/" + suffix + ".mp3", "duration_seconds": 3600,
            "discovered_at": "2026-10-13T01:00:00+00:00", "status": "discovered",
            "source_kind": "rss_metadata", "segments": [], "coverage": {"scope": "metadata_only"},
            "processing": {"paid_api_calls": 0},
        }
        if source == "diary":
            value["official_transcript_url"] = "https://rss.flightcast.com/transcripts/TEST_123.vtt"
        self.runtime.poc.save_episode(value)
        return value

    @contextmanager
    def successful_fetches(self, runtime=None):
        runtime = runtime or self.runtime
        def target(source, url, _html):
            return url if source == "dwarkesh" else url.rstrip("/") + "-transcript"
        with patch.object(runtime, "fetch_official_transcript", return_value="synthetic publisher response") as fetch, \
             patch.object(reading_service.official_transcripts, "find_transcript_url", side_effect=target), \
             patch.object(reading_service.official_transcripts, "parse_transcript", side_effect=lambda *_a, **_k: parsed_transcript()), \
             patch.object(reading_service.official_transcripts, "parse_webvtt", side_effect=lambda *_a, **_k: parsed_transcript()):
            yield fetch

    def test_only_latest_eligible_episode_is_read_without_historical_backfill(self):
        old = self.episode(suffix="old", published_at="2026-09-01T00:00:00+00:00")
        latest = self.episode()
        future = self.episode(suffix="future", published_at="2026-10-14T00:00:00+00:00")
        undated = self.episode(suffix="undated", published_at="not a publication date")
        preserved = {row["id"]: self.runtime.poc.episode_path(row["id"]).read_bytes()
                     for row in (old, future, undated)}
        with self.successful_fetches() as fetch:
            self.runtime.acquire_podcast_transcripts()
            self.runtime.acquire_podcast_transcripts()
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual([job["article_id"] for job in self.runtime.engine.snapshot()], [latest["id"]])
        for content_id, original in preserved.items():
            self.assertEqual(self.runtime.poc.episode_path(content_id).read_bytes(), original)

    def test_success_preserves_rss_identity_and_queues_one_summary_across_restarts(self):
        original = self.episode()
        with self.successful_fetches():
            self.runtime.acquire_podcast_transcripts()
        saved = self.runtime.poc.load_episode(original["id"])
        for key in ("id", "source_id", "title", "show", "language", "published_at", "source_url",
                    "feed_url", "guid", "audio_url", "duration_seconds", "discovered_at", "processing"):
            self.assertEqual(saved[key], original[key], key)
        self.assertEqual(saved["transcript_acquisition"]["state"], "ready")
        self.assertEqual(saved["transcript_source_url"], original["source_url"])
        first = self.runtime.engine.snapshot()
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["type"], "summary")
        self.assertEqual(first[0]["state"], "queued")
        self.assertFalse(first[0]["manual"])
        restarted = self.new_runtime()
        with patch.object(restarted, "fetch_official_transcript", side_effect=AssertionError("Do not fetch ready text")):
            restarted.acquire_podcast_transcripts()
        self.assertEqual([job["id"] for job in restarted.engine.snapshot()], [first[0]["id"]])

    def test_crash_after_saving_text_recovers_queue_without_refetch(self):
        original = self.episode()
        original.update(parsed_transcript(), status="transcribed", transcript_acquisition={"state": "ready"})
        self.runtime.poc.save_episode(original)
        self.assertEqual(self.runtime.engine.snapshot(), [])
        with patch.object(self.runtime, "fetch_official_transcript", side_effect=AssertionError("Already saved")):
            self.runtime.acquire_podcast_transcripts()
        jobs = self.runtime.engine.snapshot()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["article_id"], original["id"])
        self.assertEqual(jobs[0]["state"], "queued")

    def test_daily_three_input_limit_survives_restart_and_newer_feed_items(self):
        for source in ("dwarkesh", "lex", "diary"):
            self.episode(source)
        with self.successful_fetches():
            self.runtime.acquire_podcast_transcripts()
        self.assertEqual(len(self.runtime.engine.snapshot()), 3)
        fourth = self.episode(suffix="newer", published_at="2026-10-13T10:00:00+00:00")
        restarted = self.new_runtime()
        with patch.object(restarted, "fetch_official_transcript", side_effect=AssertionError("Daily limit exhausted")):
            restarted.acquire_podcast_transcripts()
        self.assertFalse(restarted.poc.load_episode(fourth["id"])["segments"])
        reservations = restarted.engine._runtime_value("podcast_daily_reservations")
        self.assertEqual(reservations["date"], "2026-10-13")
        self.assertEqual(len(reservations["ids"]), 3)
        self.moment += timedelta(days=1)
        with self.successful_fetches(restarted) as fetch:
            restarted.acquire_podcast_transcripts()
        self.assertEqual(fetch.call_count, 1)
        self.assertTrue(restarted.poc.load_episode(fourth["id"])["segments"])
        self.assertEqual(len(restarted.engine.snapshot()), 4)

    def test_refresh_crossing_beijing_midnight_reserves_each_input_on_its_start_date(self):
        self.moment = datetime.fromisoformat("2026-10-13T23:59:59+08:00")
        rows = {source: self.episode(source) for source in ("dwarkesh", "lex", "diary")}
        at_fetch = []
        def cross_midnight(source, _url, **_kwargs):
            at_fetch.append((source, self.runtime.engine._runtime_value("podcast_daily_reservations")))
            if source == "dwarkesh":
                self.moment = datetime.fromisoformat("2026-10-14T00:00:02+08:00")
            return "synthetic publisher response"
        with self.successful_fetches() as fetch:
            fetch.side_effect = cross_midnight
            self.runtime.acquire_podcast_transcripts()
        self.assertEqual(at_fetch[0], ("dwarkesh", {"date": "2026-10-13", "ids": [rows["dwarkesh"]["id"]]}))
        self.assertEqual(at_fetch[1], ("lex", {"date": "2026-10-14", "ids": [rows["lex"]["id"]]}))
        self.assertEqual(self.runtime.engine._runtime_value("podcast_daily_reservations"),
                         {"date": "2026-10-14", "ids": [rows["lex"]["id"], rows["diary"]["id"]]})
        # The first input was reserved yesterday, so one new input still fits
        # today's allowance; a later fourth reservation must remain blocked.
        self.moment = datetime.fromisoformat("2026-10-14T00:02:00+08:00")
        third_today = self.episode(suffix="midnight-new", published_at="2026-10-14T00:01:00+08:00")
        restarted = self.new_runtime()
        with self.successful_fetches(restarted) as fetch:
            restarted.acquire_podcast_transcripts()
        self.assertEqual(fetch.call_count, 1)
        self.assertTrue(restarted.poc.load_episode(third_today["id"])["segments"])
        fourth_today = self.episode("diary", suffix="midnight-new", published_at="2026-10-14T00:01:00+08:00")
        with patch.object(restarted, "fetch_official_transcript", side_effect=AssertionError("Today's three reservations are used")):
            restarted.acquire_podcast_transcripts()
        self.assertEqual(restarted.poc.load_episode(fourth_today["id"])["segments"], [])
        self.assertEqual(len(restarted.engine._runtime_value("podcast_daily_reservations")["ids"]), 3)

    def test_older_ready_input_recovers_after_new_episode_without_consuming_new_day_allowance(self):
        old = self.episode(suffix="saved-before-crash", published_at="2026-10-12T00:00:00+00:00")
        old.update(parsed_transcript(), status="transcribed", transcript_acquisition={"state": "ready"})
        self.runtime.poc.save_episode(old)
        old_bytes = self.runtime.poc.episode_path(old["id"]).read_bytes()
        self.runtime.engine._runtime_update(podcast_daily_reservations={"date": "2026-10-13", "ids": [old["id"]]})
        new = [self.episode(source) for source in ("dwarkesh", "lex", "diary")]
        self.moment = datetime.fromisoformat("2026-10-14T20:00:00+08:00")
        restarted = self.new_runtime()
        with self.successful_fetches(restarted) as fetch:
            restarted.acquire_podcast_transcripts()
        # Three newly fetched inputs plus the old ready input are all queued.
        # Lex takes two requests; recovery of the old saved input takes none.
        self.assertEqual(fetch.call_count, 4)
        self.assertEqual({job["article_id"] for job in restarted.engine.snapshot()},
                         {old["id"], *(row["id"] for row in new)})
        self.assertTrue(all(job["state"] == "queued" for job in restarted.engine.snapshot()))
        self.assertEqual(restarted.engine._runtime_value("podcast_daily_reservations"),
                         {"date": "2026-10-14", "ids": [row["id"] for row in new]})
        self.assertEqual(restarted.poc.episode_path(old["id"]).read_bytes(), old_bytes)
        job_ids = {job["id"] for job in restarted.engine.snapshot()}
        with patch.object(restarted, "fetch_official_transcript", side_effect=AssertionError("All text is already saved")):
            restarted.acquire_podcast_transcripts()
        self.assertEqual({job["id"] for job in restarted.engine.snapshot()}, job_ids)

    def test_reviewed_transcript_is_never_replaced_or_automatically_reanalyzed(self):
        value = self.episode()
        value.update(parsed_transcript(), status="summarized", summary_signature="reviewed-input",
                     summary={"overview": "复核后的摘要", "points": [{"text": "已有证据支持的观点", "segment_ids": ["s0001"]}]},
                     quality_review={"status": "assistant_reviewed"},
                     model_draft_summary={"overview": "旧的模型草稿"})
        self.runtime.poc.save_episode(value)
        before = self.runtime.poc.episode_path(value["id"]).read_bytes()
        with patch.object(self.runtime, "fetch_official_transcript", side_effect=AssertionError("Reviewed content is preserved")):
            self.runtime.acquire_podcast_transcripts()
        self.assertEqual(self.runtime.poc.episode_path(value["id"]).read_bytes(), before)
        self.assertEqual(self.runtime.engine.snapshot(), [])

    def assert_failure_retries_after_one_day(self, failure, *, parsing=False):
        value = self.episode()
        with patch.object(self.runtime, "fetch_official_transcript", side_effect=None if parsing else failure,
                          return_value="show notes with no transcript") as fetch, \
             patch.object(reading_service.official_transcripts, "parse_transcript", side_effect=failure if parsing else AssertionError("Not fetched")):
            self.runtime.acquire_podcast_transcripts()
        self.assertEqual(fetch.call_count, 1)
        saved = self.runtime.poc.load_episode(value["id"])
        self.assertEqual(saved["source_kind"], "rss_metadata")
        self.assertEqual(saved["segments"], [])
        self.assertEqual(saved["transcript_acquisition"]["state"], "unavailable")
        retry_at = datetime.fromisoformat(saved["transcript_acquisition"]["next_retry_at"])
        self.assertEqual(retry_at - self.moment, timedelta(hours=24))
        self.assertNotIn("secret fetched HTML", str(saved))
        self.assertEqual(self.runtime.engine.snapshot(), [])
        self.moment += timedelta(hours=23)
        restarted = self.new_runtime()
        with patch.object(restarted, "fetch_official_transcript", side_effect=AssertionError("Backoff survives restart")):
            restarted.acquire_podcast_transcripts()
        self.moment += timedelta(hours=1)
        with self.successful_fetches(restarted) as fetch:
            restarted.acquire_podcast_transcripts()
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(restarted.engine.snapshot()[0]["state"], "queued")

    def test_404_never_becomes_summary_and_retries_after_24_hours(self):
        self.assert_failure_retries_after_one_day(HTTPError("https://www.dwarkesh.com/p/test-guest", 404,
                                                           "secret fetched HTML", {}, None))

    def test_parser_rejection_never_becomes_summary_and_retries_after_24_hours(self):
        self.assert_failure_retries_after_one_day(ValueError("secret fetched HTML"), parsing=True)

    def test_peak_makes_no_network_request_or_acquisition_reservation(self):
        self.moment = datetime.fromisoformat("2026-10-13T10:00:00+08:00")
        value = self.episode()
        original = self.runtime.poc.episode_path(value["id"]).read_bytes()
        with patch.object(reading_service, "build_opener", side_effect=AssertionError("No network at peak")):
            with self.assertRaises(reading_service.schedule_policy.OffPeakDeferred):
                self.runtime.acquire_podcast_transcripts()
        self.assertEqual(self.runtime.poc.episode_path(value["id"]).read_bytes(), original)
        self.assertIsNone(self.runtime.engine._runtime_value("podcast_daily_reservations"))
        self.assertEqual(self.runtime.engine.snapshot(), [])

    def test_crossing_peak_between_show_page_and_transcript_is_pending_then_resumes(self):
        self.moment = datetime.fromisoformat("2026-10-13T08:59:59+08:00")
        value = self.episode("lex")
        opener = MagicMock()
        def first_response(*_args, **_kwargs):
            self.moment = datetime.fromisoformat("2026-10-13T09:00:00+08:00")
            return io.BytesIO(b'<a href="/test-guest-transcript">Transcript</a>')
        opener.open.side_effect = first_response
        with patch.object(reading_service, "build_opener", return_value=opener):
            with self.assertRaises(reading_service.schedule_policy.OffPeakDeferred):
                self.runtime.acquire_podcast_transcripts()
        self.assertEqual(opener.open.call_count, 1)
        saved = self.runtime.poc.load_episode(value["id"])
        self.assertEqual(saved["transcript_acquisition"]["state"], "pending")
        self.assertNotIn("error_code", saved["transcript_acquisition"])
        self.assertEqual(saved["segments"], [])
        self.assertEqual(self.runtime.engine.snapshot(), [])
        self.moment = datetime.fromisoformat("2026-10-13T12:00:00+08:00")
        with self.successful_fetches() as fetch:
            self.runtime.acquire_podcast_transcripts()
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(self.runtime.engine.snapshot()[0]["state"], "queued")

    def test_unsupported_sources_and_disabled_or_blocked_worker_do_not_fetch(self):
        self.episode("allin")
        with patch.object(self.runtime, "fetch_official_transcript", side_effect=AssertionError("No supported source")):
            self.runtime.acquire_podcast_transcripts()
        self.episode()
        self.runtime.publish_configuration(model="deepseek-v4-flash", enabled=False)
        with patch.object(self.runtime, "fetch_official_transcript", side_effect=AssertionError("Disabled worker")):
            self.runtime.acquire_podcast_transcripts()
        self.runtime.publish_configuration(model="deepseek-v4-flash", enabled=True)
        self.runtime.engine._runtime_update(provider_blocked="quota_exceeded")
        with patch.object(self.runtime, "fetch_official_transcript", side_effect=AssertionError("Blocked provider")):
            self.runtime.acquire_podcast_transcripts()
        self.assertEqual(self.runtime.engine.snapshot(), [])

    def test_payload_distinguishes_archive_waiting_and_unsupported_without_writes(self):
        rows = [self.episode(), self.episode(suffix="old", published_at="2026-09-01T00:00:00+00:00"),
                self.episode("allin")]
        before = {row["id"]: self.runtime.poc.episode_path(row["id"]).read_bytes() for row in rows}
        payload = self.runtime.podcast_payload()
        states = {row["id"]: row["transcript_acquisition"]["state"] for row in payload["episodes"]}
        self.assertEqual(states, {"dwarkesh-latest": "pending", "dwarkesh-old": "archive", "allin-latest": "unsupported"})
        self.assertFalse(payload["automation"]["automatic_audio_transcription"])
        for content_id, content in before.items():
            self.assertEqual(self.runtime.poc.episode_path(content_id).read_bytes(), content)

    def test_official_fetch_rejects_hosts_credentials_ports_and_unapproved_paths_before_network(self):
        invalid = [
            ("dwarkesh", "http://www.dwarkesh.com/p/test"),
            ("dwarkesh", "https://127.0.0.1/p/test"),
            ("dwarkesh", "https://www.dwarkesh.com.evil.example/p/test"),
            ("dwarkesh", "https://user:password@www.dwarkesh.com/p/test"),
            ("dwarkesh", "https://www.dwarkesh.com:443/p/test"),
            ("dwarkesh", "https://www.dwarkesh.com/p/../admin"),
            ("dwarkesh", "https://www.dwarkesh.com/p/%2e%2e/admin"),
            ("dwarkesh", "https://www.dwarkesh.com/api/private"),
            ("lex", "https://lexfridman.com/wp-admin/index.php"),
            ("diary", "https://rss.flightcast.com/transcripts/test.html"),
            ("diary", "https://rss.flightcast.com/other/test.vtt"),
            ("allin", "https://www.allin.com/test-transcript"),
        ]
        with patch.object(reading_service, "build_opener", side_effect=AssertionError("Forbidden URL reached network")):
            for source, url in invalid:
                with self.subTest(source=source, url=url), self.assertRaises(ValueError):
                    self.runtime.fetch_official_transcript(source, url)

    def test_official_fetch_strips_tracking_and_does_not_follow_redirects(self):
        requested = []
        class RedirectingHTTPS(BaseHandler):
            handler_order = 100
            def https_open(self, request):
                requested.append(request.full_url)
                headers = Message()
                headers["Location"] = "https://127.0.0.1/private"
                response = addinfourl(io.BytesIO(b"redirect"), headers, request.full_url, code=302)
                response.msg = "Found"
                return response
        def opener_with_fake_network(*handlers):
            return build_opener(RedirectingHTTPS(), *handlers)
        with patch.object(reading_service, "build_opener", side_effect=opener_with_fake_network):
            with self.assertRaises(HTTPError) as caught:
                self.runtime.fetch_official_transcript("dwarkesh", "https://www.dwarkesh.com/p/test?utm_source=rss#read")
        self.assertEqual(caught.exception.code, 302)
        self.assertEqual(requested, ["https://www.dwarkesh.com/p/test"])

    def test_official_fetch_enforces_stream_size_and_deadline(self):
        opener = MagicMock()
        opener.open.return_value = io.BytesIO(b"x" * 4_000_001)
        with patch.object(reading_service, "build_opener", return_value=opener):
            with self.assertRaisesRegex(ValueError, "size limit"):
                self.runtime.fetch_official_transcript("dwarkesh", "https://www.dwarkesh.com/p/test")
        opener.open.return_value = io.BytesIO(b"within limit")
        with patch.object(reading_service, "build_opener", return_value=opener), \
             patch.object(reading_service.time, "monotonic", side_effect=[0, 41]):
            with self.assertRaises(TimeoutError):
                self.runtime.fetch_official_transcript("dwarkesh", "https://www.dwarkesh.com/p/test")

    def test_official_fetch_cannot_start_during_peak(self):
        self.moment = datetime.fromisoformat("2026-10-13T10:00:00+08:00")
        with patch.object(reading_service, "build_opener", side_effect=AssertionError("No network at peak")):
            with self.assertRaises(reading_service.schedule_policy.OffPeakDeferred):
                self.runtime.fetch_official_transcript("dwarkesh", "https://www.dwarkesh.com/p/test")

    def test_diary_discovery_adds_official_vtt_only_to_metadata_preserving_existing_text(self):
        def rss_item(guid, transcript):
            return (f'<item><guid>{guid}</guid><title>Synthetic guest interview</title>'
                    '<pubDate>Tue, 13 Oct 2026 00:00:00 +0000</pubDate>'
                    '<enclosure url="https://example.org/audio.mp3" type="audio/mpeg"/>'
                    f'<podcast:transcript type="text/vtt" url="{transcript}"/></item>')
        transcript_url = "https://rss.flightcast.com/transcripts/NEW_123.vtt"
        guids = ("existing-metadata", "existing-reviewed", "untrusted-caption")
        ids = ["diary-" + hashlib.sha256(guid.encode()).hexdigest()[:12] for guid in guids]
        metadata = self.episode("diary")
        self.runtime.poc.episode_path(metadata["id"]).unlink()
        metadata.update(id=ids[0], guid=guids[0], transcript_acquisition={"state": "unavailable"})
        metadata.pop("official_transcript_url")
        self.runtime.poc.save_episode(metadata)
        reviewed = {**metadata, **parsed_transcript(), "id": ids[1], "guid": guids[1], "status": "summarized",
                    "quality_review": {"status": "assistant_reviewed"}, "official_transcript_url": "https://rss.flightcast.com/transcripts/OLD.vtt"}
        self.runtime.poc.save_episode(reviewed)
        before = self.runtime.poc.episode_path(ids[1]).read_bytes()
        body = ('<rss xmlns:podcast="https://podcastindex.org/namespace/1.0"><channel>'
                + rss_item(guids[0], transcript_url) + rss_item(guids[1], transcript_url)
                + rss_item(guids[2], "https://evil.example/transcript.vtt") + '</channel></rss>').encode()
        with patch.object(self.runtime.poc, "fetch", return_value=body), redirect_stdout(io.StringIO()):
            result = self.runtime.poc.discover(["diary"], limit=3)
        self.assertTrue(result[0]["success"])
        upgraded = self.runtime.poc.load_episode(ids[0])
        self.assertEqual(upgraded["official_transcript_url"], transcript_url)
        self.assertNotIn("transcript_acquisition", upgraded)
        self.assertEqual(upgraded["segments"], [])
        self.assertEqual(upgraded["source_kind"], "rss_metadata")
        self.assertEqual(self.runtime.poc.episode_path(ids[1]).read_bytes(), before)
        self.assertNotIn("official_transcript_url", self.runtime.poc.load_episode(ids[2]))
        self.assertEqual(self.runtime.engine.snapshot(), [])


if __name__ == "__main__":
    unittest.main()
