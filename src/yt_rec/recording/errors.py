"""yt-dlp 오류 메시지를 처리 가능한 범주로 나눈다.

비공개·삭제·접근 제한처럼 기다려도 소용없는 상태와, 아직 스트림이 준비되지 않은
일시적인 상태를 구분해야 감시 루프(#3)가 재시도 여부를 판단할 수 있다.
"""

from __future__ import annotations

import re
from enum import Enum

__all__ = [
    "DenialCategory",
    "MetadataUnavailableError",
    "RecordingError",
    "ToolFailure",
    "classify_error",
    "is_transient",
]


class DenialCategory(str, Enum):
    """녹화를 진행할 수 없는 이유."""

    PRIVATE = "private"
    MEMBERS_ONLY = "members_only"
    REMOVED = "removed"
    GEO_BLOCKED = "geo_blocked"
    AGE_RESTRICTED = "age_restricted"
    LOGIN_REQUIRED = "login_required"
    NOT_STARTED = "not_started"
    STREAM_NOT_READY = "stream_not_ready"
    NETWORK = "network"
    UNKNOWN = "unknown"

    def __str__(self) -> str:  # pragma: no cover - 표시용
        return self.value


#: 기다렸다 다시 시도할 가치가 있는 범주.
_TRANSIENT = frozenset(
    {
        DenialCategory.NOT_STARTED,
        DenialCategory.STREAM_NOT_READY,
        DenialCategory.NETWORK,
    }
)

#: 위에서부터 먼저 맞는 규칙을 쓴다. 구체적인 문구를 앞에 둔다.
_PATTERNS: tuple[tuple[DenialCategory, re.Pattern[str]], ...] = (
    (
        DenialCategory.MEMBERS_ONLY,
        re.compile(
            r"join this channel to get access|members-only|"
            r"available to this channel's members|members only",
            re.IGNORECASE,
        ),
    ),
    (
        DenialCategory.PRIVATE,
        re.compile(r"private video|this video is private", re.IGNORECASE),
    ),
    (
        DenialCategory.GEO_BLOCKED,
        re.compile(
            r"not made this video available in your country|"
            r"blocked it in your country|geo restricted|"
            r"not available (?:from|in) your (?:location|country)",
            re.IGNORECASE,
        ),
    ),
    (
        DenialCategory.AGE_RESTRICTED,
        re.compile(
            r"confirm your age|age[- ]restricted|inappropriate for some users",
            re.IGNORECASE,
        ),
    ),
    (
        DenialCategory.REMOVED,
        re.compile(
            r"has been removed by the uploader|removed for violating|"
            r"account associated with this video has been terminated|"
            r"no longer available due to a copyright claim|"
            r"video has been removed|this video is unavailable|"
            r"video unavailable",
            re.IGNORECASE,
        ),
    ),
    (
        DenialCategory.NOT_STARTED,
        re.compile(
            r"this live event will begin|premieres in|"
            r"is not currently live|live event will begin in",
            re.IGNORECASE,
        ),
    ),
    (
        DenialCategory.STREAM_NOT_READY,
        re.compile(
            r"live stream recording is not available|the livestream is offline|"
            r"no video formats found|requested format (?:is )?not available|"
            r"this live stream is offline|still processing",
            re.IGNORECASE,
        ),
    ),
    (
        DenialCategory.LOGIN_REQUIRED,
        re.compile(
            r"sign in to confirm you'?re not a bot|please sign in|"
            r"login required|use --cookies|sign in to view",
            re.IGNORECASE,
        ),
    ),
    (
        DenialCategory.NETWORK,
        re.compile(
            r"unable to download (?:webpage|api page|video data|json metadata)|"
            r"urlopen error|getaddrinfo failed|connection (?:reset|refused|aborted)|"
            r"timed? out|temporary failure in name resolution|"
            r"http error 5\d\d|http error 429|remote end closed connection|"
            r"read operation timed out|network is unreachable",
            re.IGNORECASE,
        ),
    ),
)


def classify_error(text: str | None) -> DenialCategory:
    """yt-dlp 가 남긴 오류 문구를 범주로 옮긴다. 못 알아보면 ``UNKNOWN``."""
    if not text:
        return DenialCategory.UNKNOWN
    for category, pattern in _PATTERNS:
        if pattern.search(text):
            return category
    return DenialCategory.UNKNOWN


def is_transient(category: DenialCategory) -> bool:
    """기다렸다 다시 시도할 가치가 있으면 참."""
    return category in _TRANSIENT


class RecordingError(RuntimeError):
    """녹화 경로에서 발생한 오류의 기반 클래스."""

    def __init__(self, message: str, category: DenialCategory = DenialCategory.UNKNOWN):
        super().__init__(message)
        self.category = category

    @property
    def transient(self) -> bool:
        return is_transient(self.category)


class MetadataUnavailableError(RecordingError):
    """녹화 시작 시점에 제목·채널·시작 시각을 확보하지 못했다."""


class ToolFailure(RecordingError):
    """외부 도구(yt-dlp/ffmpeg/ffprobe)가 실패했다."""

    def __init__(self, message: str, *, returncode: int | None = None, output: str = ""):
        super().__init__(message, classify_error(output or message))
        self.returncode = returncode
        self.output = output
