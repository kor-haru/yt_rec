"""상태 저장소 계약 검증 (이슈 #7)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from PySide6.QtCore import QThread
from PySide6.QtTest import QTest

from yt_rec.state import commands as cmd
from yt_rec.state import events as ev
from yt_rec.state.models import (
    AccountInfo,
    CompletedRecording,
    CompletionStatus,
    ConnectionState,
    LogEntry,
    QuotaStatus,
    Recording,
    RecordingState,
    Severity,
    StopReason,
    Subscription,
    WatchedChannel,
    WatchState,
)
from yt_rec.state.store import MAX_COMPLETED, MAX_LOGS, AppState, EventSource
from yt_rec.ui.formatting import now


def test_기본값은_연결_안_됨(state: AppState) -> None:
    """백엔드가 기동하지 않아도 저장소는 정상 생성되고 `연결 안 됨`이다."""
    assert state.connection is ConnectionState.DISCONNECTED
    assert state.watch.state is WatchState.UNKNOWN
    assert state.watch.stop_reason is StopReason.BACKEND_DOWN
    assert state.recordings == ()
    assert state.channels == ()
    assert state.completed == ()
    assert state.account.label == ""
    assert state.subscriptions == ()


def test_연결_끊기면_감시_상태를_알_수_없음으로_되돌린다(state: AppState) -> None:
    state.apply(ev.ConnectionChanged(ConnectionState.CONNECTED))
    state.apply(ev.WatchStatusChanged(WatchState.WATCHING, channel_count=2))
    assert state.watch.state is WatchState.WATCHING

    state.apply(ev.ConnectionChanged(ConnectionState.DISCONNECTED))
    assert state.watch.state is WatchState.UNKNOWN
    assert state.watch.stop_reason is StopReason.BACKEND_DOWN


def test_마지막_소스를_떼면_연결_안_됨으로_되돌린다(state: AppState) -> None:
    """이벤트를 줄 백엔드가 하나도 없는데 `연결됨` 으로 남으면 안 된다.

    실측 회귀: :meth:`AppState.detach` 가 목록에서만 빼고 연결·감시 상태를
    그대로 뒀다. 화면은 백엔드가 사라진 뒤에도 `감시 중` 을 계속 보여줬다.
    """
    source = EventSource()
    state.attach(source)
    state.apply(ev.ConnectionChanged(ConnectionState.CONNECTED))
    state.apply(ev.WatchStatusChanged(WatchState.WATCHING, channel_count=2))
    assert state.connection is ConnectionState.CONNECTED
    assert state.watch.state is WatchState.WATCHING

    state.detach(source)
    assert state.connection is ConnectionState.DISCONNECTED
    # 연결 이벤트와 같은 경로를 타므로 감시 요약도 함께 정리된다.
    assert state.watch.state is WatchState.UNKNOWN
    assert state.watch.stop_reason is StopReason.BACKEND_DOWN


def test_소스가_남아_있으면_하나를_떼도_연결이_유지된다(state: AppState) -> None:
    """소스 하나를 떼는 것은 백엔드가 내려간 것과 다르다."""
    first, second = EventSource(), EventSource()
    state.attach(first)
    state.attach(second)
    state.apply(ev.ConnectionChanged(ConnectionState.CONNECTED))
    state.apply(ev.WatchStatusChanged(WatchState.WATCHING, channel_count=2))

    state.detach(first)
    assert state.connection is ConnectionState.CONNECTED
    assert state.watch.state is WatchState.WATCHING

    state.detach(second)
    assert state.connection is ConnectionState.DISCONNECTED


def test_소스를_떼면_화면에도_연결_해제가_통지된다(state: AppState) -> None:
    """모델만 바뀌고 시그널이 안 나가면 화면은 옛 값을 계속 그린다."""
    source = EventSource()
    state.attach(source)
    state.apply(ev.ConnectionChanged(ConnectionState.CONNECTED))

    seen: list[object] = []
    state.connection_changed.connect(seen.append)
    state.detach(source)
    assert seen == [ConnectionState.DISCONNECTED]


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


class _CallFromThread(QThread):
    """작업 스레드에서 주어진 함수를 부르고 결과나 예외를 담아 온다."""

    def __init__(self, call) -> None:
        super().__init__()
        self._call = call
        self.error: BaseException | None = None
        self.result: object = None

    def run(self) -> None:  # noqa: D102
        try:
            self.result = self._call()
        except BaseException as exc:  # noqa: BLE001 - 그대로 실어 온다
            self.error = exc


def _run_in_worker(call):
    thread = _CallFromThread(call)
    thread.start()
    assert thread.wait(5000), "작업 스레드가 끝나지 않았다"
    return thread


# ----------------------------------------------------------------------
# 스레드 계약 — 잘못된 스레드에서 부르면 즉시 실패한다
# ----------------------------------------------------------------------
def test_작업_스레드에서_apply를_부르면_즉시_실패한다(qapp) -> None:
    """조용히 깨지는 대신 예외가 나야 한다.

    실측 회귀: 기본값 ``emit_interval_ms=200`` 에서 작업 스레드가 ``apply()`` 를
    부르면 모델은 갱신되는데 ``QTimer.start()`` 가 다른 스레드라서 조용히
    실패했다. **시그널이 한 번도 방출되지 않고 Qt 경고도 예외도 없어** 화면이
    영구 정지했다. 여기서는 상태가 바뀌지 않았다는 것까지 함께 확인한다.
    """
    store = AppState(emit_interval_ms=200)
    emissions: list[object] = []
    store.recordings_changed.connect(emissions.append)

    thread = _run_in_worker(
        lambda: store.apply(ev.RecordingStarted(Recording(recording_id="x", title="t")))
    )
    QTest.qWait(50)

    assert isinstance(thread.error, RuntimeError), (
        f"작업 스레드의 apply() 가 통과했다: {thread.error!r}"
    )
    assert "post_event" in str(thread.error), "무엇을 써야 하는지 알려 줘야 한다"
    # 조용히 반쯤 적용되는 일이 없어야 한다.
    assert store.recordings == ()
    assert emissions == []
    store.deleteLater()


@pytest.mark.parametrize(
    "name",
    ["flush", "snapshot", "mark_errors_seen", "apply_all", "attach", "detach"],
)
def test_작업_스레드에서_다른_조작도_막힌다(qapp, name: str) -> None:
    """``emit_interval_ms=0`` 이면 반대 문제가 생긴다.

    ``flush()``/``snapshot()`` 이 작업 스레드에서 실행되면 GUI 스레드의 쓰기와
    동기화 없이 내부 리스트를 읽는다. 주입 경로만 막고 나머지를 열어 두면
    계약이 반쪽이다.
    """
    store = AppState(emit_interval_ms=0)
    source = EventSource()
    calls = {
        "flush": store.flush,
        "snapshot": store.snapshot,
        "mark_errors_seen": store.mark_errors_seen,
        "apply_all": lambda: store.apply_all([]),
        "attach": lambda: store.attach(source),
        "detach": lambda: store.detach(source),
    }
    thread = _run_in_worker(calls[name])
    assert isinstance(thread.error, RuntimeError), f"{name} 이 작업 스레드에서 통과했다"
    store.deleteLater()


@pytest.mark.parametrize(
    "name", ["connection", "watch", "channels", "recordings", "completed", "logs", "quota"]
)
def test_작업_스레드에서_읽기도_막힌다(qapp, name: str) -> None:
    """읽기도 GUI 스레드 전용이다. 내부 컨테이너를 동기화 없이 읽으면 안 된다."""
    store = AppState(emit_interval_ms=0)
    thread = _run_in_worker(lambda: getattr(store, name))
    assert isinstance(thread.error, RuntimeError), f"{name} 읽기가 통과했다"
    store.deleteLater()


def test_post_event만_작업_스레드에서_통한다(qapp) -> None:
    """막는 것과 함께 열어 둔 길이 실제로 통하는지도 확인한다."""
    store = AppState(emit_interval_ms=0)
    thread = _run_in_worker(
        lambda: store.post_event(ev.ConnectionChanged(ConnectionState.CONNECTED))
    )
    assert thread.error is None, f"post_event 가 막혔다: {thread.error!r}"

    deadline = 0
    while store.connection is not ConnectionState.CONNECTED and deadline < 100:
        QTest.qWait(20)
        deadline += 1
    assert store.connection is ConnectionState.CONNECTED
    store.deleteLater()


# ----------------------------------------------------------------------
# 주입 경로의 순서
# ----------------------------------------------------------------------
def test_post_event가_먼저_보낸_이벤트를_먼저_적용한다(state: AppState) -> None:
    """실측 회귀: 방출 순서 ``[2, 1]``, 최종 상태 ``1``.

    ``_posted`` 가 명시적 ``QueuedConnection`` 이라 GUI 스레드에서 불러도
    지연됐다. 반면 ``apply()`` 는 동기였다. 그래서 **먼저 보낸 이벤트가 나중
    것을 덮어썼다.**
    """
    order: list[int] = []
    state.watch_changed.connect(lambda watch: order.append(watch.channel_count))

    state.post_event(ev.WatchStatusChanged(WatchState.WATCHING, channel_count=1))
    state.apply(ev.WatchStatusChanged(WatchState.WATCHING, channel_count=2))
    QTest.qWait(20)

    assert order == [1, 2], f"부른 순서대로 적용되지 않았다: {order}"
    assert state.watch.channel_count == 2, "나중에 보낸 값이 최종 상태여야 한다"


def test_세_주입_경로의_순서_의미가_같다(state: AppState) -> None:
    """``apply`` / ``post_event`` / ``attach`` 한 소스의 시그널을 섞어도 FIFO 다."""
    source = EventSource()
    state.attach(source)
    order: list[int] = []
    state.watch_changed.connect(lambda watch: order.append(watch.channel_count))

    def watch(count: int) -> ev.WatchStatusChanged:
        return ev.WatchStatusChanged(WatchState.WATCHING, channel_count=count)

    state.apply(watch(1))
    state.post_event(watch(2))
    source.event_ready.emit(watch(3))
    state.post_event(watch(4))
    state.apply(watch(5))
    QTest.qWait(20)

    assert order == [1, 2, 3, 4, 5], f"경로마다 순서가 다르다: {order}"
    assert state.watch.channel_count == 5
    state.detach(source)


def test_작업_스레드에서_온_이벤트는_도착_순서로_적용된다(qapp) -> None:
    """다른 스레드에서 온 것은 GUI 이벤트 루프에 도착한 순서대로 적용된다."""
    store = AppState(emit_interval_ms=0)
    order: list[int] = []
    store.watch_changed.connect(lambda watch: order.append(watch.channel_count))

    def send() -> None:
        for i in range(1, 6):
            store.post_event(ev.WatchStatusChanged(WatchState.WATCHING, channel_count=i))

    thread = _run_in_worker(send)
    assert thread.error is None
    deadline = 0
    while len(order) < 5 and deadline < 100:
        QTest.qWait(20)
        deadline += 1

    assert order == [1, 2, 3, 4, 5], f"보낸 순서가 뒤집혔다: {order}"
    store.deleteLater()


# ----------------------------------------------------------------------
# 시간대 계약
# ----------------------------------------------------------------------
def test_시간대_없는_시각은_경고를_낸다(state: AppState) -> None:
    naive = datetime(2026, 8, 13, 14, 47)
    with pytest.warns(ev.NaiveDatetimeWarning, match="started_at"):
        state.apply(
            ev.RecordingStarted(
                Recording(recording_id="r1", title="t", started_at=naive)
            )
        )
    # 경고만 내고 이벤트 자체는 적용한다. 장시간 구동 앱을 죽이지 않는다.
    assert [r.recording_id for r in state.recordings] == ["r1"]


def test_중첩된_필드의_naive_시각도_찾는다() -> None:
    naive = datetime(2026, 8, 13, 14, 47)
    event = ev.ChannelsChanged(
        (
            WatchedChannel(channel_id="a", name="a", next_check_at=now()),
            WatchedChannel(channel_id="b", name="b", last_check_at=naive),
        )
    )
    assert ev.naive_datetime_fields(event) == ("channels[1].last_check_at",)


def test_시간대_있는_시각은_경고가_없다(state: AppState, recwarn) -> None:
    utc = datetime(2026, 8, 13, 5, 47, tzinfo=timezone.utc)
    state.apply(ev.RecordingStarted(Recording(recording_id="r1", title="t", started_at=utc)))
    state.apply(ev.QuotaChanged(QuotaStatus(used=1, limit=2, resets_at=now())))
    state.apply(ev.LogAppended(LogEntry(at=now(), severity=Severity.INFO, message="x")))
    naive_warnings = [w for w in recwarn if issubclass(w.category, ev.NaiveDatetimeWarning)]
    assert not naive_warnings, f"시간대 있는 값에 경고가 났다: {naive_warnings}"


def test_상태_계층이_채운_시각은_시간대를_갖는다(state: AppState) -> None:
    """``started_at`` 을 비워 보내면 상태 계층이 채운다. 그 값도 계약을 지켜야 한다."""
    state.apply(ev.RecordingStarted(Recording(recording_id="r1", title="t")))
    rec = state.recordings[0]
    assert rec.started_at is not None and rec.started_at.tzinfo is not None
    assert rec.reported_at is not None and rec.reported_at.tzinfo is not None

    state.apply(ev.RecordingFinished(CompletedRecording(recording_id="r1", title="t")))
    done = state.completed[0]
    assert done.finished_at is not None and done.finished_at.tzinfo is not None


# ----------------------------------------------------------------------
# 화면 → 백엔드 명령
# ----------------------------------------------------------------------
def test_명령이_백엔드로_전달된다(state: AppState) -> None:
    received: list[object] = []
    state.command_requested.connect(received.append)
    state.apply(ev.ConnectionChanged(ConnectionState.CONNECTED))

    assert state.stop_recording("rec-1", reason="사용자 중지") is True
    assert state.set_watched_channels(["c1", "c2"]) is True
    assert state.update_settings(output_dir=r"D:\rec", max_quality="1080p") is True

    assert received == [
        cmd.StopRecording("rec-1", reason="사용자 중지"),
        cmd.SetWatchedChannels(("c1", "c2")),
        cmd.UpdateSettings({"output_dir": r"D:\rec", "max_quality": "1080p"}),
    ]


def test_백엔드가_없으면_명령이_거부된다(state: AppState) -> None:
    """조용히 사라지면 사용자는 `눌렀는데 아무 일도 없다` 를 보게 된다."""
    sent: list[object] = []
    rejected: list[tuple[object, str]] = []
    state.command_requested.connect(sent.append)
    state.command_rejected.connect(lambda command, why: rejected.append((command, why)))

    assert state.connection is ConnectionState.DISCONNECTED
    assert state.stop_recording("rec-1") is False
    assert sent == []
    assert len(rejected) == 1
    assert rejected[0][0] == cmd.StopRecording("rec-1")
    assert rejected[0][1]


def test_명령은_상태를_직접_바꾸지_않는다(state: AppState) -> None:
    """명령은 요청이다. 상태를 정하는 곳은 백엔드 하나여야 한다."""
    state.apply(ev.ConnectionChanged(ConnectionState.CONNECTED))
    state.apply(ev.RecordingStarted(Recording(recording_id="r1", title="t")))
    assert state.stop_recording("r1") is True
    # 백엔드가 RecordingFinished 를 보내기 전까지 카드는 남아 있어야 한다.
    assert [r.recording_id for r in state.recordings] == ["r1"]


def test_알_수_없는_명령은_거부한다(state: AppState) -> None:
    state.apply(ev.ConnectionChanged(ConnectionState.CONNECTED))
    with pytest.raises(TypeError):
        state.send_command(object())  # type: ignore[arg-type]


def test_명령은_작업_스레드에서_보낼_수_없다(qapp) -> None:
    store = AppState(emit_interval_ms=0)
    store.apply(ev.ConnectionChanged(ConnectionState.CONNECTED))
    thread = _run_in_worker(lambda: store.stop_recording("r1"))
    assert isinstance(thread.error, RuntimeError)
    store.deleteLater()


def test_계정과_구독이_이벤트로_반영된다(state: AppState) -> None:
    synced = now()
    state.apply(ev.AccountChanged(AccountInfo(label="내 채널", last_synced_at=synced)))
    state.apply(
        ev.SubscriptionsChanged(
            (
                Subscription(channel_id="UC1", name="하나", selected=True),
                Subscription(channel_id="UC2", name="둘", unavailable=True, selected=True),
            )
        )
    )
    assert state.account.label == "내 채널"
    assert state.account.last_synced_at == synced
    assert [s.channel_id for s in state.subscriptions] == ["UC1", "UC2"]
    assert state.subscriptions[1].unavailable is True
    snap = state.snapshot()
    assert snap.account.label == "내 채널"
    assert len(snap.subscriptions) == 2


def test_미연결이어도_소스가_있으면_계정_연결_명령을_보낸다(state: AppState) -> None:
    """연결 버튼은 미연결에서 눌러야 한다. 소스만 붙어 있으면 보낸다."""
    source = EventSource()
    state.attach(source)
    received: list[object] = []
    state.command_requested.connect(received.append)

    assert state.connection is ConnectionState.DISCONNECTED
    assert state.connect_account() is True
    assert received == [cmd.ConnectAccount()]


def test_소스가_없으면_계정_연결도_거부된다(state: AppState) -> None:
    rejected: list[tuple[object, str]] = []
    state.command_rejected.connect(lambda command, why: rejected.append((command, why)))
    assert state.connect_account() is False
    assert len(rejected) == 1
    assert isinstance(rejected[0][0], cmd.ConnectAccount)


def test_미연결에서_채널_선택은_거부된다(state: AppState) -> None:
    source = EventSource()
    state.attach(source)
    assert state.set_watched_channels(["UC1"]) is False
