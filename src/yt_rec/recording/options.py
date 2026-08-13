"""녹화 설정과 지속화.

화질 상한, 재시도 상한, 정지 판정 임계 등 녹화 동작을 결정하는 값을 한 곳에 모은다.
GUI(#11)는 이 dataclass 를 읽고 쓰기만 하면 된다.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

__all__ = [
    "QUALITY_PRESETS",
    "RecordingOptions",
    "default_settings_path",
    "load_settings",
    "save_settings",
]

#: GUI 콤보박스용 화질 상한 프리셋. ``None`` 은 상한 없음.
QUALITY_PRESETS: dict[str, int | None] = {
    "최고 화질 (제한 없음)": None,
    "2160p (4K)": 2160,
    "1440p (2K)": 1440,
    "1080p": 1080,
    "720p": 720,
    "480p": 480,
    "360p": 360,
}


@dataclass(frozen=True)
class RecordingOptions:
    """녹화 엔진 동작 설정.

    재시도 상한 기본값이 유한한 것은 의도된 것이다. 무한으로 두면 방송 종료 시점에
    서버에서 사라진 마지막 조각을 영원히 다시 요청하며 녹화가 정지한다(#14).
    """

    #: 완성된 녹화 파일을 놓을 디렉터리.
    output_dir: Path

    #: 중간 파일을 놓을 루트. 기본은 ``output_dir/.yt-rec``.
    #: 최종 파일을 rename 으로 옮기려면 output_dir 과 같은 볼륨이어야 한다.
    work_root: Path | None = None

    #: 영상 화질 상한(세로 해상도). ``None`` 이면 상한 없음.
    max_height: int | None = 1080

    #: 최종 컨테이너. ``mp4`` 또는 ``mkv``.
    container: str = "mp4"

    #: 전체(HTTP 요청) 재시도 상한. yt-dlp ``--retries``.
    total_retries: int = 10

    #: 조각 재시도 상한. yt-dlp ``--fragment-retries``. 반드시 유한해야 한다.
    fragment_retries: int = 20

    #: 추출기 오류 재시도 상한. yt-dlp ``--extractor-retries``.
    extractor_retries: int = 3

    #: 조각 재시도 사이 대기. yt-dlp ``--retry-sleep fragment:...`` 표현식.
    fragment_retry_sleep: str = "linear=1:5"

    #: 동시 조각 다운로드 수. DVR 백로그 회수 속도에 영향.
    concurrent_fragments: int = 1

    #: 이 시간 동안 진전이 없으면 정지로 판정한다(초).
    stall_timeout_seconds: float = 900.0

    #: ``--live-from-start`` 사용 여부. 방송 시작 지점부터 받으려면 켜야 한다.
    live_from_start: bool = True

    #: video id 로 소스 URL 을 만드는 템플릿. 스텁 서버 테스트에서 갈아끼운다.
    url_template: str = "https://www.youtube.com/watch?v={video_id}"

    #: 병합 검증에 성공해도 중간 파일을 남긴다.
    keep_intermediates: bool = False

    #: 패킷 단위 검증(역행 타임스탬프·프레임 간격) 수행 여부. 긴 파일에서는 느리다.
    verify_deep: bool = True

    #: 최종 파일 이름 템플릿. ``{date}`` ``{title}`` ``{channel}`` ``{video_id}`` 사용 가능.
    filename_template: str = "{date}_{title}"

    #: 파일명에 넣을 제목의 최대 글자 수.
    max_title_chars: int = 120

    #: yt-dlp 에 덧붙일 추가 인자. 쿠키·프록시처럼 엔진이 정하지 않는 것에 쓴다.
    #: 엔진이 직접 정하는 옵션(재시도 상한, ``-o``, ``--live-from-start`` 등)은
    #: 여기에 넣어도 거부된다. 안전 장치를 덮어쓰지 못하게 하기 위한 것이다.
    extra_ytdlp_args: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.fragment_retries < 0:
            raise ValueError("fragment_retries 는 음수일 수 없다")
        if self.total_retries < 0:
            raise ValueError("total_retries 는 음수일 수 없다")
        if self.stall_timeout_seconds <= 0:
            raise ValueError("stall_timeout_seconds 는 양수여야 한다")
        if self.container not in ("mp4", "mkv"):
            raise ValueError(f"지원하지 않는 컨테이너: {self.container}")
        object.__setattr__(self, "output_dir", Path(self.output_dir))
        if self.work_root is not None:
            object.__setattr__(self, "work_root", Path(self.work_root))
        object.__setattr__(self, "extra_ytdlp_args", tuple(self.extra_ytdlp_args))

    # -- 파생값 ---------------------------------------------------------------

    def resolved_work_root(self) -> Path:
        """중간 파일 루트. 기본값은 출력 디렉터리 아래 숨김 디렉터리."""
        return self.work_root or (self.output_dir / ".yt-rec")

    def format_selector(self) -> str:
        """화질 상한을 적용한 yt-dlp 포맷 선택식.

        상한 이하에서 최상의 영상 + 최상의 오디오를 고른다. 라이브가 상한보다 낮은
        화질로만 송출되면 ``height<=`` 조건이 자연히 그 화질을 고르므로 실패하지
        않는다. 상한 이하 포맷이 하나도 없는 극단적인 경우에도 마지막 대안
        (``bv*+ba/b``)이 있어 녹화가 실패하지 않는다.
        """
        if self.max_height is None:
            return "bv*+ba/b"
        cap = int(self.max_height)
        return f"bv*[height<={cap}]+ba/b[height<={cap}]/bv*+ba/b"

    def with_(self, **changes: object) -> RecordingOptions:
        """일부 값만 바꾼 사본. (frozen dataclass 라 ``replace`` 를 감싼다)"""
        return replace(self, **changes)  # type: ignore[arg-type]

    # -- 직렬화 ---------------------------------------------------------------

    def to_dict(self) -> dict:
        data = asdict(self)
        data["output_dir"] = str(self.output_dir)
        data["work_root"] = str(self.work_root) if self.work_root else None
        data["extra_ytdlp_args"] = list(self.extra_ytdlp_args)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> RecordingOptions:
        known = {f for f in cls.__dataclass_fields__}
        kwargs = {k: v for k, v in data.items() if k in known}
        kwargs["output_dir"] = Path(kwargs["output_dir"])
        if kwargs.get("work_root"):
            kwargs["work_root"] = Path(kwargs["work_root"])
        else:
            kwargs["work_root"] = None
        kwargs["extra_ytdlp_args"] = tuple(kwargs.get("extra_ytdlp_args") or ())
        return cls(**kwargs)


def default_settings_path() -> Path:
    """설정 파일 기본 위치. 재실행 후에도 설정이 유지되도록 사용자 설정 경로에 둔다."""
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":  # pragma: no cover - 플랫폼 의존
        base = Path.home() / "Library" / "Application Support"
    else:  # pragma: no cover - 플랫폼 의존
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "yt-rec" / "settings.json"


def save_settings(options: RecordingOptions, path: Path | None = None) -> Path:
    """설정을 JSON 으로 저장한다. 같은 디렉터리에 임시 파일을 쓴 뒤 교체한다."""
    target = Path(path or default_settings_path())
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(
        json.dumps(options.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(tmp, target)
    return target


def load_settings(
    path: Path | None = None, *, default: RecordingOptions | None = None
) -> RecordingOptions:
    """저장된 설정을 읽는다. 파일이 없거나 깨졌으면 ``default`` 를 돌려준다."""
    target = Path(path or default_settings_path())
    fallback = default or RecordingOptions(output_dir=Path.home() / "Videos" / "yt-rec")
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback
    try:
        return RecordingOptions.from_dict(raw)
    except (KeyError, TypeError, ValueError):
        return fallback
