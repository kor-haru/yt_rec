from __future__ import annotations

from datetime import datetime, timezone

from yt_rec.backend.controller import WatchController
from yt_rec.backend.oauth import AuthError, ClientConfigError
from yt_rec.backend.selection import MemorySelectionStore
from yt_rec.backend.tokens import MemoryTokenStore
from yt_rec.backend.youtube import ChannelRef, LiveBroadcast, YouTubeError
from yt_rec.state import commands as cmd
from yt_rec.state import events as ev
from yt_rec.state.models import ConnectionState, StopReason, WatchState

from backend_fakes import FakeAuth, FakeRecorder, FakeYouTube

FIXED = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


def make_controller(**kwargs) -> tuple[WatchController, list, FakeYouTube, FakeRecorder, MemorySelectionStore, MemoryTokenStore]:
    events: list = []
    youtube = kwargs.pop("youtube", None) or FakeYouTube(
        subs=[ChannelRef("UC1", "하나"), ChannelRef("UC2", "둘")],
        lives=[],
    )
    recorder = kwargs.pop("recorder", None) or FakeRecorder()
    selection = kwargs.pop("selection", None) or MemorySelectionStore()
    tokens = kwargs.pop("tokens", None) or MemoryTokenStore()
    auth = kwargs.pop("auth", None) or FakeAuth()
    controller = WatchController(
        emit=events.append,
        auth=auth,
        tokens=tokens,
        selection=selection,
        recorder=recorder,
        youtube_factory=lambda _creds: youtube,
        clock=lambda: FIXED,
        poll_interval=60,
        **kwargs,
    )
    return controller, events, youtube, recorder, selection, tokens


def _of(events, type_):
    return [item for item in events if isinstance(item, type_)]


def test_연결하면_구독을_불러_오고_선택을_복원한다() -> None:
    selection = MemorySelectionStore(["UC1"])
    controller, events, youtube, recorder, selection, tokens = make_controller(selection=selection)
    controller.handle_command(cmd.ConnectAccount())

    assert tokens.load() == "creds"
    assert _of(events, ev.ConnectionChanged)[-1].state is ConnectionState.CONNECTED
    subs = _of(events, ev.SubscriptionsChanged)[-1].subscriptions
    assert [item.channel_id for item in subs] == ["UC1", "UC2"]
    assert subs[0].selected is True
    assert subs[1].selected is False
    watch = _of(events, ev.WatchStatusChanged)[-1]
    assert watch.state is WatchState.WATCHING
    assert watch.channel_count == 1
    assert recorder.started == []


def test_조회되지_않는_선택은_유지하고_조회_불가로_표시한다() -> None:
    selection = MemorySelectionStore(["UC1", "UC-gone"])
    youtube = FakeYouTube(subs=[ChannelRef("UC1", "하나")])
    controller, events, youtube, recorder, selection, tokens = make_controller(
        selection=selection, youtube=youtube
    )
    controller.handle_command(cmd.ConnectAccount())
    subs = _of(events, ev.SubscriptionsChanged)[-1].subscriptions
    gone = next(item for item in subs if item.channel_id == "UC-gone")
    assert gone.selected is True
    assert gone.unavailable is True
    assert selection.load() == ("UC1", "UC-gone")


def test_라이브는_video_id당_한_번만_녹화한다() -> None:
    selection = MemorySelectionStore(["UC1"])
    youtube = FakeYouTube(
        subs=[ChannelRef("UC1", "하나")],
        lives=[LiveBroadcast("vid-1", "UC1", "라이브", "하나")],
    )
    controller, events, youtube, recorder, selection, tokens = make_controller(
        selection=selection, youtube=youtube
    )
    controller.handle_command(cmd.ConnectAccount())
    controller.tick()
    controller.tick()
    assert recorder.started == ["vid-1"]
    started = _of(events, ev.ChannelsChanged)[-1].channels
    assert started[0].live_now is True
    assert "라이브 1건 감지" in started[0].last_check_result


def test_선택이_없으면_감시하지_않는다() -> None:
    controller, events, youtube, recorder, selection, tokens = make_controller()
    controller.handle_command(cmd.ConnectAccount())
    watch = _of(events, ev.WatchStatusChanged)[-1]
    assert watch.state is WatchState.STOPPED
    assert watch.stop_reason is StopReason.NO_CHANNELS
    assert youtube.find_calls == []


def test_네트워크_오류_뒤_다음_틱에서_재개한다() -> None:
    selection = MemorySelectionStore(["UC1"])
    youtube = FakeYouTube(subs=[ChannelRef("UC1", "하나")])
    controller, events, youtube, recorder, selection, tokens = make_controller(
        selection=selection, youtube=youtube
    )
    controller.handle_command(cmd.ConnectAccount())
    youtube.fail = YouTubeError("network", "down")
    controller.tick()
    assert _of(events, ev.WatchStatusChanged)[-1].stop_reason is StopReason.NETWORK_DOWN
    youtube.fail = None
    youtube.lives = [LiveBroadcast("vid-2", "UC1", "다시", "하나")]
    controller.tick()
    assert recorder.started == ["vid-2"]


def test_인증_만료면_연결을_끊는다() -> None:
    selection = MemorySelectionStore(["UC1"])
    youtube = FakeYouTube(subs=[ChannelRef("UC1", "하나")])
    tokens = MemoryTokenStore("old")
    controller, events, youtube, recorder, selection, tokens = make_controller(
        selection=selection, youtube=youtube, tokens=tokens
    )
    controller.handle_command(cmd.ConnectAccount())
    youtube.fail = YouTubeError("auth", "expired")
    controller.tick()
    assert _of(events, ev.ConnectionChanged)[-1].state is ConnectionState.DISCONNECTED
    assert _of(events, ev.WatchStatusChanged)[-1].stop_reason is StopReason.AUTH_EXPIRED
    assert tokens.load() is None


def test_시작_시_저장된_토큰으로_복원한다() -> None:
    tokens = MemoryTokenStore("stored")
    selection = MemorySelectionStore(["UC1"])
    youtube = FakeYouTube(subs=[ChannelRef("UC1", "하나")])
    auth = FakeAuth()
    controller, events, youtube, recorder, selection, tokens = make_controller(
        selection=selection, youtube=youtube, tokens=tokens, auth=auth
    )
    controller.start()
    assert auth.restore_calls == 1
    assert _of(events, ev.ConnectionChanged)[-1].state is ConnectionState.CONNECTED


def test_클라이언트_설정이_없으면_연결에_실패한다() -> None:
    auth = FakeAuth()
    auth.fail = ClientConfigError("없음")
    controller, events, youtube, recorder, selection, tokens = make_controller(auth=auth)
    controller.handle_command(cmd.ConnectAccount())
    assert _of(events, ev.ConnectionChanged)[-1].state is ConnectionState.DISCONNECTED
    assert any("없음" in item.entry.message for item in _of(events, ev.LogAppended))


def test_로그인_실패는_오류로_남긴다() -> None:
    auth = FakeAuth()
    auth.fail = AuthError("거부")
    controller, events, youtube, recorder, selection, tokens = make_controller(auth=auth)
    controller.handle_command(cmd.ConnectAccount())
    assert _of(events, ev.ConnectionChanged)[-1].state is ConnectionState.DISCONNECTED


def test_채널_선택_명령이_저장되고_감시를_연다() -> None:
    controller, events, youtube, recorder, selection, tokens = make_controller()
    controller.handle_command(cmd.ConnectAccount())
    controller.handle_command(cmd.SetWatchedChannels(("UC2",)))
    assert selection.load() == ("UC2",)
    watch = _of(events, ev.WatchStatusChanged)[-1]
    assert watch.state is WatchState.WATCHING
    assert watch.channel_count == 1
