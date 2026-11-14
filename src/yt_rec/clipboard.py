"""클립보드 텍스트에서 YouTube 영상 ID만 고른다."""

from __future__ import annotations

import re

__all__ = ["youtube_video_id"]

# 라이브·일반 시청 주소만. 쇼츠와 채널 페이지는 녹화 대상이 아니다.
_VIDEO = re.compile(
    r"(?:https?://)?(?:www\.|m\.)?(?:youtube\.com/(?:watch\?[^\s]*?v=|live/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})\b",
    re.IGNORECASE,
)


def youtube_video_id(text: str) -> str | None:
    """텍스트 안의 첫 YouTube 영상 ID. 없으면 None."""
    if not text:
        return None
    match = _VIDEO.search(text)
    return match.group(1) if match else None
