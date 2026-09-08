"""외부 실행 파일(yt-dlp, ffmpeg, ffprobe) 경로를 한 곳에서 해결한다.

독립 실행형 앱은 함께 배포한 도구를 먼저 사용하고 소스 실행은 PATH에서 찾는다.

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

    PyInstaller의 내부 경로와 실행 파일 옆 ``bin/`` 을 후보로 본다.
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


def prepare_bundled_environment() -> None:
    """Let bundled yt-dlp find its bundled Deno runtime as a child process."""
    directories = [str(path) for path in _bundle_dirs() if path.name == "bin" and path.is_dir()]
    if directories:
        os.environ["PATH"] = os.pathsep.join([*directories, os.environ.get("PATH", "")])


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
