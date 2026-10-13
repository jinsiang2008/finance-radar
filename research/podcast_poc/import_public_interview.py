"""Build a private reading seed from a pinned, locally supplied public transcript.

This adapter performs no network requests, inference, or production writes. The
source descriptor is public metadata; transcript bytes and the resulting archive
must remain outside the checkout. The release importer skips existing records.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tarfile


MAX_SOURCE_BYTES = 2 * 1024 * 1024
CHECKOUT = Path(__file__).resolve().parents[2]
TURN = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\]\s*([^：:]+)[：:]\s*(.+)$")
BLOCK = re.compile(r"^## (\d{2}:\d{2}:\d{2})[–-](\d{2}:\d{2}:\d{2})(?:（[^）]+）)?$")


def seconds(value):
    hours, minutes, secs = map(int, value.split(":"))
    if minutes >= 60 or secs >= 60:
        raise ValueError("Invalid transcript timestamp")
    return hours * 3600 + minutes * 60 + secs


def parse_transcript(text, *, duration_seconds):
    """Preserve source order, text, continuation paragraphs and block warnings."""
    blocks, turns = [], []
    block = None
    continuations = 0
    for line_number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        heading = BLOCK.fullmatch(line)
        if heading:
            start, end = seconds(heading[1]), seconds(heading[2])
            if not 0 <= start < end <= duration_seconds:
                raise ValueError("Transcript block is outside the declared duration")
            if blocks and (start <= blocks[-1]["start_seconds"] or start > blocks[-1]["end_seconds"]):
                raise ValueError("Transcript blocks are out of order or have a coverage gap")
            block = {"start_seconds": start, "end_seconds": end, "notes": []}
            blocks.append(block)
            continue
        match = TURN.fullmatch(line)
        if match:
            if block is None:
                raise ValueError("Transcript turn has no coverage block")
            start = seconds(match[1])
            # The pinned source contains one turn two seconds past its block's
            # nominal end. Preserve that timestamp; never normalize it silently.
            if not block["start_seconds"] <= start <= min(duration_seconds, block["end_seconds"] + 3):
                raise ValueError("Transcript turn is outside its source block")
            turns.append({"id": f"s{len(turns) + 1:04d}", "start_seconds": start,
                          "speaker": match[2].strip(), "text": match[3].strip(),
                          "source_line": line_number, "source_block": len(blocks) - 1})
            continue
        if line.startswith("[") or line.startswith("##"):
            raise ValueError("Unrecognized timestamp or coverage heading")
        if block is None:
            continue  # Title and provenance preamble are not spoken evidence.
        if line.startswith(">"):
            block["notes"].append(line.lstrip("> "))
        elif turns and turns[-1]["source_block"] == len(blocks) - 1 and not line.startswith("#"):
            turns[-1]["text"] += "\n\n" + line
            continuations += 1
        else:
            raise ValueError("Unrecognized material inside the transcript")
    if not turns or not blocks or blocks[0]["start_seconds"] != 0 or blocks[-1]["end_seconds"] != duration_seconds:
        raise ValueError("Source is not a transcript covering the declared recording")
    if turns[0]["start_seconds"] != 0 or duration_seconds - turns[-1]["start_seconds"] > 30:
        raise ValueError("Transcript lacks its beginning or closing turns")
    for index, turn in enumerate(turns):
        source_block = blocks[turn["source_block"]]
        turn["source_notes"] = list(source_block["notes"])
        # These are transcript navigation boundaries, not measured speech ends.
        next_start = next((row["start_seconds"] for row in turns[index + 1:]
                           if row["start_seconds"] > turn["start_seconds"]), turn["start_seconds"])
        turn["end_seconds"] = next_start
    stats = {"turns": len(turns), "blocks": len(blocks), "continuations": continuations,
             "timestamp_backtracks": sum(b["start_seconds"] < a["start_seconds"]
                                         for a, b in zip(turns, turns[1:])),
             "last_timestamp_seconds": turns[-1]["start_seconds"]}
    return turns, blocks, stats


def build_episode(source, raw, *, fetched_at=None):
    if source.get("schema_version") != 1 or source.get("adapter") != "timestamped_interview_markdown_v1":
        raise ValueError("Unsupported public transcript descriptor")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", source.get("id", "")):
        raise ValueError("Invalid episode identity")
    if len(raw) > MAX_SOURCE_BYTES or hashlib.sha256(raw).hexdigest() != source.get("transcript_sha256"):
        raise ValueError("Transcript does not match the reviewed source checksum")
    turns, blocks, stats = parse_transcript(raw.decode("utf-8"), duration_seconds=source["duration_seconds"])
    if stats != source.get("expected_structure"):
        raise ValueError("Transcript structure differs from the reviewed source")
    exclusions = source.get("excluded_blocks", [])
    excluded_starts = {row["start_seconds"] for row in exclusions}
    if not excluded_starts.issubset({block["start_seconds"] for block in blocks}):
        raise ValueError("An excluded source block is missing")
    excluded_regions = []
    for exclusion in exclusions:
        block_index = next(index for index, block in enumerate(blocks)
                           if block["start_seconds"] == exclusion["start_seconds"])
        block = blocks[block_index]
        excluded_regions.append({"source_block_start_seconds": block["start_seconds"],
                                 "source_block_end_seconds": block["end_seconds"],
                                 "reason": exclusion["reason"],
                                 "excluded_segments": sum(turn["source_block"] == block_index for turn in turns)})
    segments = [turn for turn in turns if blocks[turn["source_block"]]["start_seconds"] not in excluded_starts]
    if not segments:
        raise ValueError("No usable transcript remains after exclusions")
    # IDs retain the original source numbering, including gaps for exclusions.
    # In particular, overlapping turns are not reordered, deduplicated or edited.
    provenance = {"provider": source["transcript_provider"], "source_url": source["transcript_url"],
                  "original_url": source["source_url"], "source_commit": source["transcript_commit"],
                  "source_sha256": source["transcript_sha256"], "method": source["transcription_method"],
                  "verification": "not_audio_verified", "speaker_attribution": "unverified",
                  "rights_notice": source["rights_notice"], "license_url": source["license_url"],
                  "excluded_regions": excluded_regions, "warnings": source["warnings"],
                  "source_structure": stats, "timestamp_order": "source_order_with_overlaps",
                  "end_timestamps": "next_provided_timestamp_not_measured_speech_end"}
    episode = {key: source.get(key) for key in ("id", "show", "title", "language", "source_url", "published_at",
                                               "published_at_note", "duration_basis",
                                               "announcement_release_date", "announcement_source_url")}
    episode.update({"status": "transcribed", "source_kind": "third_party_transcript", "segments": segments,
                    "audio_url": None, "duration_seconds": source["duration_seconds"],
                    "transcript_source_url": source["transcript_url"], "transcript_provenance": provenance,
                    "coverage": {"scope": "partial" if exclusions else "full",
                                 "start_seconds": segments[0]["start_seconds"],
                                 "end_seconds": segments[-1]["start_seconds"],
                                 "segments_total": len(segments), "segments_processed": 0,
                                 "note": source["coverage_note"]},
                    "processing": {"paid_api_calls": 0},
                    "fetched_at": fetched_at or datetime.now(timezone.utc).isoformat()})
    return episode


def write_seed(episode, output):
    output = Path(output).resolve()
    if output.is_relative_to(CHECKOUT):
        raise ValueError("Private transcript archives must be written outside the checkout")
    payload = json.dumps(episode, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > 12 * 1024 * 1024:
        raise ValueError("Episode exceeds the production import bound")
    member = tarfile.TarInfo(f"episodes/{episode['id']}.json")
    member.mode = 0o600
    member.size = len(payload)
    # Exclusive creation also avoids replacing an earlier seed used for review.
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            with tarfile.open(fileobj=stream, mode="w:gz") as archive:
                archive.addfile(member, io.BytesIO(payload))
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return {"archive": str(output), "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            "episode_id": episode["id"], "segments": len(episode["segments"]),
            "text_characters": sum(len(row["text"]) for row in episode["segments"]),
            "source_kind": episode["source_kind"], "coverage": episode["coverage"],
            "excluded_regions": episode["transcript_provenance"]["excluded_regions"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--transcript", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    with args.transcript.open("rb") as stream:
        raw = stream.read(MAX_SOURCE_BYTES + 1)
    source = json.loads(args.source.read_text(encoding="utf-8"))
    print(json.dumps(write_seed(build_episode(source, raw), args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()
