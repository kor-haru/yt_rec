"""Explicit live-notification -> recording handoff, with no polling machinery.

This is NOT a YouTube push receiver. A trusted adapter must deliver verified
notifications on the backend worker; constructing a synthetic event proves only
this downstream contract. BackendSource's explicit event-only option wires this
handler; legacy construction still uses polling. No receiver is installed here.

A notified video may be a *scheduled* live rather than a running one. Those are
recorded in a ScheduleStore and re-checked a bounded number of times: once a
minute inside a fixed window that ends at their YouTube-supplied
scheduledStartTime, since broadcasters do go live early, then a separately
budgeted number of times after it (see .schedule). Nothing here polls before that
window opens, and an idle handler still issues zero requests.

Wire recorder's post-slot-release on_result to recording_finished(), and ONLY
positive-byte engine ProgressReported events to report_progress(). Call resume()
after an explicit reconnect/capacity change, never from a timer. Pending notices
are session-local; reconnect replay is the receiver's responsibility.
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
from datetime import datetime
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol

from yt_rec.logs import redact

from .schedule import (
    MAX_EARLY_CHECKS,
    MAX_RECHECKS,
    MAX_SCHEDULE_AHEAD_SECONDS,
    ScheduledLive,
    ScheduleStore,
    bump,
)
from .selection import MemorySeenStore, SeenStore, SelectionStore
from .youtube import VideoState, YouTubeApi

_LOG = logging.getLogger(__name__)


class Recorder(Protocol):
    def is_recording(self, video_id: str) -> bool: ...

    def start(self, video_id: str, *, channel_id: str, channel_name: str, title: str) -> bool: ...


@dataclass(frozen=True)
class LiveNotification:
    video_id: str
    received_at: float
    synthetic: bool = True

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", self.video_id):
            raise ValueError("알림에는 YouTube 영상 ID 하나만 허용됩니다")
        if not math.isfinite(self.received_at) or self.received_at < 0:
            raise ValueError("알림 수신 시각이 올바르지 않습니다")


@dataclass(frozen=True)
class NotificationResult:
    notification: LiveNotification
    status: str
    reason: str = ""
    handed_to_recorder_at: float | None = None
    first_media_at: float | None = None
    # A received byte or successful mux says nothing about earliest coverage.
    coverage: str = "unknown"
    coverage_reason: str = "시작 지점 확보 여부는 아직 확인되지 않았습니다 (알림 지연/DVR/되감기 제한 가능)"


def _local_time(epoch: float) -> str:
    """예정 시각을 사람이 읽는 로컬 시각으로. 로그에 epoch 를 그대로 내보내지 않는다."""
    try:
        return datetime.fromtimestamp(float(epoch)).astimezone().strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "시각 미상"


class NotificationRecorder:
    def __init__(
        self, *, youtube: Callable[[], YouTubeApi | None], selection: SelectionStore,
        recorder: Recorder, seen: SeenStore | None = None,
        on_update: Callable[[NotificationResult], None] | None = None,
        clock: Callable[[], float] = time.time,
        can_start: Callable[[], bool] = lambda: True,
        schedules: ScheduleStore | None = None,
        record_premieres: Callable[[], bool] = lambda: False,
        on_schedule: Callable[[], None] | None = None,
    ) -> None:
        self._youtube = youtube
        self._selection = selection
        self._recorder = recorder
        self._seen = seen if seen is not None else MemorySeenStore()
        self._on_update = on_update
        self._clock = clock
        self._can_start = can_start
        self._schedule_store = schedules
        self._record_premieres = record_premieres
        # 예약이 생기거나 사라졌음을 알리는 신호. 막히면 안 된다.
        self._on_schedule = on_schedule
        # Dispatch -> state is the only lock order. Engine callbacks use state
        # alone, so a slow API lookup cannot stall an already-running engine.
        self._dispatch_lock = threading.RLock()
        self._lock = threading.RLock()
        self._pending: dict[str, LiveNotification] = {}
        self._active: dict[str, NotificationResult] = {}
        self._draining = False
        # 재시작 복원. 예정 시각이 이미 지난 예약은 첫 확인에서 바로 처리된다.
        self._schedules: dict[str, ScheduledLive] = {
            item.video_id: item for item in (schedules.load() if schedules is not None else ())
        }

    @property
    def pending_video_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._pending)

    @property
    def scheduled_video_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._schedules))

    def next_schedule_at(self) -> float | None:
        """다음 확인이 필요한 시각. 예약이 없으면 ``None`` — 기다릴 일이 없다."""
        with self._lock:
            due = [entry.due_at() for entry in self._schedules.values()]
        return min(due) if due else None

    def _persist_schedules_locked(self) -> None:
        if self._schedule_store is None:
            return
        try:
            self._schedule_store.save(tuple(self._schedules.values()))
        except OSError as exc:
            # 저장 실패가 이번 실행의 예약까지 버리게 두지 않는다.
            _LOG.warning("예약 라이브 일정을 저장하지 못했습니다: %s", redact(str(exc)))

    def _forget_schedule_locked(self, video_id: str) -> None:
        if self._schedules.pop(video_id, None) is not None:
            self._persist_schedules_locked()
            self._notify_schedule_changed()

    def _notify_schedule_changed(self) -> None:
        if self._on_schedule is None:
            return
        try:
            self._on_schedule()
        except Exception as exc:
            _LOG.warning("예약 변경을 알리지 못했습니다: %s", redact(str(exc)))

    def _publish(self, result: NotificationResult) -> NotificationResult:
        if self._on_update is not None:
            try:
                self._on_update(result)
            except Exception as exc:
                # Keep accepted work, but make observer failures diagnosable.
                _LOG.warning("알림 상태 전달 실패: %s", redact(str(exc)))
        return result

    def receive(self, notification: LiveNotification) -> NotificationResult:
        """Process one trusted event now; duplicate notices issue no API request."""
        with self._dispatch_lock:
            with self._lock:
                video_id = notification.video_id
                if video_id in self._active:
                    return self._active[video_id]
                if video_id in self._pending:
                    return NotificationResult(self._pending[video_id], "queued", "이미 대기 중입니다")
                if video_id in self._schedules:
                    return NotificationResult(notification, "scheduled", "이미 예정 시각을 기다리는 중입니다")
            return self._attempt(notification)

    def _attempt(self, notice: LiveNotification) -> NotificationResult:
        video_id = notice.video_id
        base = NotificationResult(notice, "ignored")
        try:
            with self._lock:
                self._pending.pop(video_id, None)
                if not self._can_start():
                    return self._publish(replace(base, reason="종료 중이므로 새 녹화를 시작하지 않습니다"))
                if self._seen.is_done(video_id):
                    self._forget_schedule_locked(video_id)
                    return self._publish(replace(base, reason="이미 완료한 영상입니다"))
                if self._recorder.is_recording(video_id):
                    self._forget_schedule_locked(video_id)
                    return self._publish(replace(base, reason="이미 녹화 중입니다"))
                if not self._selection.load():
                    self._forget_schedule_locked(video_id)
                    return self._publish(replace(base, reason="선택한 채널이 없습니다"))
                api = self._youtube()
                if api is None:
                    self._pending[video_id] = notice
                    return self._publish(replace(base, status="queued", reason="연결 복구를 기다립니다"))
            # 같은 요청이 live / upcoming / none / ended 를 한 번에 답한다 (#82).
            state = api.get_video_state(video_id)
            with self._lock:
                if not self._can_start():
                    return self._publish(replace(base, reason="종료 중이므로 새 녹화를 시작하지 않습니다"))
                if state.status == "upcoming":
                    return self._schedule_upcoming_locked(notice, state, base)
                live = state.broadcast if state.status == "live" else None
                if live is None or live.video_id != video_id:
                    self._forget_schedule_locked(video_id)
                    return self._publish(replace(base, reason="현재 송출 중인 영상이 아닙니다 (예약/종료/확인 불가)"))
                # Re-read AFTER network I/O: selection may have changed while waiting.
                if live.channel_id not in self._selection.load():
                    self._forget_schedule_locked(video_id)
                    return self._publish(replace(base, reason="선택이 해제되었거나 선택하지 않은 채널입니다"))
                self._forget_schedule_locked(video_id)
                self._seen.mark_started(video_id)
                handed = replace(base, status="handed", handed_to_recorder_at=self._clock())
                self._active[video_id] = handed
                accepted = self._recorder.start(
                    video_id, channel_id=live.channel_id, channel_name=live.channel_name, title=live.title,
                )
                if accepted is False:
                    self._active.pop(video_id, None)
                    self._seen.unmark_started(video_id)
                    if not self._can_start():
                        return self._publish(replace(base, reason="종료 중이므로 새 녹화를 시작하지 않습니다"))
                    self._pending[video_id] = notice
                    return self._publish(replace(base, status="queued", reason="녹화 슬롯을 기다립니다"))
                return self._publish(handed)  # Handoff, not first media or success.
        except Exception as exc:
            with self._lock:
                self._active.pop(video_id, None)
                self._seen.unmark_started(video_id)
            return self._publish(replace(base, status="failed", reason=redact(str(exc))))

    def _schedule_upcoming_locked(
        self, notice: LiveNotification, state: VideoState, base: NotificationResult,
    ) -> NotificationResult:
        """예약 라이브를 적어만 둔다. 지금은 아무것도 녹화하지 않는다."""
        video_id = notice.video_id
        live, scheduled_at = state.broadcast, state.scheduled_start
        if live is None or scheduled_at is None:
            self._forget_schedule_locked(video_id)
            return self._publish(replace(base, reason="예약 라이브이지만 예정 시각을 알 수 없습니다"))
        if live.channel_id not in self._selection.load():
            self._forget_schedule_locked(video_id)
            return self._publish(replace(base, reason="선택이 해제되었거나 선택하지 않은 채널입니다"))
        if state.premiere and not self._record_premieres():
            self._forget_schedule_locked(video_id)
            return self._publish(replace(base, reason="프리미어로 보여 예약하지 않습니다 (설정에서 켤 수 있습니다)"))
        if scheduled_at - self._clock() > MAX_SCHEDULE_AHEAD_SECONDS:
            self._forget_schedule_locked(video_id)
            return self._publish(replace(base, reason="예정 시각이 너무 멀어 예약하지 않습니다"))
        previous = self._schedules.get(video_id)
        # 같은 예정 시각이면 이미 쓴 확인 횟수를 유지한다. 시각이 밀렸으면 처음부터.
        kept = previous if previous is not None and previous.scheduled_at == scheduled_at else None
        entry = ScheduledLive(
            video_id, scheduled_at, notice.received_at,
            kept.attempts if kept is not None else 0, notice.synthetic,
            kept.early_attempts if kept is not None else 0,
        )
        self._schedules[video_id] = entry
        self._persist_schedules_locked()
        result = replace(
            base, status="scheduled",
            reason=f"예약 라이브입니다. {_local_time(scheduled_at)} 예정이며, 그 전 최대 "
                   f"{MAX_EARLY_CHECKS}회와 그 뒤 최대 {MAX_RECHECKS}회 확인합니다",
        )
        if previous is None or previous.scheduled_at != scheduled_at:
            self._notify_schedule_changed()
            return self._publish(result)
        # 재확인 중에는 같은 말을 매분 남기지 않는다.
        return result

    def check_schedules(self, *, now: float | None = None) -> tuple[str, ...]:
        """만기가 된 예약만 다시 확인한다. 아직이면 요청을 하나도 보내지 않는다.

        만기는 창이 열린 뒤의 이른 확인이거나 예정 시각 이후의 재확인이고, 둘은
        예산을 따로 센다 — 각각 :data:`~.schedule.MAX_EARLY_CHECKS` 회,
        :data:`~.schedule.MAX_RECHECKS` 회로 끝난다. 돌려주는 값은 이번에 실제로
        확인한 video id 다.
        """
        moment = self._clock() if now is None else now
        with self._dispatch_lock:
            with self._lock:
                if not self._can_start():
                    return ()
                due: list[ScheduledLive] = []
                expired: list[ScheduledLive] = []
                for entry in tuple(self._schedules.values()):
                    if entry.due_at() > moment:
                        continue
                    (expired if entry.exhausted else due).append(entry)
                for entry in expired:
                    self._schedules.pop(entry.video_id, None)
                for entry in due:
                    # 요청을 보내기 전에 소모한다. 실패해도 같은 만기를 되풀이하지 않는다.
                    self._schedules[entry.video_id] = bump(entry, moment)
                if due or expired:
                    self._persist_schedules_locked()
            for entry in expired:
                self._publish(NotificationResult(
                    LiveNotification(entry.video_id, entry.received_at, entry.synthetic),
                    "expired",
                    f"예정 시각 이후 {MAX_RECHECKS}회 확인했지만 방송이 시작되지 않아 예약을 취소합니다",
                ))
            for entry in due:
                self._attempt(LiveNotification(entry.video_id, entry.received_at, entry.synthetic))
            if expired and not due:
                self._notify_schedule_changed()
            return tuple(entry.video_id for entry in due)

    def recording_finished(self, video_id: str, succeeded: bool, *, resume: bool = True) -> None:
        """Invoke after the recorder releases its slot, including failed starts."""
        try:
            with self._lock:
                result = self._active.pop(video_id, None)
                try:
                    if succeeded:
                        self._seen.mark_done(video_id)
                    else:
                        self._seen.unmark_started(video_id)
                finally:
                    if result is not None:
                        self._publish(replace(result, status="completed" if succeeded else "failed"))
        finally:
            if resume:
                self.resume()

    def resume(self) -> None:
        """One FIFO drain after an explicit event; no autonomous retry or timer."""
        with self._dispatch_lock:
            with self._lock:
                if self._draining or not self._can_start():
                    return
                self._draining = True
                notices = tuple(self._pending.values())
            try:
                for notice in notices:
                    result = self._attempt(notice)
                    if result.status == "queued":
                        # Keep the blocked head ahead of later arrivals.
                        with self._lock:
                            self._pending = {notice.video_id: notice, **self._pending}
                        break
            finally:
                with self._lock:
                    self._draining = False

    def report_progress(self, video_id: str, downloaded_bytes: int, *, at: float | None = None) -> None:
        """Observe first actual positive-byte progress, never process creation."""
        with self._lock:
            result = self._active.get(video_id)
            if result is None or result.first_media_at is not None or downloaded_bytes <= 0:
                return
            stamp = self._clock() if at is None else at
            if not math.isfinite(stamp) or stamp < (result.handed_to_recorder_at or 0):
                return  # Reject stale/out-of-order progress from an earlier attempt.
            result = replace(result, status="receiving", first_media_at=stamp)
            self._active[video_id] = result
            self._publish(result)
