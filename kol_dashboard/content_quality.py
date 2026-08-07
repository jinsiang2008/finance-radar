"""Deterministic evidence-quality rules shared by collection and serving."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


_URL_TOKEN = re.compile(
    r"(?:(?:https?:)?//|www\.)[^\s<>()]+|"
    r"(?<![\w@])(?:[a-z0-9-]+\.)+(?:com|org|net|xyz|io|gov|edu)"
    r"(?:/[^\s<>()]*)?",
    re.IGNORECASE,
)
_REPOST_PREFIX = re.compile(
    r"^\s*(?:RT|RETRUTH)\b[\s:：|\-—]*", re.IGNORECASE
)
_REPOST_ATTRIBUTION = re.compile(
    r"^\s*(?:FROM|VIA|BY)\b[\s:：|\-—]*", re.IGNORECASE
)
_SOCIAL_MENTION = re.compile(r"(?<![\w@])@[A-Za-z0-9_]{1,64}\b")
_CASHTAG = re.compile(r"(?<![\w$])\$[A-Za-z][A-Za-z0-9._-]{0,9}\b")
_HAN = re.compile(r"[\u3400-\u9fff]")


def has_substantive_social_text(value: Any) -> bool:
    """Return whether text contains evidence beyond links/repost metadata."""
    candidate = _REPOST_PREFIX.sub("", str(value or ""), count=1)
    candidate = _REPOST_ATTRIBUTION.sub("", candidate, count=1)
    candidate = _URL_TOKEN.sub(" ", candidate)
    candidate = _SOCIAL_MENTION.sub(" ", candidate)
    if _CASHTAG.search(candidate):
        return True
    meaningful = re.sub(r"[^\w]+", "", candidate, flags=re.UNICODE).replace(
        "_", ""
    )
    return len(meaningful) >= 3 or len(_HAN.findall(candidate)) >= 2


def is_event_content_eligible(event: Mapping[str, Any]) -> bool:
    """Reject known Truth Social placeholders with no textual evidence."""
    source = str(event.get("source") or "").strip().casefold()[:160]
    url = str(
        event.get("canonical_url") or event.get("url") or ""
    ).strip().casefold()[:2_048]
    if "truth social" not in source and "truthsocial.com/" not in url:
        return True
    return has_substantive_social_text(event.get("snippet") or event.get("title"))
