"""yt-dlp 출력 해석과 정지 감지 (#14)."""

from __future__ import annotations

import pytest

from yt_rec.recording.progress import (
    PROGRESS_MARKER,
    LineSplitter,
    StallDetector,
    parse_fragment_retry,
    parse_gave_up,
    parse_progress_line,
    parse_skipped_fragment,
)


def progress_line(**fields) -> str:
    values = {
        "status": "downloading",
        "downloaded_bytes": "1048576",
        "total_bytes": "NA",
        "total_bytes_estimate": "NA",
        "speed": "524288.5",
        "eta": "NA",
        "elapsed": "12.5",
        "fragment_index": "42",
        "fragment_count": "NA",
        "format_id": "137",
    }
    values.update({k: str(v) for k, v in fields.items()})
    return PROGRESS_MARKER + "|" + "|".join(str(values[k]) for k in (
        "status", "downloaded_bytes", "total_bytes", "total_bytes_estimate",
        "speed", "eta", "elapsed", "fragment_index", "fragment_count", "format_id",
    ))


def test_진행_줄을_해석한다():
    snapshot = parse_progress_line(progress_line())
    assert snapshot is not None
    assert snapshot.status == "downloading"
    assert snapshot.downloaded_bytes == 1_048_576
    assert snapshot.fragment_index == 42
    assert snapshot.format_id == "137"
    assert snapshot.speed == pytest.approx(524288.5)


def test_NA_는_None_이_된다():
    snapshot = parse_progress_line(progress_line())
    assert snapshot.total_bytes is None
    assert snapshot.eta is None
    assert snapshot.fragment_count is None


def test_라이브는_총_크기를_몰라_퍼센트가_없다():
    assert parse_progress_line(progress_line()).percent is None


def test_총_크기를_알면_퍼센트가_나온다():
    snapshot = parse_progress_line(progress_line(total_bytes=4_194_304))
    assert snapshot.percent == pytest.approx(25.0)
    assert snapshot.expected_bytes == 4_194_304


def test_추정_크기도_퍼센트에_쓰인다():
    snapshot = parse_progress_line(progress_line(total_bytes_estimate=2_097_152))
    assert snapshot.percent == pytest.approx(50.0)


def test_앞에_다른_출력이_붙어도_해석한다():
    assert parse_progress_line("[download] " + progress_line()) is not None


def test_진행_줄이_아니면_None():
    assert parse_progress_line("[download] Destination: abc.f137.mp4") is None
    assert parse_progress_line("") is None


# -- 재시도/건너뜀 -------------------------------------------------------------


def test_조각_재시도_줄을_해석한다():
    line = "[download] Got error: HTTP Error 404: Not Found. Retrying fragment 2867 (3/20)..."
    assert parse_fragment_retry(line) == (2867, 3, 20)


def test_건너뛴_조각_줄을_해석한다():
    assert parse_skipped_fragment("[download] Skipping fragment 2867 ...") == 2867


def test_포기_줄을_해석한다():
    line = "[download] Got error: HTTP Error 404: Not Found; Giving up after 20 retries"
    assert parse_gave_up(line) == 20


def test_관계없는_줄에서는_아무것도_안_나온다():
    line = "[info] Downloading 1 format(s): 137+140"
    assert parse_fragment_retry(line) is None
    assert parse_skipped_fragment(line) is None


# -- 줄 나누기 ----------------------------------------------------------------


def test_캐리지_리턴으로도_줄이_나뉜다():
    splitter = LineSplitter()
    lines = list(splitter.feed(b"first\rsecond\nthird\r\n"))
    assert lines == ["first", "second", "third"]


def test_조각난_입력을_이어_붙인다():
    """멀티바이트 문자가 청크 경계에서 잘려도 뒤 조각과 합쳐 읽는다."""
    splitter = LineSplitter()
    payload = "한글 절반\n".encode("utf-8")
    assert list(splitter.feed(payload[:5])) == []
    assert list(splitter.feed(payload[5:])) == ["한글 절반"]


def test_UTF8_로_디코딩한다():
    splitter = LineSplitter()
    assert list(splitter.feed("제목 테스트\n".encode("utf-8"))) == ["제목 테스트"]


def test_마지막_줄바꿈이_없어도_flush_로_나온다():
    splitter = LineSplitter()
    assert list(splitter.feed(b"tail")) == []
    assert list(splitter.flush()) == ["tail"]


# -- 정지 감지 ----------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_진전이_없으면_정지로_판정한다():
    clock = FakeClock()
    detector = StallDetector(60, clock=clock)

    detector.note(("137", 100, 1))
    clock.advance(59)
    assert not detector.is_stalled()

    clock.advance(2)
    assert detector.is_stalled()
    assert detector.idle_seconds == pytest.approx(61)


def test_진전이_있으면_시계가_되돌아간다():
    clock = FakeClock()
    detector = StallDetector(60, clock=clock)

    detector.note(("137", 100, 1))
    clock.advance(50)
    assert detector.note(("137", 200, 2)) is True
    clock.advance(50)

    assert not detector.is_stalled()


def test_같은_값이_반복되면_진전이_아니다():
    clock = FakeClock()
    detector = StallDetector(60, clock=clock)

    detector.note(("137", 100, 1))
    clock.advance(30)
    assert detector.note(("137", 100, 1)) is False
    clock.advance(31)
    assert detector.is_stalled()


def test_포맷이_바뀌어_바이트가_초기화돼도_진전으로_본다():
    """영상 다음 음성을 받기 시작하면 downloaded_bytes 가 0으로 돌아간다."""
    clock = FakeClock()
    detector = StallDetector(60, clock=clock)

    detector.note(("137", 900_000, 300))
    clock.advance(30)
    assert detector.note(("140", 1_000, 1)) is True


def test_첫_진행_신호_전에는_정지로_판정하지_않는다():
    clock = FakeClock()
    detector = StallDetector(60, clock=clock)
    clock.advance(600)
    assert not detector.started
    assert not detector.is_stalled()


def test_조각_건너뜀도_활동으로_친다():
    clock = FakeClock()
    detector = StallDetector(60, clock=clock)
    detector.note(("137", 100, 1))
    clock.advance(59)
    detector.note_activity()
    clock.advance(30)
    assert not detector.is_stalled()
