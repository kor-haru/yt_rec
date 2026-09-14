"""AppState 에 붙는 실제 이벤트 소스."""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from datetime import datetime, timezone

from PySide6.QtCore import QThread, QTimer, Slot

from yt_rec.recording.options import RecordingOptions, default_settings_path, load_settings, save_settings
from yt_rec.recording.events import ProgressReported
from yt_rec.logs import LogStore, sanitize_event
from yt_rec.state import commands as cmd
from yt_rec.state import events as ev
from yt_rec.state.models import CompletedRecording, CompletionStatus, ConnectionState, LogEntry, Severity
from yt_rec.state.store import EventSource, MAX_COMPLETED

from .archive import ArchiveStore, load_archive, open_archive_path
from .controller import WATCH_INTERVAL_SECONDS, WatchController
from .notifications import LiveNotification, NotificationRecorder, NotificationResult
from .oauth import GoogleAuth
from .recorder import EngineRecorder
from .selection import FileSeenStore, FileSelectionStore
from .tokens import default_token_store
from .youtube import YouTubeApi, session_from_credentials

__all__ = ["BackendSource", "create_backend_source"]

_SENTINEL = object()


class BackendSource(EventSource):
    """컨트롤러를 감싼 EventSource.

    ``background=True`` 이면 명령을 단일 FIFO 워커에서 직렬화한다. OAuth
    루프백 대기와 API 호출이 GUI 를 막지 않게 하기 위해서다. 결과는
    :attr:`event_ready` 로만 나간다.
    """

    def __init__(
        self,
        controller: WatchController,
        *,
        background: bool = False,
        poll_interval_ms: int = WATCH_INTERVAL_SECONDS * 1000,
        log_store: LogStore | None = None,
        archive_store: ArchiveStore | None = None,
    ) -> None:
        super().__init__()
        if controller.event_only and not background:
            raise ValueError("이벤트 전용 소스는 백그라운드 작업 스레드가 필요합니다")
        self._controller = controller
        self._background = background
        self._poll_interval_ms = poll_interval_ms
        self._poll_timer: QTimer | None = None
        self._queue: queue.Queue[object] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._poll_pending = False
        self._poll_flag = threading.Lock()
        self._stopping = False
        self.log_store = log_store
        self._archive_store = archive_store
        self._unsaved_results: dict[str, CompletedRecording] = {}
        self._results_lock = threading.Lock()
        self._notification_capacity = controller._options.max_recordings
        self._notifications = NotificationRecorder(
            youtube=lambda: controller._youtube if controller._connected else None,
            selection=controller._selection, recorder=controller._recorder,
            seen=controller._seen, on_update=self._notification_update,
            can_start=lambda: not self._stopping,
        ) if controller.event_only else None

    @Slot(object)
    def handle_command(self, command: object) -> None:
        def handle() -> None:
            if isinstance(command, cmd.RefreshArchive):
                self._refresh_archive()
            elif isinstance(command, cmd.OpenRecordingPath):
                try:
                    open_archive_path(command.path, reveal=command.reveal)
                except (OSError, ValueError) as exc:
                    self._warning(f"파일을 열지 못했습니다: {exc}")
            else:
                self._controller.handle_command(command)  # type: ignore[arg-type]
                if self._notifications is not None and isinstance(command, cmd.SetWatchedChannels):
                    self._notifications.resume()
        self._run(handle)

    def receive_notification(self, notice: LiveNotification, *, trusted: bool = False) -> bool:
        """Queue one verified adapter input; True means accepted, not recorded.

        ``trusted`` is an internal caller assertion, NOT authentication. The
        native receiver must authenticate its source before calling this API.
        Probe/synthetic input is rejected even with trusted=True. The app connects
        its native receiver only; this method accepts no raw payload or URL.
        """
        if self._stopping or self._worker is None:
            return False
        if (self._notifications is None or trusted is not True
                or not isinstance(notice, LiveNotification) or notice.synthetic is not False):
            self._run(lambda: self._warning("알림 거절: 이벤트 전용 모드의 검증된 실제 수신 입력만 허용합니다. 합성·프로브 입력은 녹화하지 않습니다."))
            return False
        def receive() -> None:
            self._notification_update(NotificationResult(notice, "received"))
            self._notifications.receive(notice)
        self._run(receive)
        return True

    def _notification_update(self, result: NotificationResult) -> None:
        self.publish(ev.LogAppended(LogEntry(
            at=datetime.now(timezone.utc),
            severity=Severity.WARNING if result.status == "failed" else Severity.INFO,
            source="notification",
            message=(f"알림 {result.notification.video_id}: {result.status}; "
                     f"수신={result.notification.received_at}; 녹화 전달={result.handed_to_recorder_at}; "
                     f"첫 미디어={result.first_media_at}; {result.reason}; "
                     f"coverage={result.coverage}: {result.coverage_reason}"),
        )))

    def recording_progress(self, event: ProgressReported) -> None:
        """Engine byte evidence only. This callback performs no network I/O."""
        if self._notifications is not None:
            self._notifications.report_progress(
                event.video_id, int(event.snapshot.downloaded_bytes or 0), at=event.at,
            )

    def recording_finished(self, video_id: str, succeeded: bool) -> None:
        """Post-slot-release bookkeeping survives shutdown; only the worker drains."""
        if self._notifications is not None:
            # Do not enqueue finalization: shutdown discards queued commands.
            # The handler lock also orders actual byte evidence before completion.
            self._notifications.recording_finished(video_id, succeeded, resume=False)
            self._run(self._notifications.resume)
        elif succeeded:
            self._controller._seen.mark_done(video_id)
        else:
            self._controller._seen.unmark_started(video_id)
        self._run(self._refresh_archive)

    def publish(self, event: object) -> None:
        """백엔드 사건을 가리고 저장한 다음 화면에 전달한다."""
        event = sanitize_event(event)
        if isinstance(event, ev.RecordingFinished):
            done = event.completed
            with self._results_lock:
                if done.status is CompletionStatus.FAILED and done.output_path is None:
                    self._unsaved_results.pop(done.recording_id, None)
                    self._unsaved_results[done.recording_id] = done
                    if len(self._unsaved_results) > MAX_COMPLETED:
                        self._unsaved_results.pop(next(iter(self._unsaved_results)))
                else:
                    self._unsaved_results.pop(done.recording_id, None)
        if isinstance(event, ev.LogAppended) and self.log_store is not None:
            try:
                self.log_store.append(event.entry)
            except OSError as exc:
                self._warning(f"로그를 저장하지 못했습니다: {exc}", persist=False)
        self.event_ready.emit(event)
        if (self._notifications is not None and isinstance(event, ev.ConnectionChanged)
                and event.state is ConnectionState.CONNECTED):
            self._run(self._notifications.resume)
        if isinstance(event, ev.SettingsChanged):
            if self.log_store is not None:
                try:
                    self.log_store.set_retention_days(event.options.log_retention_days)
                except OSError as exc:
                    self._warning(f"로그 보관 기간을 적용하지 못했습니다: {exc}", persist=False)
            if self._archive_store is not None:
                try:
                    self._archive_store.remember(
                        event.options.output_dir, work_root=event.options.work_root
                    )
                except OSError as exc:
                    self._warning(f"보관함 위치를 저장하지 못했습니다: {exc}")
            self._refresh_archive()
            if self._notifications is not None and event.options.max_recordings != self._notification_capacity:
                self._notification_capacity = event.options.max_recordings
                self._run(self._notifications.resume)

    def _warning(self, message: str, *, persist: bool = True) -> None:
        event = ev.LogAppended(LogEntry(
            at=datetime.now(timezone.utc), severity=Severity.WARNING,
            source="backend", message=message,
        ))
        if persist:
            self.publish(event)
        else:
            self.event_ready.emit(sanitize_event(event))

    def _refresh_archive(self) -> None:
        try:
            if self._archive_store is not None:
                recordings = self._archive_store.load()
            else:
                options = self._controller._options
                recordings = load_archive(options.output_dir, work_root=options.work_root)
            # 준비 실패는 기존 state.json을 보호하려고 디스크에 쓰지 않는다.
            # 디스크 원본을 바꾸지 않고 이 세션의 최근 실패만 함께 보여 준다.
            items = {(item.recording_id, item.output_path): item for item in recordings}
            with self._results_lock:
                failures = tuple(self._unsaved_results.values())
            stamp = lambda item: item.finished_at.timestamp() if item.finished_at else 0
            for item in failures:
                key = (item.recording_id, item.output_path)
                if key not in items or stamp(item) >= stamp(items[key]):
                    items[key] = item
            self.publish(ev.CompletedChanged(tuple(sorted(items.values(), key=stamp, reverse=True))))
        except (OSError, ValueError) as exc:
            self._warning(f"보관함을 불러오지 못했습니다: {exc}")

    def start(self) -> None:
        if self._background and self._worker is None:
            self._worker = threading.Thread(
                target=self._worker_loop, name="yt-rec-backend", daemon=False
            )
            self._worker.start()
        self._run(self._controller.start)
        if self._poll_interval_ms > 0 and not self._controller.event_only:
            timer = QTimer(self)
            timer.setInterval(self._poll_interval_ms)
            timer.timeout.connect(self._on_poll)
            timer.start()
            self._poll_timer = timer

    def begin_shutdown(self) -> None:
        """GUI 스레드에서 새 작업을 차단하고 종료를 요청한다. 기다리지 않는다."""
        if self._stopping:
            return
        self._stopping = True
        if self._poll_timer is not None:
            self._poll_timer.stop()
        recorder = getattr(self._controller, "_recorder", None)
        if recorder is not None:
            shutdown = getattr(recorder, "begin_shutdown", None) or getattr(recorder, "stop_all", None)
            if shutdown is not None:
                shutdown()
        if self._background:
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
            self._queue.put(_SENTINEL)

    def stop(self) -> None:
        """종료 요청 뒤 작업 스레드를 기다린다. GUI 밖에서 호출할 수 있다."""
        if not self._stopping:
            if QThread.currentThread() is not self.thread():
                raise RuntimeError("GUI 스레드에서 begin_shutdown()을 먼저 호출하세요")
            self.begin_shutdown()
        recorder = getattr(self._controller, "_recorder", None)
        if self._background:
            worker = self._worker
            if worker is not None and worker is not threading.current_thread():
                worker.join()
        if recorder is not None:
            # 종료 요청 당시 API 조회 중이었다면 뒤늦게 생긴 녹화도 멈춘다.
            stop_all = getattr(recorder, "stop_all", None)
            if stop_all is not None:
                stop_all()
            joiner = getattr(recorder, "join_all", None)
            if joiner is not None:
                joiner(timeout=None)
        if self.log_store is not None:
            try:
                self.log_store.close()
            except OSError as exc:
                self._warning(f"로그 파일을 닫지 못했습니다: {exc}", persist=False)

    def tick(self) -> None:
        if not self._controller.event_only:
            self._run(self._controller.tick, coalesce_poll=True)

    def _on_poll(self) -> None:
        self.tick()

    def _run(self, work: Callable[[], None], *, coalesce_poll: bool = False) -> None:
        if self._stopping:
            return
        if not self._background:
            work()
            return
        if coalesce_poll:
            with self._poll_flag:
                if self._poll_pending:
                    return
                self._poll_pending = True

            def wrapped() -> None:
                try:
                    work()
                finally:
                    with self._poll_flag:
                        self._poll_pending = False

            self._queue.put(wrapped)
            return
        self._queue.put(work)

    def _worker_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is _SENTINEL:
                return
            fn = item
            assert callable(fn)
            try:
                fn()
            except Exception as exc:
                # 한 명령의 실패가 이후 로그인·녹화 명령까지 끊지 않게 한다.
                self._warning(f"백엔드 작업을 완료하지 못했습니다: {exc}")


def create_backend_source(
    *,
    background: bool = True,
    poll_interval: float | None = None,
    event_only: bool = True,
) -> BackendSource:
    """생산용 소스는 알림 전용이다. 수신기 수명주기는 app이 소유한다.

    event_only=False는 기존 내부 통합용 호환 옵션이며 일반 실행은 쓰지 않는다.
    수신기 오류 시에도 폴링으로 전환하지 않는다.
    """

    if event_only and not background:
        raise ValueError("이벤트 전용 소스는 백그라운드 작업 스레드가 필요합니다")

    box: dict[str, BackendSource] = {}
    seen = FileSeenStore()
    startup_warnings: list[str] = []

    def emit(event: object) -> None:
        box["source"].publish(event)

    def on_result(video_id: str, ok: bool) -> None:
        box["source"].recording_finished(video_id, ok)

    options = load_settings()
    if not default_settings_path().exists():
        try:
            options.output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            startup_warnings.append(f"기본 녹화 폴더를 만들지 못했습니다. 설정에서 폴더를 선택하세요: {exc}")
    recorder = EngineRecorder(
        options, emit, on_result=on_result,
        on_progress=lambda event: box["source"].recording_progress(event),
    )  # type: ignore[arg-type]

    def persist_settings(updated: RecordingOptions) -> None:
        previous = recorder.options
        autostart_changed = updated.autostart != previous.autostart
        if autostart_changed:
            from yt_rec.desktop import set_autostart

            set_autostart(updated.autostart)
        try:
            save_settings(updated)
        except OSError:
            if autostart_changed:
                set_autostart(previous.autostart)
            raise

    effective_interval = options.poll_interval_seconds if poll_interval is None else poll_interval

    def youtube_factory(credentials: object) -> YouTubeApi:
        return YouTubeApi(session_from_credentials(credentials))

    controller = WatchController(
        emit=emit,  # type: ignore[arg-type]
        auth=GoogleAuth(),
        tokens=default_token_store(),
        selection=FileSelectionStore(),
        recorder=recorder,
        youtube_factory=youtube_factory,  # type: ignore[arg-type]
        poll_interval=effective_interval,
        seen=seen,
        options=options,
        settings_saver=persist_settings,
        event_only=event_only,
    )
    source = BackendSource(
        controller,
        background=background,
        # 짧은 타이머는 일정을 확인할 뿐 API는 controller가 실제 간격대로 호출한다.
        poll_interval_ms=1000 if effective_interval > 0 else 0,
    )
    try:
        source._archive_store = ArchiveStore()
    except (OSError, ValueError) as exc:
        startup_warnings.append(f"저장된 보관함 위치를 불러오지 못했습니다. 현재 폴더만 표시합니다: {exc}")
    try:
        source.log_store = LogStore(retention_days=options.log_retention_days)
    except OSError as exc:
        startup_warnings.append(f"로그 파일을 열지 못했습니다: {exc}")
    # attach 뒤에 전달해야 첫 경고가 사라지지 않는다.
    for message in startup_warnings:
        QTimer.singleShot(0, lambda message=message: source._warning(message))
    box["source"] = source
    return source
