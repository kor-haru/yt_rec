"""백엔드가 상태 계층으로 밀어 넣는 이벤트.

방향은 항상 백엔드 → :class:`~yt_rec.state.store.AppState` → GUI 한 방향이다.
GUI가 백엔드를 주기적으로 조회하는 경로는 존재하지 않는다.

이벤트는 모두 불변 데이터 클래스이므로 작업 스레드에서 만들어 그대로
시그널에 실어 보내도 안전하다. 상태 적용과 위젯 갱신은 GUI 스레드에서만
일어난다(:mod:`yt_rec.state.store` 참고).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .models import (
    CompletedRecording,
    ConnectionState,
    LogEntry,
    QuotaStatus,
    Recording,
    RecordingState,
    StopReason,
    WatchedChannel,
    WatchState,
)

__all__ = [
    "ConnectionChanged",
    "WatchStatusChanged",
    "ChannelsChanged",
    "RecordingStarted",
    "RecordingProgress",
    "RecordingFinished",
    "LogAppended",
    "QuotaChanged",
    "BackendEvent",
]


@dataclass(frozen=True, slots=True)
class ConnectionChanged:
    """백엔드 연결 상태가 바뀌었다."""

    state: ConnectionState


@dataclass(frozen=True, slots=True)
class WatchStatusChanged:
    """감시 루프 요약이 바뀌었다."""

    state: WatchState
    channel_count: int = 0
    stop_reason: StopReason | None = None
    next_check_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ChannelsChanged:
    """감시 대상 채널 목록 전체 교체."""

    channels: tuple[WatchedChannel, ...] = ()


@dataclass(frozen=True, slots=True)
class RecordingStarted:
    """녹화가 시작됐다. 같은 ``recording_id`` 가 이미 있으면 덮어쓴다."""

    recording: Recording


@dataclass(frozen=True, slots=True)
class RecordingProgress:
    """진행 중 녹화의 보고값 갱신. 초당 수십 건까지 올 수 있다.

    ``reported_bytes`` 와 ``reported_elapsed`` 는 녹화 프로세스가 보고한
    값이어야 한다. 이 이벤트를 만드는 쪽이 ``os.stat`` 으로 크기를 재서
    채우면 계약이 깨진다.
    """

    recording_id: str
    reported_bytes: int
    reported_elapsed: timedelta
    state: RecordingState = RecordingState.RECORDING
    retry_count: int = 0
    detail: str = ""
    reported_at: datetime | None = None
    """보고 시각. 생략하면 상태 계층이 수신 시각으로 채운다."""


@dataclass(frozen=True, slots=True)
class RecordingFinished:
    """녹화가 마무리됐다. 진행 목록에서 빠지고 완료 이력 맨 앞에 붙는다."""

    completed: CompletedRecording


@dataclass(frozen=True, slots=True)
class LogAppended:
    """로그 한 줄이 쌓였다. ``ERROR`` 수준이면 오류 카운터가 함께 올라간다."""

    entry: LogEntry


@dataclass(frozen=True, slots=True)
class QuotaChanged:
    """API quota 사용량이 바뀌었다."""

    quota: QuotaStatus = field(default_factory=QuotaStatus)


BackendEvent = (
    ConnectionChanged
    | WatchStatusChanged
    | ChannelsChanged
    | RecordingStarted
    | RecordingProgress
    | RecordingFinished
    | LogAppended
    | QuotaChanged
)
