"""Fixed historical picks: public metadata and pure identity matching only.

An episode's mutable ``curation`` field is never an acquisition permission.
Only a match to this shipped catalog can join the historical transcript queue.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


COLLECTION = "influential_history"
CATALOG_PATH = Path(__file__).with_name("curated_podcasts.json")


def catalog():
    return json.loads(CATALOG_PATH.read_text())["episodes"]


def canonical_url(value):
    """Compare publisher page identities without tracking or slash differences."""
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value)
        if (parts.scheme != "https" or not parts.hostname or parts.username
                or parts.password or parts.port is not None):
            return None
    except ValueError:
        return None
    return urlunsplit(("https", parts.hostname.lower(), parts.path.rstrip("/"), "", ""))


def matches(episode, entry):
    if episode.get("content_kind") == "wechat_article":
        return False
    if episode.get("source_id") not in (None, entry["source_id"]):
        return False
    if canonical_url(episode.get("source_url")) == canonical_url(entry["source_url"]):
        return True
    # Only publisher GUIDs verified in the catalog are identity aliases. A
    # private record's id/curation flag alone cannot grant fetch permission.
    aliases = entry.get("rss_guids", [])
    return bool(episode.get("source_id") == entry["source_id"]
                and episode.get("guid") and episode["guid"] in aliases)


def match_episode(episode):
    return next((entry for entry in catalog() if matches(episode, entry)), None)


def entry_for_url(source_id, url):
    return match_episode({"source_id": source_id, "source_url": url})


def existing_episode(episodes, entry):
    matches_found = [episode for episode in episodes if matches(episode, entry)]
    # Prefer an already-read copy if old imports accidentally created aliases;
    # never delete or overwrite the other records as part of catalog seeding.
    return min(matches_found, key=lambda row: (
        not bool(row.get("summary")), not bool(row.get("segments")), row["id"]), default=None)


def curation(entry):
    return {"collection": COLLECTION, "guest_ids": list(entry["guest_ids"]),
            "guests": list(entry["guests"]), "reason": entry["reason"], "priority": entry["priority"]}


def metadata_episode(entry, discovered_at):
    return {**deepcopy({key: entry[key] for key in
            ("id", "source_id", "show", "title", "language", "published_at", "source_url")}),
            "status": "discovered", "source_kind": "official_episode_metadata",
            "segments": [], "coverage": {"scope": "metadata_only", "segments_total": 0,
            "segments_processed": 0, "note": "历史精选已收录，等待取得官方文字稿。"},
            "processing": {"paid_api_calls": 0}, "discovered_at": discovered_at,
            "published_precision": entry.get("published_precision", "timestamp"),
            "curation": curation(entry)}
