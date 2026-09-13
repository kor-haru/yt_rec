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
    ConnectAccount,
    DisconnectAccount,
    GuiCommand,
    OpenNotificationBrowser,
    OpenNotificationSettings,
    OpenRecordingPath,
    RefreshArchive,
    RefreshSubscriptions,
    SetWatchedChannels,
    StopRecording,
    UpdateSettings,
)
from .events import (
    AccountChanged,
    BackendEvent,
    ChannelsChanged,
    CompletedChanged,
    ConnectionChanged,
    LogAppended,
    NaiveDatetimeWarning,
    NotificationStatusChanged,
    QuotaChanged,
    RecordingFinished,
    RecordingProgress,
    RecordingStarted,
    SubscriptionsChanged,
    SettingsChanged,
    SettingsSaveFailed,
    WatchStatusChanged,
    naive_datetime_fields,
)
from .models import (
    AccountInfo,
    AppSnapshot,
    CompletedRecording,
    CompletionStatus,
    ConnectionState,
    LogEntry,
    NotificationStatus,
    QuotaStatus,
    Recording,
    RecordingState,
    Severity,
    StopReason,
    Subscription,
    WatchedChannel,
    WatchState,
    WatchStatus,
)
from .store import AppState, EventSource

__all__ = [
    "AccountChanged",
    "AccountInfo",
    "AppSnapshot",
    "AppState",
    "BackendEvent",
    "ChannelsChanged",
    "CompletedRecording",
    "CompletedChanged",
    "CompletionStatus",
    "ConnectAccount",
    "ConnectionChanged",
    "ConnectionState",
    "DisconnectAccount",
    "EventSource",
    "GuiCommand",
    "LogAppended",
    "LogEntry",
    "NaiveDatetimeWarning",
    "NotificationStatus",
    "NotificationStatusChanged",
    "OpenNotificationBrowser",
    "OpenNotificationSettings",
    "OpenRecordingPath",
    "QuotaChanged",
    "QuotaStatus",
    "Recording",
    "RecordingFinished",
    "RecordingProgress",
    "RecordingStarted",
    "RecordingState",
    "RefreshSubscriptions",
    "RefreshArchive",
    "SetWatchedChannels",
    "SettingsChanged",
    "SettingsSaveFailed",
    "Severity",
    "StopReason",
    "StopRecording",
    "Subscription",
    "SubscriptionsChanged",
    "UpdateSettings",
    "WatchState",
    "WatchStatus",
    "WatchStatusChanged",
    "WatchedChannel",
    "naive_datetime_fields",
]
