"""Explicit live-notification -> recording handoff, with no polling machinery.

This is NOT a YouTube push receiver. A trusted adapter must deliver verified
notifications on the backend worker; constructing a synthetic event proves only
this downstream contract. BackendSource's explicit event-only option wires this
handler; legacy construction still uses polling. No receiver is installed here.

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
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol

from yt_rec.logs import redact

from .selection import MemorySeenStore, SeenStore, SelectionStore
from .youtube import YouTubeApi

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


class NotificationRecorder:
    def __init__(
        self, *, youtube: Callable[[], YouTubeApi | None], selection: SelectionStore,
        recorder: Recorder, seen: SeenStore | None = None,
        on_update: Callable[[NotificationResult], None] | None = None,
        clock: Callable[[], float] = time.time,
        can_start: Callable[[], bool] = lambda: True,
    ) -> None:
        self._youtube = youtube
        self._selection = selection
        self._recorder = recorder
        self._seen = seen if seen is not None else MemorySeenStore()
        self._on_update = on_update
        self._clock = clock
        self._can_start = can_start
        # Dispatch -> state is the only lock order. Engine callbacks use state
        # alone, so a slow API lookup cannot stall an already-running engine.
        self._dispatch_lock = threading.RLock()
        self._lock = threading.RLock()
        self._pending: dict[str, LiveNotification] = {}
        self._active: dict[str, NotificationResult] = {}
        self._draining = False

    @property
    def pending_video_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._pending)

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
                    return self._publish(replace(base, reason="이미 완료한 영상입니다"))
                if self._recorder.is_recording(video_id):
                    return self._publish(replace(base, reason="이미 녹화 중입니다"))
                if not self._selection.load():
                    return self._publish(replace(base, reason="선택한 채널이 없습니다"))
                api = self._youtube()
                if api is None:
                    self._pending[video_id] = notice
                    return self._publish(replace(base, status="queued", reason="연결 복구를 기다립니다"))
            live = api.get_live(video_id)
            with self._lock:
                if not self._can_start():
                    return self._publish(replace(base, reason="종료 중이므로 새 녹화를 시작하지 않습니다"))
                if live is None or live.video_id != video_id:
                    return self._publish(replace(base, reason="현재 송출 중인 영상이 아닙니다 (예약/종료/확인 불가)"))
                # Re-read AFTER network I/O: selection may have changed while waiting.
                if live.channel_id not in self._selection.load():
                    return self._publish(replace(base, reason="선택이 해제되었거나 선택하지 않은 채널입니다"))
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
