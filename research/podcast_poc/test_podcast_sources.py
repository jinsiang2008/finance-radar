"""Subscription discovery contracts, with public-feed fixtures and no network."""
from contextlib import redirect_stdout
from datetime import datetime
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

from research.podcast_poc import poc, schedule_policy


ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"


def item(guid, *, title=None, published="Tue, 13 Oct 2026 12:00:00 +0000", kind=None,
         audio=True):
    value = ET.Element("item")
    ET.SubElement(value, "guid").text = guid
    ET.SubElement(value, "title").text = title or "Conversation " + guid
    ET.SubElement(value, "pubDate").text = published
    ET.SubElement(value, "link").text = "https://example.org/episodes/" + guid
    ET.SubElement(value, "description").text = "Show notes are not a transcript."
    if audio:
        ET.SubElement(value, "enclosure", url="https://media.example.org/" + guid + ".mp3",
                      type="audio/mpeg")
    if kind:
        ET.SubElement(value, "{" + ITUNES + "}episodeType").text = kind
    return value


def rss(*items):
    root = ET.Element("rss", version="2.0")
    channel = ET.SubElement(root, "channel")
    for value in items:
        channel.append(value)
    return ET.tostring(root, encoding="utf-8")


def episode_id(key, guid):
    return key + "-" + hashlib.sha256(guid.encode()).hexdigest()[:12]


class PodcastSourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name)
        data_patch = patch.object(poc, "DATA", self.data)
        data_patch.start()
        self.addCleanup(data_patch.stop)
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def source(self, key):
        return next(value for value in poc.payload()["sources"] if value["id"] == key)

    def test_six_primary_creators_cover_seven_feeds_and_keep_other_subscriptions(self):
        payload = poc.payload()
        sources = payload["sources"]
        self.assertEqual(len(sources), 14)
        self.assertEqual(sum(source["language"] == "zh" for source in sources), 4)
        primary = [source for source in sources if source["priority"] == "primary"]
        self.assertEqual({source["id"] for source in primary},
                         {"dwarkesh", "lex", "allin", "nikhil_people", "nikhil_wtf", "jre", "diary"})
        self.assertEqual({source["group"] for source in primary},
                         {"dwarkesh", "lex", "allin", "nikhil", "jre", "diary"})
        self.assertEqual(payload["discovery"]["primary_group_count"], 6)
        self.assertEqual({source["id"] for source in sources
                          if source["priority"] == "standard" and source["language"] == "en"},
                         {"latent", "acquired", "iltb"})
        self.assertTrue(all(source["status"] == "pending" for source in sources))
        self.assertFalse(payload["discovery"]["automatic_transcript_fetch"])
        self.assertFalse(payload["discovery"]["automatic_audio_transcription"])

    def test_previous_feed_ids_and_urls_are_preserved_for_stored_episode_identity(self):
        expected = {
            "sv101": "https://feeds.fireside.fm/sv101/rss",
            "latepost": "https://podcast.latepost.com/rss",
            "luanfanshu": "https://feed.xyzfm.space/yxuruh3f9mc4",
            "xiaojun": "https://feed.xyzfm.space/dk4yh3pkpjp3",
            "latent": "https://api.substack.com/feed/podcast/1084089.rss",
            "dwarkesh": "https://apple.dwarkesh-podcast.workers.dev/feed.rss",
            "acquired": "https://feeds.transistor.fm/acquired",
            "iltb": "https://feeds.megaphone.fm/CLS2859450455",
        }
        for key, url in expected.items():
            self.assertEqual(poc.FEEDS[key][2], url, key)

    def test_new_subscriptions_use_verified_direct_publisher_feed_endpoints(self):
        expected = {
            "lex": "https://lexfridman.com/feed/podcast/",
            "allin": "https://rss.libsyn.com/shows/254861/destinations/1928300.xml",
            "nikhil_people": "https://feeds.hubhopper.com/db41907e6c28518ffe336b392c715ef1.rss",
            "nikhil_wtf": "https://feeds.hubhopper.com/664690fdea0d7a6f61a052da119934d3.rss",
            "jre": "https://feeds.megaphone.fm/GLT1412515089",
            "diary": "https://rss2.flightcast.com/xmsftuzjjykcmqwolaqn6mdn",
        }
        for key, url in expected.items():
            self.assertEqual(poc.FEEDS[key][2], url, key)
            parsed = urlparse(url)
            self.assertEqual(parsed.scheme, "https")
            self.assertIsNone(parsed.username)
            self.assertIsNone(parsed.password)

    def test_newest_three_are_selected_by_publication_not_feed_order_with_guid_dedup(self):
        material = rss(
            item("old", published="Fri, 09 Oct 2026 12:00:00 +0000"),
            item("third", published="Sun, 11 Oct 2026 12:00:00 +0000"),
            item("newest", published="Tue, 13 Oct 2026 12:00:00 +0000"),
            item("newest", published="Tue, 13 Oct 2026 12:00:00 +0000"),
            item("second", published="Mon, 12 Oct 2026 12:00:00 +0000"),
            item("undated", published="not-a-date"),
        )
        with patch.object(poc, "fetch", return_value=material):
            self.assertEqual(poc.discover(["lex"], limit=3)[0]["items"], 3)
            before = {path.name: path.read_bytes() for path in (self.data / "episodes").glob("*.json")}
            poc.discover(["lex"], limit=3)
        self.assertEqual({path.name: path.read_bytes() for path in (self.data / "episodes").glob("*.json")}, before)
        episodes = poc.payload()["episodes"]
        self.assertEqual([episode["guid"] for episode in episodes], ["newest", "second", "third"])
        self.assertEqual(episodes[0]["id"], episode_id("lex", "newest"))
        self.assertEqual(self.source("lex")["latest_published_at"], "Tue, 13 Oct 2026 12:00:00 +0000")

    def test_feed_descriptions_are_metadata_and_never_make_a_summary_or_transcript(self):
        with patch.object(poc, "fetch", return_value=rss(item("notes"))):
            poc.discover(["lex"], limit=3)
        episode = poc.payload()["episodes"][0]
        self.assertEqual(episode["status"], "discovered")
        self.assertEqual(episode["source_kind"], "rss_metadata")
        self.assertEqual(episode["coverage"]["scope"], "metadata_only")
        self.assertEqual(episode["segments"], [])
        self.assertNotIn("summary", episode)
        source = self.source("lex")
        self.assertEqual((source["discovered_count"], source["transcript_count"], source["summary_count"]), (1, 0, 0))
        with self.assertRaises(ValueError):
            poc.summarize(episode["id"])

    def test_primary_feed_skips_trailers_before_applying_episode_limit(self):
        material = rss(item("trailer-kind", title="Meet our next guest", kind="trailer"),
                       item("trailer-title", title="Official TRAILER: a new conversation"),
                       item("full", title="A full conversation", kind="full"))
        with patch.object(poc, "fetch", return_value=material):
            poc.discover(["lex"], limit=1)
        self.assertEqual([episode["guid"] for episode in poc.payload()["episodes"]], ["full"])

    def test_nikhil_actual_episode_trailer_is_excluded_and_trailer_business_interview_kept(self):
        material = rss(
            item("musk-trailer", title="Elon Musk x Nikhil Kamath | Ep. 16 Trailer | People by WTF"),
            item("company", title="Building a trailer business: the full conversation", kind="full"),
            item("actors", title="Trailer Park Boys: a full conversation", kind="full"),
        )
        with patch.object(poc, "fetch", return_value=material):
            poc.discover(["nikhil_people"], limit=3)
        self.assertEqual({episode["guid"] for episode in poc.payload()["episodes"]}, {"company", "actors"})

    def test_nikhil_excludes_labelled_hindi_duplicates_but_keeps_mixed_language_original(self):
        material = rss(item("dub-one", title="Elon Musk - Hindi Dubbed"),
                       item("dub-two", title="Elon Musk (Hindi Version)"),
                       item("dub-three", title="Elon Musk - Dubbed in Hindi"),
                       item("original", title="People by WTF: English and Hindi conversation"))
        with patch.object(poc, "fetch", return_value=material):
            poc.discover(["nikhil_people", "nikhil_wtf"], limit=3)
        episodes = poc.payload()["episodes"]
        self.assertEqual(len(episodes), 2)
        self.assertTrue(all(episode["guid"] == "original" for episode in episodes))
        self.assertEqual({episode["source_group"] for episode in episodes}, {"nikhil"})

    def test_nikhil_publisher_hindi_title_is_excluded_without_rejecting_short_quoted_term(self):
        dubbed = "Elon Musk: एक अलग बातचीत Nikhil Kamath के साथ | पूरा एपिसोड | People by WTF Ep. 16"
        original = "Elon Musk: A Different Conversation | People by WTF Ep. 16"
        quoted = "India's AI opportunities: why 'नम' matters"
        material = rss(item("publisher-dub", title=dubbed),
                       item("labelled-dub", title="Elon Musk (Hindi)"),
                       item("english-original", title=original), item("quoted-term", title=quoted))
        with patch.object(poc, "fetch", return_value=material):
            poc.discover(["nikhil_people"], limit=3)
        self.assertEqual({episode["title"] for episode in poc.payload()["episodes"]}, {original, quoted})

    def test_no_summary_value_is_counted_as_unprocessed_without_breaking_catalog(self):
        poc.save_episode({"id": "lex-no-summary", "show": "Lex Fridman Podcast", "status": "transcribed",
                          "source_kind": "official_transcript", "segments": [{"id": "s0001", "text": "Original"}],
                          "coverage": {"scope": "full"}, "summary": None})
        source = self.source("lex")
        self.assertEqual(source["transcript_count"], 1)
        self.assertEqual(source["summary_count"], 0)

    def test_legacy_records_gain_response_source_fields_without_rewriting_saved_work(self):
        summary = {"overview": "已有中文摘要", "points": [{"text": "保留证据", "segment_ids": ["s0001"]}]}
        records = [
            {"id": "legacy-feed", "feed_url": poc.FEEDS["lex"][2], "show": "Legacy title"},
            {"id": "dwarkesh-ilya-2", "show": "Imported interview"},
            {"id": "legacy-show", "show": poc.FEEDS["acquired"][0]},
        ]
        for record in records:
            record.update(status="summarized", source_kind="official_transcript", coverage={"scope": "full"},
                          segments=[{"id": "s0001", "text": "Unmodified original words"}], summary=summary,
                          quality_review={"status": "assistant_reviewed"})
            poc.save_episode(record)
        before = {path.name: path.read_bytes() for path in (self.data / "episodes").glob("*.json")}
        payload = poc.payload()
        self.assertEqual({episode["id"]: episode["source_id"] for episode in payload["episodes"]},
                         {"legacy-feed": "lex", "dwarkesh-ilya-2": "dwarkesh", "legacy-show": "acquired"})
        for episode in payload["episodes"]:
            self.assertEqual(episode["summary"], summary)
            self.assertEqual(episode["segments"][0]["text"], "Unmodified original words")
        self.assertEqual({path.name: path.read_bytes() for path in (self.data / "episodes").glob("*.json")}, before)

    def test_discovery_never_overwrites_existing_episode_transcript_or_review(self):
        value = {"id": episode_id("lex", "retained"), "status": "summarized", "source_kind": "official_transcript",
                 "segments": [{"id": "s0001", "text": "Original"}], "summary": {"overview": "已有摘要"},
                 "quality_review": {"status": "assistant_reviewed"}, "title": "Verified title"}
        poc.save_episode(value)
        before = poc.episode_path(value["id"]).read_bytes()
        with patch.object(poc, "fetch", return_value=rss(item("retained", title="Changed RSS title"))):
            poc.discover(["lex"], limit=3)
        self.assertEqual(poc.episode_path(value["id"]).read_bytes(), before)

    def test_source_health_handles_empty_failure_and_recovery_without_discarding_success(self):
        times = ["2026-10-13T10:00:00+00:00", "2026-10-13T11:00:00+00:00",
                 "2026-10-13T12:00:00+00:00", "2026-10-13T13:00:00+00:00"]
        with patch.object(poc, "now", return_value=times[0]), patch.object(poc, "fetch", return_value=rss(item("known"))):
            poc.discover(["lex"], limit=3)
        with patch.object(poc, "now", return_value=times[1]), patch.object(poc, "fetch", return_value=rss(item("no-audio", audio=False))):
            row = poc.discover(["lex"], limit=3)[0]
        self.assertEqual(row["status"], "empty")
        self.assertFalse(row["success"])
        self.assertEqual(row["last_success_at"], times[0])
        with patch.object(poc, "now", return_value=times[2]), patch.object(poc, "fetch", side_effect=OSError("feed unavailable")):
            row = poc.discover(["lex"], limit=3)[0]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["last_success_at"], times[0])
        self.assertEqual(self.source("lex")["discovered_count"], 1)
        with patch.object(poc, "now", return_value=times[3]), patch.object(poc, "fetch", return_value=rss(item("recovered"))):
            row = poc.discover(["lex"], limit=3)[0]
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["last_success_at"], times[3])
        self.assertIsNone(row["error"])
        self.assertEqual(self.source("lex")["discovered_count"], 2)

    def test_source_failure_does_not_prevent_other_subscriptions_from_updating(self):
        with patch.object(poc, "fetch", side_effect=[ValueError("malformed XML"), rss(item("available"))]):
            results = poc.discover(["lex", "dwarkesh"], limit=3)
        self.assertEqual([row["status"] for row in results], ["failed", "ok"])
        self.assertEqual(poc.payload()["episodes"][0]["source_id"], "dwarkesh")

    def test_peak_boundary_preserves_first_result_and_untouched_source_timestamp(self):
        old = "2026-10-12T12:00:00+00:00"
        with patch.object(poc, "now", return_value=old), patch.object(poc, "fetch", return_value=rss(item("old"))):
            poc.discover(["lex", "dwarkesh"], limit=3)
        previous = json.loads((self.data / "discovery.json").read_text())
        untouched = next(row for row in previous["results"] if row["feed"] == "dwarkesh")
        pause = schedule_policy.OffPeakDeferred(datetime.fromisoformat("2026-10-13T09:00:00+08:00"))
        current = "2026-10-13T00:59:59+00:00"
        with patch.object(poc, "now", return_value=current), \
             patch.object(poc, "fetch", side_effect=[rss(item("new")), pause]) as fetch:
            with self.assertRaises(schedule_policy.OffPeakDeferred):
                poc.discover(["lex", "dwarkesh", "diary"], limit=3)
        self.assertEqual(fetch.call_count, 2)
        stored = {row["feed"]: row for row in json.loads((self.data / "discovery.json").read_text())["results"]}
        self.assertEqual(stored["lex"]["last_checked_at"], current)
        self.assertEqual(stored["dwarkesh"], untouched)
        self.assertNotIn("diary", stored)
        self.assertTrue(poc.episode_path(episode_id("lex", "new")).exists())

    def test_unknown_names_are_rejected_before_any_network_even_after_valid_name(self):
        with patch.object(poc, "fetch") as fetch:
            for names in (["https://127.0.0.1/secret"], ["lex", "missing"]):
                with self.assertRaises(ValueError):
                    poc.discover(names, limit=3)
        fetch.assert_not_called()

    def test_invalid_discovery_limit_cannot_start_network_or_create_episodes(self):
        with patch.object(poc, "fetch") as fetch:
            for limit in (0, -1, "3", None):
                with self.assertRaises(ValueError):
                    poc.discover(["lex"], limit=limit)
        fetch.assert_not_called()
        self.assertFalse((self.data / "episodes").exists())


if __name__ == "__main__":
    unittest.main()
