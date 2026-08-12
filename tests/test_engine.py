"""녹화 엔진 전체 경로.

네트워크가 필요한 부분은 가짜 yt-dlp 로 대신한다. 병합과 검증은 실제 ffmpeg/ffprobe
를 쓴다.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from yt_rec.recording import (
    DenialCategory,
    LiveMetadata,
    MetadataUnavailableError,
    ProgressReported,
    RecordingEngine,
    RecordingOptions,
    RecordingStatus,
    StallDetected,
    Toolchain,
)
from yt_rec.recording.engine import STATE_FILENAME
from yt_rec.recording.progress import PROGRESS_MARKER

FAKE_YTDLP = Path(__file__).parent / "fake_ytdlp.py"
KST = timezone(timedelta(hours=9))
VIDEO_ID = "zoYkEERlM0w"

DUMMY_TOOLCHAIN = Toolchain(
    ytdlp=Path("yt-dlp"), ffmpeg=Path("ffmpeg"), ffprobe=Path("ffprobe")
)


def progress(downloaded: int, fragment: int, fmt: str = "137") -> str:
    return "|".join(
        [PROGRESS_MARKER, "downloading", str(downloaded), "NA", "NA", "1000.0", "NA",
         "1.0", str(fragment), "NA", fmt]
    )


def stored_metadata(title: str = "세제개편안 이후 대응 전략(메디테라)") -> LiveMetadata:
    return LiveMetadata(
        video_id=VIDEO_ID,
        title=title,
        channel="메디테라",
        release_timestamp=int(datetime(2026, 8, 11, 20, 0, tzinfo=KST).timestamp()),
        live_status="is_live",
        fetched_at=time.time(),
    )


class FakeYtdlpEngine(RecordingEngine):
    """다운로드만 가짜 yt-dlp 로 돌리는 엔진. 나머지는 그대로 쓴다."""

    plan_path: Path

    def build_download_argv(self, video_id: str) -> list[str]:
        argv = super().build_download_argv(video_id)
        return [sys.executable, str(FAKE_YTDLP), str(self.plan_path)] + argv[1:]


def make_engine(
    tmp_path: Path, toolchain: Toolchain, plan: dict, **option_overrides
) -> FakeYtdlpEngine:
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    options = RecordingOptions(
        output_dir=tmp_path / "녹화",
        stall_timeout_seconds=option_overrides.pop("stall_timeout_seconds", 300.0),
        **option_overrides,
    )
    engine = FakeYtdlpEngine(options, toolchain=toolchain, tz=KST)
    engine.plan_path = plan_path
    return engine


# -- yt-dlp 인자 구성 -----------------------------------------------------------


def base_engine(tmp_path: Path, **overrides) -> RecordingEngine:
    options = RecordingOptions(output_dir=tmp_path / "out", **overrides)
    return RecordingEngine(options, toolchain=DUMMY_TOOLCHAIN)


def test_방송_시작_지점부터_받는다(tmp_path):
    assert "--live-from-start" in base_engine(tmp_path).build_download_argv(VIDEO_ID)


def test_조각_재시도_상한이_유한하게_들어간다(tmp_path):
    argv = base_engine(tmp_path, fragment_retries=20).build_download_argv(VIDEO_ID)
    value = argv[argv.index("--fragment-retries") + 1]
    assert value == "20"
    assert value != "infinite"


def test_전체_재시도와_조각_재시도를_구분해_넘긴다(tmp_path):
    argv = base_engine(
        tmp_path, total_retries=10, fragment_retries=20
    ).build_download_argv(VIDEO_ID)
    assert argv[argv.index("--retries") + 1] == "10"
    assert argv[argv.index("--fragment-retries") + 1] == "20"


def test_상한에_걸린_조각은_건너뛰도록_지시한다(tmp_path):
    argv = base_engine(tmp_path).build_download_argv(VIDEO_ID)
    assert "--skip-unavailable-fragments" in argv
    assert "--abort-on-unavailable-fragments" not in argv


def test_화질_상한이_포맷_인자로_들어간다(tmp_path):
    argv = base_engine(tmp_path, max_height=720).build_download_argv(VIDEO_ID)
    assert argv[argv.index("-f") + 1] == "bv*[height<=720]+ba/b[height<=720]/bv*+ba/b"


def test_사용자_설정과_무관하게_동작하도록_설정_파일을_무시한다(tmp_path):
    assert "--ignore-config" in base_engine(tmp_path).build_download_argv(VIDEO_ID)


def test_출력은_video_id_로_두고_최종_이름은_우리가_정한다(tmp_path):
    argv = base_engine(tmp_path).build_download_argv(VIDEO_ID)
    assert argv[argv.index("-o") + 1] == "%(id)s.%(ext)s"
    assert argv[-1].endswith(VIDEO_ID)


def test_추가_인자는_뒤에_붙는다(tmp_path):
    argv = base_engine(
        tmp_path, extra_ytdlp_args=("--cookies-from-browser", "chrome")
    ).build_download_argv(VIDEO_ID)
    assert argv[-3:] == ["--cookies-from-browser", "chrome", argv[-1]]


# -- 정상 종료 ------------------------------------------------------------------


@pytest.mark.integration
def test_정상_종료하면_보관된_제목으로_파일이_저장된다(
    tmp_path, toolchain, sample_streams, monkeypatch
):
    merged = tmp_path / "merged.mp4"
    _premerge(sample_streams, merged, toolchain)

    engine = make_engine(
        tmp_path,
        toolchain,
        {"files": {f"{VIDEO_ID}.mp4": str(merged)}, "lines": [progress(1000, 1)]},
    )
    _prestore(engine, stored_metadata())

    result = engine.record(VIDEO_ID)

    assert result.status is RecordingStatus.COMPLETED
    assert result.output_path.name == "2026-08-11_세제개편안 이후 대응 전략(메디테라).mp4"
    assert result.output_path.exists()
    assert result.verification.complete is True


@pytest.mark.integration
def test_검증에_성공하면_중간_파일을_치운다(
    tmp_path, toolchain, sample_streams, monkeypatch
):
    video, audio = sample_streams
    engine = make_engine(
        tmp_path,
        toolchain,
        {
            "files": {
                f"{VIDEO_ID}.f137.mp4": str(video),
                f"{VIDEO_ID}.f140.m4a": str(audio),
            },
            "lines": [progress(1000, 1)],
        },
    )
    work_dir = engine.work_dir_for(VIDEO_ID)
    _prestore(engine, stored_metadata())

    result = engine.record(VIDEO_ID)

    assert result.succeeded
    assert not (work_dir / f"{VIDEO_ID}.f137.mp4").exists()
    assert not (work_dir / f"{VIDEO_ID}.f140.m4a").exists()
    # 메타데이터와 상태는 남긴다.
    assert (work_dir / "metadata.json").exists()
    assert (work_dir / STATE_FILENAME).exists()


@pytest.mark.integration
def test_같은_제목이_이미_있으면_덮어쓰지_않는다(tmp_path, toolchain, sample_streams):
    merged = tmp_path / "merged.mp4"
    _premerge(sample_streams, merged, toolchain)

    output_dir = tmp_path / "녹화"
    output_dir.mkdir(parents=True)
    existing = output_dir / "2026-08-11_세제개편안 이후 대응 전략(메디테라).mp4"
    existing.write_text("먼저 있던 파일", encoding="utf-8")

    engine = make_engine(
        tmp_path, toolchain, {"files": {f"{VIDEO_ID}.mp4": str(merged)}}
    )
    _prestore(engine, stored_metadata())

    result = engine.record(VIDEO_ID)

    assert existing.read_text(encoding="utf-8") == "먼저 있던 파일"
    assert result.output_path.name.endswith(" (2).mp4")


# -- 메타데이터 선확보 -----------------------------------------------------------


@pytest.mark.integration
def test_방송_종료_후_조회가_막혀도_보관된_제목으로_이름을_정한다(
    tmp_path, toolchain, sample_streams, monkeypatch
):
    """멤버 전용 전환을 흉내 낸다. 시작 시점 조회만 성공하고 이후는 모두 실패한다."""
    merged = tmp_path / "merged.mp4"
    _premerge(sample_streams, merged, toolchain)

    calls = []

    def fetch_once(video_id, url, tools, work_dir, **kwargs):
        calls.append(video_id)
        if len(calls) > 1:
            raise MetadataUnavailableError(
                "Join this channel to get access to members-only content",
                DenialCategory.MEMBERS_ONLY,
            )
        return stored_metadata("왕초보도 하루만에 끝내는 경매 기초! 🔥")

    monkeypatch.setattr("yt_rec.recording.engine.fetch_metadata", fetch_once)

    engine = make_engine(
        tmp_path, toolchain, {"files": {f"{VIDEO_ID}.mp4": str(merged)}}
    )
    result = engine.record(VIDEO_ID)

    assert calls == [VIDEO_ID]  # 마무리 단계에서 다시 조회하지 않는다
    assert result.output_path.name == "2026-08-11_왕초보도 하루만에 끝내는 경매 기초! 🔥.mp4"


def test_시작_시점_조회가_영구히_막히면_녹화를_시작하지_않는다(tmp_path, monkeypatch):
    def blocked(*args, **kwargs):
        raise MetadataUnavailableError("Private video", DenialCategory.PRIVATE)

    monkeypatch.setattr("yt_rec.recording.engine.fetch_metadata", blocked)

    engine = RecordingEngine(
        RecordingOptions(output_dir=tmp_path / "out"), toolchain=DUMMY_TOOLCHAIN
    )
    result = engine.record(VIDEO_ID)

    assert result.status is RecordingStatus.DENIED
    assert result.denial is DenialCategory.PRIVATE
    assert result.retryable is False


def test_일시적인_실패는_다시_시도할_수_있다고_알린다(tmp_path, monkeypatch):
    def not_started(*args, **kwargs):
        raise MetadataUnavailableError(
            "This live event will begin in 3 hours", DenialCategory.NOT_STARTED
        )

    monkeypatch.setattr("yt_rec.recording.engine.fetch_metadata", not_started)

    engine = RecordingEngine(
        RecordingOptions(output_dir=tmp_path / "out"), toolchain=DUMMY_TOOLCHAIN
    )
    result = engine.record(VIDEO_ID)

    assert result.status is RecordingStatus.DENIED
    assert result.retryable is True


def test_확보한_메타데이터는_곧바로_보관된다(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "yt_rec.recording.engine.fetch_metadata",
        lambda *a, **k: stored_metadata("보관될 제목"),
    )
    engine = make_engine(tmp_path, DUMMY_TOOLCHAIN, {"exit_code": 1})
    engine.record(VIDEO_ID)

    saved = LiveMetadata.load(engine.work_dir_for(VIDEO_ID))
    assert saved is not None and saved.title == "보관될 제목"


# -- 정지 판정과 복구 ------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.slow
def test_진전이_없으면_정지로_판정하고_병합해_마무리한다(
    tmp_path, toolchain, sample_streams
):
    video, audio = sample_streams
    engine = make_engine(
        tmp_path,
        toolchain,
        {
            "files": {
                f"{VIDEO_ID}.f137.mp4": str(video),
                f"{VIDEO_ID}.f140.m4a": str(audio),
            },
            "lines": [progress(1000, 1), progress(2000, 2)],
            "sleep": 120,
        },
        stall_timeout_seconds=3.0,
    )
    _prestore(engine, stored_metadata())

    events = []
    engine.add_listener(events.append)

    started = time.monotonic()
    result = engine.record(VIDEO_ID)
    elapsed = time.monotonic() - started

    assert elapsed < 60  # 120초를 다 기다리지 않고 끊었다
    assert result.stalled is True
    assert result.succeeded  # 지금까지 받은 것을 병합해 살렸다
    assert result.output_path.exists()
    assert any(isinstance(e, StallDetected) for e in events)
    assert any(isinstance(e, ProgressReported) for e in events)


@pytest.mark.integration
def test_중간_파일밖에_없어도_병합해_마무리한다(tmp_path, toolchain, sample_streams):
    video, audio = sample_streams
    engine = make_engine(
        tmp_path,
        toolchain,
        {
            "files": {
                f"{VIDEO_ID}.f137.mp4": str(video),
                f"{VIDEO_ID}.f140.m4a": str(audio),
            },
            "exit_code": 1,
        },
    )
    _prestore(engine, stored_metadata())

    result = engine.record(VIDEO_ID)

    assert result.succeeded
    assert result.output_path.exists()
    assert result.verification.playable is True


@pytest.mark.integration
def test_병합_검증에_실패하면_중간_파일을_남긴다(tmp_path, toolchain):
    """검증이 실패했는데 중간 파일까지 지우면 손으로도 살릴 수 없다."""
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"\x00" * 8192)

    engine = make_engine(
        tmp_path,
        toolchain,
        {"files": {f"{VIDEO_ID}.f137.mp4": str(broken)}, "exit_code": 1},
    )
    work_dir = engine.work_dir_for(VIDEO_ID)
    _prestore(engine, stored_metadata())

    result = engine.record(VIDEO_ID)

    assert result.status is RecordingStatus.FAILED
    assert result.output_path is None
    assert (work_dir / f"{VIDEO_ID}.f137.mp4").exists()


@pytest.mark.integration
def test_받은_것이_없으면_실패로_기록한다(tmp_path, toolchain):
    engine = make_engine(tmp_path, toolchain, {"exit_code": 1})
    _prestore(engine, stored_metadata())

    result = engine.record(VIDEO_ID)

    assert result.status is RecordingStatus.FAILED
    assert result.output_path is None


@pytest.mark.integration
def test_재시작_후_남은_녹화를_복구한다(tmp_path, toolchain, sample_streams):
    """프로세스가 죽어 중간 파일만 남은 상태에서 앱을 다시 켠 상황."""
    video, audio = sample_streams
    options = RecordingOptions(output_dir=tmp_path / "녹화")
    work_dir = options.resolved_work_root() / VIDEO_ID
    work_dir.mkdir(parents=True)
    stored_metadata().save(work_dir)
    (work_dir / f"{VIDEO_ID}.f137.mp4").write_bytes(video.read_bytes())
    (work_dir / f"{VIDEO_ID}.f140.m4a").write_bytes(audio.read_bytes())
    (work_dir / STATE_FILENAME).write_text(
        json.dumps({"status": "recording", "started_at": time.time()}), encoding="utf-8"
    )

    engine = RecordingEngine(options, toolchain=toolchain, tz=KST)
    results = engine.recover_pending()

    assert len(results) == 1
    assert results[0].succeeded
    assert results[0].stalled is True
    assert results[0].output_path.name.startswith("2026-08-11_")


@pytest.mark.integration
def test_이미_끝난_녹화는_다시_건드리지_않는다(tmp_path, toolchain, sample_streams):
    video, _ = sample_streams
    options = RecordingOptions(output_dir=tmp_path / "녹화")
    work_dir = options.resolved_work_root() / VIDEO_ID
    work_dir.mkdir(parents=True)
    stored_metadata().save(work_dir)
    (work_dir / f"{VIDEO_ID}.f137.mp4").write_bytes(video.read_bytes())
    (work_dir / STATE_FILENAME).write_text(
        json.dumps({"status": "completed"}), encoding="utf-8"
    )

    engine = RecordingEngine(options, toolchain=toolchain, tz=KST)
    assert engine.recover_pending() == []


@pytest.mark.integration
def test_이전_시도가_남긴_복구본을_결과로_내보내지_않는다(
    tmp_path, toolchain, sample_streams
):
    """낡은 복구본을 그대로 쓰면 이번에 받은 중간 파일이 버려진다."""
    video, audio = sample_streams
    engine = make_engine(
        tmp_path,
        toolchain,
        {
            "files": {
                f"{VIDEO_ID}.f137.mp4": str(video),
                f"{VIDEO_ID}.f140.m4a": str(audio),
            }
        },
    )
    work_dir = engine.work_dir_for(VIDEO_ID)
    work_dir.mkdir(parents=True, exist_ok=True)
    # 지난 시도가 남긴, 검증에 실패했던 복구본.
    (work_dir / f"{VIDEO_ID}.recovered.mp4").write_bytes(b"\x00" * 8192)
    _prestore(engine, stored_metadata())

    result = engine.record(VIDEO_ID)

    assert result.succeeded, "이번에 받은 중간 파일로 다시 병합했어야 한다"
    assert result.verification.playable is True
    assert not (work_dir / f"{VIDEO_ID}.recovered.mp4").exists()


@pytest.mark.integration
@pytest.mark.slow
def test_후처리_중에는_정지로_판정하지_않는다(tmp_path, toolchain, sample_streams):
    """병합·보정 단계에는 진행률이 나오지 않는다. 그동안 끊으면 안 된다."""
    merged = tmp_path / "merged.mp4"
    _premerge(sample_streams, merged, toolchain)

    engine = make_engine(
        tmp_path,
        toolchain,
        {
            "files": {f"{VIDEO_ID}.mp4": str(merged)},
            "lines": [
                progress(1000, 1),
                3,  # 진행률 없는 3초
                "[Merger] Merging formats into \"zoYkEERlM0w.mp4\"",
                3,  # 다시 3초
                "[FixupTimestamp] Fixing frame timestamp",
            ],
        },
        stall_timeout_seconds=5.0,  # 후처리 표시가 없었다면 6초 침묵에 걸렸을 값
    )
    _prestore(engine, stored_metadata())

    events = []
    engine.add_listener(events.append)
    result = engine.record(VIDEO_ID)

    assert result.stalled is False
    assert not any(isinstance(e, StallDetected) for e in events)
    assert result.succeeded


@pytest.mark.integration
def test_출력_경로가_길면_이름을_줄여서라도_저장한다(
    tmp_path, toolchain, sample_streams
):
    merged = tmp_path / "merged.mp4"
    _premerge(sample_streams, merged, toolchain)

    deep = tmp_path / ("가" * 60) / ("나" * 60)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps({"files": {f"{VIDEO_ID}.mp4": str(merged)}}), encoding="utf-8"
    )
    options = RecordingOptions(output_dir=deep, max_title_chars=200)
    engine = FakeYtdlpEngine(options, toolchain=toolchain, tz=KST)
    engine.plan_path = plan_path
    _prestore(engine, stored_metadata("제" * 200))

    result = engine.record(VIDEO_ID)

    assert result.succeeded, result.message
    assert result.output_path.exists()
    assert len(str(result.output_path)) <= 260


# -- 상태 기록 ------------------------------------------------------------------


@pytest.mark.integration
def test_결과_상태를_파일로_남긴다(tmp_path, toolchain, sample_streams):
    merged = tmp_path / "merged.mp4"
    _premerge(sample_streams, merged, toolchain)
    engine = make_engine(tmp_path, toolchain, {"files": {f"{VIDEO_ID}.mp4": str(merged)}})
    _prestore(engine, stored_metadata())

    result = engine.record(VIDEO_ID)

    state = json.loads(
        (engine.work_dir_for(VIDEO_ID) / STATE_FILENAME).read_text(encoding="utf-8")
    )
    assert state["status"] == result.status.value
    assert state["output_path"] == str(result.output_path)
    assert state["verification"]["playable"] is True
    assert state["metadata"]["title"] == "세제개편안 이후 대응 전략(메디테라)"


@pytest.mark.integration
def test_건너뛴_조각을_기록한다(tmp_path, toolchain, sample_streams):
    merged = tmp_path / "merged.mp4"
    _premerge(sample_streams, merged, toolchain)
    engine = make_engine(
        tmp_path,
        toolchain,
        {
            "files": {f"{VIDEO_ID}.mp4": str(merged)},
            "lines": [
                progress(1000, 1),
                "[download] Got error: HTTP Error 404: Not Found. "
                "Retrying fragment 2867 (20/20)...",
                "[download] Skipping fragment 2867 ...",
            ],
        },
    )
    _prestore(engine, stored_metadata())

    result = engine.record(VIDEO_ID)

    assert result.skipped_fragments == (2867,)


@pytest.mark.integration
def test_yt_dlp_원문_출력을_로그로_남긴다(tmp_path, toolchain, sample_streams):
    merged = tmp_path / "merged.mp4"
    _premerge(sample_streams, merged, toolchain)
    engine = make_engine(
        tmp_path,
        toolchain,
        {
            "files": {f"{VIDEO_ID}.mp4": str(merged)},
            "lines": ["[download] Destination: zoYkEERlM0w.f137.mp4"],
        },
    )
    _prestore(engine, stored_metadata())
    engine.record(VIDEO_ID)

    log = (engine.work_dir_for(VIDEO_ID) / "yt-dlp.log").read_text(encoding="utf-8")
    assert "Destination" in log


# -- 도우미 ---------------------------------------------------------------------


def _prestore(engine: RecordingEngine, metadata: LiveMetadata) -> None:
    """녹화 시작 전에 메타데이터를 보관해 둔다(조회를 건너뛰게 한다)."""
    work_dir = engine.work_dir_for(metadata.video_id)
    work_dir.mkdir(parents=True, exist_ok=True)
    metadata.save(work_dir)


def _premerge(sample_streams, dest: Path, toolchain: Toolchain) -> Path:
    from yt_rec.recording.merge import merge_streams

    return merge_streams(list(sample_streams), dest, toolchain)
