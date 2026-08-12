"""GUI가 참조하는 유일한 상태 모델.

이 모듈의 데이터 클래스는 모두 불변(frozen)이다. 백엔드는 새 값을 만들어
이벤트로 통지하고, GUI는 받은 값을 그리기만 한다. GUI가 파일시스템이나
외부 프로세스를 직접 조회해 여기 담긴 값을 보정하는 일은 없어야 한다.

특히 :class:`Recording` 의 ``reported_bytes`` 와 ``reported_elapsed`` 는
이름 그대로 **녹화 프로세스가 보고한 값**이다. Windows는 쓰기 핸들이 열려
있는 파일의 크기를 디렉터리 엔트리에 즉시 반영하지 않으므로, 진행 중 녹화의
크기를 ``os.stat``/``Path.stat``/``os.path.getsize`` 로 읽으면 실제보다 훨씬
작은 값이 나온다(실측: 1182.3 MB 파일이 22.7 MB로 보임). 그래서 이 값들은
반드시 녹화 프로세스가 보고한 것만 사용한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

__all__ = [
    "ConnectionState",
    "WatchState",
    "StopReason",
    "RecordingState",
    "CompletionStatus",
    "Severity",
    "WatchStatus",
    "WatchedChannel",
    "Recording",
    "CompletedRecording",
    "LogEntry",
    "QuotaStatus",
    "AppSnapshot",
]


class ConnectionState(Enum):
    """GUI와 백엔드(감시·녹화 담당) 사이의 연결 상태."""

    DISCONNECTED = "disconnected"
    """백엔드가 아직 기동하지 않았거나 끊겼다. GUI 최초 기동 시의 기본값."""

    CONNECTING = "connecting"
    CONNECTED = "connected"


class WatchState(Enum):
    """감시 루프의 상태."""

    WATCHING = "watching"
    """정상 감시 중. 상단 배지는 `감시 중 N채널`."""

    STOPPED = "stopped"
    """감시가 멈췄다. 사유는 :class:`StopReason` 으로 구분한다."""

    UNKNOWN = "unknown"
    """백엔드에 연결되지 않아 알 수 없다. 상단 배지는 `연결 안 됨`."""


class StopReason(Enum):
    """감시가 멈춘 사유. 사용자가 무엇을 해야 하는지 구분해 안내하기 위한 값."""

    BACKEND_DOWN = "backend_down"
    USER_STOPPED = "user_stopped"
    NO_CHANNELS = "no_channels"
    AUTH_EXPIRED = "auth_expired"
    QUOTA_EXCEEDED = "quota_exceeded"
    NETWORK_DOWN = "network_down"


class RecordingState(Enum):
    """진행 중 녹화 한 건의 상태."""

    STARTING = "starting"
    RECORDING = "recording"

    RETRYING = "retrying"
    """오류 후 재시도 중. 정상 진행과 시각적으로 구분해야 한다."""

    STALLED = "stalled"
    """일정 시간 이상 진행 보고가 없다. `응답 없음` 으로 표시한다."""

    STOPPING = "stopping"


class CompletionStatus(Enum):
    """마무리된 녹화의 결과."""

    COMPLETED = "completed"
    PARTIAL = "partial"
    """부분 복구. 끝 구간이 누락된 채 마무리됐다."""

    FAILED = "failed"
    MISSING = "missing"
    """이력은 있으나 파일이 이동·삭제됐다."""


class Severity(Enum):
    """로그·오류 수준."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class WatchStatus:
    """감시 루프 전체의 요약. 상단 배지와 하단 상태 표시줄이 이것만 본다."""

    state: WatchState = WatchState.UNKNOWN
    channel_count: int = 0
    stop_reason: StopReason | None = StopReason.BACKEND_DOWN
    next_check_at: datetime | None = None
    """다음 확인 **시각**. 남은 시간이 아니라 절대 시각으로 주고받는다.

    남은 시간을 초 단위로 흘려보내면 초당 이벤트가 발생하므로, 백엔드는
    확인 주기가 바뀔 때만 이 값을 갱신하고 GUI가 로컬 시계로 남은 시간을
    계산해 그린다. 이것은 백엔드 폴링이 아니라 이미 받은 값의 재렌더링이다.
    """


@dataclass(frozen=True, slots=True)
class WatchedChannel:
    """감시 대상 채널 한 건."""

    channel_id: str
    name: str
    next_check_at: datetime | None = None
    last_check_at: datetime | None = None
    last_check_result: str = ""
    live_now: bool = False


@dataclass(frozen=True, slots=True)
class Recording:
    """진행 중인 녹화 한 건.

    크기와 경과 시간은 녹화 프로세스가 보고한 값이다. GUI가 stat으로 산출하지
    않는다(모듈 docstring 참고).
    """

    recording_id: str
    title: str
    channel_id: str = ""
    channel_name: str = ""
    quality: str = ""
    state: RecordingState = RecordingState.STARTING
    started_at: datetime | None = None

    reported_bytes: int = 0
    """녹화 프로세스가 보고한 누적 바이트."""

    reported_elapsed: timedelta = timedelta()
    """녹화 프로세스가 보고한 경과 시간."""

    reported_at: datetime | None = None
    """위 두 값을 보고받은 시각. `응답 없음` 판정의 기준."""

    retry_count: int = 0
    detail: str = ""
    output_path: str | None = None


@dataclass(frozen=True, slots=True)
class CompletedRecording:
    """마무리된 녹화 한 건. 대시보드 `최근 완료` 와 보관함이 함께 쓴다."""

    recording_id: str
    title: str
    channel_name: str = ""
    finished_at: datetime | None = None
    duration: timedelta = timedelta()
    total_bytes: int = 0
    """녹화 프로세스가 마지막으로 보고한 최종 크기."""

    status: CompletionStatus = CompletionStatus.COMPLETED
    output_path: str | None = None
    note: str = ""


@dataclass(frozen=True, slots=True)
class LogEntry:
    """로그·오류 한 줄. 대시보드는 개수만, 로그 뷰어(#12)는 전체를 쓴다."""

    at: datetime
    severity: Severity
    message: str
    source: str = ""


@dataclass(frozen=True, slots=True)
class QuotaStatus:
    """API quota 사용량. 하단 상태 표시줄이 쓴다."""

    used: int = 0
    limit: int | None = None
    resets_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class AppSnapshot:
    """어느 시점의 상태 전체. 화면을 처음 그릴 때와 통째로 다시 그릴 때 쓴다."""

    connection: ConnectionState = ConnectionState.DISCONNECTED
    watch: WatchStatus = field(default_factory=WatchStatus)
    channels: tuple[WatchedChannel, ...] = ()
    recordings: tuple[Recording, ...] = ()
    completed: tuple[CompletedRecording, ...] = ()
    logs: tuple[LogEntry, ...] = ()
    error_count: int = 0
    """누적 오류 수. 하단 상태 표시줄에 표시한다."""

    unseen_error_count: int = 0
    """아직 확인하지 않은 오류 수. 로그 뷰어를 열면 0이 된다(#12)."""

    quota: QuotaStatus = field(default_factory=QuotaStatus)
