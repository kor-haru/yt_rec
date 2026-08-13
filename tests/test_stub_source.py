"""스텁 이벤트 소스 검증 (이슈 #7의 테스트 하니스).

`실제 녹화 없이 상태 이벤트를 임의로 주입하는 테스트 하니스를 만들어,
주입한 값이 그대로 반영되는지 확인한다`에 해당한다.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from PySide6.QtTest import QTest

from yt_rec.state import events as ev
from yt_rec.state.models import (
    CompletionStatus,
    ConnectionState,
    RecordingState,
    StopReason,
    WatchState,
)
from yt_rec.state.store import AppState
from yt_rec.state.stub import (
    EMOJI_TITLE,
    LONG_TITLE,
    PRESETS,
    StubEventSource,
    recording_lifecycle,
)


def test_비어_있는_프리셋(state: AppState, stub: StubEventSource) -> None:
    stub.load_preset("empty")
    assert state.connection is ConnectionState.CONNECTED
    assert state.watch.state is WatchState.STOPPED
    assert state.watch.stop_reason is StopReason.NO_CHANNELS
    assert state.channels == ()
    assert state.recordings == ()
    assert state.completed == ()


def test_미연결_프리셋(state: AppState, stub: StubEventSource) -> None:
    stub.load_preset("disconnected")
    assert state.connection is ConnectionState.DISCONNECTED
    assert state.watch.state is WatchState.UNKNOWN


def test_더미_데이터_프리셋이_주입한_값이_그대로_반영된다(
    state: AppState, stub: StubEventSource
) -> None:
    stub.load_preset("populated")
    assert state.connection is ConnectionState.CONNECTED
    assert state.watch.state is WatchState.WATCHING
    assert state.watch.channel_count == 3
    assert len(state.channels) == 3
    assert len(state.recordings) == 3
    assert len(state.completed) == 4
    assert state.error_count == 1
    assert state.quota.used == 3120

    states = {r.state for r in state.recordings}
    assert RecordingState.RECORDING in states
    assert RecordingState.RETRYING in states
    assert RecordingState.STALLED in states

    statuses = {c.status for c in state.completed}
    assert statuses == {
        CompletionStatus.COMPLETED,
        CompletionStatus.PARTIAL,
        CompletionStatus.FAILED,
        CompletionStatus.MISSING,
    }


def test_긴_제목과_이모지_더미가_그대로_들어온다(
    state: AppState, stub: StubEventSource
) -> None:
    stub.load_preset("populated")
    titles = {r.title for r in state.recordings}
    assert LONG_TITLE in titles
    assert EMOJI_TITLE in titles
    assert len(LONG_TITLE) == 120
    assert "🔴" in EMOJI_TITLE


def test_알_수_없는_프리셋은_거부한다(stub: StubEventSource) -> None:
    with pytest.raises(ValueError):
        stub.load_preset("없는프리셋")


def test_모든_프리셋이_예외_없이_적용된다(state: AppState, stub: StubEventSource) -> None:
    for name in PRESETS:
        stub.load_preset(name)


def test_시작_진행_완료_오류_시나리오_전이(state: AppState, stub: StubEventSource) -> None:
    """이슈 #7 `테스트 방식`의 이벤트 순서를 재생하고 각 전이를 확인한다."""
    seen: list[str] = []

    def note_recordings(recordings) -> None:
        for rec in recordings:
            seen.append(f"rec:{rec.state.value}")

    def note_completed(completed) -> None:
        for done in completed:
            seen.append(f"done:{done.status.value}")

    state.recordings_changed.connect(note_recordings)
    state.completed_changed.connect(note_completed)

    stub.play(recording_lifecycle(recording_id="scenario"), speed=20.0)

    for _ in range(200):
        QTest.qWait(10)
        if state.error_count and state.watch.state is WatchState.STOPPED:
            break

    assert "rec:starting" in seen
    assert "rec:recording" in seen
    assert "rec:retrying" in seen
    assert "done:partial" in seen
    assert state.recordings == ()
    assert state.completed[0].recording_id == "scenario"
    assert state.error_count == 1
    assert state.watch.stop_reason is StopReason.QUOTA_EXCEEDED


def test_부하_하니스가_진행_이벤트를_계속_보낸다(state: AppState, stub: StubEventSource) -> None:
    timer = stub.start_flood(recording_id="flood", hz=200)
    QTest.qWait(150)
    timer.stop()
    rec = {r.recording_id: r for r in state.recordings}["flood"]
    assert rec.reported_bytes > 0
    assert rec.reported_elapsed > timedelta()


def test_직접_주입도_가능하다(state: AppState, stub: StubEventSource) -> None:
    stub.emit_event(ev.ConnectionChanged(ConnectionState.CONNECTING))
    assert state.connection is ConnectionState.CONNECTING
