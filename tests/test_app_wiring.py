from __future__ import annotations

import time

from yt_rec.app import build_application
from yt_rec.state.models import ConnectionState
from yt_rec.state.store import EventSource
from yt_rec.state.stub import StubEventSource


class _FakeBackend(EventSource):
    def __init__(self) -> None:
        super().__init__()
        self.started = False
        self.commands: list[object] = []

    def handle_command(self, command: object) -> None:
        self.commands.append(command)

    def start(self) -> None:
        self.started = True


def test_스텁_없이_실제_백엔드를_붙인다(qapp, monkeypatch) -> None:
    fake = _FakeBackend()
    monkeypatch.setattr("yt_rec.app.create_backend_source", lambda: fake)
    context = build_application([])
    try:
        assert context.source is fake
        assert fake.started is True
        assert not isinstance(context.source, StubEventSource)
        assert context.state.connection is ConnectionState.DISCONNECTED
    finally:
        context.window.close()


def test_명령은_fifo로_직렬화된다(qapp) -> None:
    from yt_rec.backend.controller import WatchController
    from yt_rec.backend.selection import MemorySelectionStore
    from yt_rec.backend.source import BackendSource
    from yt_rec.backend.tokens import MemoryTokenStore
    from yt_rec.state import commands as cmd
    from yt_rec.state.events import ConnectionChanged
    from yt_rec.state.models import ConnectionState

    from backend_fakes import FakeAuth, FakeRecorder, FakeYouTube

    order: list[str] = []

    class SlowAuth(FakeAuth):
        def login(self) -> object:
            order.append("login")
            time.sleep(0.1)
            return super().login()

    events: list = []
    youtube = FakeYouTube()
    controller = WatchController(
        emit=events.append,
        auth=SlowAuth(),
        tokens=MemoryTokenStore(),
        selection=MemorySelectionStore(),
        recorder=FakeRecorder(),
        youtube_factory=lambda _c: youtube,
        poll_interval=60,
    )
    source = BackendSource(controller, background=True, poll_interval_ms=0)
    source.start()
    try:
        source.handle_command(cmd.ConnectAccount())
        source.handle_command(cmd.DisconnectAccount())
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            states = [
                item.state for item in events if isinstance(item, ConnectionChanged)
            ]
            if "login" in order and ConnectionState.DISCONNECTED in states[1:]:
                break
            time.sleep(0.05)
        states = [item.state for item in events if isinstance(item, ConnectionChanged)]
        assert ConnectionState.CONNECTING in states
        assert states[-1] is ConnectionState.DISCONNECTED
        assert order == ["login"]
    finally:
        source.stop()


def test_stub_플래그는_스텁을_붙인다(qapp) -> None:
    context = build_application(["--stub", "empty"])
    try:
        assert isinstance(context.source, StubEventSource)
    finally:
        context.window.close()
