"""중간 파일 병합과 결과 검증.

녹화가 정지하거나 프로세스가 죽어도 영상·음성 중간 파일이 온전하면
``ffmpeg -c copy`` 로 재생 가능한 단일 파일을 만들 수 있다(실측 2건 복구).

검증은 다음 다섯 지표를 본다.

1. 컨테이너 데먹싱 오류 0건
2. 프레임 수 = 재생 길이 x 프레임률
3. 역행 타임스탬프 0건
4. 최대 프레임 간격이 1프레임 이내
5. 영상과 음성 길이 차이 1초 이내

1번과 3번이 깨지면 파일이 재생되지 않는 것으로 본다(``playable=False``). 2/4/5 만
어긋나면 누락 구간이 있는 부분 복구로 기록한다(``playable=True, complete=False``).
중간 파일 정리는 ``playable`` 일 때만 한다.
"""

from __future__ import annotations

import json
import subprocess
from array import array
from dataclasses import dataclass, field
from pathlib import Path

from .binaries import Toolchain
from .errors import ToolFailure

__all__ = [
    "MediaVerification",
    "StreamInfo",
    "check_demux",
    "find_intermediates",
    "merge_streams",
    "probe_streams",
    "verify_media",
]

#: 중간 파일로 인정하는 확장자.
_MEDIA_SUFFIXES = frozenset({".mp4", ".m4a", ".webm", ".mkv", ".mka", ".m4v", ".ts", ".aac"})

#: 중간 파일이 아닌 부산물.
_IGNORED_SUFFIXES = frozenset({".ytdl", ".json", ".log", ".txt", ".temp", ".tmp"})


def _run(argv: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def _decode(raw: bytes | None) -> str:
    return (raw or b"").decode("utf-8", errors="replace")


def _rational(text: str | None) -> float | None:
    """``30000/1001`` 같은 유리수 문자열을 실수로."""
    if not text or text in ("0/0", "N/A"):
        return None
    try:
        if "/" in text:
            num, den = text.split("/", 1)
            denominator = float(den)
            return float(num) / denominator if denominator else None
        return float(text)
    except (TypeError, ValueError):
        return None


def _float_or_none(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class StreamInfo:
    index: int
    codec_type: str
    codec_name: str | None
    duration: float | None
    nb_frames: int | None
    frame_rate: float | None


@dataclass(frozen=True)
class MediaVerification:
    """병합 결과 검증 요약."""

    path: Path
    #: 컨테이너가 온전해 재생 가능한가. 중간 파일 정리 여부를 이 값으로 정한다.
    playable: bool
    #: 누락 없이 완전한가. 거짓이면 부분 복구다.
    complete: bool
    duration: float | None = None
    demux_errors: tuple[str, ...] = ()
    video_frames: int | None = None
    expected_frames: int | None = None
    backward_timestamps: int | None = None
    max_frame_gap: float | None = None
    frame_interval: float | None = None
    av_duration_delta: float | None = None
    streams: tuple[StreamInfo, ...] = ()
    issues: tuple[str, ...] = field(default_factory=tuple)

    @property
    def status_text(self) -> str:
        if not self.playable:
            return "검증 실패"
        return "정상" if self.complete else "부분 복구"

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "playable": self.playable,
            "complete": self.complete,
            "duration": self.duration,
            "demux_errors": list(self.demux_errors),
            "video_frames": self.video_frames,
            "expected_frames": self.expected_frames,
            "backward_timestamps": self.backward_timestamps,
            "max_frame_gap": self.max_frame_gap,
            "frame_interval": self.frame_interval,
            "av_duration_delta": self.av_duration_delta,
            "issues": list(self.issues),
        }


def find_intermediates(work_dir: Path, video_id: str) -> list[Path]:
    """work 디렉터리에서 ``{video_id}.f{format}.{ext}`` 꼴 중간 파일을 모은다.

    ``.part`` 로 끝나는 파일은 확장자를 되돌려 ffmpeg 가 컨테이너를 알아보게 한다.
    ``-FragNNN`` 같은 조각 임시 파일과 ``.ytdl`` 재개 정보는 뺀다.
    """
    work_dir = Path(work_dir)
    if not work_dir.is_dir():
        return []

    found: list[Path] = []
    for path in sorted(work_dir.iterdir()):
        if not path.is_file():
            continue
        name = path.name
        if not name.startswith(f"{video_id}.f"):
            continue
        if "-Frag" in name:
            continue

        candidate = path
        if name.endswith(".part"):
            restored = path.with_name(name[: -len(".part")])
            if restored.exists():
                continue  # 완결된 같은 이름 파일이 있으면 그쪽을 쓴다
            try:
                path.rename(restored)
            except OSError:
                continue  # 다른 프로세스가 붙잡고 있다. 건드리지 않고 넘어간다.
            candidate = restored

        suffix = candidate.suffix.lower()
        if suffix in _IGNORED_SUFFIXES or suffix not in _MEDIA_SUFFIXES:
            continue
        try:
            if candidate.stat().st_size == 0:
                continue
        except OSError:
            continue
        found.append(candidate)
    return found


def probe_streams(path: Path, toolchain: Toolchain) -> tuple[float | None, list[StreamInfo]]:
    """ffprobe 로 컨테이너 길이와 스트림 목록을 읽는다."""
    proc = _run(
        [
            str(toolchain.ffprobe),
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=index,codec_type,codec_name,duration,nb_frames,"
            "avg_frame_rate,r_frame_rate",
            "-of",
            "json",
            str(path),
        ]
    )
    if proc.returncode != 0:
        raise ToolFailure(
            f"ffprobe 실패: {path.name}",
            returncode=proc.returncode,
            output=_decode(proc.stderr),
        )
    try:
        data = json.loads(_decode(proc.stdout) or "{}")
    except ValueError as exc:
        raise ToolFailure(f"ffprobe 출력을 해석할 수 없다: {path.name}") from exc

    duration = _float_or_none((data.get("format") or {}).get("duration"))
    streams: list[StreamInfo] = []
    for raw in data.get("streams") or []:
        nb_frames = raw.get("nb_frames")
        streams.append(
            StreamInfo(
                index=int(raw.get("index", len(streams))),
                codec_type=raw.get("codec_type") or "unknown",
                codec_name=raw.get("codec_name"),
                duration=_float_or_none(raw.get("duration")),
                nb_frames=int(nb_frames) if str(nb_frames).isdigit() else None,
                frame_rate=_rational(raw.get("avg_frame_rate"))
                or _rational(raw.get("r_frame_rate")),
            )
        )
    return duration, streams


def merge_streams(
    sources: list[Path], dest: Path, toolchain: Toolchain, *, timeout: float | None = None
) -> Path:
    """영상·음성 중간 파일을 다시 인코딩하지 않고 하나로 묶는다."""
    if not sources:
        raise ToolFailure("병합할 중간 파일이 없다")

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    argv = [str(toolchain.ffmpeg), "-y", "-hide_banner", "-nostdin", "-v", "error"]
    for source in sources:
        argv += ["-i", str(source)]
    for index in range(len(sources)):
        # 영상·음성만 가져온다. '?' 는 해당 종류가 없어도 넘어가라는 뜻이다.
        argv += ["-map", f"{index}:v?", "-map", f"{index}:a?"]
    argv += ["-c", "copy", str(dest)]

    proc = _run(argv, timeout=timeout)
    stderr = _decode(proc.stderr).strip()
    if proc.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        raise ToolFailure(
            f"ffmpeg 병합 실패: {dest.name}", returncode=proc.returncode, output=stderr
        )
    return dest


def check_demux(path: Path, toolchain: Toolchain, *, timeout: float | None = None) -> list[str]:
    """컨테이너를 끝까지 읽어보며 데먹싱 오류를 센다."""
    proc = _run(
        [
            str(toolchain.ffmpeg),
            "-hide_banner",
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(path),
            "-c",
            "copy",
            "-f",
            "null",
            "-",
        ],
        timeout=timeout,
    )
    lines = [line.strip() for line in _decode(proc.stderr).splitlines() if line.strip()]
    if proc.returncode != 0 and not lines:
        lines.append(f"ffmpeg 종료 코드 {proc.returncode}")
    return lines


def _scan_video_packets(
    path: Path, toolchain: Toolchain, *, timeout: float | None = None
) -> tuple[int, int, float | None] | None:
    """영상 패킷을 훑어 (개수, 역행 타임스탬프 수, 최대 프레임 간격)을 낸다.

    역행 판정은 DTS 로 한다. B-프레임이 있으면 디코드 순서의 PTS 는 정상적으로도
    뒤로 갈 수 있어서 PTS 로 재면 거짓 양성이 난다. 간격은 PTS 를 정렬해 잰다.

    훑기에 실패하면 ``None``. 0건으로 돌려주면 멀쩡한 녹화를 프레임 누락으로
    오판하게 되므로, 모른다는 사실을 그대로 알린다.
    """
    argv = [
        str(toolchain.ffprobe),
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "packet=pts_time,dts_time",
        "-of",
        "csv=p=0",
        str(path),
    ]
    presentation = array("d")
    backward = 0
    previous_dts: float | None = None
    count = 0

    # stderr 를 파이프로 받아 두고 읽지 않으면 버퍼가 차서 멈출 수 있다. 버린다.
    try:
        with subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        ) as proc:
            assert proc.stdout is not None
            for raw in proc.stdout:
                fields = raw.decode("ascii", errors="replace").strip().split(",")
                if not fields or not fields[0]:
                    continue
                count += 1
                pts = _float_or_none(fields[0])
                if pts is not None:
                    presentation.append(pts)
                dts = _float_or_none(fields[1]) if len(fields) > 1 else None
                if dts is not None:
                    if previous_dts is not None and dts < previous_dts:
                        backward += 1
                    previous_dts = dts
            try:
                returncode = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                return None
    except OSError:
        return None

    if returncode != 0:
        return None

    max_gap: float | None = None
    if len(presentation) > 1:
        ordered = sorted(presentation)
        max_gap = max(b - a for a, b in zip(ordered, ordered[1:]))
    return count, backward, max_gap


def verify_media(
    path: Path,
    toolchain: Toolchain,
    *,
    deep: bool = True,
    frame_tolerance: int = 1,
    gap_tolerance: float = 1.5,
    av_delta_tolerance: float = 1.0,
    timeout: float | None = None,
) -> MediaVerification:
    """병합 결과를 검증한다. 예외를 던지지 않고 결과에 담아 돌려준다."""
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return MediaVerification(
            path=path, playable=False, complete=False, issues=("파일이 없거나 비어 있다",)
        )

    issues: list[str] = []
    try:
        duration, streams = probe_streams(path, toolchain)
    except (ToolFailure, OSError, subprocess.SubprocessError) as exc:
        return MediaVerification(
            path=path, playable=False, complete=False, issues=(f"ffprobe 실패: {exc}",)
        )

    video = next((s for s in streams if s.codec_type == "video"), None)
    audio = next((s for s in streams if s.codec_type == "audio"), None)
    if video is None:
        issues.append("영상 스트림이 없다")
    if audio is None:
        issues.append("음성 스트림이 없다")

    try:
        demux_errors = tuple(check_demux(path, toolchain, timeout=timeout))
    except (OSError, subprocess.SubprocessError) as exc:
        demux_errors = (f"데먹싱 검사를 마치지 못했다: {exc}",)
    if demux_errors:
        issues.append(f"데먹싱 오류 {len(demux_errors)}건")

    video_duration = (video.duration if video else None) or duration
    audio_duration = (audio.duration if audio else None) or duration
    av_delta = (
        abs(video_duration - audio_duration)
        if video_duration is not None and audio_duration is not None and audio is not None
        else None
    )
    if av_delta is not None and av_delta > av_delta_tolerance:
        issues.append(f"영상/음성 길이 차이 {av_delta:.2f}초")

    frame_rate = video.frame_rate if video else None
    frame_interval = 1.0 / frame_rate if frame_rate else None

    video_frames: int | None = video.nb_frames if video else None
    backward: int | None = None
    max_gap: float | None = None
    if deep and video is not None:
        scan = _scan_video_packets(path, toolchain, timeout=timeout)
        if scan is None:
            issues.append("패킷 검사를 마치지 못했다")
        else:
            video_frames, backward, max_gap = scan
            if backward:
                issues.append(f"역행 타임스탬프 {backward}건")
            if (
                max_gap is not None
                and frame_interval
                and max_gap > frame_interval * gap_tolerance
            ):
                issues.append(
                    f"최대 프레임 간격 {max_gap:.3f}초 (1프레임 {frame_interval:.3f}초)"
                )

    expected_frames: int | None = None
    if video_duration and frame_rate:
        expected_frames = round(video_duration * frame_rate)
        if video_frames is not None and abs(video_frames - expected_frames) > frame_tolerance:
            issues.append(f"프레임 수 {video_frames} != 예상 {expected_frames}")

    playable = not demux_errors and not backward and video is not None and bool(duration)
    complete = playable and not issues

    return MediaVerification(
        path=path,
        playable=playable,
        complete=complete,
        duration=duration,
        demux_errors=demux_errors,
        video_frames=video_frames,
        expected_frames=expected_frames,
        backward_timestamps=backward,
        max_frame_gap=max_gap,
        frame_interval=frame_interval,
        av_duration_delta=av_delta,
        streams=tuple(streams),
        issues=tuple(issues),
    )
