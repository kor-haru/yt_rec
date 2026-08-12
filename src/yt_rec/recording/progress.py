"""yt-dlp 출력에서 진행 상황을 읽고, 진전 없는 상태를 감지한다.

진행률과 크기를 ``os.stat`` 으로 재면 안 된다. Windows 는 쓰기 핸들이 열린 파일의
크기를 디렉터리 엔트리에 즉시 반영하지 않아 실제보다 훨씬 작은 값이 나온다.
그래서 yt-dlp 가 직접 알려주는 숫자만 쓴다.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable, Iterator

__all__ = [
    "PROGRESS_MARKER",
    "PROGRESS_TEMPLATE",
    "LineSplitter",
    "ProgressSnapshot",
    "StallDetector",
    "parse_fragment_retry",
    "parse_gave_up",
    "parse_progress_line",
    "parse_skipped_fragment",
]

#: 우리가 찍은 진행 줄을 yt-dlp 의 다른 출력과 구분하는 표식.
PROGRESS_MARKER = "@ytrec"

#: ``--progress-template`` 에 넘길 값. 숫자와 ASCII 만 담아 인코딩 문제를 피한다.
PROGRESS_TEMPLATE = (
    f"download:{PROGRESS_MARKER}"
    "|%(progress.status)s"
    "|%(progress.downloaded_bytes)s"
    "|%(progress.total_bytes)s"
    "|%(progress.total_bytes_estimate)s"
    "|%(progress.speed)s"
    "|%(progress.eta)s"
    "|%(progress.elapsed)s"
    "|%(progress.fragment_index)s"
    "|%(progress.fragment_count)s"
    "|%(info.format_id)s"
)

# 상한이 무한이면 yt-dlp 는 ``(34/inf)`` 처럼 찍는다. 그 자체가 위험 신호다.
_FRAGMENT_RETRY = re.compile(
    r"Retrying\s+fragment\s+(\d+)\s*\((\d+)\s*/\s*(\d+|inf)\)", re.IGNORECASE
)
_FRAGMENT_SKIP = re.compile(r"Skipping\s+fragment\s+(\d+)", re.IGNORECASE)
_GIVING_UP = re.compile(r"Giving up after\s+(\d+)\s+retries", re.IGNORECASE)


def _number(token: str) -> float | None:
    """``NA``/``None``/빈 값은 ``None`` 으로."""
    token = token.strip()
    if not token or token in ("NA", "None", "none", "-"):
        return None
    try:
        return float(token)
    except ValueError:
        return None


def _integer(token: str) -> int | None:
    value = _number(token)
    return None if value is None else int(value)


@dataclass(frozen=True)
class ProgressSnapshot:
    """yt-dlp 가 알려준 한 시점의 진행 상황."""

    status: str
    downloaded_bytes: int | None = None
    total_bytes: int | None = None
    total_bytes_estimate: int | None = None
    speed: float | None = None
    eta: float | None = None
    elapsed: float | None = None
    fragment_index: int | None = None
    fragment_count: int | None = None
    format_id: str | None = None

    @property
    def expected_bytes(self) -> int | None:
        """확정 크기가 없으면 추정 크기를 쓴다."""
        return self.total_bytes if self.total_bytes is not None else self.total_bytes_estimate

    @property
    def percent(self) -> float | None:
        """0~100. 총 크기를 모르면 ``None``. 라이브는 대개 모른다."""
        total = self.expected_bytes
        if not total or self.downloaded_bytes is None:
            return None
        return min(100.0, self.downloaded_bytes * 100.0 / total)

    @property
    def advance_key(self) -> tuple:
        """'진전이 있었는가'를 판정하는 값. 값이 달라지면 진전이 있었던 것이다."""
        return (self.format_id, self.downloaded_bytes, self.fragment_index)


def parse_progress_line(line: str) -> ProgressSnapshot | None:
    """:data:`PROGRESS_TEMPLATE` 이 찍은 줄을 해석한다. 아니면 ``None``."""
    start = line.find(PROGRESS_MARKER)
    if start < 0:
        return None
    fields = line[start + len(PROGRESS_MARKER) :].split("|")
    if len(fields) < 11:
        return None
    _, status, downloaded, total, estimate, speed, eta, elapsed, frag_i, frag_n, fmt = (
        fields[:11]
    )
    format_id = fmt.strip()
    return ProgressSnapshot(
        status=status.strip() or "unknown",
        downloaded_bytes=_integer(downloaded),
        total_bytes=_integer(total),
        total_bytes_estimate=_integer(estimate),
        speed=_number(speed),
        eta=_number(eta),
        elapsed=_number(elapsed),
        fragment_index=_integer(frag_i),
        fragment_count=_integer(frag_n),
        format_id=None if format_id in ("", "NA") else format_id,
    )


def parse_fragment_retry(line: str) -> tuple[int, int, int | None] | None:
    """``Retrying fragment 12 (3/20)`` -> ``(12, 3, 20)``.

    상한이 무한이면 세 번째 값이 ``None`` 이다.
    """
    match = _FRAGMENT_RETRY.search(line)
    if not match:
        return None
    limit = match.group(3)
    return (
        int(match.group(1)),
        int(match.group(2)),
        None if limit.lower() == "inf" else int(limit),
    )


def parse_skipped_fragment(line: str) -> int | None:
    """``Skipping fragment 12 ...`` -> ``12``. 재시도 상한에 걸려 건너뛴 조각이다."""
    match = _FRAGMENT_SKIP.search(line)
    return int(match.group(1)) if match else None


def parse_gave_up(line: str) -> int | None:
    """``Giving up after 20 retries`` -> ``20``."""
    match = _GIVING_UP.search(line)
    return int(match.group(1)) if match else None


class LineSplitter:
    """바이트 조각을 줄 단위로 나눈다.

    yt-dlp 는 진행률을 ``\\r`` 로 덮어쓰기도 하므로 ``\\n`` 만으로는 줄이 안 나뉜다.
    """

    def __init__(self, encoding: str = "utf-8") -> None:
        self._buffer = bytearray()
        self._encoding = encoding

    def feed(self, chunk: bytes) -> Iterator[str]:
        self._buffer.extend(chunk)
        while True:
            index = min(
                (i for i in (self._buffer.find(b"\n"), self._buffer.find(b"\r")) if i >= 0),
                default=-1,
            )
            if index < 0:
                return
            line = bytes(self._buffer[:index])
            del self._buffer[: index + 1]
            text = line.decode(self._encoding, errors="replace").strip()
            if text:
                yield text

    def flush(self) -> Iterator[str]:
        text = bytes(self._buffer).decode(self._encoding, errors="replace").strip()
        self._buffer.clear()
        if text:
            yield text


class StallDetector:
    """일정 시간 이상 진전이 없으면 정지로 판정한다.

    ``--fragment-retries`` 를 유한하게 두면 대부분의 무한 재시도는 애초에 생기지
    않지만, 다른 이유로 멈추는 경우까지 덮으려면 바깥에서 한 겹 더 봐야 한다.
    """

    def __init__(
        self, timeout_seconds: float, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds 는 양수여야 한다")
        self.timeout_seconds = float(timeout_seconds)
        self._clock = clock
        self._last_key: tuple | None = None
        self._last_advance_at: float | None = None

    def note(self, key: tuple) -> bool:
        """진행 상태를 알린다. 진전이 있었으면 참."""
        now = self._clock()
        if self._last_advance_at is None:
            self._last_key = key
            self._last_advance_at = now
            return True
        if key != self._last_key:
            self._last_key = key
            self._last_advance_at = now
            return True
        return False

    def note_activity(self) -> None:
        """진행률과 무관한 활동(예: 병합 시작)으로 시계를 되돌린다."""
        self._last_advance_at = self._clock()

    @property
    def started(self) -> bool:
        """첫 진행 신호를 받았는가. 받기 전에는 정지로 판정하지 않는다."""
        return self._last_advance_at is not None

    @property
    def idle_seconds(self) -> float:
        if self._last_advance_at is None:
            return 0.0
        return max(0.0, self._clock() - self._last_advance_at)

    def is_stalled(self) -> bool:
        return self.started and self.idle_seconds >= self.timeout_seconds
