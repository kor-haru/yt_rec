"""표시 문자열 변환.

여기 있는 함수는 전부 순수 함수다. 파일시스템을 건드리지 않는다 — 크기는
항상 상태 모델이 들고 있는 `녹화 프로세스가 보고한 값`을 받아 포맷할 뿐이다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..state.models import (
    CompletionStatus,
    ConnectionState,
    RecordingState,
    Severity,
    StopReason,
    WatchState,
    WatchStatus,
)

__all__ = [
    "format_bytes",
    "format_duration",
    "format_countdown",
    "format_timestamp",
    "watch_badge_text",
    "stop_reason_text",
    "recording_state_text",
    "completion_status_text",
    "severity_text",
    "now",
]

_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def now() -> datetime:
    """로컬 시간대의 현재 시각. 테스트가 갈아끼울 수 있도록 한 곳에 모은다."""
    return datetime.now(timezone.utc).astimezone()


def format_bytes(count: int | None) -> str:
    """바이트 수를 사람이 읽는 크기 문자열로. 1024 기준."""
    if count is None or count < 0:
        return "—"
    size = float(count)
    for unit in _UNITS:
        if size < 1024 or unit == _UNITS[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return "—"  # pragma: no cover - 위 루프가 항상 반환한다


def format_duration(value: timedelta | None) -> str:
    """경과 시간을 ``H:MM:SS`` 또는 ``M:SS`` 로."""
    if value is None:
        return "—"
    total = int(value.total_seconds())
    sign = "-" if total < 0 else ""
    total = abs(total)
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{sign}{hours}:{minutes:02d}:{seconds:02d}"
    return f"{sign}{minutes}:{seconds:02d}"


def format_countdown(deadline: datetime | None, *, reference: datetime | None = None) -> str:
    """다음 확인까지 남은 시간.

    백엔드가 준 **절대 시각**과 로컬 시계만으로 계산한다. 백엔드를 되묻지
    않으므로 폴링이 아니다.
    """
    if deadline is None:
        return "—"
    ref = reference or now()
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=ref.tzinfo)
    remaining = int((deadline - ref).total_seconds())
    if remaining <= 0:
        return "확인 중"
    if remaining < 60:
        return f"{remaining}초 후"
    minutes, seconds = divmod(remaining, 60)
    if minutes < 60:
        return f"{minutes}분 {seconds}초 후"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}시간 {minutes}분 후"


def format_timestamp(value: datetime | None) -> str:
    """이력 표시용 짧은 시각 문자열."""
    if value is None:
        return "—"
    return value.strftime("%m-%d %H:%M")


def watch_badge_text(connection: ConnectionState, watch: WatchStatus) -> str:
    """상단 바 감시 상태 배지 문구."""
    if connection is not ConnectionState.CONNECTED:
        return "연결 중" if connection is ConnectionState.CONNECTING else "연결 안 됨"
    if watch.state is WatchState.WATCHING:
        return f"감시 중 {watch.channel_count}채널"
    if watch.state is WatchState.STOPPED:
        return "중지됨"
    return "연결 안 됨"


_STOP_REASON_TEXT = {
    StopReason.BACKEND_DOWN: "백엔드에 연결되지 않았습니다",
    StopReason.USER_STOPPED: "사용자가 감시를 중지했습니다",
    StopReason.NO_CHANNELS: "감시할 채널이 선택되지 않았습니다",
    StopReason.AUTH_EXPIRED: "계정 인증이 만료되었습니다",
    StopReason.QUOTA_EXCEEDED: "API 사용량 한도를 초과했습니다",
    StopReason.NETWORK_DOWN: "네트워크에 연결할 수 없습니다",
}


def stop_reason_text(reason: StopReason | None) -> str:
    """감시 중단 사유 문구. 사용자가 무엇을 해야 할지 구분되게 쓴다."""
    if reason is None:
        return ""
    return _STOP_REASON_TEXT.get(reason, "감시가 중지되었습니다")


_RECORDING_STATE_TEXT = {
    RecordingState.STARTING: "시작 중",
    RecordingState.RECORDING: "녹화 중",
    RecordingState.RETRYING: "재시도 중",
    RecordingState.STALLED: "응답 없음",
    RecordingState.STOPPING: "중지 중",
}


def recording_state_text(state: RecordingState) -> str:
    return _RECORDING_STATE_TEXT.get(state, "알 수 없음")


_COMPLETION_TEXT = {
    CompletionStatus.COMPLETED: "완료",
    CompletionStatus.PARTIAL: "부분 복구",
    CompletionStatus.FAILED: "실패",
    CompletionStatus.MISSING: "파일 없음",
}


def completion_status_text(status: CompletionStatus) -> str:
    return _COMPLETION_TEXT.get(status, "알 수 없음")


_SEVERITY_TEXT = {
    Severity.INFO: "정보",
    Severity.WARNING: "경고",
    Severity.ERROR: "오류",
}


def severity_text(severity: Severity) -> str:
    return _SEVERITY_TEXT.get(severity, "정보")
