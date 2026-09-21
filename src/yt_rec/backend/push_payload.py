"""Shared notification-payload rules for every YouTube push receiver.

Both receivers (QtWebEngine and Chrome) must identify a video the same way, so
the parser lives here instead of in either one. It fails closed: an ambiguous,
oversized or malformed payload yields no video ID at all rather than a guess.
This module imports no browser engine; it is pure data validation.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

YOUTUBE = "https://www.youtube.com"
NOTIFICATION_SETTINGS_URL = YOUTUBE + "/account_notifications"
_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")


def _is_youtube_url(value: str) -> bool:
    try:
        if not isinstance(value, str) or any(ord(char) <= 32 for char in value):
            return False
        parsed = urlsplit(value)
        return (parsed.scheme == "https" and parsed.hostname == "www.youtube.com"
                and parsed.port in (None, 443) and parsed.username is None and parsed.password is None)
    except (TypeError, ValueError):
        return False


def _video_id_from_data(data: object) -> str | None:
    """Fail closed on ambiguity or traversal limits; never parse titles as IDs."""
    videos: set[str] = set()
    remaining = 256

    def visit(value: object, depth: int = 0) -> None:
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 6:
            raise ValueError
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ("videoId", "video_id"):
                    if not isinstance(item, str) or not _VIDEO_ID.fullmatch(item):
                        raise ValueError
                    videos.add(item)
                visit(item, depth + 1)
        elif isinstance(value, list):
            for item in value:
                visit(item, depth + 1)
        elif isinstance(value, str) and _is_youtube_url(value):
            # YouTube's payload schema is not public. A canonical watch URL is
            # usable under any key; unrelated icon/avatar URLs are not IDs.
            parsed = urlsplit(value)
            if parsed.path == "/watch":
                ids = parse_qs(parsed.query, keep_blank_values=True).get("v", [])
                if len(ids) != 1 or not _VIDEO_ID.fullmatch(ids[0]):
                    raise ValueError
                videos.add(ids[0])

    try:
        visit(data)
    except (ValueError, RecursionError):
        return None
    return next(iter(videos)) if len(videos) == 1 else None
