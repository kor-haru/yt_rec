"""녹화 엔진이 바깥에 알리는 상태와 사건.

GUI(#7)는 여기 정의된 값만 보고 화면을 갱신한다. Qt 에 의존하지 않는 평범한
dataclass 라서, GUI 쪽은 콜백 하나를 시그널로 옮기기만 하면 된다.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .errors import DenialCategory, is_transient
from .merge import MediaVerification
from .metadata import LiveMetadata
from .progress import ProgressSnapshot

__all__ = [
    "FragmentRetried",
    "FragmentSkipped",
    "LogLine",
    "MetadataReady",
    "ProgressReported",
    "RecordingEvent",
    "RecordingFinished",
    "RecordingResult",
    "RecordingStatus",
    "StallDetected",
    "StatusChanged",
]


class RecordingStatus(str, Enum):
    """녹화 한 건의 상태."""

    PENDING = "pending"
    FETCHING_METADATA = "fetching_metadata"
    RECORDING = "recording"
    STALLED = "stalled"
    MERGING = "merging"
    VERIFYING = "verifying"
    #: 누락 없이 끝났다.
    COMPLETED = "completed"
    #: 재생 가능한 파일은 만들었지만 누락 구간이 있다.
    PARTIAL = "partial"
    #: 재생 가능한 파일을 만들지 못했다. 중간 파일은 남겨 둔다.
    FAILED = "failed"
    #: 비공개·삭제·멤버 전용 등으로 녹화할 수 없다.
    DENIED = "denied"

    @property
    def terminal(self) -> bool:
        return self in (
            RecordingStatus.COMPLETED,
            RecordingStatus.PARTIAL,
            RecordingStatus.FAILED,
            RecordingStatus.DENIED,
        )

    def __str__(self) -> str:  # pragma: no cover - 표시용
        return self.value


@dataclass(frozen=True, kw_only=True)
class RecordingResult:
    """녹화 한 건의 최종 결과. ``state.json`` 에 그대로 보관된다."""

    video_id: str
    status: RecordingStatus
    metadata: LiveMetadata
    work_dir: Path
    output_path: Path | None = None
    verification: MediaVerification | None = None
    denial: DenialCategory | None = None
    stalled: bool = False
    skipped_fragments: tuple[int, ...] = ()
    downloaded_bytes: int | None = None
    started_at: float = 0.0
    finished_at: float = 0.0
    message: str = ""

    @property
    def succeeded(self) -> bool:
        """재생 가능한 결과 파일이 나왔는가."""
        return self.status in (RecordingStatus.COMPLETED, RecordingStatus.PARTIAL)

    @property
    def retryable(self) -> bool:
        """잠시 뒤 다시 시도할 가치가 있는가. 감시 루프(#3)가 이 값을 본다."""
        return self.denial is not None and is_transient(self.denial)

    @property
    def duration_seconds(self) -> float:
        return max(0.0, self.finished_at - self.started_at)

    def to_dict(self) -> dict:
        return {
            "video_id": self.video_id,
            "status": self.status.value,
            "metadata": self.metadata.to_dict(),
            "work_dir": str(self.work_dir),
            "output_path": str(self.output_path) if self.output_path else None,
            "verification": self.verification.to_dict() if self.verification else None,
            "denial": self.denial.value if self.denial else None,
            "stalled": self.stalled,
            "skipped_fragments": list(self.skipped_fragments),
            "downloaded_bytes": self.downloaded_bytes,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "message": self.message,
        }


@dataclass(frozen=True, kw_only=True)
class RecordingEvent:
    """모든 사건의 공통 부분."""

    video_id: str
    at: float = field(default_factory=time.time)


@dataclass(frozen=True, kw_only=True)
class StatusChanged(RecordingEvent):
    status: RecordingStatus
    detail: str = ""


@dataclass(frozen=True, kw_only=True)
class MetadataReady(RecordingEvent):
    """녹화 시작 시점에 확보해 보관한 메타데이터."""

    metadata: LiveMetadata


@dataclass(frozen=True, kw_only=True)
class ProgressReported(RecordingEvent):
    """yt-dlp 가 알려준 진행 상황. 파일 크기를 직접 재서 만든 값이 아니다."""

    snapshot: ProgressSnapshot


@dataclass(frozen=True, kw_only=True)
class FragmentRetried(RecordingEvent):
    fragment_index: int
    attempt: int
    #: 재시도 상한. ``None`` 이면 무한 — 이 값이 보이면 설정이 잘못된 것이다(#14).
    max_attempts: int | None


@dataclass(frozen=True, kw_only=True)
class FragmentSkipped(RecordingEvent):
    """재시도 상한에 걸려 건너뛴 조각. 누락 구간의 원인이 된다."""

    fragment_index: int


@dataclass(frozen=True, kw_only=True)
class StallDetected(RecordingEvent):
    idle_seconds: float


@dataclass(frozen=True, kw_only=True)
class LogLine(RecordingEvent):
    """yt-dlp 원문 출력 한 줄. 로그 뷰어(#12)용."""

    text: str


@dataclass(frozen=True, kw_only=True)
class RecordingFinished(RecordingEvent):
    result: RecordingResult
