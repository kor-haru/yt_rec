"""상태 저장소. 백엔드 이벤트를 받아 모델을 갱신하고 Qt 시그널로 GUI에 밀어준다.

GUI 쪽 규칙
-----------
* 화면은 :class:`AppState` 의 시그널만 구독한다. 백엔드를 직접 조회하지 않고,
  타이머로 상태를 되묻지도 않는다.
* :class:`AppState` 는 GUI 스레드에 산다. 작업 스레드는 :meth:`AppState.post_event`
  나 :class:`EventSource` 의 시그널로 이벤트를 보내며, 그 경우 Qt가 큐 연결로
  GUI 스레드에 마셜링한다. 작업 스레드가 위젯을 직접 만지는 경로는 없다.

갱신 빈도 제한
--------------
녹화 진행 이벤트는 초당 수십~수백 건까지 올라올 수 있다. 이벤트는 도착 즉시
내부 모델에 반영하되(값 갱신은 사전 조작이라 값싸다), GUI로 나가는 시그널은
:attr:`AppState.emit_interval_ms` 마다 한 번으로 묶어 내보낸다. 그래서 입력이
아무리 빨라도 화면 갱신은 초당 최대 ``1000 / emit_interval_ms`` 회로 묶인다.
``emit_interval_ms=0`` 이면 묶지 않고 즉시 내보낸다(테스트용).
"""

from __future__ import annotations

from dataclasses import replace as _replace
from datetime import datetime, timezone

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot

from . import events as ev
from .models import (
    AppSnapshot,
    CompletedRecording,
    ConnectionState,
    LogEntry,
    QuotaStatus,
    Recording,
    Severity,
    StopReason,
    WatchedChannel,
    WatchState,
    WatchStatus,
)

__all__ = ["AppState", "EventSource", "MAX_COMPLETED", "MAX_LOGS"]


MAX_COMPLETED = 200
"""완료 이력 보관 상한. 장시간 구동에서 메모리가 무한히 늘지 않게 한다."""

MAX_LOGS = 1000
"""로그 보관 상한. 초과분은 오래된 것부터 버린다(오류 누적 카운터는 유지)."""

DEFAULT_EMIT_INTERVAL_MS = 200
"""기본 갱신 간격. 초당 5회로 묶는다."""


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


class EventSource(QObject):
    """백엔드 이벤트 공급자의 공통 기반.

    구현체는 이벤트가 생길 때마다 :attr:`event_ready` 를 방출한다. 소스가 작업
    스레드에 살면 Qt가 자동으로 큐 연결을 골라 GUI 스레드로 넘겨준다.

    실제 감시·녹화 백엔드(#3, #4)와 :class:`~yt_rec.state.stub.StubEventSource`
    가 이 인터페이스를 공유한다. 화면 이슈는 어느 쪽이 붙었는지 알 필요가 없다.
    """

    event_ready = Signal(object)
    """payload: :data:`~yt_rec.state.events.BackendEvent`"""

    def start(self) -> None:
        """이벤트 공급을 시작한다. 기본 구현은 아무것도 하지 않는다."""

    def stop(self) -> None:
        """이벤트 공급을 멈춘다. 기본 구현은 아무것도 하지 않는다."""


class AppState(QObject):
    """GUI가 참조하는 단일 상태 저장소."""

    connection_changed = Signal(object)
    """payload: :class:`~yt_rec.state.models.ConnectionState`"""

    watch_changed = Signal(object)
    """payload: :class:`~yt_rec.state.models.WatchStatus`"""

    channels_changed = Signal(object)
    """payload: ``tuple[WatchedChannel, ...]``"""

    recordings_changed = Signal(object)
    """payload: ``tuple[Recording, ...]`` — 진행 중 녹화 전체"""

    completed_changed = Signal(object)
    """payload: ``tuple[CompletedRecording, ...]`` — 최신순"""

    logs_changed = Signal(object)
    """payload: ``tuple[LogEntry, ...]`` — 최신순"""

    errors_changed = Signal(int, int)
    """payload: ``(누적 오류 수, 미확인 오류 수)``"""

    quota_changed = Signal(object)
    """payload: :class:`~yt_rec.state.models.QuotaStatus`"""

    snapshot_changed = Signal(object)
    """payload: :class:`~yt_rec.state.models.AppSnapshot` — 무엇이든 바뀌면 방출.

    세밀한 시그널을 각각 붙이기 번거로운 화면은 이것 하나만 구독해도 된다.
    """

    _posted = Signal(object)  # 작업 스레드 → GUI 스레드 마셜링용 내부 시그널

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        emit_interval_ms: int = DEFAULT_EMIT_INTERVAL_MS,
    ) -> None:
        super().__init__(parent)
        self.emit_interval_ms = emit_interval_ms

        self._connection = ConnectionState.DISCONNECTED
        self._watch = WatchStatus()
        self._channels: tuple[WatchedChannel, ...] = ()
        self._recordings: dict[str, Recording] = {}
        self._completed: list[CompletedRecording] = []
        self._logs: list[LogEntry] = []
        self._error_count = 0
        self._unseen_error_count = 0
        self._quota = QuotaStatus()

        self._dirty: set[str] = set()
        self._sources: list[EventSource] = []

        self._flush_timer = QTimer(self)
        self._flush_timer.setSingleShot(True)
        self._flush_timer.timeout.connect(self.flush)

        # 작업 스레드에서 post_event()로 넣은 이벤트를 GUI 스레드에서 적용한다.
        self._posted.connect(self._on_posted, Qt.ConnectionType.QueuedConnection)

    # ------------------------------------------------------------------
    # 읽기
    # ------------------------------------------------------------------
    @property
    def connection(self) -> ConnectionState:
        return self._connection

    @property
    def watch(self) -> WatchStatus:
        return self._watch

    @property
    def channels(self) -> tuple[WatchedChannel, ...]:
        return self._channels

    @property
    def recordings(self) -> tuple[Recording, ...]:
        return tuple(self._recordings.values())

    @property
    def completed(self) -> tuple[CompletedRecording, ...]:
        return tuple(self._completed)

    @property
    def logs(self) -> tuple[LogEntry, ...]:
        return tuple(self._logs)

    @property
    def error_count(self) -> int:
        return self._error_count

    @property
    def unseen_error_count(self) -> int:
        return self._unseen_error_count

    @property
    def quota(self) -> QuotaStatus:
        return self._quota

    def snapshot(self) -> AppSnapshot:
        """현재 상태 전체를 한 덩어리로 돌려준다."""
        return AppSnapshot(
            connection=self._connection,
            watch=self._watch,
            channels=self._channels,
            recordings=self.recordings,
            completed=self.completed,
            logs=self.logs,
            error_count=self._error_count,
            unseen_error_count=self._unseen_error_count,
            quota=self._quota,
        )

    # ------------------------------------------------------------------
    # 이벤트 주입
    # ------------------------------------------------------------------
    def attach(self, source: EventSource) -> None:
        """이벤트 소스를 연결한다. 소스가 다른 스레드면 Qt가 큐 연결로 넘긴다."""
        source.event_ready.connect(self._on_source_event)
        self._sources.append(source)

    def detach(self, source: EventSource) -> None:
        """연결을 끊는다. 백엔드가 내려가면 `연결 안 됨` 으로 되돌린다."""
        try:
            source.event_ready.disconnect(self._on_source_event)
        except (RuntimeError, TypeError):
            pass
        if source in self._sources:
            self._sources.remove(source)

    def post_event(self, event: ev.BackendEvent) -> None:
        """어느 스레드에서 불러도 안전한 이벤트 주입구.

        시그널 방출은 스레드 안전하며, 큐 연결이 GUI 스레드에서 적용되도록
        보장한다. 작업 스레드가 :meth:`apply` 를 직접 부르면 안 된다.
        """
        self._posted.emit(event)

    @Slot(object)
    def _on_source_event(self, event: object) -> None:
        self.apply(event)  # type: ignore[arg-type]

    @Slot(object)
    def _on_posted(self, event: object) -> None:
        self.apply(event)  # type: ignore[arg-type]

    def apply(self, event: ev.BackendEvent) -> None:
        """이벤트 하나를 모델에 반영한다. **GUI 스레드에서만** 호출한다."""
        handler = self._HANDLERS.get(type(event))
        if handler is None:
            raise TypeError(f"알 수 없는 백엔드 이벤트: {type(event)!r}")
        handler(self, event)
        self._schedule_emit()

    def apply_all(self, events: object) -> None:
        """이벤트 여러 건을 한 번에 반영한다."""
        for event in events:  # type: ignore[union-attr]
            self.apply(event)

    # ------------------------------------------------------------------
    # GUI가 부르는 조작
    # ------------------------------------------------------------------
    def mark_errors_seen(self) -> None:
        """미확인 오류 배지를 해제한다. 로그 뷰어(#12)를 열 때 호출한다."""
        if self._unseen_error_count:
            self._unseen_error_count = 0
            self._dirty.add("errors")
            self._schedule_emit()

    # ------------------------------------------------------------------
    # 개별 이벤트 처리
    # ------------------------------------------------------------------
    def _on_connection(self, event: ev.ConnectionChanged) -> None:
        if self._connection == event.state:
            return
        self._connection = event.state
        self._dirty.add("connection")
        if event.state is not ConnectionState.CONNECTED:
            # 백엔드가 없으면 감시 상태를 알 수 없다. 마지막 값을 그대로
            # 보여주면 사용자가 감시 중이라고 오해한다.
            self._watch = WatchStatus(
                state=WatchState.UNKNOWN,
                channel_count=self._watch.channel_count,
                stop_reason=StopReason.BACKEND_DOWN,
                next_check_at=None,
            )
            self._dirty.add("watch")

    def _on_watch(self, event: ev.WatchStatusChanged) -> None:
        self._watch = WatchStatus(
            state=event.state,
            channel_count=event.channel_count,
            stop_reason=event.stop_reason,
            next_check_at=event.next_check_at,
        )
        self._dirty.add("watch")

    def _on_channels(self, event: ev.ChannelsChanged) -> None:
        self._channels = tuple(event.channels)
        self._dirty.add("channels")

    def _on_recording_started(self, event: ev.RecordingStarted) -> None:
        rec = event.recording
        if rec.started_at is None:
            rec = _replace(rec, started_at=_now())
        if rec.reported_at is None:
            rec = _replace(rec, reported_at=rec.started_at)
        self._recordings[rec.recording_id] = rec
        self._dirty.add("recordings")

    def _on_recording_progress(self, event: ev.RecordingProgress) -> None:
        current = self._recordings.get(event.recording_id)
        if current is None:
            # 시작 이벤트를 놓쳤어도 진행 보고만으로 카드를 세울 수 있어야 한다.
            current = Recording(
                recording_id=event.recording_id,
                title=event.recording_id,
                started_at=_now(),
            )
        self._recordings[event.recording_id] = _replace(
            current,
            reported_bytes=event.reported_bytes,
            reported_elapsed=event.reported_elapsed,
            reported_at=event.reported_at or _now(),
            state=event.state,
            retry_count=event.retry_count,
            detail=event.detail,
        )
        self._dirty.add("recordings")

    def _on_recording_finished(self, event: ev.RecordingFinished) -> None:
        done = event.completed
        self._recordings.pop(done.recording_id, None)
        if done.finished_at is None:
            done = _replace(done, finished_at=_now())
        self._completed.insert(0, done)
        del self._completed[MAX_COMPLETED:]
        self._dirty.add("recordings")
        self._dirty.add("completed")

    def _on_log(self, event: ev.LogAppended) -> None:
        self._logs.insert(0, event.entry)
        del self._logs[MAX_LOGS:]
        self._dirty.add("logs")
        if event.entry.severity is Severity.ERROR:
            self._error_count += 1
            self._unseen_error_count += 1
            self._dirty.add("errors")

    def _on_quota(self, event: ev.QuotaChanged) -> None:
        self._quota = event.quota
        self._dirty.add("quota")

    _HANDLERS = {
        ev.ConnectionChanged: _on_connection,
        ev.WatchStatusChanged: _on_watch,
        ev.ChannelsChanged: _on_channels,
        ev.RecordingStarted: _on_recording_started,
        ev.RecordingProgress: _on_recording_progress,
        ev.RecordingFinished: _on_recording_finished,
        ev.LogAppended: _on_log,
        ev.QuotaChanged: _on_quota,
    }

    # ------------------------------------------------------------------
    # 방출
    # ------------------------------------------------------------------
    def _schedule_emit(self) -> None:
        if not self._dirty:
            return
        if self.emit_interval_ms <= 0:
            self.flush()
            return
        if not self._flush_timer.isActive():
            self._flush_timer.start(self.emit_interval_ms)

    @Slot()
    def flush(self) -> None:
        """묶어 두었던 변경을 지금 즉시 방출한다."""
        self._flush_timer.stop()
        if not self._dirty:
            return
        dirty, self._dirty = self._dirty, set()

        if "connection" in dirty:
            self.connection_changed.emit(self._connection)
        if "watch" in dirty:
            self.watch_changed.emit(self._watch)
        if "channels" in dirty:
            self.channels_changed.emit(self._channels)
        if "recordings" in dirty:
            self.recordings_changed.emit(self.recordings)
        if "completed" in dirty:
            self.completed_changed.emit(self.completed)
        if "logs" in dirty:
            self.logs_changed.emit(self.logs)
        if "errors" in dirty:
            self.errors_changed.emit(self._error_count, self._unseen_error_count)
        if "quota" in dirty:
            self.quota_changed.emit(self._quota)

        self.snapshot_changed.emit(self.snapshot())

    # ------------------------------------------------------------------
    def is_gui_thread(self) -> bool:
        """이 저장소가 사는 스레드에서 호출됐는지. 테스트와 진단용."""
        return QThread.currentThread() is self.thread()
