"""접근 제한 상태와 일시적인 미준비 상태의 구분 (#4)."""

from __future__ import annotations

import pytest

from yt_rec.recording.errors import DenialCategory, classify_error, is_transient

# 실제로 관측되거나 yt-dlp 가 내는 문구들.
CASES = [
    (
        "ERROR: [youtube] abc: Join this channel to get access to members-only "
        "content like this video, and other exclusive perks.",
        DenialCategory.MEMBERS_ONLY,
    ),
    (
        "ERROR: [youtube] abc: This video is available to this channel's members "
        "on level: 후원자 (or any higher level).",
        DenialCategory.MEMBERS_ONLY,
    ),
    (
        "ERROR: [youtube] abc: Private video. Sign in if you've been granted "
        "access to this video",
        DenialCategory.PRIVATE,
    ),
    (
        "ERROR: [youtube] abc: Video unavailable. This video has been removed by "
        "the uploader",
        DenialCategory.REMOVED,
    ),
    (
        "ERROR: [youtube] abc: Video unavailable. This video is no longer "
        "available due to a copyright claim",
        DenialCategory.REMOVED,
    ),
    (
        "ERROR: [youtube] abc: The uploader has not made this video available in "
        "your country",
        DenialCategory.GEO_BLOCKED,
    ),
    (
        "ERROR: [youtube] abc: Sign in to confirm your age. This video may be "
        "inappropriate for some users.",
        DenialCategory.AGE_RESTRICTED,
    ),
    (
        "ERROR: [youtube] abc: Sign in to confirm you're not a bot. Use --cookies",
        DenialCategory.LOGIN_REQUIRED,
    ),
    (
        "ERROR: [youtube] abc: This live event will begin in 3 hours.",
        DenialCategory.NOT_STARTED,
    ),
    (
        "ERROR: [youtube] abc: This live stream recording is not available.",
        DenialCategory.STREAM_NOT_READY,
    ),
    (
        "ERROR: [youtube] abc: Requested format is not available",
        DenialCategory.STREAM_NOT_READY,
    ),
    (
        "ERROR: unable to download webpage: <urlopen error [Errno 11001] "
        "getaddrinfo failed>",
        DenialCategory.NETWORK,
    ),
    (
        "ERROR: unable to download video data: HTTP Error 503: Service Unavailable",
        DenialCategory.NETWORK,
    ),
]


@pytest.mark.parametrize("message,expected", CASES, ids=[c[1].value for c in CASES])
def test_오류_문구를_범주로_옮긴다(message, expected):
    assert classify_error(message) is expected


def test_알아보지_못한_문구는_UNKNOWN():
    assert classify_error("ERROR: 뭔가 잘못됐다") is DenialCategory.UNKNOWN
    assert classify_error("") is DenialCategory.UNKNOWN
    assert classify_error(None) is DenialCategory.UNKNOWN


def test_기다릴_가치가_있는_범주만_일시적이다():
    transient = {c for c in DenialCategory if is_transient(c)}
    assert transient == {
        DenialCategory.NOT_STARTED,
        DenialCategory.STREAM_NOT_READY,
        DenialCategory.NETWORK,
    }


def test_접근_제한은_기다려도_소용없다():
    for category in (
        DenialCategory.PRIVATE,
        DenialCategory.MEMBERS_ONLY,
        DenialCategory.REMOVED,
        DenialCategory.GEO_BLOCKED,
    ):
        assert not is_transient(category)


def test_멤버_전용이_삭제보다_먼저_잡힌다():
    """멤버 전용 문구에 'video unavailable' 이 함께 나와도 멤버 전용으로 본다."""
    message = (
        "ERROR: [youtube] abc: Video unavailable. Join this channel to get access "
        "to members-only content"
    )
    assert classify_error(message) is DenialCategory.MEMBERS_ONLY
