"""Fixed historical picks share the existing bounded official-reader pipeline."""
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta
import io
import tempfile
import unittest
from unittest.mock import patch

from kol_dashboard import reading_service
from kol_dashboard.tests.test_podcast_automation import parsed_transcript
from research.podcast_poc import curated_podcasts


class CuratedPodcastTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.moment = datetime.fromisoformat("2026-10-14T20:00:00+08:00")
        self.runtime = reading_service.ReadingRuntime(directory.name, clock=lambda: self.moment)
        self.runtime.publish_configuration(model="deepseek-v4-flash", enabled=True)
        self.entries = curated_podcasts.catalog()

    def seed(self, index=0, **changes):
        episode = curated_podcasts.metadata_episode(self.entries[index], "2026-10-14T12:00:00+00:00")
        episode.update(changes)
        self.runtime.poc.save_episode(episode)
        return episode

    def fetches(self):
        return patch.object(self.runtime, "fetch_official_transcript", return_value="synthetic official body")

    def parse(self):
        return patch.object(reading_service.official_transcripts, "parse_transcript", return_value=parsed_transcript())

    @staticmethod
    def target(source, url, _body):
        return url.rstrip("/") + "-transcript" if source == "lex" else url

    def test_catalog_has_only_three_explicit_public_historical_picks(self):
        self.assertEqual(len(self.entries), 3)
        self.assertEqual({guest for entry in self.entries for guest in entry["guest_ids"]},
                         {"ray-dalio", "elon-musk", "jensen-huang"})
        self.assertEqual({entry["published_at"][:10] for entry in self.entries},
                         {"2018-04-08", "2023-11-09", "2026-04-15"})
        self.assertEqual([entry["priority"] for entry in self.entries], [1, 2, 3])
        for entry in self.entries:
            self.assertNotIn("segments", entry)
            self.assertNotIn("summary", entry)
            self.assertLess(self.runtime.poc.published_timestamp(entry["published_at"]), self.moment.timestamp())
        self.assertEqual(self.entries[0]["published_at"], "2018-04-08")
        self.assertEqual(self.entries[0]["published_precision"], "date")
        self.assertIsNone(self.runtime.poc.published_timestamp("2018-04-08T12:00:00"))

    def test_ensure_is_idempotent_and_does_not_fetch_or_queue_metadata(self):
        with patch.object(self.runtime, "fetch_official_transcript", side_effect=AssertionError("Metadata only")):
            with self.runtime.poc.cli_work_guard():
                first = self.runtime.ensure_curated_podcasts()
            before = {path.name: path.read_bytes() for path in (self.runtime.data / "episodes").glob("*.json")}
            with self.runtime.poc.cli_work_guard():
                second = self.runtime.ensure_curated_podcasts()
        self.assertEqual(first["created"], 3)
        self.assertEqual(second["created"], 0)
        self.assertEqual(second["tagged"], 0)
        self.assertEqual(first["episode_ids"], second["episode_ids"])
        self.assertEqual(before, {path.name: path.read_bytes() for path in (self.runtime.data / "episodes").glob("*.json")})
        self.assertEqual(self.runtime.engine.snapshot(), [])

    def test_existing_reviewed_rss_record_only_gains_curation(self):
        old = self.seed(1, id="lex-existing-guid", source_url=self.entries[1]["source_url"] + "?utm_source=rss#read",
                        published_at="Thu, 09 Nov 2023 10:00:00 +0000", guid="original-rss-guid",
                        feed_url=self.runtime.poc.FEEDS["lex"][2], **parsed_transcript())
        old.pop("curation")
        old.update(status="summarized", summary={"overview": "已复核内容", "points": [{"text": "有证据的观点", "segment_ids": ["s0001"]}]},
                   summary_signature="preserved", quality_review={"status": "assistant_reviewed"},
                   model_draft_summary={"overview": "保留模型稿"}, summary_history=[{"summary": "历史稿"}])
        self.runtime.poc.save_episode(old)
        with self.runtime.poc.cli_work_guard():
            result = self.runtime.ensure_curated_podcasts()
        saved = self.runtime.poc.load_episode(old["id"])
        self.assertEqual(saved.pop("curation"), curated_podcasts.curation(self.entries[1]))
        self.assertEqual(saved, old)
        self.assertIn(old["id"], result["episode_ids"])
        self.assertFalse(self.runtime.poc.episode_path(self.entries[1]["id"]).exists())
        self.assertEqual(self.runtime.engine.snapshot(), [])

    def test_existing_unanalyzed_text_is_queued_once_as_automatic(self):
        old = self.seed(2, id="dwarkesh-existing", **parsed_transcript())
        old.pop("curation")
        self.runtime.poc.save_episode(old)
        for _ in range(2):
            with self.runtime.poc.cli_work_guard():
                self.runtime.ensure_curated_podcasts()
        jobs = self.runtime.engine.snapshot()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["article_id"], old["id"])
        self.assertFalse(jobs[0]["manual"])
        self.assertFalse(jobs[0]["on_demand"])

    def test_get_does_not_seed_fetch_write_or_grant_a_private_curation_flag(self):
        forged = self.seed(0, id="unselected-history", source_url="https://freakonomics.com/podcast/not-selected/")
        self.runtime.engine.snapshot()
        before = {str(path.relative_to(self.runtime.data)): path.read_bytes()
                  for path in self.runtime.data.rglob("*") if path.is_file()}
        with patch.object(self.runtime, "ensure_curated_podcasts", side_effect=AssertionError("No GET mutation")), \
             patch.object(self.runtime.poc, "save_episode", side_effect=AssertionError("No GET mutation")), \
             patch.object(self.runtime, "fetch_official_transcript", side_effect=AssertionError("No GET fetch")):
            result = self.runtime.podcast_payload()
        self.assertEqual(len(result["episodes"]), 1)
        episode = result["episodes"][0]
        self.assertEqual(episode["id"], forged["id"])
        self.assertNotIn("curation", episode)
        self.assertEqual(episode["transcript_acquisition"]["state"], "unsupported")
        self.assertEqual(result["curated_collection"]["episode_count"], 0)
        self.assertNotIn("freakonomics", {source["id"] for source in result["sources"]})
        self.assertEqual(result["automation"]["transcript_policy"], "official_latest_and_curated")
        self.assertEqual(result["discovery"]["automatic_scope"], "latest_one_per_source_and_curated")
        self.assertEqual(before, {str(path.relative_to(self.runtime.data)): path.read_bytes()
                                 for path in self.runtime.data.rglob("*") if path.is_file()})

    def test_curated_priority_shares_three_daily_slots_then_latest_resumes_next_day(self):
        with self.runtime.poc.cli_work_guard():
            self.runtime.ensure_curated_podcasts()
        latest = []
        for index, source in enumerate(("dwarkesh", "lex")):
            item = self.seed(index + 1, id=source + "-new", source_id=source,
                source_url="https://www.dwarkesh.com/p/new" if source == "dwarkesh" else "https://lexfridman.com/new/",
                published_at="2026-10-14T00:00:00+00:00")
            latest.append(item)
        old = self.seed(1, id="lex-ordinary-archive", source_url="https://lexfridman.com/old/",
                        published_at="2020-01-01T00:00:00+00:00")
        with self.fetches() as fetch, self.parse(), \
             patch.object(reading_service.official_transcripts, "find_transcript_url", side_effect=self.target):
            self.runtime.acquire_podcast_transcripts()
            self.runtime.acquire_podcast_transcripts()
        self.assertEqual(fetch.call_count, 4)  # Lex's page and actual transcript.
        self.assertEqual({job["article_id"] for job in self.runtime.engine.snapshot()},
                         {entry["id"] for entry in self.entries})
        self.assertEqual(self.runtime.engine._runtime_value("podcast_daily_reservations")["ids"],
                         [entry["id"] for entry in self.entries])
        self.assertTrue(all(not self.runtime.poc.load_episode(row["id"])["segments"] for row in latest + [old]))
        self.moment += timedelta(days=1)
        with self.fetches() as fetch, self.parse(), \
             patch.object(reading_service.official_transcripts, "find_transcript_url", side_effect=self.target):
            self.runtime.acquire_podcast_transcripts()
        self.assertEqual(fetch.call_count, 3)
        self.assertTrue(all(self.runtime.poc.load_episode(row["id"])["segments"] for row in latest))
        self.assertEqual(self.runtime.poc.load_episode(old["id"])["segments"], [])

    def test_curated_failure_backs_off_and_ready_text_recovers_without_refetch(self):
        selected = self.seed()
        with patch.object(self.runtime, "fetch_official_transcript", side_effect=ValueError("bad publisher body")):
            self.runtime.acquire_podcast_transcripts()
        self.moment += timedelta(hours=23)
        with patch.object(self.runtime, "fetch_official_transcript", side_effect=AssertionError("Backoff")):
            self.runtime.acquire_podcast_transcripts()
        self.moment += timedelta(hours=1)
        with self.fetches(), self.parse(), patch.object(self.runtime.engine, "enqueue", side_effect=RuntimeError("Queue interrupted")):
            with self.assertRaises(RuntimeError):
                self.runtime.acquire_podcast_transcripts()
        self.assertEqual(self.runtime.poc.load_episode(selected["id"])["transcript_acquisition"]["state"], "ready")
        with patch.object(self.runtime, "fetch_official_transcript", side_effect=AssertionError("Saved text")):
            self.runtime.acquire_podcast_transcripts()
        self.assertEqual(len(self.runtime.engine.snapshot()), 1)

    def test_curated_fetch_still_defers_at_peak_without_consuming_allowance(self):
        selected = self.seed()
        self.moment = datetime.fromisoformat("2026-10-14T10:00:00+08:00")
        before = self.runtime.poc.episode_path(selected["id"]).read_bytes()
        with patch.object(self.runtime, "fetch_official_transcript", side_effect=AssertionError("Peak")):
            with self.assertRaises(reading_service.schedule_policy.OffPeakDeferred):
                self.runtime.acquire_podcast_transcripts()
        self.assertEqual(self.runtime.poc.episode_path(selected["id"]).read_bytes(), before)
        self.assertIsNone(self.runtime.engine._runtime_value("podcast_daily_reservations"))

    def test_freakonomics_whitelist_allows_only_the_fixed_episode(self):
        valid = self.entries[0]["source_url"]
        self.assertEqual(self.runtime._official_url("freakonomics", valid + "?tracking=yes#transcript"), valid)
        bad_urls = [valid.replace("https:", "http:"), valid.replace("freakonomics.com", "freakonomics.com.evil.example"),
                    valid.replace("freakonomics.com", "user@freakonomics.com"), valid.replace("freakonomics.com", "freakonomics.com:443"),
                    "https://freakonomics.com/podcast/not-selected/"]
        with patch.object(reading_service, "build_opener", side_effect=AssertionError("Never request an unselected URL")):
            for url in bad_urls:
                with self.subTest(url=url), self.assertRaises(ValueError):
                    self.runtime.fetch_official_transcript("freakonomics", url)
        forged = self.seed(0, source_url=bad_urls[-1])
        with patch.object(self.runtime, "fetch_official_transcript", side_effect=AssertionError("Flag is not permission")):
            self.runtime.acquire_podcast_transcripts()
        self.assertEqual(self.runtime.poc.load_episode(forged["id"])["segments"], [])

    def test_rss_discovery_reuses_curated_identity_and_preserves_existing_analysis(self):
        selected = self.seed(1, **parsed_transcript(), summary={"overview": "保留稿"},
                             quality_review={"status": "assistant_reviewed"}, summary_history=[{"old": True}])
        before = self.runtime.poc.episode_path(selected["id"]).read_bytes()
        xml = ('<rss><channel><item><title>RSS title</title><guid>original-rss-guid</guid>'
               '<pubDate>Thu, 09 Nov 2023 10:00:00 +0000</pubDate>'
               '<link>https://lexfridman.com/elon-musk-4?utm_source=rss</link>'
               '<enclosure type="audio/mpeg" url="https://media.example.org/episode.mp3" />'
               '</item></channel></rss>')
        with patch.object(self.runtime.poc, "fetch", return_value=xml.encode()), redirect_stdout(io.StringIO()):
            result = self.runtime.poc.discover(["lex"], limit=3)
        self.assertTrue(result[0]["success"])
        self.assertEqual(len(list((self.runtime.data / "episodes").glob("*.json"))), 1)
        self.assertEqual(self.runtime.poc.episode_path(selected["id"]).read_bytes(), before)

    def test_verified_rss_guid_alias_uses_fixed_source_url_instead_of_mutable_url(self):
        catalog = deepcopy(self.entries)
        catalog[0]["rss_guids"] = ["verified-guid"]
        old = self.seed(0, source_url="https://untrusted.example/", guid="verified-guid")
        with patch.object(curated_podcasts, "catalog", return_value=catalog), self.fetches() as fetch, self.parse():
            self.runtime.acquire_podcast_transcripts()
        self.assertEqual(fetch.call_args.args, ("freakonomics", catalog[0]["source_url"]))
        self.assertTrue(self.runtime.poc.load_episode(old["id"])["segments"])

    def test_already_duplicated_curated_alias_is_not_selected_again_as_latest(self):
        first = self.seed(1, id="lex-a")
        self.seed(1, id="lex-b", source_url=self.entries[1]["source_url"] + "?alias=1")
        with self.fetches() as fetch, self.parse(), \
             patch.object(reading_service.official_transcripts, "find_transcript_url", side_effect=self.target):
            self.runtime.acquire_podcast_transcripts()
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual([job["article_id"] for job in self.runtime.engine.snapshot()], [first["id"]])


if __name__ == "__main__":
    unittest.main()
