"""백엔드가 상태 계층으로 밀어 넣는 이벤트.

방향은 항상 백엔드 → :class:`~yt_rec.state.store.AppState` → GUI 한 방향이다.
GUI가 백엔드를 주기적으로 조회하는 경로는 존재하지 않는다. 반대 방향(화면이
백엔드에 무엇을 해 달라고 요청하는 경로)은 :mod:`yt_rec.state.commands` 다.

이벤트는 모두 불변 데이터 클래스이므로 작업 스레드에서 만들어 그대로
시그널에 실어 보내도 안전하다. 상태 적용과 위젯 갱신은 GUI 스레드에서만
일어난다(:mod:`yt_rec.state.store` 참고).

시간대 계약
-----------
**이벤트에 담기는 모든 ``datetime`` 은 시간대를 가진(aware) 값이어야 한다.**
:mod:`yt_rec.state.models` 의 모델 필드도 같다. ``datetime.now(timezone.utc)``
든 ``datetime.now().astimezone()`` 든 어느 시간대여도 좋다 — 화면이 표시
직전에 로컬로 옮긴다(:func:`yt_rec.ui.formatting.to_local`).

시간대가 없는(naive) 값은 계약 위반이다. 파이썬 표준 규칙에 따라 **로컬 벽시계
시각**으로 해석되므로, ``datetime.utcnow()`` 처럼 naive-UTC 를 보내면 시간대
차이만큼 어긋난 시각이 표시된다. 조용히 지나가지 않도록
:class:`AppState <yt_rec.state.store.AppState>` 가 적용할 때
:class:`NaiveDatetimeWarning` 을 낸다.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime, timedelta

from .models import (
    AccountInfo,
    CompletedRecording,
    ConnectionState,
    LogEntry,
    QuotaStatus,
    Recording,
    RecordingState,
    StopReason,
    Subscription,
    WatchedChannel,
    WatchState,
)

__all__ = [
    "ConnectionChanged",
    "WatchStatusChanged",
    "ChannelsChanged",
    "RecordingStarted",
    "RecordingProgress",
    "RecordingFinished",
    "LogAppended",
    "QuotaChanged",
    "AccountChanged",
    "SubscriptionsChanged",
    "BackendEvent",
    "NaiveDatetimeWarning",
    "naive_datetime_fields",
    "warn_if_naive",
]


@dataclass(frozen=True, slots=True)
class ConnectionChanged:
    """백엔드 연결 상태가 바뀌었다."""

    state: ConnectionState


@dataclass(frozen=True, slots=True)
class WatchStatusChanged:
    """감시 루프 요약이 바뀌었다."""

    state: WatchState
    channel_count: int = 0
    stop_reason: StopReason | None = None
    next_check_at: datetime | None = None
    """다음 확인 시각. 시간대를 가진 값이어야 한다(모듈 docstring `시간대 계약`)."""


@dataclass(frozen=True, slots=True)
class ChannelsChanged:
    """감시 대상 채널 목록 전체 교체."""

    channels: tuple[WatchedChannel, ...] = ()


@dataclass(frozen=True, slots=True)
class RecordingStarted:
    """녹화가 시작됐다. 같은 ``recording_id`` 가 이미 있으면 덮어쓴다."""

    recording: Recording


@dataclass(frozen=True, slots=True)
class RecordingProgress:
    """진행 중 녹화의 보고값 갱신. 초당 수십 건까지 올 수 있다.

    ``reported_bytes`` 와 ``reported_elapsed`` 는 녹화 프로세스가 보고한
    값이어야 한다. 이 이벤트를 만드는 쪽이 ``os.stat`` 으로 크기를 재서
    채우면 계약이 깨진다.
    """

    recording_id: str
    reported_bytes: int
    reported_elapsed: timedelta
    state: RecordingState = RecordingState.RECORDING
    retry_count: int = 0
    detail: str = ""
    reported_at: datetime | None = None
    """보고 시각. 생략하면 상태 계층이 수신 시각(시간대 있음)으로 채운다.

    직접 채울 때도 시간대를 가진 값이어야 한다(모듈 docstring `시간대 계약`).
    """


@dataclass(frozen=True, slots=True)
class RecordingFinished:
    """녹화가 마무리됐다. 진행 목록에서 빠지고 완료 이력 맨 앞에 붙는다."""

    completed: CompletedRecording


@dataclass(frozen=True, slots=True)
class LogAppended:
    """로그 한 줄이 쌓였다. ``ERROR`` 수준이면 오류 카운터가 함께 올라간다."""

    entry: LogEntry


@dataclass(frozen=True, slots=True)
class QuotaChanged:
    """API quota 사용량이 바뀌었다."""

    quota: QuotaStatus = field(default_factory=QuotaStatus)


@dataclass(frozen=True, slots=True)
class AccountChanged:
    """연결된 계정 정보가 바뀌었다. 미연결이면 빈 :class:`AccountInfo`."""

    account: AccountInfo = field(default_factory=AccountInfo)


@dataclass(frozen=True, slots=True)
class SubscriptionsChanged:
    """구독 채널 목록 전체 교체. 채널 관리 화면이 이것을 그린다."""

    subscriptions: tuple[Subscription, ...] = ()


BackendEvent = (
    ConnectionChanged
    | WatchStatusChanged
    | ChannelsChanged
    | RecordingStarted
    | RecordingProgress
    | RecordingFinished
    | LogAppended
    | QuotaChanged
    | AccountChanged
    | SubscriptionsChanged
)


# ----------------------------------------------------------------------
# 시간대 계약 감시
# ----------------------------------------------------------------------
class NaiveDatetimeWarning(UserWarning):
    """시간대가 없는 ``datetime`` 이 상태 계층에 들어왔다.

    예외가 아니라 경고인 이유: 장시간 구동하는 앱을 필드 하나 때문에 죽이는
    것보다, 개발 중에 눈에 띄게 만드는 편이 낫다. 테스트에서는
    ``pytest.warns(NaiveDatetimeWarning)`` 또는 ``-W error`` 로 실패로 승격할
    수 있다.
    """


def naive_datetime_fields(value: object, *, _path: str = "") -> tuple[str, ...]:
    """``value`` 안에서 시간대가 없는 ``datetime`` 필드의 경로를 모두 찾는다.

    이벤트와 모델은 중첩된 불변 데이터 클래스와 튜플뿐이므로 그 둘만 따라간다.
    돌려주는 것은 ``('recording.started_at', ...)`` 같은 점으로 이은 경로다.

    백엔드 구현이 스스로 검사할 때도 쓸 수 있다::

        assert not naive_datetime_fields(event), "시간대 없는 시각을 보냈다"
    """
    if isinstance(value, datetime):
        return () if value.tzinfo is not None else (_path or "<value>",)
    if is_dataclass(value) and not isinstance(value, type):
        found: list[str] = []
        for spec in fields(value):
            child = getattr(value, spec.name)
            prefix = f"{_path}.{spec.name}" if _path else spec.name
            found.extend(naive_datetime_fields(child, _path=prefix))
        return tuple(found)
    if isinstance(value, (tuple, list)):
        found = []
        for index, child in enumerate(value):
            prefix = f"{_path}[{index}]" if _path else f"[{index}]"
            found.extend(naive_datetime_fields(child, _path=prefix))
        return tuple(found)
    return ()


def warn_if_naive(event: object, *, stacklevel: int = 3) -> tuple[str, ...]:
    """시간대 없는 시각이 섞여 있으면 경고를 내고 그 경로를 돌려준다."""
    naive = naive_datetime_fields(event)
    if naive:
        warnings.warn(
            f"{type(event).__name__} 의 {', '.join(naive)} 에 시간대가 없다. "
            "상태 계층은 시간대를 가진 datetime 을 요구한다 "
            "(datetime.now(timezone.utc) 또는 datetime.now().astimezone()). "
            "naive 값은 로컬 벽시계 시각으로 해석되므로 UTC 를 그대로 보내면 "
            "시간대 차이만큼 어긋난 시각이 표시된다.",
            NaiveDatetimeWarning,
            stacklevel=stacklevel,
        )
    return naive
