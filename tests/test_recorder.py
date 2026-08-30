from __future__ import annotations

import threading
import time
from datetime import timedelta
from pathlib import Path

from yt_rec.backend.recorder import EngineRecorder, translate_engine_event
from yt_rec.recording.options import RecordingOptions
from yt_rec.recording.events import ProgressReported, RecordingStatus, StallDetected, StatusChanged
from yt_rec.recording.progress import ProgressSnapshot
from yt_rec.state.events import RecordingFinished, RecordingProgress
from yt_rec.state.models import RecordingState


def test_진행_보고는_엔진이_준_바이트를_쓴다() -> None:
    event = ProgressReported(
        video_id="vid",
        snapshot=ProgressSnapshot(
            status="downloading",
            downloaded_bytes=1_000_000,
            elapsed=12.5,
        ),
    )
    mapped = translate_engine_event(
        event, title="t", channel_id="UC1", channel_name="ch", quality="1080p"
    )
    progress = mapped[0]
    assert isinstance(progress, RecordingProgress)
    assert progress.reported_bytes == 1_000_000
    assert progress.reported_elapsed == timedelta(seconds=12.5)
    assert progress.state is RecordingState.RECORDING


def test_정지_감지는_응답_없음이다() -> None:
    mapped = translate_engine_event(
        StallDetected(video_id="vid", idle_seconds=90),
        title="t",
        channel_id="UC1",
        channel_name="ch",
        quality="1080p",
    )
    assert mapped[0].state is RecordingState.STALLED  # type: ignore[union-attr]


def test_병합_상태_보고는_진행_값을_덮지_않는다() -> None:
    mapped = translate_engine_event(
        StatusChanged(video_id="vid", status=RecordingStatus.MERGING),
        title="t",
        channel_id="UC1",
        channel_name="ch",
        quality="1080p",
    )
    assert mapped == []
    assert not any(isinstance(item, RecordingFinished) for item in mapped)


def test_stop은_녹화_스레드를_기다린다(tmp_path: Path) -> None:
    started = threading.Event()

    class SlowEngine:
        def __init__(self, options, on_event=None) -> None:
            self._stop = threading.Event()

        def clear_stop(self) -> None:
            return None

        def request_stop(self) -> None:
            self._stop.set()

        def record(self, video_id: str) -> object:
            started.set()
            time.sleep(0.15)
            return type("R", (), {"succeeded": True})()

    events: list = []
    recorder = EngineRecorder(
        RecordingOptions(output_dir=tmp_path),
        events.append,
        engine_cls=SlowEngine,  # type: ignore[arg-type]
    )
    recorder.start("vid")
    assert started.wait(2)
    t0 = time.monotonic()
    recorder.stop("vid")
    assert time.monotonic() - t0 >= 0.1
    assert recorder.is_recording("vid") is False


def test_복구는_엔진_recover_pending을_부른다(tmp_path: Path) -> None:
    from yt_rec.recording.events import RecordingResult, RecordingStatus
    from yt_rec.recording.metadata import LiveMetadata
    from yt_rec.state.events import RecordingFinished

    class RecoverEngine:
        def __init__(self, options, on_event=None) -> None:
            self.called = 0

        def recover_pending(self) -> list:
            self.called += 1
            return [
                RecordingResult(
                    video_id="vid",
                    status=RecordingStatus.COMPLETED,
                    metadata=LiveMetadata(video_id="vid", title="t", channel="c"),
                    work_dir=tmp_path,
                    output_path=tmp_path / "t.mp4",
                    finished_at=1.0,
                )
            ]

    events: list = []
    recorder = EngineRecorder(
        RecordingOptions(output_dir=tmp_path),
        events.append,
        engine_cls=RecoverEngine,  # type: ignore[arg-type]
    )
    recorder.recover_pending()
    assert any(isinstance(item, RecordingFinished) for item in events)
