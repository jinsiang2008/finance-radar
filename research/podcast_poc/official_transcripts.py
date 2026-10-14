"""Pure adapters for public publisher transcripts; no I/O or audio verification.

The caller must enforce the fetch allowlist, size/deadline limits and schedule.
These adapters never promote descriptions or chapter lists to transcripts.
"""
from __future__ import annotations

from collections import Counter
import re
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup


class TranscriptUnavailable(ValueError):
    """The fetched document cannot be established as a usable transcript."""


MIN_SEGMENTS = 20
MIN_TEXT_CHARS = 2000
_CLOCK = r"(?:\d{1,2}:)?\d{1,2}:\d{2}"
_STOP_HEADINGS = {"footnotes", "references", "comments", "discussion", "subscribe", "related posts"}


def _url(url: str, hosts: set[str], path_pattern: str) -> str:
    try:
        parts = urlsplit(url)
        port = parts.port
    except (TypeError, ValueError) as exc:
        raise TranscriptUnavailable("Invalid official transcript URL") from exc
    if (parts.scheme != "https" or parts.hostname not in hosts or parts.username
            or parts.password or port is not None
            or not re.fullmatch(path_pattern, parts.path)):
        raise TranscriptUnavailable("URL is outside the supported official transcript paths")
    return urlunsplit(("https", parts.hostname, parts.path, "", ""))


def validate_transcript_url(source_id: str, url: str) -> str:
    """Validate a final transcript URL and return it without tracking parameters."""
    if source_id == "dwarkesh":
        return _url(url, {"www.dwarkesh.com", "dwarkesh.com"}, r"/p/[a-zA-Z0-9-]+/?")
    if source_id == "lex":
        return _url(url, {"lexfridman.com", "www.lexfridman.com"}, r"/[a-zA-Z0-9-]+-transcript/?")
    if source_id == "diary":
        return _url(url, {"rss.flightcast.com"}, r"/transcripts/[a-zA-Z0-9_-]+\.vtt")
    if source_id == "freakonomics":
        return _url(url, {"freakonomics.com"}, r"/podcast/extra-ray-dalio-full-interview-ep-330/")
    raise TranscriptUnavailable("This publisher has no supported official transcript adapter")


def find_transcript_url(source_id: str, source_url: str, html: str) -> str:
    """Find the actual episode transcript link; never invent a transcript URL."""
    if source_id in {"dwarkesh", "freakonomics"}:
        return validate_transcript_url(source_id, source_url)
    if source_id != "lex":
        raise TranscriptUnavailable("This publisher requires an explicit transcript feed link")
    show_url = _url(source_url, {"lexfridman.com", "www.lexfridman.com"}, r"/[a-zA-Z0-9-]+/?")
    show = urlsplit(show_url)
    if show.path.rstrip("/").endswith("-transcript"):
        return validate_transcript_url("lex", show_url)
    # Match the episode identity, as a page may also link unrelated transcripts.
    expected_path = show.path.rstrip("/") + "-transcript"
    matches = set()
    for anchor in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        try:
            candidate = validate_transcript_url("lex", urljoin(show_url, anchor["href"]))
        except TranscriptUnavailable:
            continue
        target = urlsplit(candidate)
        if target.hostname == show.hostname and target.path.rstrip("/") == expected_path:
            matches.add(candidate)
    if not matches:
        raise TranscriptUnavailable("No official transcript link for this episode")
    # Prefer a slash variant only when the publisher actually linked it. The
    # fetcher intentionally refuses redirects, so do not synthesize a variant.
    return sorted(matches, key=lambda value: (not value.endswith("/"), value))[0]


def _seconds(value: str, *, milliseconds: bool = False) -> float | int:
    pattern = r"(?:\d{2,}:)?\d{2}:\d{2}\.\d{3}" if milliseconds else _CLOCK
    if not re.fullmatch(pattern, value):
        raise TranscriptUnavailable("Malformed transcript timestamp")
    fields = value.split(":")
    seconds = float(fields[-1]) if milliseconds else int(fields[-1])
    minutes = int(fields[-2])
    hours = int(fields[-3]) if len(fields) == 3 else 0
    if minutes >= 60 or seconds >= 60:
        raise TranscriptUnavailable("Transcript timestamp is out of range")
    result = hours * 3600 + minutes * 60 + seconds
    if result > 48 * 3600:
        raise TranscriptUnavailable("Transcript timestamp exceeds the supported duration")
    return result


def _speaker(value: str) -> str:
    value = " ".join(value.strip(" :\n\t").split())
    if (not value or len(value) > 80 or len(value.split()) > 10
            or not any(char.isalpha() for char in value)
            or re.search(r"[\[\]!?;<>]", value)):
        raise TranscriptUnavailable("Unrecognized transcript speaker label")
    return value


def _result(segments: list[dict], url: str, note: str, *, captions: bool = False) -> dict:
    if len(segments) < MIN_SEGMENTS or sum(len(s["text"]) for s in segments) < MIN_TEXT_CHARS:
        raise TranscriptUnavailable("Not enough transcript body; show notes cannot be analyzed as a transcript")
    previous = -1
    for index, segment in enumerate(segments):
        start = segment["start_seconds"]
        if start < previous:
            raise TranscriptUnavailable("Transcript timestamps are not monotonic")
        previous = start
        segment["id"] = f"s{index + 1:04d}"
        segment.setdefault("end_seconds", None)
        if not segment["text"].strip():
            raise TranscriptUnavailable("Empty transcript speech segment")
        if segment["end_seconds"] is not None and segment["end_seconds"] <= start:
            raise TranscriptUnavailable("Transcript cue ends before it begins")
    if segments[-1]["start_seconds"] - segments[0]["start_seconds"] < 60:
        raise TranscriptUnavailable("Transcript has insufficient timestamp coverage")
    if not captions:
        counts = Counter(segment["speaker"] for segment in segments)
        if len(counts) > 12 or max(counts.values()) < 3:
            raise TranscriptUnavailable("Timestamp labels look like chapters rather than recurring speakers")
        next_start = None
        group_start = None
        next_group_start = None
        for segment in reversed(segments):
            start = segment["start_seconds"]
            if group_start is None:
                group_start = start
            elif start < group_start:
                next_group_start, group_start = group_start, start
            # Chapter-level turns share an anchor and the same known chapter end.
            segment["end_seconds"] = (next_group_start if segment.get("timestamp_precision") == "chapter"
                                      else next_start)
            next_start = start
    return {
        "segments": segments,
        "source_kind": "official_transcript" if captions else "official_edited_transcript",
        "transcript_url": url,
        "coverage": {
            "scope": "full", "start_seconds": segments[0]["start_seconds"],
            "end_seconds": segments[-1]["end_seconds"] or segments[-1]["start_seconds"],
            "segments_total": len(segments), "segments_processed": 0, "note": note,
        },
    }


def _dwarkesh(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    body = soup.select_one(".body.markup")
    if body is None:
        raise TranscriptUnavailable("Official article body was not found")
    for node in body.select("script, style, .subscription-widget, .button-wrapper"):
        node.decompose()
    blocks = body.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p"])
    marker = next((i for i, block in enumerate(blocks)
                   if block.name.startswith("h") and block.get_text(" ", strip=True).casefold().rstrip(":") == "transcript"), None)
    if marker is None:
        raise TranscriptUnavailable("No explicit Transcript heading in official article")
    turn = re.compile(rf"^(.{{1,80}}?)\s+\(?\[?({_CLOCK})\]?\)?\s*:?[ \t]*(.*)$")
    time_first = re.compile(rf"^\[({_CLOCK})\]\s*([^:]{{1,80}}):\s*(.*)$")
    chapter = re.compile(rf"^\(?({_CLOCK})\)?\s*(?:[-–—:]|$)")
    segments = []
    pending = None
    chapter_time = None

    def finish():
        nonlocal pending
        if pending and pending["text"].strip():
            pending["text"] = pending["text"].strip()
            segments.append(pending)
        pending = None

    for block in blocks[marker + 1:]:
        value = block.get_text(" ", strip=True)
        if not value:
            continue
        if block.name.startswith("h"):
            if value.casefold().rstrip(":") in _STOP_HEADINGS:
                break
            match = chapter.match(value)
            if match:
                last_speaker = pending["speaker"] if pending else None
                finish()
                stamp = _seconds(match[1])
                if chapter_time is not None and stamp < chapter_time:
                    raise TranscriptUnavailable("Transcript chapter timestamps are not monotonic")
                chapter_time = stamp
                if last_speaker:
                    pending = {"speaker": last_speaker, "start_seconds": stamp,
                               "timestamp_precision": "chapter", "text": ""}
            # Headings are not spoken paragraphs.
            continue
        first = time_first.match(value)
        match = turn.match(value)
        if first or match:
            finish()
            speaker, stamp, speech = (first[2], first[1], first[3]) if first else (match[1], match[2], match[3])
            pending = {"speaker": _speaker(speaker), "start_seconds": _seconds(stamp),
                       "timestamp_precision": "speaker_turn", "text": speech.strip()}
            continue
        bold = block.find(["strong", "b"])
        if bold and bold.get_text(" ", strip=True).strip(" :") == value.strip(" :"):
            # Current publisher format uses a chapter timestamp followed by
            # isolated bold speaker names, then one or more speech paragraphs.
            if chapter_time is None:
                raise TranscriptUnavailable("Speaker paragraphs have no timestamp anchor")
            finish()
            pending = {"speaker": _speaker(value), "start_seconds": chapter_time,
                       "timestamp_precision": "chapter", "text": ""}
        elif pending:
            pending["text"] += ("\n" if pending["text"] else "") + value
    finish()
    note = "完整处理所获取的公开官方整理稿；未独立核对整段音频，不保证与音频逐字一致。"
    if any(segment.get("timestamp_precision") == "chapter" for segment in segments):
        note += "部分时间点仅为官方章节锚点，同章发言共用时间，不能精确定位每句话。"
    else:
        note += "时间点沿用官方文字稿。"
    return _result(segments, url, note)


def _lex(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    body = soup.select_one(".entry-content")
    if body is None:
        raise TranscriptUnavailable("Official transcript content was not found")
    segments = []
    previous_speaker = None
    for turn in body.select(".ts-segment"):
        name, stamp, speech = (turn.select_one(selector) for selector in (".ts-name", ".ts-timestamp", ".ts-text"))
        if name is None or stamp is None or speech is None:
            raise TranscriptUnavailable("Incomplete official transcript speaker turn")
        for node in speech.select("script, style"):
            node.decompose()
        label = name.get_text(" ", strip=True)
        # Older publisher pages leave the name span empty for continuation
        # paragraphs. Keep their own clock and explicitly record inheritance.
        speaker = _speaker(label) if label else previous_speaker
        if speaker is None:
            raise TranscriptUnavailable("Official continuation lacks a previous speaker")
        previous_speaker = speaker
        segment = {"speaker": speaker,
                         "start_seconds": _seconds(stamp.get_text(" ", strip=True).strip("()")),
                         "timestamp_precision": "speaker_turn", "text": speech.get_text(" ", strip=True)}
        if not label:
            segment["source_notes"] = ["原站本段说话人标签为空，按相邻续段沿用上一标签。"]
        segments.append(segment)
    return _result(segments, url, "完整处理所获取的公开官方整理稿；时间点沿用官方文字稿，未独立核对整段音频，不保证与音频逐字一致。")


def _freakonomics(html: str, url: str) -> dict:
    """Read the pinned full interview, retaining untimed dialogue paragraphs.

    This publisher has no segment clocks. Do not weaken the timestamp checks
    of the other adapters or manufacture a duration for these speech turns.
    """
    soup = BeautifulSoup(html, "html.parser")
    body = soup.select_one("section#transcript #transcript_inner")
    if body is None:
        raise TranscriptUnavailable("Official full interview container was not found")
    for node in body.select("script, style"):
        node.decompose()
    paragraphs = [p.get_text(" ", strip=True) for p in body.find_all("p")]
    turn = re.compile(r"^(?P<speaker>(?:Stephen\s+)?DUBNER|(?:Ray\s+)?DALIO):\s*(?P<speech>.+)$")
    labeled = [(index, turn.fullmatch(text)) for index, text in enumerate(paragraphs)
               if turn.fullmatch(text)]
    if not labeled or not labeled[-1][1]["speech"].endswith("Be well, Ray. Bye."):
        raise TranscriptUnavailable("Pinned interview closing turn was not found")
    first, last = labeled[0][0], labeled[-1][0]
    segments = []
    for text in paragraphs[first:last + 1]:
        if not text:
            continue
        match = turn.fullmatch(text)
        if match:
            name = "Ray Dalio" if match["speaker"].endswith("DALIO") else "Stephen Dubner"
            segments.append({"id": f"s{len(segments)+1:04d}", "speaker": name,
                             "start_seconds": None, "end_seconds": None,
                             "timestamp_precision": "none", "text": match["speech"]})
        else:
            if re.match(r"^[A-Z][A-Z .]{2,40}:", text):
                raise TranscriptUnavailable("Unexpected full interview speaker label")
            segments[-1]["text"] += "\n\n" + text
    counts = Counter(segment["speaker"] for segment in segments)
    if (len(segments) < MIN_SEGMENTS or sum(len(s["text"]) for s in segments) < MIN_TEXT_CHARS
            or set(counts) != {"Ray Dalio", "Stephen Dubner"} or min(counts.values()) < 3):
        raise TranscriptUnavailable("Not enough complete full interview dialogue")
    return {"segments": segments, "source_kind": "official_edited_transcript", "transcript_url": url,
            "coverage": {"scope": "full", "start_seconds": None, "end_seconds": None,
                         "segments_total": len(segments), "segments_processed": 0,
                         "note": "完整处理取得的官方对谈文字，排除编辑导语与制作说明；原站未提供段落时间戳，未逐秒核对音频，不表示完整音频覆盖。"}}


def parse_transcript(source_id: str, html: str, *, source_url: str) -> dict:
    """Parse a supported official HTML transcript; fail closed on format drift."""
    url = validate_transcript_url(source_id, source_url)
    if source_id == "dwarkesh":
        return _dwarkesh(html, url)
    if source_id == "lex":
        return _lex(html, url)
    if source_id == "freakonomics":
        return _freakonomics(html, url)
    raise TranscriptUnavailable("This publisher does not use the HTML transcript adapter")


def parse_webvtt(text: str, *, source_url: str) -> dict:
    """Parse Diary of a CEO's publisher-hosted WebVTT, not inferred subtitles."""
    url = validate_transcript_url("diary", source_url)
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n[ \t]*\n", text.strip())
    if not blocks or not re.fullmatch(r"WEBVTT(?:[ \t].*)?", blocks[0].split("\n")[0]):
        raise TranscriptUnavailable("Official subtitle response is not WebVTT")
    segments = []
    timestamp = r"(?:\d{2,}:)?\d{2}:\d{2}\.\d{3}"
    timing = re.compile(rf"^({timestamp})[ \t]+-->[ \t]+({timestamp})(?:[ \t]+.*)?$")
    for block in blocks[1:]:
        lines = block.split("\n")
        if re.match(r"^(NOTE(?:\s|$)|STYLE$|REGION$)", lines[0]):
            continue
        line_index = 0 if "-->" in lines[0] else 1
        if line_index >= len(lines) or not timing.fullmatch(lines[line_index]):
            raise TranscriptUnavailable("Malformed WebVTT cue")
        match = timing.fullmatch(lines[line_index])
        raw = "\n".join(lines[line_index + 1:]).strip()
        if not raw:
            raise TranscriptUnavailable("Empty WebVTT cue")
        # VTT voice tags are <v Speaker> rather than HTML attributes.
        voice = re.match(r"^<v(?:\.[^\s>]+)?\s+([^>]+)>", raw)
        speaker = _speaker(voice[1]) if voice else ""
        cleaned = re.sub(r"<\d{2}:\d{2}(?::\d{2})?\.\d{3}>", "", raw)
        speech = BeautifulSoup(cleaned, "html.parser").get_text(" ", strip=True)
        segments.append({"speaker": speaker, "start_seconds": _seconds(match[1], milliseconds=True),
                         "end_seconds": _seconds(match[2], milliseconds=True),
                         "timestamp_precision": "caption", "text": speech})
    return _result(segments, url, "完整处理所获取的官方字幕文件；字幕时间点沿用原文件，未独立听校，不保证字幕覆盖整段音频或与音频逐字一致。", captions=True)
