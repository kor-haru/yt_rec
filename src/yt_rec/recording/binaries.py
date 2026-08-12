"""외부 실행 파일(yt-dlp, ffmpeg, ffprobe) 경로를 한 곳에서 해결한다.

지금은 PATH에서 찾는다. 번들링(#5)이 도입되면 :func:`_bundle_dirs` 에 번들 디렉터리를
추가하는 것만으로 전환이 끝나고, 이 모듈을 쓰는 나머지 코드는 손대지 않는다.

환경 변수로 개별 경로를 덮어쓸 수 있다 (개발·테스트용):
``YT_REC_YTDLP``, ``YT_REC_FFMPEG``, ``YT_REC_FFPROBE``.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "BinaryNotFoundError",
    "Toolchain",
    "find_executable",
    "resolve_toolchain",
]

#: 논리 이름 -> (실행 파일 이름, 환경 변수 이름)
_TOOLS = {
    "ytdlp": ("yt-dlp", "YT_REC_YTDLP"),
    "ffmpeg": ("ffmpeg", "YT_REC_FFMPEG"),
    "ffprobe": ("ffprobe", "YT_REC_FFPROBE"),
}


class BinaryNotFoundError(RuntimeError):
    """필요한 외부 실행 파일을 찾지 못했다."""

    def __init__(self, tool: str, executable: str) -> None:
        super().__init__(
            f"{executable}을(를) 찾을 수 없다. PATH에 설치하거나 "
            f"{_TOOLS[tool][1]} 환경 변수로 경로를 지정하라."
        )
        self.tool = tool
        self.executable = executable


@dataclass(frozen=True)
class Toolchain:
    """녹화에 필요한 실행 파일 경로 묶음."""

    ytdlp: Path
    ffmpeg: Path
    ffprobe: Path

    @property
    def ffmpeg_dir(self) -> Path:
        """yt-dlp ``--ffmpeg-location`` 에 넘길 디렉터리."""
        return self.ffmpeg.parent


def _bundle_dirs() -> list[Path]:
    """번들된 실행 파일을 찾을 후보 디렉터리.

    #5(패키징)가 들어오기 전까지는 대부분 비어 있다. PyInstaller 로 얼린 경우에만
    임시 추출 경로와 실행 파일 옆 ``bin/`` 을 후보로 본다.
    """
    dirs: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        dirs.append(Path(meipass) / "bin")
        dirs.append(Path(meipass))
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        dirs.append(exe_dir / "bin")
        dirs.append(exe_dir)
    return dirs


def find_executable(tool: str, *, search_dirs: list[Path] | None = None) -> Path | None:
    """``tool`` (논리 이름)의 실행 파일 경로를 찾는다. 없으면 ``None``.

    탐색 순서: 환경 변수 → 호출자가 준 디렉터리 → 번들 디렉터리 → PATH.
    """
    try:
        executable, env_var = _TOOLS[tool]
    except KeyError:  # pragma: no cover - 프로그래밍 오류
        raise ValueError(f"알 수 없는 도구: {tool}") from None

    override = os.environ.get(env_var)
    if override:
        candidate = Path(override)
        if candidate.is_file():
            return candidate
        found = shutil.which(override)
        return Path(found) if found else None

    for directory in list(search_dirs or []) + _bundle_dirs():
        found = shutil.which(executable, path=str(directory))
        if found:
            return Path(found)

    found = shutil.which(executable)
    return Path(found) if found else None


def resolve_toolchain(*, search_dirs: list[Path] | None = None) -> Toolchain:
    """세 실행 파일을 모두 해결한다. 하나라도 없으면 :class:`BinaryNotFoundError`."""
    resolved: dict[str, Path] = {}
    for tool, (executable, _) in _TOOLS.items():
        path = find_executable(tool, search_dirs=search_dirs)
        if path is None:
            raise BinaryNotFoundError(tool, executable)
        resolved[tool] = path
    return Toolchain(**resolved)
