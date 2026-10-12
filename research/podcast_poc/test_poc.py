import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research.podcast_poc import poc


class PodcastCoverageTests(unittest.TestCase):
    def test_serialized_chunk_bound_preserves_escaping_and_oversized_turns(self):
        segments = [{"id": "s1", "text": '\\"\n' * 6000},
                    {"id": "s2", "text": "末尾证据" * 20}]
        def prompt(chunk):
            return json.dumps(chunk, ensure_ascii=False)
        chunks = poc.bounded_summary_chunks(segments, prompt, maximum=2000)
        for original in segments:
            joined = "".join(s["text"] for chunk in chunks for s in chunk if s["id"] == original["id"])
            self.assertEqual(joined, original["text"])
        self.assertTrue(all(poc.serialized_prompt_chars(prompt(c)) <= 2000 for c in chunks))

    def test_chunking_preserves_every_character_including_oversized_turn(self):
        segments = [{"id": "s1", "text": "起点" * 11}, {"id": "s2", "text": "中间重要的相反观点" * 50},
                    {"id": "s3", "text": "结尾" * 17}]
        chunks = poc.chunk_segments(segments, max_chars=37)
        flattened = [segment for chunk in chunks for segment in chunk]
        for original in segments:
            actual = "".join(s["text"] for s in flattened if s["id"] == original["id"])
            self.assertEqual(actual, original["text"])
        self.assertTrue(all(sum(len(s["text"]) for s in c) <= 37 for c in chunks))

    def test_unsourced_summary_is_rejected(self):
        for refs in ([], ["s9999"], [None]):
            with self.assertRaises(ValueError):
                poc.validate_summary({"overview": "这是观点", "points": [{"text": "观点", "segment_ids": refs}]}, {"s0001"})

    def test_changed_transcript_invalidates_but_repeated_import_preserves_result(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(poc, "DATA", Path(directory)):
            source = Path(directory) / "source.json"
            material = {"id": "test", "source_url": "https://example.org/p/test", "segments": [{"text": "原话", "start_seconds": 0}]}
            source.write_text(json.dumps(material))
            episode = poc.import_transcript(source)
            episode.update({"status": "summarized", "summary": {"overview": "已有摘要"}})
            poc.save_episode(episode)
            self.assertEqual(poc.import_transcript(source)["status"], "summarized")
            material["segments"][0]["text"] = "修订后的相反观点"
            source.write_text(json.dumps(material))
            changed = poc.import_transcript(source)
            self.assertEqual(changed["status"], "transcribed")
            self.assertNotIn("summary", changed)
            self.assertEqual(changed["coverage"]["segments_processed"], 0)

    def test_parser_excludes_chapter_markers_and_preserves_turns(self):
        html = '<h1>访谈</h1><div class="body markup"><p>简介不是全文</p><h2>Transcript</h2>'
        for i in range(25):
            html += f'<h3>00:00:{i:02d} – Chapter</h3><p>Speaker 00:00:{i:02d}</p><p>真实段落{i}</p>'
        html += '</div><p>站外推荐内容</p>'
        with tempfile.TemporaryDirectory() as directory, patch.object(poc, "DATA", Path(directory)), patch.object(poc, "fetch", return_value=html.encode()):
            episode = poc.acquire_official("https://example.org/p/test", "test", "Test")
            self.assertEqual(len(episode["segments"]), 25)
            self.assertEqual([s["text"] for s in episode["segments"]], [f"真实段落{i}" for i in range(25)])

    def test_descriptions_without_transcript_heading_fail_closed(self):
        html = '<div class="body markup"><h2>Show notes</h2><p>仅节目简介。</p></div>'
        with patch.object(poc, "fetch", return_value=html.encode()), self.assertRaises(ValueError):
            poc.acquire_official("https://example.org/p/test", "test", "Test")

    def test_parser_supports_timestamp_first_speaker_turns(self):
        html = '<div class="body markup"><h2>Transcript</h2>'
        for i in range(25):
            html += f'<p>[00:00:{i:02d}] Speaker: text {i}</p>'
        html += '</div>'
        with tempfile.TemporaryDirectory() as directory, patch.object(poc, "DATA", Path(directory)), patch.object(poc, "fetch", return_value=html.encode()):
            episode = poc.acquire_official("https://example.org/p/test", "test", "Test")
            self.assertEqual(len(episode["segments"]), 25)
            self.assertEqual(episode["segments"][12]["text"], "text 12")

    def test_english_points_are_not_a_chinese_summary(self):
        with self.assertRaises(ValueError):
            poc.validate_summary({"overview": "中文总览", "points": [{"text": "English only", "segment_ids": ["s1"]}]}, {"s1"})

    def test_episode_id_cannot_escape_data_directory(self):
        with self.assertRaises(ValueError):
            poc.episode_path("../../secret")

    def test_json_recovery_only_accepts_single_stray_quote(self):
        self.assertEqual(poc.parse_json('{"ok":true}"'), {"ok": True})
        for text in ('{"ok":true}{"other":false}', '{"ok":true} added claim', '{"ok":'):
            with self.assertRaises(ValueError):
                poc.parse_json(text)

    def test_review_keeps_model_draft_and_rejects_stale_input(self):
        draft = {"overview": "自动草稿", "points": [{"text": "原始观点", "segment_ids": ["s1"]}]}
        corrected = {"overview": "复核版本", "points": [{"text": "限定范围后的观点", "segment_ids": ["s1"]}]}
        with tempfile.TemporaryDirectory() as directory, patch.object(poc, "DATA", Path(directory)):
            poc.save_episode({"id": "test", "status": "summarized", "summary_signature": "current", "summary": draft,
                              "segments": [{"id": "s1", "text": "原话"}]})
            path = Path(directory) / "review.json"
            poc.write_json(path, {"summary_signature": "old", "summary": corrected, "issues": ["范围过强"]})
            with self.assertRaises(ValueError):
                poc.apply_review("test", path)
            poc.write_json(path, {"summary_signature": "current", "summary": corrected, "issues": ["范围过强"]})
            episode = poc.apply_review("test", path)
            self.assertEqual(episode["model_draft_summary"], draft)
            self.assertEqual(episode["summary"], corrected)
            self.assertEqual(episode["quality_review"]["status"], "assistant_reviewed")


if __name__ == "__main__":
    unittest.main()
