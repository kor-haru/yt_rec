"""백엔드 없이 화면을 개발·검증하기 위한 스텁 이벤트 소스.

감시·녹화 백엔드(#3, #4)가 아직 없어도 화면 이슈(#8~#12)가 병렬로 진행될 수
있게 하는 하니스다. 실제 백엔드와 같은 :class:`~yt_rec.state.store.EventSource`
인터페이스를 구현하므로, 붙는 쪽 코드는 어느 쪽인지 구분할 필요가 없다.

사용법::

    source = StubEventSource()
    state.attach(source)
    source.emit_event(ConnectionChanged(ConnectionState.CONNECTED))
    source.load_preset("populated")          # 더미 감시/녹화/완료 한 번에
    source.play(recording_lifecycle())       # 시나리오 재생

명령행에서는 ``python -m yt_rec --stub populated`` 로 바로 띄울 수 있다.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta, timezone

from PySide6.QtCore import QTimer

from . import events as ev
from .models import (
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
from .store import EventSource

__all__ = [
    "StubEventSource",
    "ScriptStep",
    "PRESETS",
    "empty_preset",
    "populated_preset",
    "disconnected_preset",
    "recording_lifecycle",
    "flood_script",
]

# 긴 한글 제목 렌더링과 말줄임을 확인하기 위한 정확히 120자 더미 제목.
LONG_TITLE = (
    "가나다라마바사아자차카타파하 정규 방송 다시보기 특별편 "
    "— 시청자 참여형 장시간 생중계 아카이브 기록용 제목 테스트 문자열 "
    "끝까지 잘리지 않고 말줄임 처리되어야 한다 확인용 여기까지 백이십자 마지막 열다섯 글자 채움용"
)
assert len(LONG_TITLE) == 120, f"LONG_TITLE 길이가 {len(LONG_TITLE)}자다"

EMOJI_TITLE = "🔴 긴급 생방송 🎬 신곡 최초 공개 ✨ 다 같이 보아요 🥳"


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


ScriptStep = tuple[int, ev.BackendEvent]
"""``(지연 밀리초, 이벤트)``. 지연은 재생 시작 시점 기준 누적이 아니라 상대값이다."""


class StubEventSource(EventSource):
    """임의의 상태 이벤트를 주입하는 소스."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # 타이머는 재생용·부하용 각각 하나씩만 둔다. 이벤트마다 타이머를 만들면
        # 긴 시나리오에서 수천 개가 쌓이고 수명 관리가 어려워진다.
        self._script: list[ScriptStep] = []
        self._script_index = 0
        self._script_timer = QTimer(self)
        self._script_timer.setSingleShot(True)
        self._script_timer.timeout.connect(self._advance_script)
        self._flood_timer = QTimer(self)
        self._flood_timer.timeout.connect(self._emit_flood_tick)
        self._flood_state: dict[str, object] = {}

    # ------------------------------------------------------------------
    def emit_event(self, event: ev.BackendEvent) -> None:
        """이벤트 한 건을 즉시 내보낸다."""
        self.event_ready.emit(event)

    def emit_all(self, events: Iterable[ev.BackendEvent]) -> None:
        """이벤트 여러 건을 순서대로 즉시 내보낸다."""
        for event in events:
            self.emit_event(event)

    def load_preset(self, name: str) -> None:
        """이름 붙은 더미 상태 묶음을 한 번에 주입한다."""
        try:
            builder = PRESETS[name]
        except KeyError:
            raise ValueError(
                f"알 수 없는 프리셋: {name!r} (사용 가능: {', '.join(PRESETS)})"
            ) from None
        self.emit_all(builder())

    # ------------------------------------------------------------------
    def play(self, script: Sequence[ScriptStep], *, speed: float = 1.0) -> None:
        """``(지연, 이벤트)`` 목록을 시간 순으로 재생한다.

        단일 타이머가 스스로 다음 단계를 예약하므로 GUI 스레드를 막지 않고
        타이머가 쌓이지도 않는다.
        """
        self._script_timer.stop()
        factor = 1.0 / max(speed, 0.001)
        self._script = [(max(0, int(delay * factor)), event) for delay, event in script]
        self._script_index = 0
        if self._script:
            self._script_timer.start(self._script[0][0])

    def _advance_script(self) -> None:
        if self._script_index >= len(self._script):
            return
        _delay, event = self._script[self._script_index]
        self._script_index += 1
        self.emit_event(event)
        if self._script_index < len(self._script):
            self._script_timer.start(self._script[self._script_index][0])

    def stop(self) -> None:
        """예약된 재생과 부하 주입을 모두 멈춘다."""
        self._script_timer.stop()
        self._script = []
        self._script_index = 0
        self._flood_timer.stop()
        self._flood_state.clear()

    # ------------------------------------------------------------------
    def start_flood(self, *, recording_id: str = "rec-flood", hz: int = 100) -> QTimer:
        """초당 ``hz`` 건의 진행 이벤트를 계속 쏟아붓는다.

        갱신 빈도 제한이 실제로 동작하는지 확인하는 부하 하니스다. 돌려주는
        타이머를 ``stop()`` 하면 멈춘다.
        """
        self.emit_event(
            ev.RecordingStarted(
                Recording(
                    recording_id=recording_id,
                    title="부하 시험 녹화",
                    channel_name="스텁 채널",
                    quality="1080p60",
                    state=RecordingState.RECORDING,
                    started_at=_now(),
                )
            )
        )
        self._flood_state = {"id": recording_id, "hz": max(hz, 1), "n": 0}
        self._flood_timer.setInterval(max(1, 1000 // max(hz, 1)))
        self._flood_timer.start()
        return self._flood_timer

    def _emit_flood_tick(self) -> None:
        if not self._flood_state:
            return
        count = int(self._flood_state["n"]) + 1
        self._flood_state["n"] = count
        self.emit_event(
            ev.RecordingProgress(
                recording_id=str(self._flood_state["id"]),
                reported_bytes=count * 512 * 1024,
                reported_elapsed=timedelta(seconds=count / int(self._flood_state["hz"])),
                state=RecordingState.RECORDING,
            )
        )


# ----------------------------------------------------------------------
# 프리셋
# ----------------------------------------------------------------------
def disconnected_preset() -> list[ev.BackendEvent]:
    """백엔드 미기동. GUI 기본 상태와 같다."""
    return [
        ev.ConnectionChanged(ConnectionState.DISCONNECTED),
    ]


def empty_preset() -> list[ev.BackendEvent]:
    """연결은 됐지만 채널·녹화·완료가 모두 없는 상태. 빈 상태 문구 확인용."""
    return [
        ev.ConnectionChanged(ConnectionState.CONNECTED),
        ev.WatchStatusChanged(
            state=WatchState.STOPPED,
            channel_count=0,
            stop_reason=StopReason.NO_CHANNELS,
        ),
        ev.ChannelsChanged(()),
        ev.QuotaChanged(QuotaStatus(used=0, limit=10000)),
    ]


def populated_preset() -> list[ev.BackendEvent]:
    """감시 3채널, 동시 녹화 3건, 완료 이력 4건. 레이아웃 육안 확인용."""
    now = _now()
    return [
        ev.ConnectionChanged(ConnectionState.CONNECTED),
        ev.WatchStatusChanged(
            state=WatchState.WATCHING,
            channel_count=3,
            next_check_at=now + timedelta(minutes=4, seconds=12),
        ),
        ev.ChannelsChanged(
            (
                WatchedChannel(
                    channel_id="UC0000000000000000000001",
                    name="가나다 스튜디오 공식 채널",
                    next_check_at=now + timedelta(minutes=4, seconds=12),
                    last_check_at=now - timedelta(minutes=0, seconds=48),
                    last_check_result="라이브 없음",
                ),
                WatchedChannel(
                    channel_id="UC0000000000000000000002",
                    name="🎧 심야 라디오 아카이브 채널 — 매일 밤 열두 시",
                    next_check_at=now + timedelta(minutes=1, seconds=5),
                    last_check_at=now - timedelta(minutes=3, seconds=55),
                    last_check_result="라이브 1건 감지",
                    live_now=True,
                ),
                WatchedChannel(
                    channel_id="UC0000000000000000000003",
                    name=LONG_TITLE,
                    next_check_at=now + timedelta(minutes=9, seconds=30),
                    last_check_at=now - timedelta(seconds=30),
                    last_check_result="확인 실패 — 네트워크 오류",
                ),
            )
        ),
        ev.RecordingStarted(
            Recording(
                recording_id="rec-1",
                title=LONG_TITLE,
                channel_id="UC0000000000000000000003",
                channel_name="가나다 스튜디오 공식 채널",
                quality="1080p60",
                state=RecordingState.RECORDING,
                started_at=now - timedelta(hours=1, minutes=22),
                reported_bytes=1239_500_000,
                reported_elapsed=timedelta(hours=1, minutes=22, seconds=3),
                reported_at=now,
            )
        ),
        ev.RecordingStarted(
            Recording(
                recording_id="rec-2",
                title=EMOJI_TITLE,
                channel_id="UC0000000000000000000002",
                channel_name="🎧 심야 라디오 아카이브 채널",
                quality="720p",
                state=RecordingState.RETRYING,
                started_at=now - timedelta(minutes=12),
                reported_bytes=48_300_000,
                reported_elapsed=timedelta(minutes=11, seconds=40),
                reported_at=now - timedelta(seconds=95),
                retry_count=3,
                detail="조각 내려받기 실패, 재시도 3회",
            )
        ),
        ev.RecordingStarted(
            Recording(
                recording_id="rec-3",
                title="짧은 제목",
                channel_id="UC0000000000000000000001",
                channel_name="가나다 스튜디오 공식 채널",
                quality="1440p",
                state=RecordingState.STALLED,
                started_at=now - timedelta(minutes=40),
                reported_bytes=780_000_000,
                reported_elapsed=timedelta(minutes=37, seconds=2),
                reported_at=now - timedelta(minutes=3),
                detail="3분째 진행 보고 없음",
            )
        ),
        ev.RecordingFinished(
            CompletedRecording(
                recording_id="done-1",
                title="어제 정규 방송 전체 녹화본",
                channel_name="가나다 스튜디오 공식 채널",
                finished_at=now - timedelta(hours=14),
                duration=timedelta(hours=3, minutes=12),
                total_bytes=8_912_000_000,
                status=CompletionStatus.COMPLETED,
                output_path=r"D:\recordings\어제 정규 방송 전체 녹화본.mp4",
            )
        ),
        ev.RecordingFinished(
            CompletedRecording(
                recording_id="done-2",
                title=EMOJI_TITLE,
                channel_name="🎧 심야 라디오 아카이브 채널",
                finished_at=now - timedelta(hours=9),
                duration=timedelta(hours=1, minutes=2),
                total_bytes=2_140_000_000,
                status=CompletionStatus.PARTIAL,
                output_path=r"D:\recordings\긴급 생방송.mp4",
                note="마지막 8초 조각 누락",
            )
        ),
        ev.RecordingFinished(
            CompletedRecording(
                recording_id="done-3",
                title=LONG_TITLE,
                channel_name="가나다 스튜디오 공식 채널",
                finished_at=now - timedelta(hours=3),
                duration=timedelta(minutes=4),
                total_bytes=0,
                status=CompletionStatus.FAILED,
                note="인증 만료로 시작 직후 중단",
            )
        ),
        ev.RecordingFinished(
            CompletedRecording(
                recording_id="done-4",
                title="이동된 파일 항목",
                channel_name="가나다 스튜디오 공식 채널",
                finished_at=now - timedelta(hours=1),
                duration=timedelta(minutes=52),
                total_bytes=1_182_300_000,
                status=CompletionStatus.MISSING,
                output_path=r"D:\recordings\없어진 파일.mp4",
            )
        ),
        ev.LogAppended(
            LogEntry(
                at=now - timedelta(minutes=6),
                severity=Severity.ERROR,
                source="rec-2",
                message="조각 내려받기 실패 (HTTP 403), 재시도합니다",
            )
        ),
        ev.LogAppended(
            LogEntry(
                at=now - timedelta(minutes=2),
                severity=Severity.WARNING,
                source="UC0000000000000000000003",
                message="채널 확인 실패 — 네트워크에 연결할 수 없습니다",
            )
        ),
        ev.QuotaChanged(QuotaStatus(used=3120, limit=10000, resets_at=now + timedelta(hours=6))),
    ]


PRESETS = {
    "disconnected": disconnected_preset,
    "empty": empty_preset,
    "populated": populated_preset,
}


# ----------------------------------------------------------------------
# 시나리오
# ----------------------------------------------------------------------
def recording_lifecycle(*, recording_id: str = "rec-demo") -> list[ScriptStep]:
    """시작 → 진행 → 완료 → 오류 순서의 표준 시나리오.

    이슈 #7의 `테스트 방식` 에 나온 전이 순서를 그대로 재생한다.
    """
    now = _now()
    steps: list[ScriptStep] = [
        (0, ev.ConnectionChanged(ConnectionState.CONNECTED)),
        (
            0,
            ev.WatchStatusChanged(
                state=WatchState.WATCHING,
                channel_count=1,
                next_check_at=now + timedelta(minutes=5),
            ),
        ),
        (
            0,
            ev.ChannelsChanged(
                (
                    WatchedChannel(
                        channel_id="UC-demo",
                        name="시나리오 채널",
                        next_check_at=now + timedelta(minutes=5),
                        last_check_at=now,
                        last_check_result="라이브 1건 감지",
                        live_now=True,
                    ),
                )
            ),
        ),
        (
            300,
            ev.RecordingStarted(
                Recording(
                    recording_id=recording_id,
                    title="시나리오 라이브",
                    channel_name="시나리오 채널",
                    quality="1080p60",
                    state=RecordingState.STARTING,
                    started_at=now,
                )
            ),
        ),
    ]
    for i in range(1, 6):
        steps.append(
            (
                300,
                ev.RecordingProgress(
                    recording_id=recording_id,
                    reported_bytes=i * 120_000_000,
                    reported_elapsed=timedelta(seconds=i * 30),
                    state=RecordingState.RECORDING,
                ),
            )
        )
    steps.append(
        (
            300,
            ev.RecordingProgress(
                recording_id=recording_id,
                reported_bytes=600_000_000,
                reported_elapsed=timedelta(seconds=150),
                state=RecordingState.RETRYING,
                retry_count=1,
                detail="조각 내려받기 실패, 재시도 1회",
            ),
        )
    )
    steps.append(
        (
            600,
            ev.RecordingFinished(
                CompletedRecording(
                    recording_id=recording_id,
                    title="시나리오 라이브",
                    channel_name="시나리오 채널",
                    duration=timedelta(seconds=150),
                    total_bytes=612_000_000,
                    status=CompletionStatus.PARTIAL,
                    note="마지막 조각 누락",
                )
            ),
        )
    )
    steps.append(
        (
            300,
            ev.LogAppended(
                LogEntry(
                    at=_now(),
                    severity=Severity.ERROR,
                    source=recording_id,
                    message="방송 종료 시점 마지막 조각을 받지 못했습니다",
                )
            ),
        )
    )
    steps.append(
        (
            300,
            ev.WatchStatusChanged(
                state=WatchState.STOPPED,
                channel_count=1,
                stop_reason=StopReason.QUOTA_EXCEEDED,
            ),
        )
    )
    return steps


def flood_script(*, recording_id: str = "rec-flood", count: int = 500) -> list[ScriptStep]:
    """진행 이벤트를 촘촘히 밀어 넣는 시나리오. 갱신 빈도 제한 확인용."""
    steps: list[ScriptStep] = [
        (0, ev.ConnectionChanged(ConnectionState.CONNECTED)),
        (
            0,
            ev.RecordingStarted(
                Recording(
                    recording_id=recording_id,
                    title="부하 시험 녹화",
                    channel_name="스텁 채널",
                    quality="1080p60",
                    state=RecordingState.RECORDING,
                    started_at=_now(),
                )
            ),
        ),
    ]
    for i in range(1, count + 1):
        steps.append(
            (
                10,
                ev.RecordingProgress(
                    recording_id=recording_id,
                    reported_bytes=i * 512 * 1024,
                    reported_elapsed=timedelta(milliseconds=i * 10),
                ),
            )
        )
    return steps
