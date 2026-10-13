from __future__ import annotations

import time

import pytest
from yt_rec import app as application
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

    def publish(self, event) -> None:
        self.event_ready.emit(event)


def test_스텁_없이_실제_백엔드를_붙인다(qapp, monkeypatch, fake_push_receiver, window_settings) -> None:
    fake = _FakeBackend()
    def factory(**kwargs):
        from yt_rec.backend.browser_web import BrowserYouTube
        assert isinstance(kwargs.pop("browser"), BrowserYouTube)
        assert kwargs == {"event_only": True}
        return fake
    monkeypatch.setattr("yt_rec.app.create_backend_source", factory)
    context = build_application([], settings=window_settings)
    try:
        assert context.source is fake
        assert fake.started is True
        assert not isinstance(context.source, StubEventSource)
        assert context.state.connection is ConnectionState.DISCONNECTED
    finally:
        context.notifications.stop()
        context.window.close()


def test_수신기의_열어봄_시청_결과는_앱_로그에_남는다(qapp, monkeypatch, fake_push_receiver, window_settings) -> None:
    """알림 닫기·열어봄 기록·짧은 시청의 결과는 운영 중에 보여야 한다 (#118).

    성공은 INFO, 실패는 WARNING 이고 출처는 ``notification-engagement`` 다. 수신기를
    멈춘 뒤에 늦게 온 결과는 남기지 않는다.
    """
    from PySide6.QtCore import Signal
    from yt_rec.state import events as ev
    from yt_rec.state.models import Severity

    class Receiver(fake_push_receiver):
        engagement = Signal(bool, str)

    fake = _FakeBackend()
    published: list[object] = []
    fake.event_ready.connect(published.append)
    monkeypatch.setattr("yt_rec.app.create_backend_source", lambda **_kwargs: fake)
    monkeypatch.setattr("yt_rec.app.notification_receiver_class", lambda _name: Receiver)
    context = build_application([], settings=window_settings)

    def entries():
        return [(event.entry.severity, event.entry.message) for event in published
                if isinstance(event, ev.LogAppended) and event.entry.source == "notification-engagement"]

    try:
        receiver = context.notifications.receiver
        receiver.engagement.emit(True, "알림 열어봄을 기록했습니다")
        receiver.engagement.emit(False, "알림 열어봄을 기록하지 못했습니다: auth")
        assert entries() == [
            (Severity.INFO, "알림 열어봄을 기록했습니다"),
            (Severity.WARNING, "알림 열어봄을 기록하지 못했습니다: auth"),
        ]
        assert context.state.notification.code != "error"  # 수신 상태 표시는 그대로다
        context.notifications.stop()
        receiver.engagement.emit(False, "녹화가 시작된 영상을 잠깐 보지 못했습니다: 제한 시간 안에 끝나지 않았습니다")
        assert len(entries()) == 2
    finally:
        context.notifications.stop()
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


def test_기동_실패는_파일에_남는다(tmp_path, monkeypatch) -> None:
    """콘솔 없는 GUI 런처에서는 로깅이 서기 전 예외가 갈 곳이 없다(#96)."""
    target = tmp_path / "logs" / "startup-crash.log"
    monkeypatch.setattr(application, "startup_failure_path", lambda: target)

    def explode(argv=None):
        raise RuntimeError("부팅 실패")

    monkeypatch.setattr(application, "_run", explode)
    with pytest.raises(RuntimeError):
        application.main([])

    recorded = target.read_text(encoding="utf-8")
    assert "Traceback" in recorded
    assert "RuntimeError" in recorded and "부팅 실패" in recorded


def test_테스트는_진짜_앱_데이터_폴더에_쓰지_않는다(isolated_app_data) -> None:
    """합성 예외의 기동 실패 기록이 사용자 폴더에 남았다(#98). 뿌리째 돌려 둔다."""
    from conftest import REAL_APP_DATA
    from yt_rec.recording.options import default_settings_path

    for path in (default_settings_path(), application.startup_failure_path()):
        assert path.is_relative_to(isolated_app_data)
        assert not path.is_relative_to(REAL_APP_DATA)


def test_기동_실패_기록은_앱_로그_옆에_둔다() -> None:
    from yt_rec.recording.options import default_settings_path

    assert application.startup_failure_path().parent == default_settings_path().parent / "logs"
