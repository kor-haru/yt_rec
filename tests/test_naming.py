"""파일명 결정 규칙 (#4: 금지 문자 치환, 덮어쓰기 금지)."""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

import pytest

from yt_rec.recording.naming import (
    FORBIDDEN_CHAR_MAP,
    local_date_from_epoch,
    reserve_unique_path,
    sanitize_filename_component,
)

WINDOWS_FORBIDDEN = '?:*"<>|/\\'


def test_모든_windows_금지_문자가_전각으로_치환된다():
    raw = "".join(WINDOWS_FORBIDDEN)
    result = sanitize_filename_component(raw)

    assert result == "？：＊＂＜＞｜／＼"
    for char in WINDOWS_FORBIDDEN:
        assert char not in result
        assert FORBIDDEN_CHAR_MAP[char] in result


def test_실제_제목의_금지_문자만_바뀌고_나머지는_그대로다():
    result = sanitize_filename_component("행백TV 실시간 Q&A / 내 집 마련 고민 해결")
    assert result == "행백TV 실시간 Q&A ／ 내 집 마련 고민 해결"


def test_이모지와_한글은_보존된다():
    title = "왕초보도 하루만에 끝내는 경매 기초! 이번에 완전히 종결합니다🔥"
    assert sanitize_filename_component(title) == title


def test_제어_문자는_제거된다():
    assert sanitize_filename_component("제목\x00\x1f줄바꿈\n없음") == "제목줄바꿈없음"


def test_끝의_점과_공백은_없앤다():
    # Windows 는 끝의 점·공백을 조용히 잘라내 이름이 어긋난다.
    assert sanitize_filename_component("제목...  ") == "제목"
    assert sanitize_filename_component("제목 . ") == "제목"


@pytest.mark.parametrize("reserved", ["CON", "nul", "COM1", "LPT9", "aux.mp4"])
def test_windows_예약_장치_이름은_피한다(reserved):
    assert sanitize_filename_component(reserved).startswith("_")


def test_빈_이름은_대체값으로_바뀐다():
    assert sanitize_filename_component("   ", fallback="abc123") == "abc123"
    assert sanitize_filename_component("\x00", fallback="abc123") == "abc123"


def test_길이_상한을_넘으면_자른다():
    result = sanitize_filename_component("가" * 500, max_chars=100)
    assert len(result) == 100


def test_치환_결과가_실제로_파일명으로_쓰인다(tmp_path):
    name = sanitize_filename_component('제목?:*"<>|/\\끝')
    target = tmp_path / f"{name}.mp4"
    target.write_bytes(b"x")
    assert target.exists()


# -- 날짜 -------------------------------------------------------------------


def test_심야_방송의_날짜는_로컬_기준이다():
    """UTC 로는 전날인 심야 방송. release_date 를 그대로 쓰면 하루 어긋난다."""
    kst = timezone(timedelta(hours=9))
    # 2026-08-12 01:30 KST == 2026-08-11 16:30 UTC
    epoch = datetime(2026, 8, 12, 1, 30, tzinfo=kst).timestamp()

    assert datetime.fromtimestamp(epoch, tz=timezone.utc).date() == date(2026, 8, 11)
    assert local_date_from_epoch(epoch, kst) == date(2026, 8, 12)


def test_이른_아침_방송도_로컬_기준이다():
    kst = timezone(timedelta(hours=9))
    epoch = datetime(2026, 8, 12, 8, 0, tzinfo=kst).timestamp()
    assert local_date_from_epoch(epoch, kst) == date(2026, 8, 12)


def test_서쪽_시간대에서는_반대로_어긋난다():
    """UTC 를 그대로 쓰면 다음 날로 밀리는 경우도 잡아야 한다."""
    pst = timezone(timedelta(hours=-8))
    # 2026-08-11 20:00 PST == 2026-08-12 04:00 UTC
    epoch = datetime(2026, 8, 11, 20, 0, tzinfo=pst).timestamp()

    assert datetime.fromtimestamp(epoch, tz=timezone.utc).date() == date(2026, 8, 12)
    assert local_date_from_epoch(epoch, pst) == date(2026, 8, 11)


# -- 덮어쓰기 금지 -----------------------------------------------------------


def test_같은_이름이_있으면_비켜간다(tmp_path):
    first = reserve_unique_path(tmp_path, "제목", ".mp4")
    first.write_bytes(b"original")

    second = reserve_unique_path(tmp_path, "제목", ".mp4")

    assert first.name == "제목.mp4"
    assert second.name == "제목 (2).mp4"
    assert first.read_bytes() == b"original"


def test_세_번째부터도_계속_비켜간다(tmp_path):
    names = []
    for _ in range(3):
        path = reserve_unique_path(tmp_path, "제목", ".mp4")
        path.write_bytes(b"x")
        names.append(path.name)
    assert names == ["제목.mp4", "제목 (2).mp4", "제목 (3).mp4"]


def test_예약된_경로는_비어_있고_교체할_수_있다(tmp_path):
    reserved = reserve_unique_path(tmp_path, "제목", ".mp4")
    assert reserved.exists() and reserved.stat().st_size == 0

    source = tmp_path / "src.bin"
    source.write_bytes(b"payload")
    os.replace(source, reserved)

    assert reserved.read_bytes() == b"payload"


def test_확장자에_점이_없어도_된다(tmp_path):
    assert reserve_unique_path(tmp_path, "제목", "mkv").name == "제목.mkv"
