from __future__ import annotations

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


def test_stub_플래그는_스텁을_붙인다(qapp) -> None:
    context = build_application(["--stub", "empty"])
    try:
        assert isinstance(context.source, StubEventSource)
    finally:
        context.window.close()
