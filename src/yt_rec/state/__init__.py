"""GUI 상태 계약.

화면 코드는 이 패키지만 import 한다. 백엔드 구현(yt-dlp 호출, API 조회 등)에
직접 의존하지 않는다.

* :mod:`~yt_rec.state.models` — GUI가 참조하는 불변 상태 모델
* :mod:`~yt_rec.state.events` — 백엔드 → 상태 계층 이벤트
* :mod:`~yt_rec.state.commands` — 화면 → 백엔드 명령 (반대 방향의 유일한 경로)
* :mod:`~yt_rec.state.store` — :class:`AppState` 저장소와 :class:`EventSource` 인터페이스
* :mod:`~yt_rec.state.stub` — 백엔드 없이 화면을 개발하기 위한 스텁 소스

두 가지 계약이 화면 코드 전체에 걸린다.

* **스레드**: 작업 스레드에서 부를 수 있는 것은 :meth:`AppState.post_event`
  하나뿐이다. 나머지를 다른 스레드에서 부르면 ``RuntimeError`` 가 난다.
* **시간대**: 모델과 이벤트의 모든 ``datetime`` 은 시간대를 가진 값이다.
  표시는 :func:`yt_rec.ui.formatting.to_local` 로 로컬로 옮겨 그린다.

자세한 내용은 각 모듈 docstring 에 있다.
"""

from .commands import (
    GuiCommand,
    SetWatchedChannels,
    StopRecording,
    UpdateSettings,
)
from .events import (
    BackendEvent,
    ChannelsChanged,
    ConnectionChanged,
    LogAppended,
    NaiveDatetimeWarning,
    QuotaChanged,
    RecordingFinished,
    RecordingProgress,
    RecordingStarted,
    WatchStatusChanged,
    naive_datetime_fields,
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
    "GuiCommand",
    "LogAppended",
    "LogEntry",
    "NaiveDatetimeWarning",
    "QuotaChanged",
    "QuotaStatus",
    "Recording",
    "RecordingFinished",
    "RecordingProgress",
    "RecordingStarted",
    "RecordingState",
    "SetWatchedChannels",
    "Severity",
    "StopReason",
    "StopRecording",
    "UpdateSettings",
    "WatchState",
    "WatchStatus",
    "WatchStatusChanged",
    "WatchedChannel",
    "naive_datetime_fields",
]
