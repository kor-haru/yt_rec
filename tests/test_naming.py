"""파일명 결정 규칙 (#4: 금지 문자 치환, 덮어쓰기 금지, #92: 대괄호 토큰)."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from yt_rec.recording.naming import (
    DEFAULT_FILENAME_TEMPLATE,
    FILENAME_TOKENS,
    FORBIDDEN_CHAR_MAP,
    SAMPLE_NAME_FIELDS,
    local_date_from_epoch,
    migrate_filename_template,
    render_filename,
    reserve_unique_path,
    sanitize_filename_component,
    unknown_filename_tokens,
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


# -- 파일명 배치 --------------------------------------------------------------


def test_적어_준_예시가_그대로_나온다():
    """사용자가 적어 준 두 줄(#92). 한 글자도 달라지면 안 된다."""
    assert (
        render_filename("[YYMMDD]_[채널명]_[영상제목]_([영상고유url키])", SAMPLE_NAME_FIELDS)
        == "260921_침착맨_오늘도 한다_(EYEAaG3cxME)"
    )
    assert (
        render_filename("[YYYY-MM-DD] [채널명] [영상제목]", SAMPLE_NAME_FIELDS)
        == "2026-09-21 침착맨 오늘도 한다"
    )


def test_대괄호는_출력에_남지_않고_바깥_글자는_남는다():
    assert render_filename("녹화-[영상제목]!", SAMPLE_NAME_FIELDS) == "녹화-오늘도 한다!"


def test_모든_토큰이_예시대로_풀린다():
    """이슈의 토큰 표 그대로. 이름을 줄이거나 바꾸면 여기서 걸린다."""
    rendered = {
        name: render_filename(f"[{name}]", SAMPLE_NAME_FIELDS) for name in FILENAME_TOKENS
    }
    assert rendered == {
        "YYMMDD": "260921",
        "YYYYMMDD": "20260921",
        "YYYY-MM-DD": "2026-09-21",
        "YYYY.MM.DD": "2026.09.21",
        "HHMM": "1954",
        "HH-MM": "19-54",
        "채널명": "침착맨",
        "영상제목": "오늘도 한다",
        "영상고유url키": "EYEAaG3cxME",
        "채널ID": "UCUj6rrhMTR9pipbAWBAMvUQ",
        "화질": "1080p",
    }


def test_채널명이_없으면_구분자가_접힌다():
    """빈 토큰이 `260921__제목` 같은 흔적을 남기면 안 된다."""
    fields = replace(SAMPLE_NAME_FIELDS, channel="")

    assert (
        render_filename("[YYMMDD]_[채널명]_[영상제목]_([영상고유url키])", fields)
        == "260921_오늘도 한다_(EYEAaG3cxME)"
    )
    assert render_filename("[YYYY-MM-DD] [채널명] [영상제목]", fields) == "2026-09-21 오늘도 한다"


def test_빈_토큰만_든_괄호_짝은_지운다():
    fields = replace(SAMPLE_NAME_FIELDS, video_id="", quality="")
    assert render_filename("[영상제목]_([영상고유url키])", fields) == "오늘도 한다"
    assert render_filename("[영상제목] [[화질]]", fields) == "오늘도 한다"
    assert render_filename("[영상제목]_([채널ID]-[화질])", replace(fields, channel_id="")) == "오늘도 한다"


def test_앞뒤_구분자는_턴다():
    fields = replace(SAMPLE_NAME_FIELDS, channel="", quality="")
    assert render_filename("[채널명]_[영상제목]_[화질]", fields) == "오늘도 한다"


def test_제목의_금지_문자만_바뀌고_적어_준_구분자는_그대로다():
    """조각 단위로 안전화한다. 사용자가 적은 `_` 는 건드리지 않는다."""
    fields = replace(SAMPLE_NAME_FIELDS, title="Q&A / 내 집 마련")

    assert render_filename("[채널명]_[영상제목]", fields) == "침착맨_Q&A ／ 내 집 마련"


def test_제목_길이_상한은_제목에만_걸린다():
    fields = replace(SAMPLE_NAME_FIELDS, title="가" * 300)
    rendered = render_filename("[YYMMDD]_[영상제목]_[화질]", fields, max_title_chars=20)
    assert rendered == "260921_" + "가" * 20 + "_1080p"


def test_모르는_토큰은_사유와_함께_거부한다():
    assert unknown_filename_tokens("[YYMMDD]_[없는거]_[영상제목]") == ["없는거"]
    assert unknown_filename_tokens(DEFAULT_FILENAME_TEMPLATE) == []
    with pytest.raises(ValueError, match=r"\[없는거\]"):
        render_filename("[YYMMDD]_[없는거]", SAMPLE_NAME_FIELDS)


def test_옛_문법은_대응하는_대괄호로_읽는다():
    """설정 화면에 없던 필드라 저장된 값은 사실상 전부 기본값이다(#92)."""
    assert migrate_filename_template("{date}_{title}") == DEFAULT_FILENAME_TEMPLATE
    assert (
        migrate_filename_template("{date}_{channel}_{title}_{video_id}")
        == "[YYYY-MM-DD]_[채널명]_[영상제목]_[영상고유url키]"
    )
    assert migrate_filename_template(DEFAULT_FILENAME_TEMPLATE) == DEFAULT_FILENAME_TEMPLATE
    assert render_filename("{date}_{title}", SAMPLE_NAME_FIELDS) == "2026-09-21_오늘도 한다"


def test_시각_토큰도_넘겨받은_시작_시각을_쓴다():
    """로컬로 옮긴 시각을 그대로 쓴다. 여기서 다시 시간대를 계산하지 않는다."""
    fields = replace(SAMPLE_NAME_FIELDS, start=datetime(2026, 8, 12, 1, 30))
    assert render_filename("[YYYYMMDD]_[HHMM]", fields) == "20260812_0130"
