"""저장·스트리밍 코덱 옵션. 기본은 받은 파일, AV1 은 더 작거나 같을 때만."""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from yt_rec.recording.binaries import Toolchain
from yt_rec.recording.transcode import (
    SourceFacts,
    apply_output_codec,
    build_av1_argv,
    build_remux_argv,
    commit_encoded,
    encode_timeout,
    rate_args,
)

FORBIDDEN = ("-b:v", "-maxrate", "-bufsize", "-crf")


def toolchain() -> Toolchain:
    return Toolchain(Path("yt-dlp"), Path("ffmpeg"), Path("ffprobe"))


def facts(**kwargs) -> SourceFacts:
    base = dict(
        width=1920,
        height=1080,
        r_frame_rate="30000/1001",
        avg_frame_rate="30000/1001",
        codec_name="h264",
        duration=120.0,
        video_count=1,
    )
    base.update(kwargs)
    return SourceFacts(**base)


def test_일정_프레임은_원본_유리수로_잠근다():
    argv = rate_args("30000/1001", "30000/1001")
    assert argv == ["-r:v", "30000/1001", "-fps_mode", "cfr"]


def test_어긋난_프레임은_타임스탬프를_그대로_둔다():
    argv = rate_args("19001/317", "30/1")
    assert argv == ["-fps_mode", "passthrough"]
    assert "-r:v" not in argv


def test_av1_명령은_화질만_고정하고_비트레이트_상한이_없다():
    argv = build_av1_argv("ffmpeg", Path("in.mp4"), Path("out.mp4"), facts())
    assert argv[argv.index("-c:v") + 1] == "av1_qsv"
    assert "slow" in argv
    assert argv[argv.index("-global_quality") + 1] == "21"
    assert "-s:v" in argv and "1920x1080" in argv
    assert "-c:a" in argv and argv[argv.index("-c:a") + 1] == "copy"
    assert "+faststart" in argv
    assert "-c" not in argv
    for flag in FORBIDDEN:
        assert flag not in argv


def test_홀수_해상도는_1픽셀만_맞춘다():
    argv = build_av1_argv(
        "ffmpeg", Path("in.mp4"), Path("out.mp4"), facts(width=1919, height=1080)
    )
    assert "pad=ceil(iw/2)*2:ceil(ih/2)*2" in argv
    assert "-s:v" not in argv


def test_이미_av1이면_다시_인코딩하지_않는다():
    argv = build_remux_argv("ffmpeg", Path("in.mp4"), Path("out.mp4"))
    assert "av1_qsv" not in argv
    assert "-c" in argv and argv[argv.index("-c") + 1] == "copy"
    assert "+faststart" in argv
    for flag in FORBIDDEN:
        assert flag not in argv


def test_기본_코덱은_ffmpeg를_부르지_않는다(tmp_path, monkeypatch):
    src = tmp_path / "in.mp4"
    src.write_bytes(b"abc")

    def fail_run(*_args, **_kwargs):
        raise AssertionError("copy 는 ffmpeg 를 부르면 안 된다")

    monkeypatch.setattr("yt_rec.recording.transcode._run", fail_run)
    outcome = apply_output_codec(src, toolchain(), "copy")
    assert outcome.path == src
    assert outcome.replaces_source is False
    assert outcome.note == ""


def test_더_작으면_av1을_검증_후보로_남긴다(tmp_path, monkeypatch):
    src = tmp_path / "in.mp4"
    src.write_bytes(b"a" * 1000)
    monkeypatch.setattr("yt_rec.recording.transcode.probe_source", lambda *a, **k: facts())

    def fake_run(argv, timeout=None):
        Path(argv[-1]).write_bytes(b"b" * 400)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr("yt_rec.recording.transcode._run", fake_run)
    outcome = apply_output_codec(src, toolchain(), "av1")
    assert outcome.replaces_source is True
    assert outcome.path.name == "in.av1.mp4"
    assert outcome.path.read_bytes() == b"b" * 400
    assert src.read_bytes() == b"a" * 1000
    assert "0.400배" in outcome.note


def test_1배까지는_받고_넘으면_받은_파일을_남긴다(tmp_path, monkeypatch):
    src = tmp_path / "in.mp4"
    src.write_bytes(b"a" * 1000)
    monkeypatch.setattr("yt_rec.recording.transcode.probe_source", lambda *a, **k: facts())

    def fake_run(argv, timeout=None):
        Path(argv[-1]).write_bytes(b"b" * 1000)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr("yt_rec.recording.transcode._run", fake_run)
    same = apply_output_codec(src, toolchain(), "av1")
    assert same.replaces_source is True
    assert "1.000배" in same.note

    def bigger(argv, timeout=None):
        Path(argv[-1]).write_bytes(b"b" * 1001)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr("yt_rec.recording.transcode._run", bigger)
    outcome = apply_output_codec(src, toolchain(), "av1")
    assert outcome.path == src
    assert outcome.replaces_source is False
    assert "더 크다" in outcome.note
    assert not (tmp_path / "in.av1.mp4").exists()


def test_인코딩_실패와_길이_불일치는_받은_파일을_남긴다(tmp_path, monkeypatch):
    src = tmp_path / "in.mp4"
    src.write_bytes(b"a" * 1000)
    seen = {"n": 0}

    def fake_probe(*_args, **_kwargs):
        seen["n"] += 1
        if seen["n"] == 1:
            return facts()
        return replace(facts(), duration=10.0)

    monkeypatch.setattr("yt_rec.recording.transcode.probe_source", fake_probe)

    def fake_run(argv, timeout=None):
        Path(argv[-1]).write_bytes(b"b" * 100)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr("yt_rec.recording.transcode._run", fake_run)
    mismatch = apply_output_codec(src, toolchain(), "av1")
    assert mismatch.path == src
    assert "길이" in mismatch.note
    assert not (tmp_path / "in.av1.mp4").exists()

    def broken(argv, timeout=None):
        return subprocess.CompletedProcess(argv, 1, b"", "qsv init failed".encode())

    monkeypatch.setattr("yt_rec.recording.transcode._run", broken)
    failed = apply_output_codec(src, toolchain(), "av1")
    assert failed.path == src
    assert "실패" in failed.note
    assert src.read_bytes() == b"a" * 1000


def test_시간_초과도_받은_파일을_남긴다(tmp_path, monkeypatch):
    src = tmp_path / "in.mp4"
    src.write_bytes(b"a" * 50)
    monkeypatch.setattr("yt_rec.recording.transcode.probe_source", lambda *a, **k: facts())

    def expire(argv, timeout=None):
        Path(argv[-1]).write_bytes(b"partial")
        raise subprocess.TimeoutExpired(argv, timeout or 1)

    monkeypatch.setattr("yt_rec.recording.transcode._run", expire)
    outcome = apply_output_codec(src, toolchain(), "av1")
    assert outcome.path == src
    assert "시간" in outcome.note
    assert not (tmp_path / "in.av1.mp4").exists()


def test_이미_av1이면_remux_명령만_쓴다(tmp_path, monkeypatch):
    src = tmp_path / "in.mp4"
    src.write_bytes(b"a" * 1000)
    monkeypatch.setattr(
        "yt_rec.recording.transcode.probe_source",
        lambda *a, **k: facts(codec_name="av1"),
    )
    seen: list[list[str]] = []

    def fake_run(argv, timeout=None):
        seen.append(list(argv))
        Path(argv[-1]).write_bytes(b"b" * 1000)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr("yt_rec.recording.transcode._run", fake_run)
    outcome = apply_output_codec(src, toolchain(), "av1")
    assert outcome.replaces_source is True
    assert "av1_qsv" not in seen[0]
    assert "faststart" in outcome.note


def test_검증_전에는_받은_파일을_지우지_않는다(tmp_path):
    src = tmp_path / "in.mp4"
    encoded = tmp_path / "in.av1.mp4"
    src.write_bytes(b"original")
    encoded.write_bytes(b"encoded")
    from yt_rec.recording.transcode import TranscodeOutcome

    kept = commit_encoded(
        src, TranscodeOutcome(encoded, True, ""), accepted=False
    )
    assert kept == src
    assert src.read_bytes() == b"original"
    assert not encoded.exists()

    encoded.write_bytes(b"encoded")
    adopted = commit_encoded(
        src, TranscodeOutcome(encoded, True, ""), accepted=True
    )
    assert adopted == src
    assert src.read_bytes() == b"encoded"


def test_mkv_결과는_mp4_이름으로_올린다(tmp_path):
    src = tmp_path / "in.mkv"
    encoded = tmp_path / "in.av1.mp4"
    src.write_bytes(b"original")
    encoded.write_bytes(b"encoded")
    from yt_rec.recording.transcode import TranscodeOutcome

    adopted = commit_encoded(src, TranscodeOutcome(encoded, True, ""), accepted=True)
    assert adopted == tmp_path / "in.mp4"
    assert adopted.read_bytes() == b"encoded"
    assert not src.exists()


@pytest.mark.parametrize(
    ("duration", "low", "high"),
    [(None, 8 * 3600, 8 * 3600), (10, 30 * 60, 30 * 60), (3600, 4 * 3600, 4 * 3600)],
)
def test_인코딩_시한은_유한하고_긴_영상에_맞춘다(duration, low, high):
    timeout = encode_timeout(duration)
    assert low <= timeout <= high
    assert timeout <= 8 * 3600
