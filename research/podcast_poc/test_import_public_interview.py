"""Synthetic-source checks; no third-party transcript text is checked in."""
import hashlib
import json
from pathlib import Path
import stat
import tarfile
import tempfile
import unittest

from kol_dashboard.reading_import import import_archive
from research.podcast_poc import import_public_interview as adapter


TRANSCRIPT = """# Synthetic interview
Introduction is metadata, not speech.
## 00:00:00–00:00:30
> Speaker attribution is unverified.
[00:00:00] Host: First synthetic turn.

Continuation belongs to the same turn.
[00:00:29] Guest: A retained overlapping turn.
## 00:00:20–00:00:50
> Exclude this source block.
[00:00:20] Guest: Unreliable synthetic material.
[00:00:40] Host: Another excluded turn.
## 00:00:40–00:01:00
[00:00:40] Host: Retained original-order turn.
[00:00:57] Guest: Synthetic closing turn.
"""


def descriptor(text=TRANSCRIPT):
    return {
        "schema_version": 1, "adapter": "timestamped_interview_markdown_v1",
        "id": "synthetic", "show": "Synthetic", "title": "Synthetic source",
        "language": "zh", "published_at": None, "source_url": "https://example.org/original",
        "duration_seconds": 60, "duration_basis": "Transcription provider declaration",
        "published_at_note": "Actual publication timestamp is unverified", "transcript_provider": "synthetic",
        "transcript_url": "https://example.org/transcript", "transcript_commit": "fixed-version",
        "transcript_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "transcription_method": "Unverified synthetic fixture", "rights_notice": "Fixture only",
        "license_url": "https://example.org/license", "warnings": ["Unverified transcription"],
        "coverage_note": "Partial third-party transcript; excluded questionable block.",
        "expected_structure": {"turns": 6, "blocks": 3, "continuations": 1,
                               "timestamp_backtracks": 1, "last_timestamp_seconds": 57},
        "excluded_blocks": [{"start_seconds": 20, "reason": "Unreliable block"}],
    }


class PublicInterviewAdapterTests(unittest.TestCase):
    def test_preserves_original_evidence_and_excludes_entire_flagged_block(self):
        episode = adapter.build_episode(descriptor(), TRANSCRIPT.encode())
        self.assertEqual(episode["status"], "transcribed")
        self.assertEqual(episode["source_kind"], "third_party_transcript")
        self.assertEqual(episode["coverage"]["scope"], "partial")
        self.assertEqual(episode["coverage"]["end_seconds"], 57)
        self.assertIsNone(episode["published_at"])
        self.assertEqual(episode["duration_basis"], "Transcription provider declaration")
        self.assertEqual(episode["published_at_note"], "Actual publication timestamp is unverified")
        self.assertEqual([row["id"] for row in episode["segments"]], ["s0001", "s0002", "s0005", "s0006"])
        combined = "\n".join(row["text"] for row in episode["segments"])
        self.assertIn("Continuation belongs to the same turn.", combined)
        self.assertNotIn("Unreliable synthetic material", combined)
        self.assertNotIn("Introduction is metadata", combined)
        self.assertEqual(episode["segments"][0]["source_notes"], ["Speaker attribution is unverified."])
        self.assertEqual(episode["transcript_provenance"]["excluded_regions"][0]["excluded_segments"], 2)
        self.assertEqual(episode["processing"]["paid_api_calls"], 0)
        self.assertNotIn("summary", episode)

    def test_keeps_source_order_and_overlaps_without_false_negative_durations(self):
        turns, _, stats = adapter.parse_transcript(TRANSCRIPT, duration_seconds=60)
        self.assertEqual([row["start_seconds"] for row in turns], [0, 29, 20, 40, 40, 57])
        self.assertEqual(stats["timestamp_backtracks"], 1)
        self.assertTrue(all(row["end_seconds"] >= row["start_seconds"] for row in turns))

    def test_unreviewed_changed_transcript_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "checksum"):
            adapter.build_episode(descriptor(), (TRANSCRIPT + "An unreviewed addition").encode())
        source = descriptor()
        source["expected_structure"]["turns"] = 999
        with self.assertRaisesRegex(ValueError, "structure"):
            adapter.build_episode(source, TRANSCRIPT.encode())

    def test_summary_or_incomplete_recording_cannot_masquerade_as_full_transcript(self):
        invalid = ["# Episode summary\nOnly show notes.",
                   TRANSCRIPT.replace("## 00:00:20–00:00:50", "## 00:00:35–00:00:50"),
                   TRANSCRIPT.replace("[00:00:57]", "[00:00:99]"),
                   TRANSCRIPT.replace("## 00:00:40–00:01:00", "## 00:00:40–00:00:59"),
                   TRANSCRIPT.replace("[00:00:00]", "[00:00:01]"),
                   TRANSCRIPT.replace("[00:00:57]", "[malformed timestamp]")]
        for text in invalid:
            with self.subTest(text=text[:30]), self.assertRaises(ValueError):
                adapter.parse_transcript(text, duration_seconds=60)

    def test_missing_exclusion_cannot_silently_keep_questionable_text(self):
        source = descriptor()
        source["excluded_blocks"] = [{"start_seconds": 999, "reason": "missing"}]
        with self.assertRaisesRegex(ValueError, "excluded source block"):
            adapter.build_episode(source, TRANSCRIPT.encode())

    def test_archive_matches_production_import_and_never_overwrites_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "seed.tgz"
            episode = adapter.build_episode(descriptor(), TRANSCRIPT.encode())
            adapter.write_seed(episode, output)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
            with tarfile.open(output) as archive:
                self.assertEqual(archive.getnames(), ["episodes/synthetic.json"])
                self.assertEqual(archive.getmembers()[0].mode, 0o600)
            target = root / "private-reading"
            self.assertEqual(import_archive(output, target)["imported"], 1)
            record = target / "episodes/synthetic.json"
            self.assertEqual(json.loads(record.read_text()), episode)
            record.write_text('{"existing":"preserve"}')
            self.assertEqual(import_archive(output, target)["skipped"], 1)
            self.assertEqual(record.read_text(), '{"existing":"preserve"}')
            with self.assertRaises(FileExistsError):
                adapter.write_seed(episode, output)

    def test_private_archive_cannot_be_written_under_checkout(self):
        episode = adapter.build_episode(descriptor(), TRANSCRIPT.encode())
        with self.assertRaisesRegex(ValueError, "outside the checkout"):
            adapter.write_seed(episode, adapter.CHECKOUT / "never-write-transcript.tgz")

    def test_source_descriptor_contains_metadata_only_and_retains_rights_boundary(self):
        source_path = Path(__file__).with_name("sources") / "alan-justinsun-20261010.json"
        source = json.loads(source_path.read_text())
        self.assertNotIn("segments", source)
        self.assertNotIn("summary", source)
        self.assertIsNone(source["published_at"])
        self.assertIn("不适用于逐字稿", source["rights_notice"])
        self.assertEqual(source["excluded_blocks"][0]["start_seconds"], 1680)
        self.assertEqual(source["expected_structure"]["turns"], 1743)


if __name__ == "__main__":
    unittest.main()
