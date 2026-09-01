"""AppState 에 붙는 실제 이벤트 소스."""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QTimer, Slot

from yt_rec.recording.options import RecordingOptions, load_settings
from yt_rec.state.store import EventSource

from .controller import WATCH_INTERVAL_SECONDS, WatchController
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
    ) -> None:
        super().__init__()
        self._controller = controller
        self._background = background
        self._poll_interval_ms = poll_interval_ms
        self._poll_timer: QTimer | None = None
        self._queue: queue.Queue[object] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._poll_pending = False
        self._poll_flag = threading.Lock()

    @Slot(object)
    def handle_command(self, command: object) -> None:
        self._run(lambda: self._controller.handle_command(command))  # type: ignore[arg-type]

    def start(self) -> None:
        if self._background and self._worker is None:
            self._worker = threading.Thread(
                target=self._worker_loop, name="yt-rec-backend", daemon=False
            )
            self._worker.start()
        self._run(self._controller.start)
        if self._poll_interval_ms > 0:
            timer = QTimer(self)
            timer.setInterval(self._poll_interval_ms)
            timer.timeout.connect(self._on_poll)
            timer.start()
            self._poll_timer = timer

    def stop(self) -> None:
        if self._poll_timer is not None:
            self._poll_timer.stop()
        recorder = getattr(self._controller, "_recorder", None)
        if recorder is not None:
            stop_all = getattr(recorder, "stop_all", None)
            if stop_all is not None:
                stop_all()
        if self._background:
            self._queue.put(_SENTINEL)
            worker = self._worker
            if worker is not None and worker is not threading.current_thread():
                worker.join(timeout=30)
        if recorder is not None:
            joiner = getattr(recorder, "join_all", None)
            if joiner is not None:
                joiner(timeout=600)

    def tick(self) -> None:
        self._run(self._controller.tick, coalesce_poll=True)

    def _on_poll(self) -> None:
        self.tick()

    def _run(self, work: Callable[[], None], *, coalesce_poll: bool = False) -> None:
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
            fn()


def create_backend_source(
    *,
    background: bool = True,
    poll_interval: float = WATCH_INTERVAL_SECONDS,
) -> BackendSource:
    """생산용 소스. 토큰은 OS 보안 저장소, 출력은 recordings/1080p."""

    box: dict[str, BackendSource] = {}
    seen = FileSeenStore()

    def emit(event: object) -> None:
        box["source"].event_ready.emit(event)

    def on_result(video_id: str, ok: bool) -> None:
        if ok:
            seen.mark_done(video_id)
        else:
            seen.unmark_started(video_id)

    options = load_settings(
        default=RecordingOptions(output_dir=Path("recordings"), max_height=1080)
    )
    recorder = EngineRecorder(options, emit, on_result=on_result)  # type: ignore[arg-type]

    def youtube_factory(credentials: object) -> YouTubeApi:
        return YouTubeApi(session_from_credentials(credentials))

    controller = WatchController(
        emit=emit,  # type: ignore[arg-type]
        auth=GoogleAuth(),
        tokens=default_token_store(),
        selection=FileSelectionStore(),
        recorder=recorder,
        youtube_factory=youtube_factory,  # type: ignore[arg-type]
        poll_interval=poll_interval,
        seen=seen,
    )
    source = BackendSource(
        controller,
        background=background,
        poll_interval_ms=int(poll_interval * 1000) if poll_interval > 0 else 0,
    )
    box["source"] = source
    return source
