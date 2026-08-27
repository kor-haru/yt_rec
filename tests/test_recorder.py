from __future__ import annotations

from datetime import timedelta

from yt_rec.backend.recorder import translate_engine_event
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
