"""중간 파일 병합과 결과 검증.

녹화가 정지하거나 프로세스가 죽어도 영상·음성 중간 파일이 온전하면
``ffmpeg -c copy`` 로 재생 가능한 단일 파일을 만들 수 있다(실측 2건 복구).

검증은 다음 다섯 지표를 본다.

1. 컨테이너 데먹싱 오류 0건
2. 프레임 수 = 재생 길이 x 프레임률
3. 역행 타임스탬프 0건
4. 최대 프레임 간격이 관측된 전형 프레임 간격 이내
5. 영상과 음성 길이 차이가 조각 하나 이내

여기에 구조 검사 하나를 더 본다: **결과에 영상·음성 트랙이 하나씩만 있는가.**
낡은 중간 파일이 함께 병합되면 트랙이 여러 개가 되고, 어느 것이 트랙 0 이 되는지는
포맷 id 의 사전순이 정한다. 낡은 트랙이 0번이 되면 프레임 검사가 그 트랙을 보고
통과해 버린다(실측 재현: 낡은 3초 트랙이 0번, 이번에 받은 5초가 2번).

1번·3번이 깨지거나 트랙이 겹치면 결과 파일을 쓸 수 없는 것으로 본다
(``playable=False``). 2/4/5 만 어긋나면 누락 구간이 있는 부분 복구로 기록한다
(``playable=True, complete=False``).

**중간 파일 정리는 ``complete`` 일 때만 한다** (#14 수용 기준: "중간 파일은 병합 검증에
성공한 뒤에만 정리한다"). ``playable`` 만 보고 지우면 "음성 스트림이 없다"나 "패킷
검사를 마치지 못했다"처럼 측정조차 못 한 상태에서 원본이 사라진다.

모든 하위 프로세스 호출에는 시한이 있다. ffmpeg/ffprobe 가 물리면 마무리 단계가
무기한 매달리는데, 정지 감지기는 다운로드 단계에서만 돌기 때문에 아무도 알아채지
못한다(#14 가 없애려는 "겉보기에 정상인 무기한 정지"와 같은 것이다).
"""

from __future__ import annotations

import json
import subprocess
import threading
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from .binaries import Toolchain
from .errors import ToolFailure, ToolTimeout

__all__ = [
    "DEFAULT_AV_DELTA_TOLERANCE",
    "DEFAULT_GAP_FLOOR_SECONDS",
    "DEFAULT_GAP_TOLERANCE",
    "MediaVerification",
    "SourceSelection",
    "StreamInfo",
    "check_demux",
    "find_intermediates",
    "merge_streams",
    "probe_streams",
    "select_merge_sources",
    "verify_media",
]

#: 중간 파일로 인정하는 확장자.
_MEDIA_SUFFIXES = frozenset({".mp4", ".m4a", ".webm", ".mkv", ".mka", ".m4v", ".ts", ".aac"})

#: 중간 파일이 아닌 부산물.
_IGNORED_SUFFIXES = frozenset({".ytdl", ".json", ".log", ".txt", ".temp", ".tmp"})

# -- 임계값 ---------------------------------------------------------------------
#
# 이 저장소에서 실제로 녹화한 파일 4건(724MB~1.6GB, 3910s~7041s)을 재서 정했다.
#
# **영상/음성 길이 차(av_delta)**
#   영상 스트림은 조각 경계에서 끝나고 음성이 그만큼 더 길게 남는 구조라, 차이는
#   조각 하나 길이만큼 벌어질 수 있다. 실측 키프레임(=조각) 간격은 1.0초 / 2.0초 /
#   5.0초였고(파일별 최빈값), 실측 av_delta 는 0.005266 / 0.009073 / 0.008163 /
#   0.986848 초였다. 0.986848 초인 파일은 조각 간격이 정확히 1.0초인 60fps 방송으로,
#   조각 하나의 98.7% 에 해당한다. 즉 이 차이는 방송마다 사실상 임의값이고, 조각이
#   5.0초인 방송에서는 5초까지 벌어질 수 있다. 예전 임계 1.0초는 실측값의 98.7%
#   지점이라 AAC 프레임 하나(약 0.0213~0.0232초)만 더 길었어도 정상 녹화가 PARTIAL
#   로 기록됐다. 그래서 관측된 최대 조각 길이(5.0초)에 여유를 둔 6.0초로 올린다.
#   진짜 음성 누락은 초 단위가 아니라 분 단위로 벌어지므로(음성 다운로더가 죽으면
#   수천 초가 빈다) 100배 이상의 여유가 남는다. 리뷰에서 확인된 참 양성 두 건
#   (파일 중간 훼손 -> 데먹싱 오류, 뒤쪽 절단 -> ffprobe 실패)은 이 지표와 무관하다.
DEFAULT_AV_DELTA_TOLERANCE = 6.0

#: **최대 프레임 간격** — 기준을 ``1/avg_frame_rate`` 가 아니라 *관측된* 프레임 간격의
#: 중앙값으로 잡는다. 가변 프레임률에서는 ``avg_frame_rate`` 가 파일 어디에도 없는
#: 값이 되기 때문이다(실측: 앞 5초 30fps + 뒤 5초 1.9fps 클립의 avg_frame_rate 는
#: 16.5fps -> 1프레임 0.0606초. 실제 간격은 절반이 0.0333초, 절반이 0.533초).
#: 실측 녹화 4건의 ``max_gap/중앙값`` 은 1.000000 / 1.000000 / 1.000000 / 1.000030
#: 이고 ``max_gap/(1/avg_frame_rate)`` 는 1.00001~1.00002 다. #14 테스트 방식은
#: "최대 프레임 간격이 1프레임을 넘지 않음" 이지만 실측값이 1을 미세하게 넘으므로
#: 1.0 으로는 정상 파일도 떨어진다. 중앙값 기준 2배로 둔다(실측 대비 약 2배 여유).
DEFAULT_GAP_TOLERANCE = 2.0

#: 프레임 간격이 이 값을 넘지 않으면 누락으로 보지 않는다(초).
#:
#: 프레임률이 방송 중에 떨어지는 것과 조각이 빠진 것은 타임스탬프만으로는 구분할 수
#: 없다. 그래서 "조각 하나만큼 비었는가" 를 기준으로 삼는다. 실측 조각 길이는 최소
#: 1.0초였고, 실측 가변 프레임률 클립의 최대 간격은 0.533초였다. 그 사이에서 잡되
#: 조각 하나(1.0초)는 반드시 잡히도록 0.75초로 둔다(가변 프레임률 쪽으로 1.41배,
#: 조각 누락 쪽으로 1.42배 여유). 조각을 건너뛴 사실 자체는
#: :attr:`RecordingResult.skipped_fragments` 에 따로 기록되므로 이 지표는 그것을
#: 확인하는 보조 지표다.
DEFAULT_GAP_FLOOR_SECONDS = 0.75

#: 하위 프로세스 기본 시한(초). ``None`` 을 기본값으로 두면 무기한 정지가 된다.
DEFAULT_PROBE_TIMEOUT = 300.0
DEFAULT_VERIFY_TIMEOUT = 1800.0
DEFAULT_MERGE_TIMEOUT = 3600.0


def _run(argv: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess:
    """하위 프로세스를 시한과 함께 돌린다.

    ``subprocess.run`` 은 시한을 넘기면 자식을 죽이고 거둔 뒤
    :class:`subprocess.TimeoutExpired` 를 던진다. 좀비가 남지 않는다.
    """
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


def _tag_duration(tags: dict | None) -> float | None:
    """matroska 트랙 태그의 ``DURATION`` (``00:00:30.023000000``)을 초로.

    mkv 는 스트림별 duration 을 컨테이너 필드로 내놓지 않는다. 그대로 두면 영상과
    음성이 **둘 다 컨테이너 길이**가 되어 ``av_delta`` 가 항상 0.0 이 되고, 프레임 수
    검사도 컨테이너 길이(=둘 중 긴 쪽)로 계산돼 뒤틀린다. 다행히 ffmpeg 의 matroska
    먹서는 트랙마다 ``DURATION`` 태그를 남기고, 우리가 만드는 결과물과 yt-dlp 가
    만드는 결과물은 모두 ffmpeg 로 먹싱된다. 그래서 이 태그로 되살릴 수 있다.
    """
    if not tags:
        return None
    for key, value in tags.items():
        if key.split("-", 1)[0].upper() != "DURATION":
            continue
        text = str(value).strip()
        try:
            if ":" in text:
                hours, minutes, seconds = text.split(":")
                return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
            return float(text)
        except (TypeError, ValueError):
            continue
    return None


@dataclass(frozen=True)
class StreamInfo:
    index: int
    codec_type: str
    codec_name: str | None
    duration: float | None
    nb_frames: int | None
    frame_rate: float | None
    #: 스트림 시작 타임스탬프. mkv 는 AAC 프리롤만큼(약 0.023초) 밀려 있다.
    start_time: float | None = None
    #: :attr:`duration` 을 컨테이너 필드가 아니라 태그에서 읽었는가.
    duration_from_tag: bool = False

    @property
    def content_duration(self) -> float | None:
        """프레임 수 계산에 쓸 길이. 시작 오프셋을 뺀다.

        mkv 의 ``DURATION`` 태그는 시작 오프셋을 포함한 절대 끝 시각이라, 그대로
        프레임 수를 계산하면 오프셋만큼(60fps 에서 약 1.4프레임) 어긋난다.
        컨테이너가 직접 알려준 duration 은 이미 스트림 기준이라 건드리지 않는다.
        """
        if self.duration is None:
            return None
        if self.duration_from_tag and self.start_time:
            return max(0.0, self.duration - self.start_time)
        return self.duration


@dataclass(frozen=True)
class MediaVerification:
    """병합 결과 검증 요약."""

    path: Path
    #: 결과 파일을 쓸 수 있는가. 데먹싱·역행 타임스탬프·트랙 중복을 본다.
    playable: bool
    #: 누락 없이 완전한가. 거짓이면 부분 복구다. **중간 파일 정리는 이 값으로 정한다.**
    complete: bool
    duration: float | None = None
    demux_errors: tuple[str, ...] = ()
    video_frames: int | None = None
    expected_frames: int | None = None
    backward_timestamps: int | None = None
    max_frame_gap: float | None = None
    frame_interval: float | None = None
    av_duration_delta: float | None = None
    video_stream_count: int = 0
    audio_stream_count: int = 0
    #: 검사 중 하나가 시한을 넘겨 끊겼다. 마무리 단계의 정지에 해당한다.
    timed_out: bool = False
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
            "video_stream_count": self.video_stream_count,
            "audio_stream_count": self.audio_stream_count,
            "timed_out": self.timed_out,
            "issues": list(self.issues),
        }


def _format_id_of(path: Path) -> str | None:
    """``VID.f137.mp4`` -> ``137``. 포맷 표시가 없으면 ``None``."""
    stem = path.stem
    if "." not in stem:
        return None
    tail = stem.rsplit(".", 1)[1]
    return tail[1:] if tail.startswith("f") and len(tail) > 1 else None


def find_intermediates(work_dir: Path, video_id: str) -> list[Path]:
    """work 디렉터리에서 ``{video_id}.f{format}.{ext}`` 꼴 중간 파일을 모은다.

    ``.part`` 로 끝나는 파일은 확장자를 되돌려 ffmpeg 가 컨테이너를 알아보게 한다.
    ``-FragNNN`` 같은 조각 임시 파일과 ``.ytdl`` 재개 정보는 뺀다.

    ``{name}`` 과 ``{name}.part`` 가 함께 있으면 **큰 쪽**을 쓴다. yt-dlp 관례에서
    ``.part`` 는 진행 중인 파일이므로 대개 그쪽이 최신이다. 예전 구현은 무조건
    ``.part`` 를 건너뛰어 낡은 파일을 우선했다.

    .. note::
       후보 탐색은 ``iterdir()`` + 개별 ``stat()`` 이다. ``os.scandir`` 로 바꾸면
       디렉터리 엔트리에 캐시된 낡은 크기를 읽어 살아 있는 파일을 0바이트로
       오판할 수 있다. CPython 의 ``os.stat`` 은 경로를 열어 크기를 읽으므로 안전하다
       (실측 116표본, 낡은 값 0회). **바꾸지 말 것.**
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
            if not _part_wins(path, restored):
                continue  # 완결된 쪽이 더 크다. 그쪽을 쓴다.
            try:
                path.replace(restored)
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
        if candidate not in found:
            found.append(candidate)
    return found


def _part_wins(part: Path, restored: Path) -> bool:
    """``.part`` 쪽을 써야 하는가. 완결된 이름이 없거나 더 작으면 참."""
    try:
        if not restored.exists():
            return True
        return part.stat().st_size >= restored.stat().st_size
    except OSError:
        return False


def probe_streams(
    path: Path, toolchain: Toolchain, *, timeout: float | None = DEFAULT_PROBE_TIMEOUT
) -> tuple[float | None, list[StreamInfo]]:
    """ffprobe 로 컨테이너 길이와 스트림 목록을 읽는다.

    ``stream_tags=DURATION`` 까지 읽는 것은 mkv 때문이다. matroska 는 스트림별
    duration 을 컨테이너 필드로 내놓지 않아, 태그가 없으면 영상·음성 길이가 둘 다
    컨테이너 길이가 되어 ``av_delta`` 가 항상 0.0 이 된다.
    """
    proc = _run(
        [
            str(toolchain.ffprobe),
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=index,codec_type,codec_name,duration,start_time,"
            "nb_frames,avg_frame_rate,r_frame_rate:stream_tags=DURATION",
            "-of",
            "json",
            str(path),
        ],
        timeout=timeout,
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
        stream_duration = _float_or_none(raw.get("duration"))
        from_tag = False
        if stream_duration is None:
            stream_duration = _tag_duration(raw.get("tags"))
            from_tag = stream_duration is not None
        streams.append(
            StreamInfo(
                index=int(raw.get("index", len(streams))),
                codec_type=raw.get("codec_type") or "unknown",
                codec_name=raw.get("codec_name"),
                duration=stream_duration,
                nb_frames=int(nb_frames) if str(nb_frames).isdigit() else None,
                frame_rate=_rational(raw.get("avg_frame_rate"))
                or _rational(raw.get("r_frame_rate")),
                start_time=_float_or_none(raw.get("start_time")),
                duration_from_tag=from_tag,
            )
        )
    return duration, streams


def _kinds_of(
    path: Path, toolchain: Toolchain, *, timeout: float | None
) -> tuple[bool, bool] | None:
    """``(영상이 있는가, 음성이 있는가)``. 미디어로 읽히지 않으면 ``None``.

    시한을 넘긴 경우만 :class:`ToolTimeout` 을 던진다. "읽을 수 없는 파일"로 삼켜
    버리면 살아 있는 중간 파일이 조용히 병합 대상에서 빠진다.
    """
    try:
        _, streams = probe_streams(path, toolchain, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ToolTimeout(
            f"ffprobe 가 {timeout:.0f}초 안에 끝나지 않아 끊었다: {path.name}",
            timeout=timeout,
        ) from None
    except (ToolFailure, OSError, subprocess.SubprocessError):
        return None
    has_video = any(s.codec_type == "video" for s in streams)
    has_audio = any(s.codec_type == "audio" for s in streams)
    if not has_video and not has_audio:
        return None
    return has_video, has_audio


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


@dataclass(frozen=True)
class SourceSelection:
    """실제로 병합할 중간 파일, 각 입력에서 가져올 스트림, 제외한 것들의 사유."""

    sources: tuple[Path, ...] = ()
    #: ffmpeg ``-map`` 지정자. :attr:`sources` 의 순서를 입력 번호로 쓴다.
    maps: tuple[str, ...] = ()
    excluded: tuple[str, ...] = ()


#: 사람이 읽을 종류 이름.
_KIND_LABEL = {"video": "영상", "audio": "음성"}


def select_merge_sources(
    candidates: Iterable[Path],
    toolchain: Toolchain,
    *,
    format_ids: Iterable[str] | None = None,
    timeout: float | None = DEFAULT_PROBE_TIMEOUT,
) -> SourceSelection:
    """후보 중간 파일에서 **이번 시도가 받은** 영상 하나와 음성 하나를 고른다.

    :func:`find_intermediates` 는 ``{id}.f*`` 에 맞는 모든 파일을 이름 순으로 모은다.
    지난 시도가 검증 실패로 남긴 중간 파일이 그대로 있는데(설계상 의도된 동작) 사용자가
    화질 상한을 낮춰 다시 녹화하면 포맷 id 가 달라져 낡은 파일과 새 파일이 함께 모인다.
    그걸 다 병합하면 결과에 트랙이 셋이 되고, 어느 것이 트랙 0 이 되는지는 포맷 id 의
    사전순이 정한다. 낡은 트랙이 0번이 되면 프레임 검사가 그 트랙을 보고 통과한다
    (실측 재현: 낡은 3초 트랙이 0번, 이번에 받은 5초가 2번, 판정 ``complete``).

    고르는 순서:

    1. 후보를 ffprobe 로 훑어 영상/음성 어느 쪽을 담고 있는지 본다.
    2. **종류별로** 따로 줄인다. ``format_ids`` (이번 시도에 yt-dlp 가 진행률로 알려준
       포맷 id)에 맞는 후보가 그 종류에 하나라도 있으면 그 안에서만 고른다.
    3. 남은 후보 중 **가장 최근에 바뀐** 것을 쓴다. 이름의 사전순이 아니라 시각으로.

    2번을 종류별로 하는 것이 중요하다. 전체에 한 번만 걸면, 진행률에 영상 포맷만
    잡히고 음성 포맷이 안 잡힌 경우에 살아 있는 음성 파일이 통째로 버려진다.
    걸러내기가 결과를 잃는 원인이 되어서는 안 된다.
    """
    excluded: list[str] = []
    pools: dict[str, list[Path]] = {"video": [], "audio": []}
    for path in candidates:
        kinds = _kinds_of(path, toolchain, timeout=timeout)
        if kinds is None:
            excluded.append(f"미디어로 읽히지 않는다: {path.name}")
            continue
        has_video, has_audio = kinds
        if has_video:
            pools["video"].append(path)
        if has_audio:
            pools["audio"].append(path)

    wanted = {str(f) for f in (format_ids or ()) if f}
    chosen: dict[str, Path] = {}
    for kind, pool in pools.items():
        if not pool:
            continue
        if wanted:
            matched = [p for p in pool if _format_id_of(p) in wanted]
            if matched:
                pool = matched
        # 최근에 바뀐 것부터. 시각이 같으면(복구 경로에서 포맷 id 가 없을 때) 더 큰
        # 파일을 쓴다. 이름 순이면 낡은 f137 이 새 f299 보다 항상 앞선다.
        pool = sorted(pool, key=lambda p: (-_mtime(p), -_size(p), p.name))
        chosen[kind] = pool[0]

    sources: list[Path] = []
    maps: list[str] = []
    for kind in ("video", "audio"):
        path = chosen.get(kind)
        if path is None:
            continue
        if path not in sources:
            sources.append(path)
        maps.append(f"{sources.index(path)}:{kind[0]}:0")

    for kind, pool in pools.items():
        for path in pool:
            if chosen.get(kind) is not path and path not in sources:
                excluded.append(
                    f"{_KIND_LABEL[kind]}은 더 최근 파일을 쓴다: {path.name}"
                )
    return SourceSelection(
        sources=tuple(sources), maps=tuple(maps), excluded=tuple(dict.fromkeys(excluded))
    )


def merge_streams(
    sources: Sequence[Path],
    dest: Path,
    toolchain: Toolchain,
    *,
    maps: Sequence[str] | None = None,
    timeout: float | None = DEFAULT_MERGE_TIMEOUT,
) -> Path:
    """영상·음성 중간 파일을 다시 인코딩하지 않고 하나로 묶는다.

    결과에는 **영상 한 트랙과 음성 한 트랙만** 담는다. 입력마다 ``-map i:v? -map i:a?``
    를 붙이면 입력 수만큼 트랙이 생기고, 낡은 파일이 섞여 있을 때 그것이 트랙 0 이 될
    수 있다.

    ``maps`` 를 주면 그대로 쓴다(:func:`select_merge_sources` 가 정해 준 것). 주지
    않으면 입력을 훑어 영상은 가진 첫 입력에서, 음성도 가진 첫 입력에서 한 트랙씩만
    가져온다.
    """
    if not sources:
        raise ToolFailure("병합할 중간 파일이 없다")

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    if maps:
        inputs = [Path(p) for p in sources]
        specs = list(maps)
    else:
        inputs = []
        specs = []
        unreadable: list[str] = []
        for path in sources:
            kinds = _kinds_of(path, toolchain, timeout=timeout)
            if kinds is None:
                unreadable.append(Path(path).name)
                continue
            has_video, has_audio = kinds
            take: list[str] = []
            if has_video and not any(s.endswith(":v:0") for s in specs):
                take.append("v:0")
            if has_audio and not any(s.endswith(":a:0") for s in specs):
                take.append("a:0")
            if not take:
                continue
            index = len(inputs)
            inputs.append(Path(path))
            specs += [f"{index}:{spec}" for spec in take]
        if not inputs:
            raise ToolFailure(
                "병합할 중간 파일에서 영상·음성 스트림을 찾지 못했다"
                + (f" (읽지 못한 파일: {', '.join(unreadable)})" if unreadable else "")
            )

    argv = [str(toolchain.ffmpeg), "-y", "-hide_banner", "-nostdin", "-v", "error"]
    for source in inputs:
        argv += ["-i", str(source)]
    for spec in specs:
        argv += ["-map", spec]
    argv += ["-c", "copy", str(dest)]

    try:
        proc = _run(argv, timeout=timeout)
    except subprocess.TimeoutExpired:
        # run() 이 자식을 죽이고 거뒀다. 반쯤 쓰인 결과 파일은 치운다.
        dest.unlink(missing_ok=True)
        raise ToolTimeout(
            f"ffmpeg 병합이 {timeout:.0f}초 안에 끝나지 않아 끊었다: {dest.name}",
            timeout=timeout,
        ) from None
    stderr = _decode(proc.stderr).strip()
    if proc.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        raise ToolFailure(
            f"ffmpeg 병합 실패: {dest.name}", returncode=proc.returncode, output=stderr
        )
    return dest


def check_demux(
    path: Path, toolchain: Toolchain, *, timeout: float | None = DEFAULT_VERIFY_TIMEOUT
) -> list[str]:
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
    path: Path, toolchain: Toolchain, *, timeout: float | None = DEFAULT_VERIFY_TIMEOUT
) -> tuple[int, int, float | None, float | None] | None:
    """영상 패킷을 훑어 ``(개수, 역행 타임스탬프 수, 최대 간격, 간격 중앙값)``.

    역행 판정은 DTS 로 한다. B-프레임이 있으면 디코드 순서의 PTS 는 정상적으로도
    뒤로 갈 수 있어서 PTS 로 재면 거짓 양성이 난다. 간격은 PTS 를 정렬해 잰다.

    중앙값을 함께 내는 것은 가변 프레임률 때문이다. ``avg_frame_rate`` 의 역수는
    프레임률이 방송 중에 바뀌면 파일 어디에도 없는 값이 되어, 누락이 없어도 최대 간격
    검사가 걸린다(실측: avg 16.5fps 인 클립의 실제 간격은 0.0333초와 0.533초 두 무리).

    훑기에 실패하면 ``None``. 0건으로 돌려주면 멀쩡한 녹화를 프레임 누락으로
    오판하게 되므로, 모른다는 사실을 그대로 알린다. 시한을 넘겨 끊었을 때만
    :class:`ToolTimeout` 을 던진다 — 그건 "물려 있었다"는 다른 사실이다.

    시한은 타이머로 강제한다. ffprobe 가 아무 줄도 내놓지 않고 물리면 읽기 루프가
    영원히 멈춰 있으므로, 루프 안에서 시각을 보는 것으로는 끊을 수 없다.
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
    timer: threading.Timer | None = None
    timed_out = threading.Event()

    # stderr 를 파이프로 받아 두고 읽지 않으면 버퍼가 차서 멈출 수 있다. 버린다.
    try:
        with subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        ) as proc:
            assert proc.stdout is not None
            if timeout is not None:

                def _kill() -> None:
                    timed_out.set()
                    try:
                        proc.kill()
                    except OSError:  # pragma: no cover - 이미 끝났다
                        pass

                timer = threading.Timer(timeout, _kill)
                timer.daemon = True
                timer.start()
            try:
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
                returncode = proc.wait(timeout=30)
            finally:
                if timer is not None:
                    timer.cancel()
    except (OSError, ValueError, subprocess.SubprocessError):
        if timed_out.is_set():
            raise ToolTimeout(
                f"ffprobe 패킷 훑기가 {timeout:.0f}초 안에 끝나지 않아 끊었다: {path.name}",
                timeout=timeout,
            ) from None
        return None

    if timed_out.is_set():
        raise ToolTimeout(
            f"ffprobe 패킷 훑기가 {timeout:.0f}초 안에 끝나지 않아 끊었다: {path.name}",
            timeout=timeout,
        )
    if returncode != 0:
        return None

    max_gap: float | None = None
    median_gap: float | None = None
    if len(presentation) > 1:
        ordered = sorted(presentation)
        gaps = sorted(b - a for a, b in zip(ordered, ordered[1:]))
        max_gap = gaps[-1]
        median_gap = gaps[len(gaps) // 2]
    return count, backward, max_gap, median_gap


def verify_media(
    path: Path,
    toolchain: Toolchain,
    *,
    deep: bool = True,
    frame_tolerance: int = 1,
    gap_tolerance: float = DEFAULT_GAP_TOLERANCE,
    gap_floor_seconds: float = DEFAULT_GAP_FLOOR_SECONDS,
    av_delta_tolerance: float = DEFAULT_AV_DELTA_TOLERANCE,
    timeout: float | None = DEFAULT_VERIFY_TIMEOUT,
) -> MediaVerification:
    """병합 결과를 검증한다. 예외를 던지지 않고 결과에 담아 돌려준다.

    ``timeout`` 은 하위 프로세스 하나당 시한이다. ``None`` 을 넘길 수는 있지만
    기본값은 유한하다 — 무기한 기다리면 마무리 단계가 조용히 매달린다.
    """
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return MediaVerification(
            path=path, playable=False, complete=False, issues=("파일이 없거나 비어 있다",)
        )

    issues: list[str] = []
    timed_out = False
    try:
        duration, streams = probe_streams(path, toolchain, timeout=timeout)
    except subprocess.TimeoutExpired:
        return MediaVerification(
            path=path,
            playable=False,
            complete=False,
            timed_out=True,
            issues=(f"ffprobe 가 {timeout:.0f}초 안에 끝나지 않아 끊었다",),
        )
    except (ToolFailure, OSError, subprocess.SubprocessError) as exc:
        return MediaVerification(
            path=path, playable=False, complete=False, issues=(f"ffprobe 실패: {exc}",)
        )

    videos = [s for s in streams if s.codec_type == "video"]
    audios = [s for s in streams if s.codec_type == "audio"]
    video = videos[0] if videos else None
    audio = audios[0] if audios else None
    if not videos:
        issues.append("영상 스트림이 없다")
    elif len(videos) > 1:
        # 낡은 중간 파일이 함께 병합된 것이다. 어느 트랙이 0번인지는 포맷 id 의
        # 사전순이 정하므로, 이번에 받은 것이 아닌 트랙이 대표가 될 수 있다.
        issues.append(f"영상 스트림이 {len(videos)}개다")
    if not audios:
        issues.append("음성 스트림이 없다")
    elif len(audios) > 1:
        issues.append(f"음성 스트림이 {len(audios)}개다")
    duplicated = len(videos) > 1 or len(audios) > 1

    try:
        demux_errors = tuple(check_demux(path, toolchain, timeout=timeout))
    except subprocess.TimeoutExpired:
        timed_out = True
        demux_errors = (f"데먹싱 검사가 {timeout:.0f}초 안에 끝나지 않아 끊었다",)
    except (OSError, subprocess.SubprocessError) as exc:
        demux_errors = (f"데먹싱 검사를 마치지 못했다: {exc}",)
    if demux_errors:
        issues.append(f"데먹싱 오류 {len(demux_errors)}건")

    # 스트림별 길이를 컨테이너 길이로 대신하면 영상·음성이 **둘 다 같은 값**이 되어
    # av_delta 가 항상 0.0 이 되고, 프레임 수도 둘 중 긴 쪽으로 계산돼 뒤틀린다.
    # 그런 상태를 0.0 으로 보고하면 "재 봤더니 차이가 없다"로 읽혀 최악이다. 재지
    # 못했다는 사실을 그대로 알린다. (mp4 는 스트림 필드, ffmpeg 가 만든 mkv 는 트랙
    # DURATION 태그로 각각 잴 수 있다 — 여기 걸리는 것은 둘 다 없는 컨테이너다.)
    video_duration = video.duration if video else None
    audio_duration = audio.duration if audio else None
    av_delta: float | None = None
    if audio is not None and video is not None:
        if video_duration is None and audio_duration is None:
            issues.append("스트림별 길이를 알 수 없어 영상/음성 길이를 비교하지 못했다")
        else:
            av_delta = abs((video_duration or duration or 0.0) - (audio_duration or duration or 0.0))
            if av_delta > av_delta_tolerance:
                issues.append(f"영상/음성 길이 차이 {av_delta:.2f}초")

    frame_rate = video.frame_rate if video else None
    #: 표시용 기본값. 패킷을 훑었으면 관측된 중앙값으로 바꾼다.
    frame_interval = 1.0 / frame_rate if frame_rate else None

    video_frames: int | None = video.nb_frames if video else None
    backward: int | None = None
    max_gap: float | None = None
    scan_failed = False
    if deep and video is not None:
        try:
            scan = _scan_video_packets(path, toolchain, timeout=timeout)
        except ToolTimeout as exc:
            scan, timed_out = None, True
            issues.append(str(exc))
        if scan is None:
            scan_failed = True
            if not timed_out:
                issues.append("패킷 검사를 마치지 못했다")
        else:
            video_frames, backward, max_gap, median_gap = scan
            if median_gap:
                # 관측된 전형 간격. 가변 프레임률에서 avg_frame_rate 의 역수는
                # 파일 어디에도 없는 값이라 기준으로 쓸 수 없다.
                frame_interval = median_gap
            if backward:
                issues.append(f"역행 타임스탬프 {backward}건")
            if max_gap is not None and frame_interval:
                allowed = max(frame_interval * gap_tolerance, gap_floor_seconds)
                if max_gap > allowed:
                    issues.append(
                        f"최대 프레임 간격 {max_gap:.3f}초 "
                        f"(전형 간격 {frame_interval:.3f}초, 허용 {allowed:.3f}초)"
                    )

    expected_frames: int | None = None
    content_duration = video.content_duration if video else None
    if content_duration and frame_rate:
        expected_frames = round(content_duration * frame_rate)
        if video_frames is not None and abs(video_frames - expected_frames) > frame_tolerance:
            issues.append(f"프레임 수 {video_frames} != 예상 {expected_frames}")
    elif video is not None and video_frames is not None:
        # 컨테이너 길이로 계산하면 음성이 더 긴 만큼 예상 프레임이 부풀어 정상 녹화가
        # 어긋난 것으로 나온다(실측 mkv: 프레임 수 120 != 예상 166). 그렇다고 조용히
        # 건너뛰면 지표 하나가 사라진 줄 아무도 모른다.
        issues.append("영상 길이를 알 수 없어 프레임 수를 검증하지 못했다")

    # 재생 가능성은 두 지표(데먹싱 오류, 역행 타임스탬프)로 본다. 요청한 패킷 검사가
    # 실패해 역행을 **한 번도 재지 못했다면** 통과로 취급하지 않는다. 재지 못한 것을
    # 0건으로 세면 그 상태로 중간 파일까지 지워 원본을 되살릴 길이 없어진다.
    playable = (
        not demux_errors
        and not duplicated
        and not scan_failed
        and not backward
        and video is not None
        and bool(duration)
    )
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
        video_stream_count=len(videos),
        audio_stream_count=len(audios),
        timed_out=timed_out,
        streams=tuple(streams),
        issues=tuple(issues),
    )
