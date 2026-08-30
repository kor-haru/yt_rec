"""상태 저장소. 백엔드 이벤트를 받아 모델을 갱신하고 Qt 시그널로 GUI에 밀어준다.

GUI 쪽 규칙
-----------
* 화면은 :class:`AppState` 의 시그널만 구독한다. 백엔드를 직접 조회하지 않고,
  타이머로 상태를 되묻지도 않는다.
* :class:`AppState` 는 GUI 스레드에 산다. 작업 스레드가 위젯을 직접 만지는
  경로는 없다.

스레드 계약
-----------
**작업 스레드에서 부를 수 있는 것은 :meth:`AppState.post_event` 하나뿐이다.**
나머지 public 메서드와 프로퍼티를 다른 스레드에서 부르면 그 자리에서
``RuntimeError`` 가 난다.

이 규칙이 독스트링이 아니라 코드인 이유가 있다. 예전에는 작업 스레드가
:meth:`~AppState.apply` 를 직접 부를 수 있었고, 그러면 모델은 갱신되는데
방출을 예약하는 ``QTimer.start()`` 가 다른 스레드라서 조용히 실패해 **시그널이
한 번도 나가지 않았다**. Qt 경고도, 예외도, 로그도 없이 화면만 영구 정지했다
(실측: 모델은 최신, 방출 0회, 타이머 비활성, stderr 비어 있음). 갱신 묶기를
끈 경우(``emit_interval_ms=0``)에는 반대로 작업 스레드가 내부 리스트를 GUI
스레드의 쓰기와 동기화 없이 읽었다. 어느 쪽이든 조용히 깨지는 것이 가장 나쁘다.

주입 경로와 순서
----------------
상태를 바꾸는 입구는 셋이고 순서 의미는 하나로 맞춰 두었다.

1. :meth:`AppState.apply` / :meth:`AppState.apply_all` — GUI 스레드 전용. 동기.
2. :meth:`AppState.post_event` — 어느 스레드에서든 안전.
   **GUI 스레드에서 불렀으면 동기**로 즉시 적용되고, 작업 스레드에서 불렀으면
   큐 연결로 GUI 스레드에 넘겨 적용된다.
3. :meth:`AppState.attach` 로 붙인 :class:`EventSource` 의 ``event_ready`` —
   Qt 자동 연결이므로 같은 스레드면 동기, 다른 스레드면 큐 연결이다.

한 문장으로: **같은 스레드에서 보낸 이벤트는 부른 순서대로 즉시 적용된다.
다른 스레드에서 보낸 이벤트는 GUI 이벤트 루프에 도착한 순서대로 적용된다.**

예전에는 ``post_event`` 만 GUI 스레드에서도 지연됐다. 그래서 먼저 보낸 이벤트가
나중에 적용되어 뒤에 보낸 값을 덮어썼다(실측: ``post_event(1)`` 뒤에
``apply(2)`` → 방출 순서 ``[2, 1]``, 최종 상태 ``1``).

갱신 빈도 제한
--------------
녹화 진행 이벤트는 초당 수십~수백 건까지 올라올 수 있다. 이벤트는 도착 즉시
내부 모델에 반영하되(값 갱신은 사전 조작이라 값싸다), GUI로 나가는 시그널은
:attr:`AppState.emit_interval_ms` 마다 한 번으로 묶어 내보낸다. 그래서 입력이
아무리 빨라도 화면 갱신은 초당 최대 ``1000 / emit_interval_ms`` 회로 묶인다.
``emit_interval_ms=0`` 이면 묶지 않고 즉시 내보낸다(테스트용).

화면 → 백엔드
-------------
반대 방향은 :meth:`AppState.send_command` 와 :mod:`yt_rec.state.commands` 다.
화면이 백엔드 객체를 직접 붙잡는 경로는 없다.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace as _replace
from datetime import datetime, timezone

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot

from . import commands as cmd
from . import events as ev
from .models import (
    AccountInfo,
    AppSnapshot,
    CompletedRecording,
    ConnectionState,
    LogEntry,
    QuotaStatus,
    Recording,
    Severity,
    StopReason,
    Subscription,
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

    def handle_command(self, command: object) -> None:
        """화면 명령. 기본 구현은 무시한다."""


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

    account_changed = Signal(object)
    """payload: :class:`~yt_rec.state.models.AccountInfo`"""

    subscriptions_changed = Signal(object)
    """payload: ``tuple[Subscription, ...]``"""

    snapshot_changed = Signal(object)
    """payload: :class:`~yt_rec.state.models.AppSnapshot` — 무엇이든 바뀌면 방출.

    세밀한 시그널을 각각 붙이기 번거로운 화면은 이것 하나만 구독해도 된다.
    """

    command_requested = Signal(object)
    """payload: :data:`~yt_rec.state.commands.GuiCommand` — 화면이 백엔드에 보내는 요청.

    **백엔드가 구독한다.** 백엔드가 작업 스레드에 살면 Qt 자동 연결이 큐 연결을
    골라 넘긴다. 화면은 이 시그널을 직접 방출하지 않고
    :meth:`AppState.send_command` (또는 그 얇은 감싸개)를 쓴다.
    """

    command_rejected = Signal(object, str)
    """payload: ``(GuiCommand, 사유)`` — 명령을 보내지 못했다.

    백엔드가 연결되지 않았는데 명령을 보내면 조용히 사라진다. 그러면 사용자는
    `중지를 눌렀는데 아무 일도 없다` 를 보게 된다. 상단 창이 이 시그널 하나를
    구독해 사유를 알리면 화면마다 따로 처리하지 않아도 된다.
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
        self._account = AccountInfo()
        self._subscriptions: tuple[Subscription, ...] = ()

        self._dirty: set[str] = set()
        self._sources: list[EventSource] = []

        self._flush_timer = QTimer(self)
        self._flush_timer.setSingleShot(True)
        self._flush_timer.timeout.connect(self.flush)

        # 작업 스레드에서 post_event()로 넣은 이벤트를 GUI 스레드에서 적용한다.
        self._posted.connect(self._on_posted, Qt.ConnectionType.QueuedConnection)

    # ------------------------------------------------------------------
    # 스레드 확인
    # ------------------------------------------------------------------
    def is_gui_thread(self) -> bool:
        """이 저장소가 사는 스레드에서 호출됐는지."""
        return QThread.currentThread() is self.thread()

    def _require_gui_thread(self, what: str) -> None:
        """GUI 스레드가 아니면 즉시 실패한다.

        조용히 깨지는 것을 막는 것이 목적이다. 모듈 docstring `스레드 계약`
        참고.
        """
        if self.is_gui_thread():
            return
        raise RuntimeError(
            f"AppState.{what} 은 GUI 스레드에서만 쓴다. 지금은 "
            f"{QThread.currentThread()!r} 에서 불렀다. 작업 스레드에서는 "
            "post_event(event) 로 이벤트를 보내라 — 그 경로만 스레드 안전하다."
        )

    # ------------------------------------------------------------------
    # 읽기 (GUI 스레드 전용)
    # ------------------------------------------------------------------
    @property
    def connection(self) -> ConnectionState:
        self._require_gui_thread("connection")
        return self._connection

    @property
    def watch(self) -> WatchStatus:
        self._require_gui_thread("watch")
        return self._watch

    @property
    def channels(self) -> tuple[WatchedChannel, ...]:
        self._require_gui_thread("channels")
        return self._channels

    @property
    def recordings(self) -> tuple[Recording, ...]:
        self._require_gui_thread("recordings")
        return tuple(self._recordings.values())

    @property
    def completed(self) -> tuple[CompletedRecording, ...]:
        self._require_gui_thread("completed")
        return tuple(self._completed)

    @property
    def logs(self) -> tuple[LogEntry, ...]:
        self._require_gui_thread("logs")
        return tuple(self._logs)

    @property
    def error_count(self) -> int:
        self._require_gui_thread("error_count")
        return self._error_count

    @property
    def unseen_error_count(self) -> int:
        self._require_gui_thread("unseen_error_count")
        return self._unseen_error_count

    @property
    def quota(self) -> QuotaStatus:
        self._require_gui_thread("quota")
        return self._quota

    @property
    def account(self) -> AccountInfo:
        self._require_gui_thread("account")
        return self._account

    @property
    def subscriptions(self) -> tuple[Subscription, ...]:
        self._require_gui_thread("subscriptions")
        return self._subscriptions

    def snapshot(self) -> AppSnapshot:
        """현재 상태 전체를 한 덩어리로 돌려준다. GUI 스레드 전용."""
        self._require_gui_thread("snapshot()")
        return self._snapshot()

    def _snapshot(self) -> AppSnapshot:
        # 스레드 확인은 부르는 쪽에서 이미 했다. 방출마다 지나가는 길이므로
        # 프로퍼티를 다시 거치지 않고 내부 컨테이너에서 바로 만든다.
        return AppSnapshot(
            connection=self._connection,
            watch=self._watch,
            channels=self._channels,
            recordings=tuple(self._recordings.values()),
            completed=tuple(self._completed),
            logs=tuple(self._logs),
            error_count=self._error_count,
            unseen_error_count=self._unseen_error_count,
            quota=self._quota,
            account=self._account,
            subscriptions=self._subscriptions,
        )

    # ------------------------------------------------------------------
    # 이벤트 주입
    # ------------------------------------------------------------------
    @property
    def backend_attached(self) -> bool:
        """이벤트 소스가 하나라도 붙어 있는가. 계정 연결과 별개다."""
        return bool(self._sources)

    def attach(self, source: EventSource) -> None:
        """이벤트 소스를 연결한다. 소스가 다른 스레드면 Qt가 큐 연결로 넘긴다.

        GUI 스레드 전용이다. 소스 목록과 시그널 연결을 함께 바꾸므로 다른
        스레드에서 부르면 GUI 스레드의 갱신과 뒤엉킨다.
        """
        self._require_gui_thread("attach()")
        source.event_ready.connect(self._on_source_event)
        self._sources.append(source)

    def detach(self, source: EventSource) -> None:
        """연결을 끊는다. 마지막 소스가 빠지면 `연결 안 됨` 으로 되돌린다.

        소스가 여럿일 때 하나만 떼는 것은 여전히 연결된 상태다. 하나도 남지
        않았을 때만 되돌린다. 이벤트를 줄 백엔드가 없는데 ``CONNECTED`` 로
        남아 있으면 화면이 `감시 중` 을 계속 보여줘 사용자가 오해한다.

        :meth:`attach` 와 같이 GUI 스레드 전용이다.
        """
        self._require_gui_thread("detach()")
        try:
            source.event_ready.disconnect(self._on_source_event)
        except (RuntimeError, TypeError):
            pass
        if source in self._sources:
            self._sources.remove(source)
        if not self._sources:
            # 연결 이벤트와 같은 경로를 타야 감시 요약까지 함께 정리된다.
            self.apply(ev.ConnectionChanged(ConnectionState.DISCONNECTED))

    def post_event(self, event: ev.BackendEvent) -> None:
        """어느 스레드에서 불러도 안전한 이벤트 주입구.

        **이것이 작업 스레드에서 부를 수 있는 유일한 메서드다.**

        순서는 :meth:`apply` 와 같은 규칙을 따른다. GUI 스레드에서 불렀으면
        그 자리에서 적용되고, 작업 스레드에서 불렀으면 큐 연결로 GUI 스레드에
        넘겨 적용된다. 예전에는 GUI 스레드에서 불러도 항상 지연됐고, 그래서
        먼저 보낸 이벤트가 나중에 적용돼 뒤에 보낸 값을 덮어썼다
        (모듈 docstring `주입 경로와 순서` 참고).
        """
        if self.is_gui_thread():
            self.apply(event)
            return
        # 시그널 방출은 스레드 안전하고, 큐 연결이 GUI 스레드에서 적용한다.
        self._posted.emit(event)

    @Slot(object)
    def _on_source_event(self, event: object) -> None:
        self.apply(event)  # type: ignore[arg-type]

    @Slot(object)
    def _on_posted(self, event: object) -> None:
        self.apply(event)  # type: ignore[arg-type]

    def apply(self, event: ev.BackendEvent) -> None:
        """이벤트 하나를 모델에 반영한다.

        **GUI 스레드 전용이다.** 다른 스레드에서 부르면 ``RuntimeError`` 가
        난다 — 작업 스레드에서는 :meth:`post_event` 를 쓴다. 왜 독스트링이
        아니라 예외인지는 모듈 docstring `스레드 계약` 에 적어 두었다.
        """
        self._require_gui_thread("apply()")
        handler = self._HANDLERS.get(type(event))
        if handler is None:
            raise TypeError(f"알 수 없는 백엔드 이벤트: {type(event)!r}")
        # 시간대 없는 시각은 조용히 어긋난 시각으로 표시된다. 경고로 드러낸다.
        ev.warn_if_naive(event)
        handler(self, event)
        self._schedule_emit()

    def apply_all(self, events: Iterable[ev.BackendEvent]) -> None:
        """이벤트 여러 건을 순서대로 반영한다. :meth:`apply` 와 같이 GUI 스레드 전용."""
        self._require_gui_thread("apply_all()")
        for event in events:
            self.apply(event)

    # ------------------------------------------------------------------
    # GUI가 부르는 조작 (GUI 스레드 전용)
    # ------------------------------------------------------------------
    def mark_errors_seen(self) -> None:
        """미확인 오류 배지를 해제한다. 로그 뷰어(#12)를 열 때 호출한다.

        이것은 백엔드에 보내는 명령이 아니다. `아직 안 본 오류` 는 화면에만
        있는 표시 상태이므로 백엔드가 알 필요가 없다. 백엔드가 무엇을 하게
        만드는 조작은 모두 :meth:`send_command` 를 거친다.
        """
        self._require_gui_thread("mark_errors_seen()")
        if self._unseen_error_count:
            self._unseen_error_count = 0
            self._dirty.add("errors")
            self._schedule_emit()

    # ------------------------------------------------------------------
    # 화면 → 백엔드 명령
    # ------------------------------------------------------------------
    def send_command(self, command: cmd.GuiCommand) -> bool:
        """백엔드에 명령을 보낸다. 보냈으면 ``True``.

        백엔드가 연결되지 않았으면 보내지 않고 :attr:`command_rejected` 를
        방출한 뒤 ``False`` 를 돌려준다. 아무도 받지 못하는 명령을 방출하고
        성공한 척하면 화면은 `눌렀는데 아무 일도 없음` 이 된다.

        **명령은 요청일 뿐 결과가 아니다.** ``True`` 는 `백엔드에 전달했다`는
        뜻이고 `성공했다`는 뜻이 아니다. 화면은 명령을 보낸 뒤 스스로 상태를
        바꾸지 말고 백엔드가 이벤트로 알려 주는 결과를 그린다.

        GUI 스레드 전용이다.
        """
        self._require_gui_thread("send_command()")
        if not isinstance(command, cmd.GuiCommand):
            raise TypeError(f"알 수 없는 화면 명령: {type(command)!r}")
        connected = self._connection is ConnectionState.CONNECTED
        login_while_attached = (
            isinstance(command, cmd.ConnectAccount) and bool(self._sources)
        )
        stop_while_attached = (
            isinstance(command, cmd.StopRecording) and bool(self._sources)
        )
        if not connected and not login_while_attached and not stop_while_attached:
            self.command_rejected.emit(command, "백엔드에 연결되지 않았습니다")
            return False
        self.command_requested.emit(command)
        return True

    def stop_recording(self, recording_id: str, *, reason: str = "") -> bool:
        """진행 중인 녹화 한 건을 멈춰 달라고 요청한다 (#9).

        카드를 화면에서 지우는 것은 백엔드가
        :class:`~yt_rec.state.events.RecordingFinished` 를 보낼 때다.
        """
        return self.send_command(cmd.StopRecording(recording_id, reason=reason))

    def set_watched_channels(self, channel_ids: Iterable[str]) -> bool:
        """감시 대상 채널 목록을 이 목록으로 바꿔 달라고 요청한다 (#8).

        부분 변경이 아니라 전체 교체다.
        """
        return self.send_command(cmd.SetWatchedChannels(tuple(channel_ids)))

    def update_settings(self, **values: object) -> bool:
        """기능 설정을 바꿔 달라고 요청한다 (#11).

        담은 키만 바꾼다. 창 크기·섹션 접힘 같은 화면 표시 상태는 여기 넣지
        않는다 — 그것은 :class:`~yt_rec.ui.settings_store.WindowSettings` 다.
        """
        return self.send_command(cmd.UpdateSettings(dict(values)))

    def connect_account(self) -> bool:
        """Google 계정 연결을 시작해 달라. 미연결에서도 백엔드가 붙어 있으면 보낸다."""
        return self.send_command(cmd.ConnectAccount())

    def disconnect_account(self) -> bool:
        """Google 계정 연결을 끊으라."""
        return self.send_command(cmd.DisconnectAccount())

    def refresh_subscriptions(self) -> bool:
        """구독 목록을 다시 불러 달라."""
        return self.send_command(cmd.RefreshSubscriptions())

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
            # AUTH_EXPIRED 등 명시적 중단 사유는 DISCONNECTED 가 덮어쓰지 않는다.
            preserved = self._watch.stop_reason
            keep = preserved in (
                StopReason.AUTH_EXPIRED,
                StopReason.QUOTA_EXCEEDED,
                StopReason.NETWORK_DOWN,
            )
            self._watch = WatchStatus(
                state=WatchState.STOPPED if keep else WatchState.UNKNOWN,
                channel_count=self._watch.channel_count,
                stop_reason=preserved if keep else StopReason.BACKEND_DOWN,
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

    def _on_account(self, event: ev.AccountChanged) -> None:
        self._account = event.account
        self._dirty.add("account")

    def _on_subscriptions(self, event: ev.SubscriptionsChanged) -> None:
        self._subscriptions = tuple(event.subscriptions)
        self._dirty.add("subscriptions")

    _HANDLERS = {
        ev.ConnectionChanged: _on_connection,
        ev.WatchStatusChanged: _on_watch,
        ev.ChannelsChanged: _on_channels,
        ev.RecordingStarted: _on_recording_started,
        ev.RecordingProgress: _on_recording_progress,
        ev.RecordingFinished: _on_recording_finished,
        ev.LogAppended: _on_log,
        ev.QuotaChanged: _on_quota,
        ev.AccountChanged: _on_account,
        ev.SubscriptionsChanged: _on_subscriptions,
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
        """묶어 두었던 변경을 지금 즉시 방출한다. GUI 스레드 전용."""
        self._require_gui_thread("flush()")
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
            self.recordings_changed.emit(tuple(self._recordings.values()))
        if "completed" in dirty:
            self.completed_changed.emit(tuple(self._completed))
        if "logs" in dirty:
            self.logs_changed.emit(tuple(self._logs))
        if "errors" in dirty:
            self.errors_changed.emit(self._error_count, self._unseen_error_count)
        if "quota" in dirty:
            self.quota_changed.emit(self._quota)
        if "account" in dirty:
            self.account_changed.emit(self._account)
        if "subscriptions" in dirty:
            self.subscriptions_changed.emit(self._subscriptions)

        self.snapshot_changed.emit(self._snapshot())
