"""상태 저장소 계약 검증 (이슈 #7)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from PySide6.QtCore import QThread
from PySide6.QtTest import QTest

from yt_rec.state import events as ev
from yt_rec.state.models import (
    CompletedRecording,
    CompletionStatus,
    ConnectionState,
    LogEntry,
    QuotaStatus,
    Recording,
    RecordingState,
    Severity,
    StopReason,
    WatchedChannel,
    WatchState,
)
from yt_rec.state.store import MAX_COMPLETED, MAX_LOGS, AppState
from yt_rec.ui.formatting import now


def test_기본값은_연결_안_됨(state: AppState) -> None:
    """백엔드가 기동하지 않아도 저장소는 정상 생성되고 `연결 안 됨`이다."""
    assert state.connection is ConnectionState.DISCONNECTED
    assert state.watch.state is WatchState.UNKNOWN
    assert state.watch.stop_reason is StopReason.BACKEND_DOWN
    assert state.recordings == ()
    assert state.channels == ()
    assert state.completed == ()


def test_연결_끊기면_감시_상태를_알_수_없음으로_되돌린다(state: AppState) -> None:
    state.apply(ev.ConnectionChanged(ConnectionState.CONNECTED))
    state.apply(ev.WatchStatusChanged(WatchState.WATCHING, channel_count=2))
    assert state.watch.state is WatchState.WATCHING

    state.apply(ev.ConnectionChanged(ConnectionState.DISCONNECTED))
    assert state.watch.state is WatchState.UNKNOWN
    assert state.watch.stop_reason is StopReason.BACKEND_DOWN


def test_녹화_시작_진행_완료_전이(state: AppState) -> None:
    state.apply(
        ev.RecordingStarted(
            Recording(recording_id="r1", title="라이브", state=RecordingState.STARTING)
        )
    )
    assert [r.recording_id for r in state.recordings] == ["r1"]
    assert state.recordings[0].state is RecordingState.STARTING

    state.apply(
        ev.RecordingProgress(
            recording_id="r1",
            reported_bytes=1_239_500_000,
            reported_elapsed=timedelta(minutes=42),
        )
    )
    rec = state.recordings[0]
    assert rec.state is RecordingState.RECORDING
    assert rec.reported_bytes == 1_239_500_000
    assert rec.reported_elapsed == timedelta(minutes=42)

    state.apply(
        ev.RecordingProgress(
            recording_id="r1",
            reported_bytes=1_300_000_000,
            reported_elapsed=timedelta(minutes=45),
            state=RecordingState.RETRYING,
            retry_count=2,
            detail="재시도 2회",
        )
    )
    assert state.recordings[0].state is RecordingState.RETRYING
    assert state.recordings[0].retry_count == 2

    state.apply(
        ev.RecordingFinished(
            CompletedRecording(
                recording_id="r1",
                title="라이브",
                status=CompletionStatus.PARTIAL,
                total_bytes=1_300_000_000,
            )
        )
    )
    assert state.recordings == ()
    assert [c.recording_id for c in state.completed] == ["r1"]
    assert state.completed[0].status is CompletionStatus.PARTIAL


def test_시작_이벤트를_놓쳐도_진행_보고만으로_카드가_선다(state: AppState) -> None:
    state.apply(
        ev.RecordingProgress(
            recording_id="orphan",
            reported_bytes=1024,
            reported_elapsed=timedelta(seconds=3),
        )
    )
    assert [r.recording_id for r in state.recordings] == ["orphan"]


def test_오류_로그가_누적_카운터를_올린다(state: AppState) -> None:
    for severity in (Severity.INFO, Severity.WARNING, Severity.ERROR, Severity.ERROR):
        state.apply(ev.LogAppended(LogEntry(at=now(), severity=severity, message="x")))
    assert state.error_count == 2
    assert state.unseen_error_count == 2

    state.mark_errors_seen()
    assert state.error_count == 2
    assert state.unseen_error_count == 0


def test_완료_이력과_로그에_상한이_있다(state: AppState) -> None:
    """장시간 구동에서 메모리가 무한히 늘지 않아야 한다."""
    for i in range(MAX_COMPLETED + 25):
        state.apply(
            ev.RecordingFinished(CompletedRecording(recording_id=f"c{i}", title="t"))
        )
    assert len(state.completed) == MAX_COMPLETED
    assert state.completed[0].recording_id == f"c{MAX_COMPLETED + 24}"

    for i in range(MAX_LOGS + 10):
        state.apply(
            ev.LogAppended(LogEntry(at=now(), severity=Severity.INFO, message=str(i)))
        )
    assert len(state.logs) == MAX_LOGS


def test_스냅샷이_전체_상태를_담는다(state: AppState) -> None:
    state.apply(ev.ConnectionChanged(ConnectionState.CONNECTED))
    state.apply(ev.ChannelsChanged((WatchedChannel(channel_id="c1", name="채널"),)))
    state.apply(ev.QuotaChanged(QuotaStatus(used=10, limit=100)))
    snap = state.snapshot()
    assert snap.connection is ConnectionState.CONNECTED
    assert snap.channels[0].name == "채널"
    assert snap.quota.limit == 100


# ----------------------------------------------------------------------
# 갱신 빈도 제한
# ----------------------------------------------------------------------
def test_진행_이벤트_다수가_한_번의_방출로_묶인다(qapp) -> None:
    """초당 수십~수백 건이 들어와도 화면 갱신 횟수는 묶여야 한다."""
    store = AppState(emit_interval_ms=50)
    emissions: list[object] = []
    store.recordings_changed.connect(lambda payload: emissions.append(payload))

    store.apply(ev.RecordingStarted(Recording(recording_id="r1", title="t")))
    for i in range(500):
        store.apply(
            ev.RecordingProgress(
                recording_id="r1",
                reported_bytes=i * 1024,
                reported_elapsed=timedelta(seconds=i),
            )
        )
    # 아직 타이머가 돌지 않았으므로 방출은 0회지만 모델은 이미 최신이다.
    assert emissions == []
    assert store.recordings[0].reported_bytes == 499 * 1024

    QTest.qWait(120)
    assert len(emissions) == 1
    assert emissions[0][0].reported_bytes == 499 * 1024
    store.deleteLater()


def test_묶기를_끄면_즉시_방출된다(state: AppState) -> None:
    emissions: list[object] = []
    state.recordings_changed.connect(lambda payload: emissions.append(payload))
    state.apply(ev.RecordingStarted(Recording(recording_id="r1", title="t")))
    assert len(emissions) == 1


def test_flush로_즉시_방출할_수_있다(qapp) -> None:
    store = AppState(emit_interval_ms=10_000)
    emissions: list[object] = []
    store.channels_changed.connect(lambda payload: emissions.append(payload))
    store.apply(ev.ChannelsChanged((WatchedChannel(channel_id="c", name="n"),)))
    assert emissions == []
    store.flush()
    assert len(emissions) == 1
    store.deleteLater()


# ----------------------------------------------------------------------
# 스레드
# ----------------------------------------------------------------------
class _PostingThread(QThread):
    """작업 스레드에서 이벤트를 밀어 넣는다. 위젯은 절대 만지지 않는다."""

    def __init__(self, store: AppState, count: int) -> None:
        super().__init__()
        self._store = store
        self._count = count
        self.worker_thread: QThread | None = None

    def run(self) -> None:  # noqa: D102
        self.worker_thread = QThread.currentThread()
        self._store.post_event(ev.ConnectionChanged(ConnectionState.CONNECTED))
        for i in range(self._count):
            self._store.post_event(
                ev.RecordingProgress(
                    recording_id="threaded",
                    reported_bytes=i * 4096,
                    reported_elapsed=timedelta(seconds=i),
                )
            )


def test_작업_스레드가_보낸_이벤트는_GUI_스레드에서_적용된다(qapp) -> None:
    store = AppState(emit_interval_ms=0)
    apply_threads: list[QThread] = []
    store.recordings_changed.connect(
        lambda _payload: apply_threads.append(QThread.currentThread())
    )

    main_thread = QThread.currentThread()
    worker = _PostingThread(store, 20)
    worker.start()
    assert worker.wait(5000)

    deadline = 0
    while store.recordings == () and deadline < 100:
        QTest.qWait(20)
        deadline += 1

    assert worker.worker_thread is not main_thread
    assert store.connection is ConnectionState.CONNECTED
    assert store.recordings[0].reported_bytes == 19 * 4096
    assert apply_threads, "시그널이 한 번도 방출되지 않았다"
    assert all(t is main_thread for t in apply_threads)
    store.deleteLater()


def test_알_수_없는_이벤트는_거부한다(state: AppState) -> None:
    with pytest.raises(TypeError):
        state.apply(object())  # type: ignore[arg-type]
