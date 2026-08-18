"""녹화 엔진 전체 경로.

네트워크가 필요한 부분은 가짜 yt-dlp 로 대신한다. 병합과 검증은 실제 ffmpeg/ffprobe
를 쓴다.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from yt_rec.recording import (
    BinaryNotFoundError,
    DenialCategory,
    LiveMetadata,
    LogLine,
    MetadataUnavailableError,
    ProgressReported,
    RecordingEngine,
    RecordingFinished,
    RecordingOptions,
    RecordingResult,
    RecordingStatus,
    StallDetected,
    Toolchain,
)
from yt_rec.recording import engine as engine_module
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


def test_추가_인자는_안전_옵션보다_앞에_놓인다(tmp_path):
    """yt-dlp 는 뒤에 온 옵션이 이긴다. 사용자 인자가 안전 옵션 뒤로 가면 안 된다."""
    argv = base_engine(
        tmp_path, extra_ytdlp_args=("--cookies-from-browser", "chrome")
    ).build_download_argv(VIDEO_ID)

    assert argv[1:3] == ["--cookies-from-browser", "chrome"]
    for guarded in ("--fragment-retries", "--retries", "-o", "--live-from-start"):
        assert argv.index(guarded) > argv.index("--cookies-from-browser")


# -- 추가 인자로 안전 옵션을 덮어쓸 수 없다 ---------------------------------------

#: 실제로 안전 장치를 무력화하는 인자들. 하나라도 통과하면 사고로 이어진다.
CONFLICTING_EXTRA_ARGS = (
    "--fragment-retries", "infinite",   # 사라진 조각을 영원히 재요청 (29만 회/7시간)
    "--retries", "infinite",
    "--extractor-retries", "infinite",
    "-o", "%(title)s.%(ext)s",          # 파일명 결정 로직 우회
    "--no-live-from-start",             # 방송 시작 지점부터 받는 요건 파괴
    "--abort-on-unavailable-fragment",  # 죽은 조각 하나로 전체를 포기
    "--config-location", "my.conf",     # 설정 파일로 위 옵션들을 되살리기
)


def test_추가_인자로_재시도_상한을_무력화할_수_없다(tmp_path):
    argv = base_engine(
        tmp_path,
        fragment_retries=20,
        total_retries=10,
        extractor_retries=3,
        extra_ytdlp_args=CONFLICTING_EXTRA_ARGS,
    ).build_download_argv(VIDEO_ID)

    assert "infinite" not in argv
    assert argv.count("--fragment-retries") == 1
    assert argv[argv.index("--fragment-retries") + 1] == "20"
    assert argv[argv.index("--retries") + 1] == "10"
    assert argv[argv.index("--extractor-retries") + 1] == "3"
    # 상한에 걸린 조각은 건너뛰고 나머지를 마저 받아야 한다.
    assert "--abort-on-unavailable-fragment" not in argv
    assert "--skip-unavailable-fragments" in argv


def test_추가_인자로_파일명과_시작_지점을_바꿀_수_없다(tmp_path):
    argv = base_engine(
        tmp_path, extra_ytdlp_args=CONFLICTING_EXTRA_ARGS
    ).build_download_argv(VIDEO_ID)

    assert argv.count("-o") == 1
    assert argv[argv.index("-o") + 1] == "%(id)s.%(ext)s"
    assert "--no-live-from-start" not in argv
    assert "--live-from-start" in argv
    assert "--config-location" not in argv
    # 거부한 플래그의 값이 남으면 yt-dlp 가 그 값을 URL 로 오해한다.
    assert argv[-1].endswith(VIDEO_ID)
    for orphan in ("%(title)s.%(ext)s", "my.conf", "infinite"):
        assert orphan not in argv


def test_짧은_옵션과_등호_형태도_거부한다(tmp_path):
    """``-R99`` 나 ``--fragment-retries=infinite`` 로도 우회할 수 없어야 한다."""
    argv = base_engine(
        tmp_path,
        fragment_retries=20,
        total_retries=10,
        extra_ytdlp_args=("--fragment-retries=infinite", "-R99", "-o%(title)s.%(ext)s"),
    ).build_download_argv(VIDEO_ID)

    assert argv[argv.index("--fragment-retries") + 1] == "20"
    assert argv[argv.index("--retries") + 1] == "10"
    assert argv[argv.index("-o") + 1] == "%(id)s.%(ext)s"
    assert not any(t.startswith(("-R", "-o%", "--fragment-retries=")) for t in argv[1:-1])


def test_거부한_인자는_이유와_함께_알린다(tmp_path):
    """조용히 무시하면 사용자는 자기 설정이 반영된 줄로 안다."""
    engine = base_engine(tmp_path, extra_ytdlp_args=("--fragment-retries", "infinite"))
    notes: list[str] = []
    engine.add_listener(lambda e: notes.append(e.text) if isinstance(e, LogLine) else None)

    engine.build_download_argv(VIDEO_ID)

    assert len(notes) == 1
    assert "--fragment-retries infinite" in notes[0]
    assert "재시도 상한" in notes[0]


def test_안전과_무관한_추가_인자는_그대로_넘긴다(tmp_path):
    extra = ("--cookies", "쿠키.txt", "--proxy", "socks5://127.0.0.1:1080")
    engine = base_engine(tmp_path, extra_ytdlp_args=extra)
    notes: list[LogLine] = []
    engine.add_listener(lambda e: notes.append(e) if isinstance(e, LogLine) else None)

    argv = engine.build_download_argv(VIDEO_ID)

    assert argv[1 : 1 + len(extra)] == list(extra)
    assert notes == []


def test_엔진이_넘기는_옵션은_모두_거부_목록에_있다(tmp_path):
    """목록이 뒤처지면 새로 추가한 옵션을 사용자 인자가 덮어쓸 수 있다."""
    argv = base_engine(tmp_path).build_download_argv(VIDEO_ID)
    flags = [token for token in argv if token.startswith("-")]

    assert flags
    assert [f for f in flags if f not in engine_module._ENGINE_OWNED_ARGS] == []


# -- 도구를 못 찾았을 때 ---------------------------------------------------------


def _missing(tool: str, executable: str):
    def 없다(*args, **kwargs):
        raise BinaryNotFoundError(tool, executable)

    return 없다


@pytest.mark.parametrize(
    "tool,executable",
    [("ytdlp", "yt-dlp"), ("ffmpeg", "ffmpeg"), ("ffprobe", "ffprobe")],
)
def test_도구가_없으면_예외_대신_실패_결과를_돌려준다(
    tmp_path, monkeypatch, tool, executable
):
    """record() 는 예외를 던지지 않는 계약이다. 호출부가 예외를 맞으면 안 된다."""
    monkeypatch.setattr(engine_module, "resolve_toolchain", _missing(tool, executable))
    engine = RecordingEngine(RecordingOptions(output_dir=tmp_path / "out"))
    events = []
    engine.add_listener(events.append)

    result = engine.record(VIDEO_ID)  # 예외가 새면 여기서 테스트가 깨진다

    assert result.status is RecordingStatus.FAILED
    assert result.output_path is None
    assert executable in result.message, "어느 바이너리가 없는지 알려 준다"
    assert "PATH" in result.message, "어떻게 조치하는지도 알려 준다"
    assert any(isinstance(e, RecordingFinished) for e in events)


def test_도구가_없어도_복구할_중간_파일을_잃지_않는다(tmp_path, monkeypatch):
    """도구를 못 찾은 실행이 상태를 종료로 못 박으면 복구가 이 녹화를 영구히 건너뛴다."""
    options = RecordingOptions(output_dir=tmp_path / "녹화")
    work_dir = options.resolved_work_root() / VIDEO_ID
    work_dir.mkdir(parents=True)
    (work_dir / f"{VIDEO_ID}.f137.mp4").write_bytes(b"\x00" * 1024)
    (work_dir / STATE_FILENAME).write_text(
        json.dumps({"status": "recording", "started_at": time.time()}), encoding="utf-8"
    )

    monkeypatch.setattr(engine_module, "resolve_toolchain", _missing("ffmpeg", "ffmpeg"))
    result = RecordingEngine(options).record(VIDEO_ID)

    assert result.status is RecordingStatus.FAILED
    state = json.loads((work_dir / STATE_FILENAME).read_text(encoding="utf-8"))
    assert state["status"] == "recording", "복구 대상 표시를 지우지 않았다"


def test_준비_중_예상하지_못한_오류도_결과로_바꾼다(tmp_path, monkeypatch):
    def 터진다(*args, **kwargs):
        raise RuntimeError("디스크가 갑자기 사라졌다")

    monkeypatch.setattr("yt_rec.recording.engine.fetch_metadata", 터진다)
    engine = RecordingEngine(
        RecordingOptions(output_dir=tmp_path / "out"), toolchain=DUMMY_TOOLCHAIN
    )

    result = engine.record(VIDEO_ID)

    assert result.status is RecordingStatus.FAILED
    assert "디스크가 갑자기 사라졌다" in result.message


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


# -- 상태 저장과 중간 파일 정리의 순서 -------------------------------------------


@pytest.mark.integration
def test_상태를_저장한_뒤에_중간_파일을_치운다(
    tmp_path, toolchain, sample_streams, monkeypatch
):
    """정리 직전에 프로세스가 죽는 경우.

    거꾸로 하면 중간 파일은 사라졌는데 state.json 은 ``recording`` 으로 남아,
    복구가 파일을 못 찾고 몇 시간 녹화한 결과를 영구히 건너뛴다.
    """
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
    _prestore(engine, stored_metadata())

    def 죽는다(*args, **kwargs):
        raise KeyboardInterrupt("정리하려는 순간 프로세스가 죽었다")

    monkeypatch.setattr(engine_module, "_cleanup_intermediates", 죽는다)

    with pytest.raises(KeyboardInterrupt):
        engine.record(VIDEO_ID)

    # 결과 파일과 종료 상태가 남았다. 중간 파일은 아직 그대로다(지우기 전에 죽었으니).
    state = json.loads((work_dir / STATE_FILENAME).read_text(encoding="utf-8"))
    assert state["status"] in ("completed", "partial")
    assert Path(state["output_path"]).exists()
    assert (work_dir / f"{VIDEO_ID}.f137.mp4").exists()

    # 다시 켜도 이 녹화를 다시 마무리하지 않는다 — 같은 파일을 두 번 만들지 않는다.
    assert RecordingEngine(engine.options, toolchain=toolchain).recover_pending() == []
    assert len(list((tmp_path / "녹화").glob("*.mp4"))) == 1


@pytest.mark.integration
def test_정리에_실패해도_성공한_결과를_뒤집지_않는다(
    tmp_path, toolchain, sample_streams, monkeypatch
):
    """결과 파일과 상태는 이미 제자리에 있다. 남은 중간 파일은 디스크만 차지한다."""
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
    _prestore(engine, stored_metadata())

    def 잠겨_있다(*args, **kwargs):
        raise OSError("다른 프로세스가 파일을 붙잡고 있다")

    monkeypatch.setattr(engine_module, "_cleanup_intermediates", 잠겨_있다)

    result = engine.record(VIDEO_ID)

    assert result.succeeded
    assert result.output_path.exists()


def test_상태_파일은_임시_파일을_교체해_쓴다(tmp_path, monkeypatch):
    """쓰는 도중에 죽어도 이전 상태가 잘린 JSON 으로 덮이면 안 된다."""
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / STATE_FILENAME).write_text(
        json.dumps({"status": "recording", "started_at": 1.0}), encoding="utf-8"
    )
    result = RecordingResult(
        video_id=VIDEO_ID,
        status=RecordingStatus.COMPLETED,
        metadata=stored_metadata(),
        work_dir=work_dir,
    )

    def 교체가_실패한다(*args, **kwargs):
        raise OSError("교체 실패")

    monkeypatch.setattr(engine_module.os, "replace", 교체가_실패한다)
    with pytest.raises(OSError):
        RecordingEngine._save_state(work_dir, result)

    kept = json.loads((work_dir / STATE_FILENAME).read_text(encoding="utf-8"))
    assert kept["status"] == "recording", "이전 상태가 온전히 남았다"


def test_상태를_저장하면_임시_파일이_남지_않는다(tmp_path):
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    RecordingEngine._save_state(
        work_dir,
        RecordingResult(
            video_id=VIDEO_ID,
            status=RecordingStatus.COMPLETED,
            metadata=stored_metadata(),
            work_dir=work_dir,
        ),
    )

    assert sorted(p.name for p in work_dir.iterdir()) == [STATE_FILENAME]


@pytest.mark.integration
@pytest.mark.parametrize(
    "written",
    [
        '{"status": "compl',  # 쓰는 도중에 죽어 JSON 이 잘렸다
        "123",  # JSON 이긴 하지만 객체가 아니다
        "",  # 만들기만 하고 아무것도 못 썼다
    ],
)
def test_부분_기록된_상태_파일은_복구를_막지_않는다(
    tmp_path, toolchain, sample_streams, written
):
    """상태를 못 읽으면 끝나지 않은 녹화로 봐야 한다. 건너뛰면 결과를 잃는다."""
    video, audio = sample_streams
    options = RecordingOptions(output_dir=tmp_path / "녹화")
    work_dir = options.resolved_work_root() / VIDEO_ID
    work_dir.mkdir(parents=True)
    stored_metadata().save(work_dir)
    (work_dir / f"{VIDEO_ID}.f137.mp4").write_bytes(video.read_bytes())
    (work_dir / f"{VIDEO_ID}.f140.m4a").write_bytes(audio.read_bytes())
    (work_dir / STATE_FILENAME).write_text(written, encoding="utf-8")

    results = RecordingEngine(options, toolchain=toolchain, tz=KST).recover_pending()

    assert len(results) == 1
    assert results[0].succeeded
    assert json.loads((work_dir / STATE_FILENAME).read_text(encoding="utf-8"))[
        "status"
    ] in ("completed", "partial")


# -- 도우미 ---------------------------------------------------------------------


def _prestore(engine: RecordingEngine, metadata: LiveMetadata) -> None:
    """녹화 시작 전에 메타데이터를 보관해 둔다(조회를 건너뛰게 한다)."""
    work_dir = engine.work_dir_for(metadata.video_id)
    work_dir.mkdir(parents=True, exist_ok=True)
    metadata.save(work_dir)


def _premerge(sample_streams, dest: Path, toolchain: Toolchain) -> Path:
    from yt_rec.recording.merge import merge_streams

    return merge_streams(list(sample_streams), dest, toolchain)


# -- E. record() 직전에 누른 중단이 사라지지 않는다 ---------------------------------


@pytest.mark.integration
def test_record_직전에_누른_중단이_사라지지_않는다(tmp_path, toolchain, sample_streams):
    """워커 스레드가 record() 를 부르기 직전에 사용자가 중단을 누른 경우.

    플래그가 지워지면 녹화가 방송 끝까지(수 시간) 계속된다. 감시 루프(#3)가 record()
    를 반복 호출하는 구조에서는 호출 사이에 눌린 중단이 매번 없어진다.
    """
    video, audio = sample_streams
    engine = make_engine(
        tmp_path,
        toolchain,
        {
            "files": {
                f"{VIDEO_ID}.f137.mp4": str(video),
                f"{VIDEO_ID}.f140.m4a": str(audio),
            },
            "sleep": 120,  # 중단이 무시되면 여기서 오래 매달린다
        },
    )
    _prestore(engine, stored_metadata())

    engine.request_stop()
    assert engine.stop_requested() is True

    started = time.monotonic()
    result = engine.record(VIDEO_ID)
    elapsed = time.monotonic() - started

    assert engine.stop_requested() is True, "정지 요청이 지워졌다"
    assert elapsed < 20, f"중단을 무시하고 계속 받았다 ({elapsed:.1f}초)"
    assert "중단" in result.message


@pytest.mark.integration
def test_중단_요청_직후에는_yt_dlp_를_띄우지도_않는다(tmp_path, toolchain):
    engine = make_engine(tmp_path, toolchain, {"argv_out": str(tmp_path / "argv.json")})
    _prestore(engine, stored_metadata())

    engine.request_stop()
    engine.record(VIDEO_ID)

    assert not (tmp_path / "argv.json").exists(), "다운로더를 띄웠다"


def test_clear_stop_을_불러야_다시_녹화한다(tmp_path):
    engine = base_engine(tmp_path)
    engine.request_stop()
    assert engine.stop_requested() is True

    engine.clear_stop()

    assert engine.stop_requested() is False


# -- A. 낡은 중간 파일이 결과에 섞이지 않는다 -------------------------------------


@pytest.mark.integration
def test_지난_시도의_낡은_중간_파일을_결과로_내보내지_않는다(
    tmp_path, toolchain, make_clip
):
    """지난 시도가 f137 을 남긴 뒤 화질 상한을 낮춰 f299 로 다시 받은 상황.

    낡은 파일이 트랙 0 이 되면 프레임 검사가 그 트랙을 보고 통과한다(실측 재현).
    """
    stale = make_clip("stale.mp4", seconds=3)
    fresh = make_clip("fresh.mp4", seconds=5)
    audio = make_clip("audio.m4a", seconds=5, kind="audio")
    engine = make_engine(
        tmp_path,
        toolchain,
        {
            "files": {
                f"{VIDEO_ID}.f299.mp4": str(fresh),
                f"{VIDEO_ID}.f140.m4a": str(audio),
            },
            "lines": [progress(1000, 1, "299"), progress(500, 1, "140")],
        },
    )
    work_dir = engine.work_dir_for(VIDEO_ID)
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / f"{VIDEO_ID}.f137.mp4").write_bytes(stale.read_bytes())
    _prestore(engine, stored_metadata())

    result = engine.record(VIDEO_ID)

    assert result.succeeded, result.message
    verification = result.verification
    assert verification.video_stream_count == 1
    assert verification.audio_stream_count == 1
    assert verification.video_frames == pytest.approx(150, abs=2), (
        "이번에 받은 5초(150프레임)여야 한다. 90프레임이면 낡은 3초를 본 것이다"
    )
    assert verification.duration == pytest.approx(5.0, abs=0.3)


@pytest.mark.integration
def test_yt_dlp_가_이미_병합한_결과는_낡은_중간_파일보다_앞선다(
    tmp_path, toolchain, make_clip
):
    """이번 시도는 병합까지 끝났고 중간 파일은 지워졌다. 지난 시도의 f137 만 남는다.

    남은 f* 를 '마무리가 안 됐다'로 보면 낡은 3초를 다시 묶고, 이번에 받은 5초
    결과물을 덮거나 지운다.
    """
    from yt_rec.recording.merge import merge_streams

    stale = make_clip("stale.mp4", seconds=3)
    fresh = make_clip("fresh.mp4", seconds=5)
    audio = make_clip("audio.m4a", seconds=5, kind="audio")
    completed = tmp_path / "session.mp4"
    merge_streams([fresh, audio], completed, toolchain)
    engine = make_engine(
        tmp_path,
        toolchain,
        {
            "files": {f"{VIDEO_ID}.mp4": str(completed)},
            "lines": [progress(1000, 1, "299"), progress(500, 1, "140")],
        },
    )
    work_dir = engine.work_dir_for(VIDEO_ID)
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / f"{VIDEO_ID}.f137.mp4").write_bytes(stale.read_bytes())
    _prestore(engine, stored_metadata())

    result = engine.record(VIDEO_ID)

    assert result.succeeded, result.message
    verification = result.verification
    assert verification.video_stream_count == 1
    assert verification.video_frames == pytest.approx(150, abs=2), (
        "이번에 받은 5초여야 한다. 90프레임이면 낡은 3초를 다시 묶은 것이다"
    )
    assert verification.duration == pytest.approx(5.0, abs=0.3)


@pytest.mark.integration
def test_복구는_yt_dlp_결과물을_낡은_중간_파일보다_앞세운다(
    tmp_path, toolchain, make_clip
):
    """강제 종료 직후. 포맷 id 기록이 없어도 {id}.mp4 가 더 최근이면 그것을 쓴다."""
    from yt_rec.recording.merge import merge_streams

    stale = make_clip("stale.mp4", seconds=3)
    fresh = make_clip("fresh.mp4", seconds=5)
    audio = make_clip("audio.m4a", seconds=5, kind="audio")
    options = RecordingOptions(output_dir=tmp_path / "녹화")
    work_dir = options.resolved_work_root() / VIDEO_ID
    work_dir.mkdir(parents=True)
    stored_metadata().save(work_dir)
    (work_dir / f"{VIDEO_ID}.f137.mp4").write_bytes(stale.read_bytes())
    merge_streams(
        [fresh, audio], work_dir / f"{VIDEO_ID}.mp4", toolchain
    )
    (work_dir / STATE_FILENAME).write_text(
        json.dumps({"status": "recording", "started_at": time.time()}), encoding="utf-8"
    )

    results = RecordingEngine(options, toolchain=toolchain, tz=KST).recover_pending()

    assert len(results) == 1
    assert results[0].succeeded, results[0].message
    assert results[0].verification.video_frames == pytest.approx(150, abs=2)
    assert results[0].verification.duration == pytest.approx(5.0, abs=0.3)


# -- C. 누락이 확인되면 중간 파일을 지우지 않는다 ----------------------------------


@pytest.mark.integration
def test_누락이_확인되면_중간_파일을_지우지_않는다(tmp_path, toolchain, sample_streams):
    """#14 수용 기준: 중간 파일은 병합 검증에 **성공한** 뒤에만 정리한다.

    playable 만 보고 지우면 "음성 스트림이 없다"가 확인된 결과에서도 원본이 사라져
    다시 병합할 기회가 없어진다.
    """
    video, _ = sample_streams
    engine = make_engine(
        tmp_path, toolchain, {"files": {f"{VIDEO_ID}.f137.mp4": str(video)}}
    )
    work_dir = engine.work_dir_for(VIDEO_ID)
    _prestore(engine, stored_metadata())

    result = engine.record(VIDEO_ID)

    assert result.status is RecordingStatus.PARTIAL
    assert result.verification.playable is True
    assert result.verification.complete is False
    assert any("음성" in issue for issue in result.verification.issues)
    assert (work_dir / f"{VIDEO_ID}.f137.mp4").exists(), "누락이 확인됐는데 원본을 지웠다"


# -- B. 마무리 단계에 시한이 있다 --------------------------------------------------


@pytest.mark.integration
def test_병합이_물리면_정지로_기록하고_알린다(
    tmp_path, toolchain, sample_streams, hanging_tool
):
    """마무리 단계에는 정지 감지기가 돌지 않는다. 여기서 물리면 아무도 못 알아챈다."""
    video, audio = sample_streams
    hanging = Toolchain(
        ytdlp=toolchain.ytdlp,
        ffprobe=toolchain.ffprobe,
        ffmpeg=hanging_tool("ffmpeg"),
    )
    engine = make_engine(
        tmp_path,
        hanging,
        {
            "files": {
                f"{VIDEO_ID}.f137.mp4": str(video),
                f"{VIDEO_ID}.f140.m4a": str(audio),
            }
        },
        merge_timeout_seconds=1.5,
    )
    work_dir = engine.work_dir_for(VIDEO_ID)
    _prestore(engine, stored_metadata())
    events = []
    engine.add_listener(events.append)

    started = time.monotonic()
    result = engine.record(VIDEO_ID)
    elapsed = time.monotonic() - started

    assert elapsed < 30, f"시한 없이 매달렸다 ({elapsed:.1f}초)"
    assert result.status is RecordingStatus.FAILED
    assert result.stalled is True, "정지로 기록해야 한다"
    assert "시간 초과" in result.message
    assert any(isinstance(e, StallDetected) for e in events), "표시도 해야 한다"
    assert (work_dir / f"{VIDEO_ID}.f137.mp4").exists(), "중간 파일은 남긴다"


@pytest.mark.integration
def test_검증이_물려도_중간_파일을_지우지_않는다(
    tmp_path, toolchain, sample_streams, hanging_tool
):
    video, audio = sample_streams
    # 병합은 진짜 ffmpeg 로 하고, 검증에 쓰는 ffprobe 만 물리게 한다.
    # (select_merge_sources 도 ffprobe 를 쓰므로 병합 자체가 먼저 막힌다 — 그래도
    #  결과는 실패로 보고되고 중간 파일은 남아야 한다.)
    hanging = Toolchain(
        ytdlp=toolchain.ytdlp,
        ffmpeg=toolchain.ffmpeg,
        ffprobe=hanging_tool("ffprobe"),
    )
    engine = make_engine(
        tmp_path,
        hanging,
        {
            "files": {
                f"{VIDEO_ID}.f137.mp4": str(video),
                f"{VIDEO_ID}.f140.m4a": str(audio),
            }
        },
        verify_timeout_seconds=1.5,
        merge_timeout_seconds=1.5,
    )
    work_dir = engine.work_dir_for(VIDEO_ID)
    _prestore(engine, stored_metadata())

    started = time.monotonic()
    result = engine.record(VIDEO_ID)

    assert time.monotonic() - started < 30
    assert result.succeeded is False
    assert (work_dir / f"{VIDEO_ID}.f137.mp4").exists()


def test_마무리_시한_기본값은_유한하다(tmp_path):
    options = RecordingOptions(output_dir=tmp_path / "out")

    assert options.merge_timeout_seconds > 0
    assert options.verify_timeout_seconds > 0
    with pytest.raises(ValueError):
        RecordingOptions(output_dir=tmp_path / "out", merge_timeout_seconds=0)
    with pytest.raises(ValueError):
        RecordingOptions(output_dir=tmp_path / "out", verify_timeout_seconds=-1)


# -- 저심각도: 복구 경로 ----------------------------------------------------------


@pytest.mark.integration
def test_복구본만_남은_디렉터리에서_유일한_결과물을_지우지_않는다(
    tmp_path, toolchain, sample_streams
):
    """중간 파일 없이 복구본만 남은 work 디렉터리.

    후보 수집 **전에** 복구본을 지우면 유일한 결과물을 없애 놓고 FAILED 로 처리한다.
    """
    options = RecordingOptions(output_dir=tmp_path / "녹화")
    work_dir = options.resolved_work_root() / VIDEO_ID
    work_dir.mkdir(parents=True)
    stored_metadata().save(work_dir)
    _premerge(sample_streams, work_dir / f"{VIDEO_ID}.recovered.mp4", toolchain)
    (work_dir / STATE_FILENAME).write_text(
        json.dumps({"status": "recording", "started_at": time.time()}), encoding="utf-8"
    )

    results = RecordingEngine(options, toolchain=toolchain, tz=KST).recover_pending()

    assert len(results) == 1
    assert results[0].succeeded, results[0].message
    assert results[0].output_path.exists()


@pytest.mark.integration
def test_녹화_중인_디렉터리는_복구가_건드리지_않는다(tmp_path, toolchain, sample_streams):
    """앱을 두 개 띄우거나 녹화 중에 복구를 부른 경우."""
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
    # 살아 있는 소유자 표시. 지금 이 프로세스가 녹화 중인 것과 같은 상태다.
    (work_dir / engine_module.LOCK_FILENAME).write_text(
        json.dumps({"pid": os.getpid(), "at": time.time()}), encoding="utf-8"
    )

    results = RecordingEngine(options, toolchain=toolchain, tz=KST).recover_pending()

    assert results == []
    assert (work_dir / f"{VIDEO_ID}.f137.mp4").exists(), "살아 있는 중간 파일을 지웠다"
    assert json.loads((work_dir / STATE_FILENAME).read_text(encoding="utf-8"))[
        "status"
    ] == "recording", "상태를 덮어썼다"


@pytest.mark.integration
def test_죽은_소유자_표시는_복구를_막지_않는다(tmp_path, toolchain, sample_streams):
    """강제 종료된 뒤 남은 표시 때문에 복구가 영구히 막히면 안 된다."""
    video, audio = sample_streams
    options = RecordingOptions(output_dir=tmp_path / "녹화")
    work_dir = options.resolved_work_root() / VIDEO_ID
    work_dir.mkdir(parents=True)
    stored_metadata().save(work_dir)
    (work_dir / f"{VIDEO_ID}.f137.mp4").write_bytes(video.read_bytes())
    (work_dir / f"{VIDEO_ID}.f140.m4a").write_bytes(audio.read_bytes())
    (work_dir / engine_module.LOCK_FILENAME).write_text(
        json.dumps({"pid": 0, "at": 0.0}), encoding="utf-8"
    )

    results = RecordingEngine(options, toolchain=toolchain, tz=KST).recover_pending()

    assert len(results) == 1
    assert results[0].succeeded


@pytest.mark.integration
def test_녹화_중에는_소유자_표시가_남고_끝나면_사라진다(
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
            "lines": [progress(1000, 1, "137")],
        },
    )
    work_dir = engine.work_dir_for(VIDEO_ID)
    _prestore(engine, stored_metadata())
    seen: list[bool] = []
    engine.add_listener(
        lambda e: seen.append((work_dir / engine_module.LOCK_FILENAME).exists())
        if isinstance(e, ProgressReported)
        else None
    )

    engine.record(VIDEO_ID)

    assert any(seen), "녹화 중에 소유자 표시가 없었다"
    assert not (work_dir / engine_module.LOCK_FILENAME).exists(), "끝났는데 표시가 남았다"


def test_도구가_없으면_복구를_미룬다(tmp_path, monkeypatch):
    """ffmpeg 를 다시 설치하면 다시 복구될 수 있어야 한다.

    한 건이라도 종료 상태로 못 박으면 그 녹화는 영구히 건너뛰어진다.
    """
    options = RecordingOptions(output_dir=tmp_path / "녹화")
    work_dir = options.resolved_work_root() / VIDEO_ID
    work_dir.mkdir(parents=True)
    (work_dir / f"{VIDEO_ID}.f137.mp4").write_bytes(b"\x00" * 4096)
    (work_dir / STATE_FILENAME).write_text(
        json.dumps({"status": "recording", "started_at": time.time()}), encoding="utf-8"
    )
    monkeypatch.setattr(engine_module, "resolve_toolchain", _missing("ffmpeg", "ffmpeg"))

    results = RecordingEngine(options).recover_pending()

    assert results == []
    assert json.loads((work_dir / STATE_FILENAME).read_text(encoding="utf-8"))[
        "status"
    ] == "recording", "종료 상태를 못 박으면 다시 복구되지 않는다"
    assert (work_dir / f"{VIDEO_ID}.f137.mp4").exists()


# -- 저심각도: 자식 프로세스 트리 -------------------------------------------------


@pytest.mark.integration
def test_자식을_끊을_때_손자까지_닿게_띄운다(tmp_path, toolchain, sample_streams):
    """POSIX 에서 프로세스 그룹을 만들지 않으면 ffmpeg 손자가 살아남는다."""
    import subprocess as sp

    video, audio = sample_streams
    calls: list[dict] = []
    real_popen = sp.Popen

    class SpyPopen(real_popen):  # type: ignore[misc]
        def __init__(self, *args, **kwargs):
            calls.append(dict(kwargs))
            super().__init__(*args, **kwargs)

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
    _prestore(engine, stored_metadata())
    engine_module.subprocess.Popen = SpyPopen  # type: ignore[assignment]
    try:
        engine.record(VIDEO_ID)
    finally:
        engine_module.subprocess.Popen = real_popen  # type: ignore[assignment]

    download = calls[0]
    if os.name == "nt":
        # taskkill /F /T 가 트리째 끊는다. 콘솔 창은 띄우지 않는다.
        assert download["creationflags"] & sp.CREATE_NO_WINDOW
    else:
        assert download["start_new_session"] is True, "프로세스 그룹이 없다"


# -- 저심각도: CLI -------------------------------------------------------------


def test_CLI_는_도구가_없으면_트레이스백을_내지_않는다(tmp_path, monkeypatch, capsys):
    from yt_rec.recording import __main__ as cli

    monkeypatch.setattr(cli, "resolve_toolchain", _missing("ffmpeg", "ffmpeg"))

    code = cli.main(["verify", str(tmp_path / "없음.mp4")])

    assert code != 0
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "ffmpeg" in captured.err
    assert "PATH" in captured.err
