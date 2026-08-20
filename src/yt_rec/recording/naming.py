"""출력 파일명 결정.

Windows 에서 쓸 수 없는 문자는 의미가 가까운 전각 문자로 바꾼다. 글자를 지우지
않고 대응되는 유니코드로 옮기므로 제목을 읽는 데 지장이 없다.

날짜는 ``release_timestamp``(epoch)를 **로컬 시간대**로 변환해 정한다. yt-dlp 의
``release_date`` 는 UTC 기준이라 심야 방송에서 하루 어긋난다(#14).
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timezone, tzinfo
from pathlib import Path

__all__ = [
    "FORBIDDEN_CHAR_MAP",
    "local_date_from_epoch",
    "reserve_unique_path",
    "sanitize_filename_component",
]

#: Windows 파일명 금지 문자 -> 의미가 가까운 전각 문자.
FORBIDDEN_CHAR_MAP = {
    "<": "＜",  # U+FF1C FULLWIDTH LESS-THAN SIGN
    ">": "＞",  # U+FF1E FULLWIDTH GREATER-THAN SIGN
    ":": "：",  # U+FF1A FULLWIDTH COLON
    '"': "＂",  # U+FF02 FULLWIDTH QUOTATION MARK
    "/": "／",  # U+FF0F FULLWIDTH SOLIDUS
    "\\": "＼",  # U+FF3C FULLWIDTH REVERSE SOLIDUS
    "|": "｜",  # U+FF5C FULLWIDTH VERTICAL LINE
    "?": "？",  # U+FF1F FULLWIDTH QUESTION MARK
    "*": "＊",  # U+FF0A FULLWIDTH ASTERISK
}

#: Windows 예약 장치 이름. 확장자가 붙어도 예약어라 그대로 쓸 수 없다.
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_COLLAPSE_SPACE = re.compile(r"[ \t　]{2,}")


def sanitize_filename_component(
    name: str, *, max_chars: int = 120, fallback: str = "untitled"
) -> str:
    """파일명 한 조각을 Windows/macOS/Linux 모두에서 안전한 문자열로 바꾼다.

    - 금지 문자는 :data:`FORBIDDEN_CHAR_MAP` 대로 전각 문자로 치환한다.
    - 제어 문자는 제거한다.
    - 끝의 점과 공백은 Windows 가 조용히 잘라내므로 미리 없앤다.
    - 예약 장치 이름은 밑줄을 붙여 피한다.
    - ``max_chars`` 를 넘으면 잘라낸다(경로 길이 제한 대비).
    """
    text = _CONTROL_CHARS.sub("", name or "")
    text = "".join(FORBIDDEN_CHAR_MAP.get(ch, ch) for ch in text)
    text = _COLLAPSE_SPACE.sub(" ", text).strip()

    if max_chars > 0 and len(text) > max_chars:
        text = text[:max_chars].rstrip()

    # 잘라낸 뒤에 끝이 점·공백이 될 수 있으므로 마지막에 다듬는다.
    text = text.rstrip(" .　")

    if not text:
        return fallback
    if text.split(".")[0].upper() in _RESERVED_NAMES:
        text = f"_{text}"
    return text


def local_date_from_epoch(epoch: int | float, tz: tzinfo | None = None) -> date:
    """epoch 초를 로컬(또는 지정) 시간대의 날짜로 바꾼다.

    UTC 기준 날짜 문자열을 그대로 쓰면 심야 방송에서 날짜가 하루 어긋난다.
    """
    moment = datetime.fromtimestamp(float(epoch), tz=timezone.utc)
    return moment.astimezone(tz).date()


def reserve_unique_path(directory: Path, basename: str, extension: str) -> Path:
    """``directory`` 안에서 아직 쓰이지 않은 경로를 잡아 0바이트 파일로 예약한다.

    같은 이름이 이미 있으면 ``이름 (2)``, ``이름 (3)`` … 으로 비켜간다. 예약은
    ``O_CREAT|O_EXCL`` 로 하므로 검사와 생성 사이에 다른 프로세스가 끼어들어
    기존 파일을 덮어쓰는 일이 없다.

    돌려받은 경로에는 ``os.replace`` 로 실제 파일을 올려놓으면 된다.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    suffix = extension if extension.startswith(".") else f".{extension}"

    for attempt in range(1, 10_000):
        stem = basename if attempt == 1 else f"{basename} ({attempt})"
        candidate = directory / f"{stem}{suffix}"
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            continue
        os.close(fd)
        return candidate

    raise RuntimeError(f"{basename}{suffix} 의 빈 이름을 찾지 못했다")
