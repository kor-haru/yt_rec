"""AppState 에 붙는 실제 이벤트 소스."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QTimer, Slot

from yt_rec.recording.options import RecordingOptions, load_settings
from yt_rec.state.store import EventSource

from .controller import WATCH_INTERVAL_SECONDS, WatchController
from .oauth import GoogleAuth
from .recorder import EngineRecorder
from .selection import FileSelectionStore
from .tokens import default_token_store
from .youtube import YouTubeApi, session_from_credentials

__all__ = ["BackendSource", "create_backend_source"]


class BackendSource(EventSource):
    """컨트롤러를 감싼 EventSource.

    ``background=True`` 이면 명령·폴링을 작업 스레드에서 돌린다. OAuth 루프백
    대기와 API 호출이 GUI 를 막지 않게 하기 위해서다. 결과는
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

    @Slot(object)
    def handle_command(self, command: object) -> None:
        self._run(lambda: self._controller.handle_command(command))  # type: ignore[arg-type]

    def start(self) -> None:
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
        stopper = getattr(self._controller, "_recorder", None)
        if stopper is not None:
            stop_all = getattr(stopper, "stop_all", None)
            if stop_all is not None:
                stop_all()

    def tick(self) -> None:
        self._run(self._controller.tick)

    def _on_poll(self) -> None:
        self.tick()

    def _run(self, work: Callable[[], None]) -> None:
        if not self._background:
            work()
            return
        threading.Thread(target=work, daemon=True).start()


def create_backend_source(
    *,
    background: bool = True,
    poll_interval: float = WATCH_INTERVAL_SECONDS,
) -> BackendSource:
    """생산용 소스. 토큰은 Credential Manager, 출력은 recordings/1080p."""

    box: dict[str, BackendSource] = {}

    def emit(event: object) -> None:
        box["source"].event_ready.emit(event)

    options = load_settings(
        default=RecordingOptions(output_dir=Path("recordings"), max_height=1080)
    )
    recorder = EngineRecorder(options, emit)  # type: ignore[arg-type]

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
    )
    source = BackendSource(
        controller,
        background=background,
        poll_interval_ms=int(poll_interval * 1000) if poll_interval > 0 else 0,
    )
    box["source"] = source
    return source
