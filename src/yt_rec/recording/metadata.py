"""녹화 시작 시점에 방송 메타데이터를 확보하고 보관한다.

방송이 끝난 직후 영상이 멤버 전용이나 비공개로 바뀌면 제목·채널·시작 시각을 더는
조회할 수 없다(실측: ``Join this channel to get access to members-only content``).
그래서 파일명 재료는 녹화를 **시작할 때** 받아 work 디렉터리에 적어 두고, 마무리
단계에서는 보관된 값만 쓴다.

yt-dlp 는 프리즌 바이너리라 표준출력이 OEM 코드페이지로 나가 한글이 깨질 수 있다.
그래서 값을 표준출력으로 받지 않고 ``--print-to-file`` 로 UTF-8 파일에 쓰게 한 뒤
읽는다.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone, tzinfo
from pathlib import Path

from .binaries import Toolchain
from .errors import DenialCategory, MetadataUnavailableError, classify_error
from .naming import local_date_from_epoch, sanitize_filename_component

__all__ = ["LiveMetadata", "fetch_metadata"]

#: 파일명과 보관함 표시에 필요한 필드만 추린 yt-dlp 출력 템플릿(JSON).
_PRINT_TEMPLATE = (
    "%(.{id,title,fulltitle,channel,uploader,channel_id,release_timestamp,"
    "timestamp,live_status,is_live,was_live,duration,webpage_url})j"
)

#: work 디렉터리 안 보관 파일 이름.
METADATA_FILENAME = "metadata.json"
_RAW_FILENAME = "metadata.raw.json"


@dataclass(frozen=True)
class LiveMetadata:
    """녹화 시작 시점에 확보해 보관하는 방송 정보."""

    video_id: str
    title: str | None = None
    channel: str | None = None
    channel_id: str | None = None
    uploader: str | None = None
    release_timestamp: int | None = None
    upload_timestamp: int | None = None
    live_status: str | None = None
    was_live: bool | None = None
    duration: float | None = None
    webpage_url: str | None = None
    #: 이 정보를 확보한 시각(epoch). 시작 시각을 못 받았을 때의 마지막 대안.
    fetched_at: float = 0.0
    #: 조회에 실패해 video id 만으로 만든 자리표시자인가.
    placeholder: bool = False

    # -- 파생값 ---------------------------------------------------------------

    @property
    def display_title(self) -> str:
        """표시·파일명에 쓸 제목. 없으면 video id."""
        return self.title or self.video_id

    @property
    def start_epoch(self) -> float:
        """라이브 시작 시각(epoch). 없으면 업로드 시각, 그것도 없으면 확보 시각."""
        for value in (self.release_timestamp, self.upload_timestamp):
            if value:
                return float(value)
        return self.fetched_at or time.time()

    def start_datetime(self, tz: tzinfo | None = None) -> datetime:
        """라이브 시작 시각을 로컬(또는 지정) 시간대의 datetime 으로."""
        return datetime.fromtimestamp(self.start_epoch, tz=timezone.utc).astimezone(tz)

    def local_start_date(self, tz: tzinfo | None = None) -> date:
        """파일명에 쓸 날짜. UTC 가 아니라 로컬 시간대 기준이다."""
        return local_date_from_epoch(self.start_epoch, tz)

    def basename(
        self,
        template: str = "{date}_{title}",
        *,
        max_title_chars: int = 120,
        tz: tzinfo | None = None,
    ) -> str:
        """보관된 값만으로 최종 파일 이름(확장자 제외)을 만든다."""
        title = sanitize_filename_component(
            self.display_title, max_chars=max_title_chars, fallback=self.video_id
        )
        channel = sanitize_filename_component(
            self.channel or self.uploader or "", max_chars=64, fallback=""
        )
        fields = {
            "date": self.local_start_date(tz).isoformat(),
            "title": title,
            "channel": channel,
            "video_id": self.video_id,
        }
        try:
            rendered = template.format(**fields)
        except (KeyError, IndexError, ValueError):
            # 설정에 잘못된 템플릿이 들어와도 녹화를 잃지 않는다.
            rendered = "{date}_{title}".format(**fields)
        return sanitize_filename_component(
            rendered,
            max_chars=max_title_chars + 64,  # 날짜·채널·구분자 몫을 더해 둔다
            fallback=self.video_id,
        )

    # -- 직렬화 ---------------------------------------------------------------

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> LiveMetadata:
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, work_dir: Path) -> Path:
        """work 디렉터리에 보관한다."""
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        target = work_dir / METADATA_FILENAME
        target.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return target

    @classmethod
    def load(cls, work_dir: Path) -> LiveMetadata | None:
        """보관된 값을 읽는다. 없으면 ``None``."""
        target = Path(work_dir) / METADATA_FILENAME
        try:
            return cls.from_dict(json.loads(target.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return None

    @classmethod
    def placeholder_for(cls, video_id: str) -> LiveMetadata:
        """조회에 실패했을 때 쓸 최소한의 자리표시자."""
        return cls(video_id=video_id, fetched_at=time.time(), placeholder=True)


def _parse_raw(raw_path: Path, video_id: str) -> LiveMetadata | None:
    """``--print-to-file`` 이 남긴 UTF-8 JSON 을 읽는다."""
    try:
        text = raw_path.read_text(encoding="utf-8")
    except OSError:
        return None
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        info = json.loads(lines[-1])
    except ValueError:
        return None
    if not isinstance(info, dict):
        return None
    return LiveMetadata(
        video_id=info.get("id") or video_id,
        title=info.get("title") or info.get("fulltitle"),
        channel=info.get("channel"),
        channel_id=info.get("channel_id"),
        uploader=info.get("uploader"),
        release_timestamp=info.get("release_timestamp"),
        upload_timestamp=info.get("timestamp"),
        live_status=info.get("live_status"),
        was_live=info.get("was_live"),
        duration=info.get("duration"),
        webpage_url=info.get("webpage_url"),
        fetched_at=time.time(),
    )


def fetch_metadata(
    video_id: str,
    url: str,
    toolchain: Toolchain,
    work_dir: Path,
    *,
    timeout: float = 120.0,
    extractor_retries: int = 3,
) -> LiveMetadata:
    """방송 정보를 조회한다. 실패하면 :class:`MetadataUnavailableError`.

    호출자는 성공한 결과를 곧바로 :meth:`LiveMetadata.save` 로 보관해야 한다.
    """
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    raw_path = work_dir / _RAW_FILENAME
    raw_path.unlink(missing_ok=True)  # --print-to-file 은 이어붙이기다

    argv = [
        str(toolchain.ytdlp),
        "--ignore-config",
        "--no-colors",
        "--encoding",
        "utf-8",
        "--simulate",
        "--skip-download",
        "--no-playlist",
        "--extractor-retries",
        str(extractor_retries),
        "--print-to-file",
        _PRINT_TEMPLATE,
        _RAW_FILENAME,
        url,
    ]

    try:
        proc = subprocess.run(
            argv,
            cwd=str(work_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise MetadataUnavailableError(
            f"{video_id}: 메타데이터 조회가 {timeout:.0f}초 안에 끝나지 않았다",
            DenialCategory.NETWORK,
        ) from exc
    except OSError as exc:
        raise MetadataUnavailableError(f"{video_id}: yt-dlp 실행 실패 ({exc})") from exc

    output = (proc.stdout or b"").decode("utf-8", errors="replace")
    metadata = _parse_raw(raw_path, video_id)
    if metadata is not None:
        return metadata

    raise MetadataUnavailableError(
        f"{video_id}: 메타데이터를 받지 못했다 (yt-dlp 종료 코드 {proc.returncode})\n"
        f"{output.strip()[-2000:]}",
        classify_error(output),
    )
