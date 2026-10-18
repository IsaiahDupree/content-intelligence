"""Fail-closed normalization for provider identities used in source URLs."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import unquote, urlparse


TIKTOK_HANDLE_RE = re.compile(r"[A-Za-z0-9._]{1,64}")
TIKTOK_VIDEO_PATH_RE = re.compile(r"/@([^/]+)/video/(\d+)/?")
TIKTOK_SHORT_PATH_RE = re.compile(r"/(?:t/)?[A-Za-z0-9_-]+/?")
TIKTOK_HANDLE_FIELDS = (
    "unique_id",
    "uniqueId",
    "username",
    "userName",
    "handle",
    "creator_handle",
    "author_handle",
    "author",
    "user",
)
OBJECT_MARKERS = frozenset("{}[]'\"")


def _handle_from_url(source_url: str) -> str:
    try:
        parsed = urlparse(source_url)
    except ValueError:
        return ""
    host = (parsed.hostname or "").casefold()
    if host != "tiktok.com" and not host.endswith(".tiktok.com"):
        return ""
    match = TIKTOK_VIDEO_PATH_RE.fullmatch(unquote(parsed.path))
    if not match:
        return ""
    handle = match.group(1).lstrip("@").strip()
    return handle if TIKTOK_HANDLE_RE.fullmatch(handle) else ""


def _handle_from_value(value: Any, *, depth: int = 0) -> str:
    if value is None or isinstance(value, bool) or depth > 3:
        return ""
    if isinstance(value, Mapping):
        for field in TIKTOK_HANDLE_FIELDS:
            if field in value:
                handle = _handle_from_value(value[field], depth=depth + 1)
                if handle:
                    return handle
        return ""
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for entry in value:
            handle = _handle_from_value(entry, depth=depth + 1)
            if handle:
                return handle
        return ""
    if not isinstance(value, (str, int)):
        return ""
    text = unquote(str(value)).strip()
    if text.startswith(("https://", "http://")):
        return _handle_from_url(text)
    text = text.lstrip("@").strip()
    if text.casefold() == "unknown" or any(marker in text for marker in OBJECT_MARKERS):
        return ""
    return text if TIKTOK_HANDLE_RE.fullmatch(text) else ""


def normalize_tiktok_handle(*values: Any, source_url: Any = "") -> str:
    """Return a provider handle only when it has a scalar TikTok shape.

    Provider mappings are traversed through documented identity fields only.  An
    arbitrary mapping is never stringified and therefore can never become a URL
    path such as ``@%7B...%7D``.
    """

    for value in values:
        handle = _handle_from_value(value)
        if handle:
            return handle
    return _handle_from_value(source_url)


def is_usable_tiktok_source_url(source_url: Any, external_id: Any) -> bool:
    if not isinstance(source_url, str) or not source_url.strip():
        return False
    try:
        parsed = urlparse(source_url.strip())
    except ValueError:
        return False
    if parsed.scheme.casefold() not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").casefold()
    if host != "tiktok.com" and not host.endswith(".tiktok.com"):
        return False
    path = unquote(parsed.path)
    if any(marker in path for marker in OBJECT_MARKERS):
        return False
    match = TIKTOK_VIDEO_PATH_RE.fullmatch(path)
    if match:
        handle, video_id = match.groups()
        return bool(
            TIKTOK_HANDLE_RE.fullmatch(handle)
            and video_id == str(external_id).strip()
        )
    if host in {"vm.tiktok.com", "vt.tiktok.com"}:
        return bool(TIKTOK_SHORT_PATH_RE.fullmatch(path))
    return False


def normalize_tiktok_source_url(
    source_url: Any,
    external_id: Any,
    *handle_values: Any,
) -> str:
    """Keep a verified TikTok URL or build one from a verified scalar handle."""

    raw_url = source_url.strip() if isinstance(source_url, str) else ""
    if is_usable_tiktok_source_url(raw_url, external_id):
        return raw_url
    video_id = str(external_id).strip()
    handle = normalize_tiktok_handle(*handle_values, source_url=raw_url)
    if not handle or not video_id.isdigit():
        return ""
    return f"https://www.tiktok.com/@{handle}/video/{video_id}"


def is_usable_source_url(platform: Any, external_id: Any, source_url: Any) -> bool:
    """Preserve existing providers while applying strict TikTok URL admission."""

    if str(platform).strip().casefold() == "tiktok":
        return is_usable_tiktok_source_url(source_url, external_id)
    return bool(isinstance(source_url, str) and source_url.strip())
