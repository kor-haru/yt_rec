"""병합 결과를 저장·스트리밍용으로 바꿀지 정한다.

``copy`` 는 받은 파일을 그대로 둔다. ``av1`` 은 이 기기의 확정 스펙으로 다시
만든다. 품질값 옆에 비트레이트 상한을 두면 ICQ 가 VBR 로 바뀌므로 두지 않는다.
결과가 더 크거나, 길이가 어긋나거나, 인코딩이 실패하면 받은 파일을 남긴다.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .binaries import Toolchain
from .merge import _decode, _float_or_none, _rational, _run

#: 받은 파일 대비 이 배율 이하만 영상 재인코딩 결과로 채택한다.
AV1_MAX_RATIO = 1.0
#: faststart remux 는 같은 비트에 moov 위치만 바꾸므로 약간의 증가는 허용한다.
REMUX_MAX_RATIO = 1.02
DURATION_TOLERANCE = 2.0
FRAME_RATE_SLACK = 0.05
AV1_PRESET = "slow"
AV1_QUALITY = 21
MAX_ENCODE_SECONDS = 8 * 3600
MIN_ENCODE_SECONDS = 30 * 60

_AV1_CODECS = frozenset({"av1", "av01"})
_FORBIDDEN_RATE_FLAGS = ("-b:v", "-maxrate", "-bufsize", "-crf")


@dataclass(frozen=True)
class SourceFacts:
    width: int | None
    height: int | None
    r_frame_rate: str
    avg_frame_rate: str
    codec_name: str
    duration: float | None
    video_count: int


@dataclass(frozen=True)
class TranscodeOutcome:
    """검증할 파일. ``replaces_source`` 는 검증을 통과한 뒤에만 받은 파일을 밀어낸다."""

    path: Path
    replaces_source: bool
    note: str


def encode_timeout(duration: float | None) -> float:
    """긴 라이브도 1배속 근처면 끝나게 두되, 물리면 상한에서 끊는다."""
    if duration is None or duration <= 0:
        return float(MAX_ENCODE_SECONDS)
    return float(min(MAX_ENCODE_SECONDS, max(MIN_ENCODE_SECONDS, duration * 4)))


def rate_args(r_frame_rate: str, avg_frame_rate: str) -> list[str]:
    """일정 프레임이면 그 유리수로 CFR 을 잠그고, 아니면 원본 타임스탬프를 둔다."""
    if _rates_close(r_frame_rate, avg_frame_rate):
        return ["-r:v", r_frame_rate, "-fps_mode", "cfr"]
    return ["-fps_mode", "passthrough"]


def build_av1_argv(
    ffmpeg: str,
    source: Path,
    dest: Path,
    facts: SourceFacts,
) -> list[str]:
    argv = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-dn",
        "-sn",
        "-c:v",
        "av1_qsv",
        "-preset",
        AV1_PRESET,
        "-global_quality",
        str(AV1_QUALITY),
    ]
    if facts.width and facts.height and (facts.width % 2 or facts.height % 2):
        argv += ["-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2"]
    elif facts.width and facts.height:
        argv += ["-s:v", f"{facts.width}x{facts.height}"]
    argv += rate_args(facts.r_frame_rate, facts.avg_frame_rate)
    argv += ["-c:a", "copy", "-movflags", "+faststart", str(dest)]
    return argv


def build_remux_argv(ffmpeg: str, source: Path, dest: Path) -> list[str]:
    """이미 AV1 인 영상은 다시 인코딩하지 않고 faststart 만 맞춘다."""
    return [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-dn",
        "-sn",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(dest),
    ]


def apply_output_codec(
    source: Path,
    toolchain: Toolchain,
    output_codec: str,
) -> TranscodeOutcome:
    """``av1`` 이 아니면 ``source`` 를 그대로 돌려준다. 예외로 녹화를 실패시키지 않는다."""
    source = Path(source)
    if output_codec != "av1":
        return TranscodeOutcome(source, False, "")
    try:
        return _apply_av1(source, toolchain)
    except Exception as exc:  # noqa: BLE001 - 인코딩 실패는 받은 파일을 남기는 결과다
        _encoded_path(source).unlink(missing_ok=True)
        return _keep(source, f"AV1 인코딩에 실패했다 ({exc})")


def commit_encoded(source: Path, outcome: TranscodeOutcome, *, accepted: bool) -> Path:
    """검증을 통과한 AV1 만 받은 파일 자리에 올린다.

    컨테이너가 mkv 여도 스트리밍 결과는 mp4 다. mp4 바이트를 mkv 이름에 덮지 않는다.
    """
    source = Path(source)
    if not outcome.replaces_source or outcome.path == source:
        return source
    if not accepted:
        outcome.path.unlink(missing_ok=True)
        return source
    dest = source if source.suffix.lower() == ".mp4" else source.with_suffix(".mp4")
    try:
        if dest.exists() and dest != outcome.path and dest != source:
            dest.unlink()
        os.replace(outcome.path, dest)
    except OSError:
        outcome.path.unlink(missing_ok=True)
        raise
    if dest != source:
        source.unlink(missing_ok=True)
    return dest


def probe_source(path: Path, toolchain: Toolchain, *, timeout: float) -> SourceFacts | None:
    try:
        proc = _run(
            [
                str(toolchain.ffprobe),
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=codec_type,codec_name,width,height,r_frame_rate,avg_frame_rate",
                "-of",
                "json",
                str(path),
            ],
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(_decode(proc.stdout) or "{}")
    except ValueError:
        return None
    videos = [s for s in data.get("streams") or [] if s.get("codec_type") == "video"]
    duration = _float_or_none((data.get("format") or {}).get("duration"))
    if not videos:
        return SourceFacts(None, None, "", "", "", duration, 0)
    video = videos[0]
    return SourceFacts(
        width=_positive_int(video.get("width")),
        height=_positive_int(video.get("height")),
        r_frame_rate=str(video.get("r_frame_rate") or ""),
        avg_frame_rate=str(video.get("avg_frame_rate") or ""),
        codec_name=str(video.get("codec_name") or ""),
        duration=duration,
        video_count=len(videos),
    )


def _apply_av1(source: Path, toolchain: Toolchain) -> TranscodeOutcome:
    facts = probe_source(source, toolchain, timeout=60)
    if facts is None:
        return _keep(source, "인코딩 전에 영상을 읽지 못했다")
    if facts.video_count != 1:
        return _keep(source, "영상 트랙이 하나가 아니다")
    dest = _encoded_path(source)
    ffmpeg = str(toolchain.ffmpeg)
    if facts.codec_name.casefold() in _AV1_CODECS:
        argv = build_remux_argv(ffmpeg, source, dest)
        max_ratio = REMUX_MAX_RATIO
        ok_note = "이미 AV1이라 다시 인코딩하지 않고 faststart만 맞췄다"
        fail_note = "faststart 맞추기에 실패했다"
    else:
        argv = build_av1_argv(ffmpeg, source, dest, facts)
        max_ratio = AV1_MAX_RATIO
        ok_note = ""
        fail_note = "AV1 인코딩에 실패했다"
    if any(flag in argv for flag in _FORBIDDEN_RATE_FLAGS):
        return _keep(source, "비트레이트 상한이 섞여 인코딩을 건너뛰었다")
    timeout = encode_timeout(facts.duration)
    ran, detail = _run_ffmpeg(argv, dest, timeout)
    if not ran:
        reason = "AV1 인코딩 시간이 넘었다" if detail == "timeout" else fail_note
        if detail and detail != "timeout":
            reason = f"{reason} ({_brief(detail)})"
        return _keep(source, reason)
    encoded = probe_source(dest, toolchain, timeout=60)
    if encoded is None or not _duration_ok(facts.duration, encoded.duration):
        dest.unlink(missing_ok=True)
        return _keep(source, "AV1 길이가 받은 파일과 다르다")
    try:
        src_size = source.stat().st_size
        dst_size = dest.stat().st_size
    except OSError as exc:
        dest.unlink(missing_ok=True)
        return _keep(source, f"결과 크기를 읽지 못했다 ({exc})")
    ratio = dst_size / src_size if src_size else float("inf")
    if dst_size <= 0 or ratio > max_ratio:
        dest.unlink(missing_ok=True)
        return _keep(source, f"AV1 결과가 더 크다 ({ratio:.3f}배)")
    note = ok_note or f"AV1로 바꿨다 ({ratio:.3f}배)"
    return TranscodeOutcome(dest, True, note)


def _run_ffmpeg(argv: list[str], dest: Path, timeout: float) -> tuple[bool, str]:
    try:
        proc = _run(argv, timeout=timeout)
    except subprocess.TimeoutExpired:
        dest.unlink(missing_ok=True)
        return False, "timeout"
    except OSError as exc:
        dest.unlink(missing_ok=True)
        return False, str(exc)
    stderr = _decode(proc.stderr).strip()
    if proc.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
        dest.unlink(missing_ok=True)
        return False, stderr or f"exit {proc.returncode}"
    return True, ""


def _keep(source: Path, reason: str) -> TranscodeOutcome:
    return TranscodeOutcome(source, False, f"받은 파일을 유지한다: {reason}")


def _encoded_path(source: Path) -> Path:
    return source.with_name(f"{source.stem}.av1.mp4")


def _rates_close(r_frame_rate: str, avg_frame_rate: str) -> bool:
    nominal = _rational(r_frame_rate)
    average = _rational(avg_frame_rate)
    if nominal is None or average is None or nominal <= 0 or average <= 0:
        return False
    return abs(nominal - average) <= FRAME_RATE_SLACK


def _duration_ok(source: float | None, output: float | None) -> bool:
    if source is None or output is None or source <= 0 or output <= 0:
        return False
    return abs(source - output) <= DURATION_TOLERANCE


def _positive_int(value: object) -> int | None:
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.isdigit() and int(value) > 0:
        return int(value)
    return None


def _brief(text: str) -> str:
    compact = " ".join(text.split())
    return compact[:180]
