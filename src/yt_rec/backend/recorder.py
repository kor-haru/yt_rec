"""RecordingEngine 을 상태 이벤트에 연결한다."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from yt_rec.recording.engine import RecordingEngine
from yt_rec.recording.events import (
    FragmentRetried,
    FragmentSkipped,
    MetadataReady,
    ProgressReported,
    RecordingEvent,
    RecordingFinished as EngineFinished,
    RecordingStatus,
    StallDetected,
    StatusChanged,
)
from yt_rec.recording.options import RecordingOptions
from yt_rec.state import events as ev
from yt_rec.state.models import (
    CompletedRecording,
    CompletionStatus,
    LogEntry,
    Recording,
    RecordingState,
    Severity,
)

__all__ = ["EngineRecorder", "translate_engine_event", "quality_label"]

Emit = Callable[[ev.BackendEvent], None]
ResultHook = Callable[[str, bool], None]


def quality_label(max_height: int | None) -> str:
    return f"{max_height}p" if max_height else ""


def _aware(at: float) -> datetime:
    return datetime.fromtimestamp(at, tz=timezone.utc)


def _engine_state(status: RecordingStatus) -> RecordingState | None:
    if status in (RecordingStatus.PENDING, RecordingStatus.FETCHING_METADATA):
        return RecordingState.STARTING
    if status in (RecordingStatus.RECORDING, RecordingStatus.MERGING, RecordingStatus.VERIFYING):
        return RecordingState.RECORDING
    if status is RecordingStatus.STALLED:
        return RecordingState.STALLED
    return None


def _completion_status(status: RecordingStatus) -> CompletionStatus:
    if status is RecordingStatus.COMPLETED:
        return CompletionStatus.COMPLETED
    if status is RecordingStatus.PARTIAL:
        return CompletionStatus.PARTIAL
    return CompletionStatus.FAILED


def translate_engine_event(
    event: RecordingEvent,
    *,
    title: str,
    channel_id: str,
    channel_name: str,
    quality: str,
) -> list[ev.BackendEvent]:
    """엔진 사건을 상태 계층 이벤트로 옮긴다. 파일 크기를 다시 재지 않는다."""
    recording_id = event.video_id
    at = _aware(event.at)
    if isinstance(event, MetadataReady):
        meta_title = event.metadata.display_title or title
        meta_channel = event.metadata.channel or channel_name
        meta_channel_id = event.metadata.channel_id or channel_id
        return [
            ev.RecordingStarted(
                Recording(
                    recording_id=recording_id,
                    title=meta_title,
                    channel_id=meta_channel_id,
                    channel_name=meta_channel,
                    quality=quality,
                    state=RecordingState.RECORDING,
                    started_at=at,
                )
            )
        ]
    if isinstance(event, StatusChanged):
        # RECORDING/MERGING 의 0바이트 진행 보고는 실제 진행 값을 덮어쓴다.
        # 시작과 정지·재시도만 상태로 알린다.
        if event.status in (
            RecordingStatus.RECORDING,
            RecordingStatus.MERGING,
            RecordingStatus.VERIFYING,
        ):
            return []
        state = _engine_state(event.status)
        if state is None:
            return []
        return [
            ev.RecordingProgress(
                recording_id=recording_id,
                reported_bytes=0,
                reported_elapsed=timedelta(),
                state=state,
                detail=event.detail,
                reported_at=at,
            )
        ]
    if isinstance(event, ProgressReported):
        snapshot = event.snapshot
        elapsed = snapshot.elapsed or 0.0
        return [
            ev.RecordingProgress(
                recording_id=recording_id,
                reported_bytes=int(snapshot.downloaded_bytes or 0),
                reported_elapsed=timedelta(seconds=elapsed),
                state=RecordingState.RECORDING,
                reported_at=at,
            )
        ]
    if isinstance(event, StallDetected):
        return [
            ev.RecordingProgress(
                recording_id=recording_id,
                reported_bytes=0,
                reported_elapsed=timedelta(),
                state=RecordingState.STALLED,
                detail=f"{int(event.idle_seconds)}초째 진행 보고 없음",
                reported_at=at,
            )
        ]
    if isinstance(event, FragmentRetried):
        return [
            ev.RecordingProgress(
                recording_id=recording_id,
                reported_bytes=0,
                reported_elapsed=timedelta(),
                state=RecordingState.RETRYING,
                retry_count=event.attempt,
                detail=f"조각 재시도 {event.attempt}회",
                reported_at=at,
            )
        ]
    if isinstance(event, FragmentSkipped):
        return [
            ev.LogAppended(
                LogEntry(
                    at=at,
                    severity=Severity.WARNING,
                    source=recording_id,
                    message=f"조각 {event.fragment_index} 을 건너뛰었다",
                )
            )
        ]
    if isinstance(event, EngineFinished):
        result = event.result
        output = str(result.output_path) if result.output_path else None
        return [
            ev.RecordingFinished(
                CompletedRecording(
                    recording_id=recording_id,
                    title=result.metadata.display_title or title,
                    channel_name=result.metadata.channel or channel_name,
                    finished_at=at,
                    duration=timedelta(seconds=result.duration_seconds),
                    total_bytes=int(result.downloaded_bytes or 0),
                    status=_completion_status(result.status),
                    output_path=output,
                    note=result.message,
                )
            )
        ]
    return []


class EngineRecorder:
    """video id 당 RecordingEngine 하나를 스레드에서 돌린다."""

    def __init__(
        self,
        options: RecordingOptions,
        emit: Emit,
        *,
        engine_cls: type[RecordingEngine] = RecordingEngine,
        on_result: ResultHook | None = None,
    ) -> None:
        self._options = options
        self._emit = emit
        self._engine_cls = engine_cls
        self._on_result = on_result
        self._lock = threading.Lock()
        self._engines: dict[str, RecordingEngine] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._meta: dict[str, dict[str, str]] = {}
        self._last_progress: dict[str, tuple[int, timedelta]] = {}

    def update_options(self, values: Mapping[str, Any]) -> None:
        changes: dict[str, Any] = {}
        if "output_dir" in values and values["output_dir"]:
            changes["output_dir"] = Path(str(values["output_dir"]))
        if "max_height" in values:
            height = values["max_height"]
            changes["max_height"] = int(height) if height is not None else None
        if "live_from_start" in values:
            changes["live_from_start"] = bool(values["live_from_start"])
        if changes:
            self._options = self._options.with_(**changes)

    def is_recording(self, video_id: str) -> bool:
        with self._lock:
            return video_id in self._engines

    def start(
        self,
        video_id: str,
        *,
        channel_id: str = "",
        channel_name: str = "",
        title: str = "",
    ) -> None:
        with self._lock:
            if video_id in self._engines:
                return
            quality = quality_label(self._options.max_height)
            meta = {
                "title": title or video_id,
                "channel_id": channel_id,
                "channel_name": channel_name,
                "quality": quality,
            }
            self._meta[video_id] = meta
            engine = self._engine_cls(
                self._options,
                on_event=lambda event, vid=video_id: self._on_engine_event(vid, event),
            )
            engine.clear_stop()
            self._engines[video_id] = engine
        self._emit(
            ev.RecordingStarted(
                Recording(
                    recording_id=video_id,
                    title=title or video_id,
                    channel_id=channel_id,
                    channel_name=channel_name,
                    quality=quality_label(self._options.max_height),
                    state=RecordingState.STARTING,
                    started_at=datetime.now(timezone.utc),
                )
            )
        )
        thread = threading.Thread(
            target=self._run,
            args=(video_id, engine),
            name=f"yt-rec-record-{video_id}",
            daemon=False,
        )
        with self._lock:
            self._threads[video_id] = thread
        thread.start()

    def stop(self, recording_id: str) -> None:
        with self._lock:
            engine = self._engines.get(recording_id)
            thread = self._threads.get(recording_id)
        if engine is not None:
            engine.request_stop()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=600)

    def stop_all(self) -> None:
        with self._lock:
            engines = list(self._engines.values())
        for engine in engines:
            engine.request_stop()

    def join_all(self, timeout: float | None = 600) -> None:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._lock:
            threads = list(self._threads.values())
        for thread in threads:
            remaining = None
            if deadline is not None:
                remaining = max(0.0, deadline - time.monotonic())
            if thread is not threading.current_thread():
                thread.join(timeout=remaining)

    def recover_pending(self) -> None:
        engine = self._engine_cls(self._options)
        results = engine.recover_pending()
        quality = quality_label(self._options.max_height)
        for result in results:
            finished = EngineFinished(
                video_id=result.video_id,
                at=result.finished_at or time.time(),
                result=result,
            )
            meta = result.metadata
            for backend_event in translate_engine_event(
                finished,
                title=meta.display_title or result.video_id,
                channel_id=meta.channel_id or "",
                channel_name=meta.channel or "",
                quality=quality,
            ):
                self._emit(backend_event)
            if self._on_result is not None:
                self._on_result(result.video_id, result.succeeded)

    def _run(self, video_id: str, engine: RecordingEngine) -> None:
        ok = False
        try:
            result = engine.record(video_id)
            ok = bool(getattr(result, "succeeded", False))
        finally:
            with self._lock:
                self._engines.pop(video_id, None)
                self._threads.pop(video_id, None)
                self._meta.pop(video_id, None)
                self._last_progress.pop(video_id, None)
            if self._on_result is not None:
                self._on_result(video_id, ok)

    def _on_engine_event(self, video_id: str, event: RecordingEvent) -> None:
        with self._lock:
            meta = dict(self._meta.get(video_id) or {})
            last = self._last_progress.get(video_id)
        if not meta:
            meta = {"title": video_id, "channel_id": "", "channel_name": "", "quality": ""}
        for backend_event in translate_engine_event(
            event,
            title=meta.get("title") or video_id,
            channel_id=meta.get("channel_id") or "",
            channel_name=meta.get("channel_name") or "",
            quality=meta.get("quality") or "",
        ):
            if isinstance(backend_event, ev.RecordingProgress):
                if backend_event.reported_bytes == 0 and last is not None:
                    backend_event = replace(
                        backend_event,
                        reported_bytes=last[0],
                        reported_elapsed=last[1] if backend_event.reported_elapsed == timedelta() else backend_event.reported_elapsed,
                    )
                else:
                    last = (backend_event.reported_bytes, backend_event.reported_elapsed)
                    with self._lock:
                        self._last_progress[video_id] = last
            self._emit(backend_event)
