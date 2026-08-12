"""GUI 상태 계약.

화면 코드는 이 패키지만 import 한다. 백엔드 구현(yt-dlp 호출, API 조회 등)에
직접 의존하지 않는다.

* :mod:`~yt_rec.state.models` — GUI가 참조하는 불변 상태 모델
* :mod:`~yt_rec.state.events` — 백엔드 → 상태 계층 이벤트
* :mod:`~yt_rec.state.store` — :class:`AppState` 저장소와 :class:`EventSource` 인터페이스
* :mod:`~yt_rec.state.stub` — 백엔드 없이 화면을 개발하기 위한 스텁 소스
"""

from .events import (
    BackendEvent,
    ChannelsChanged,
    ConnectionChanged,
    LogAppended,
    QuotaChanged,
    RecordingFinished,
    RecordingProgress,
    RecordingStarted,
    WatchStatusChanged,
)
from .models import (
    AppSnapshot,
    CompletedRecording,
    CompletionStatus,
    ConnectionState,
    LogEntry,
    QuotaStatus,
    Recording,
    RecordingState,
    Severity,
    StopReason,
    WatchedChannel,
    WatchState,
    WatchStatus,
)
from .store import AppState, EventSource

__all__ = [
    "AppSnapshot",
    "AppState",
    "BackendEvent",
    "ChannelsChanged",
    "CompletedRecording",
    "CompletionStatus",
    "ConnectionChanged",
    "ConnectionState",
    "EventSource",
    "LogAppended",
    "LogEntry",
    "QuotaChanged",
    "QuotaStatus",
    "Recording",
    "RecordingFinished",
    "RecordingProgress",
    "RecordingStarted",
    "RecordingState",
    "Severity",
    "StopReason",
    "WatchState",
    "WatchStatus",
    "WatchStatusChanged",
    "WatchedChannel",
]
