"""출력 파일명 결정.

Windows 에서 쓸 수 없는 문자는 의미가 가까운 전각 문자로 바꾼다. 글자를 지우지
않고 대응되는 유니코드로 옮기므로 제목을 읽는 데 지장이 없다.

날짜는 ``release_timestamp``(epoch)를 **로컬 시간대**로 변환해 정한다. yt-dlp 의
``release_date`` 는 UTC 기준이라 심야 방송에서 하루 어긋난다(#14).

이름의 배치는 사용자가 템플릿으로 정한다(#92). 대괄호가 토큰 표시이고 대괄호
자체는 출력에 남지 않는다. 토큰 바깥 글자는 사용자가 적은 그대로 남는다.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone, tzinfo
from pathlib import Path

__all__ = [
    "DEFAULT_FILENAME_TEMPLATE",
    "FILENAME_TOKENS",
    "FORBIDDEN_CHAR_MAP",
    "NameFields",
    "SAMPLE_NAME_FIELDS",
    "local_date_from_epoch",
    "migrate_filename_template",
    "render_filename",
    "reserve_unique_path",
    "sanitize_filename_component",
    "unknown_filename_tokens",
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


@dataclass(frozen=True)
class NameFields:
    """파일명 토큰을 채울 재료.

    ``start`` 는 **이미 로컬 시간대로 옮긴** 시작 시각이다. UTC 기준 값을 넘기면
    심야 방송에서 날짜가 하루 어긋난다(#14).
    """

    start: datetime
    title: str = ""
    channel: str = ""
    video_id: str = ""
    channel_id: str = ""
    #: 실제로 받은 세로 해상도 표시(``1080p``). 못 읽었으면 빈 문자열.
    quality: str = ""


#: 대괄호 토큰 이름 -> 값 뽑기. 여기 적힌 순서가 설정 화면 목록 순서다.
#:
#: 이름은 사용자가 정한 그대로다(#92). 설정 화면에서 눌러 넣으므로 길어도 된다.
FILENAME_TOKENS: dict[str, Callable[[NameFields], str]] = {
    "YYMMDD": lambda f: f.start.strftime("%y%m%d"),
    "YYYYMMDD": lambda f: f.start.strftime("%Y%m%d"),
    "YYYY-MM-DD": lambda f: f.start.strftime("%Y-%m-%d"),
    "YYYY.MM.DD": lambda f: f.start.strftime("%Y.%m.%d"),
    "HHMM": lambda f: f.start.strftime("%H%M"),
    "HH-MM": lambda f: f.start.strftime("%H-%M"),
    "채널명": lambda f: f.channel,
    "영상제목": lambda f: f.title,
    "영상고유url키": lambda f: f.video_id,
    "채널ID": lambda f: f.channel_id,
    "화질": lambda f: f.quality,
}

#: 설정 화면의 토큰 목록과 미리보기가 쓰는 예시 값.
SAMPLE_NAME_FIELDS = NameFields(
    start=datetime(2026, 9, 21, 19, 54),
    title="오늘도 한다",
    channel="침착맨",
    video_id="EYEAaG3cxME",
    channel_id="UCUj6rrhMTR9pipbAWBAMvUQ",
    quality="1080p",
)

#: 사용자가 아무것도 정하지 않았을 때의 배치. 옛 ``{date}_{title}`` 과 같다.
DEFAULT_FILENAME_TEMPLATE = "[YYYY-MM-DD]_[영상제목]"

#: 옛 ``str.format`` 문법 -> 대괄호 토큰. 토큰이 네 개뿐이라 대응표로 끝난다.
_LEGACY_TOKENS = {
    "{date}": "[YYYY-MM-DD]",
    "{title}": "[영상제목]",
    "{channel}": "[채널명]",
    "{video_id}": "[영상고유url키]",
}

_TOKEN = re.compile(r"\[([^\[\]]+)\]")
#: 토큰 사이에 놓이는 구분자.
_SEPARATORS = " \t_-"
_SEPARATOR_RUN = re.compile(r"[ \t_-]{2,}")
_EMPTY_PAIR = re.compile(r"\([ \t_-]*\)|\[[ \t_-]*\]")
#: 제목이 아닌 토큰 한 조각의 길이 상한. 제목 상한은 설정값이다.
_VALUE_CHARS = 64
_TITLE_TOKEN = "영상제목"


def migrate_filename_template(template: str) -> str:
    """옛 ``{date}_{title}`` 문법을 대괄호 토큰으로 옮긴다.

    이 설정은 화면에 없었으므로 저장된 값은 사실상 전부 기본값이다(#92).
    """
    if "{" not in template:
        return template
    for old, new in _LEGACY_TOKENS.items():
        template = template.replace(old, new)
    return template


def unknown_filename_tokens(template: str) -> list[str]:
    """템플릿에 든 모르는 토큰 이름들. 설정 화면이 저장을 막는 근거다."""
    return [
        name
        for name in _TOKEN.findall(migrate_filename_template(template))
        if name not in FILENAME_TOKENS
    ]


def render_filename(
    template: str, fields: NameFields, *, max_title_chars: int = 120
) -> str:
    """대괄호 토큰을 채워 파일 이름(확장자 제외)을 만든다.

    모르는 토큰이 하나라도 있으면 :class:`ValueError` 다. 부르는 쪽이 기본 배치로
    떨어질지 사용자에게 알릴지 정한다 — 녹화가 끝난 뒤에는 떨어져야 한다.
    """
    template = migrate_filename_template(template)
    unknown = unknown_filename_tokens(template)
    if unknown:
        raise ValueError("모르는 토큰: " + ", ".join(f"[{name}]" for name in unknown))

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        limit = max_title_chars if name == _TITLE_TOKEN else _VALUE_CHARS
        # 채워 넣은 값만 안전화한다. 사용자가 적은 구분자는 건드리지 않는다.
        return sanitize_filename_component(
            FILENAME_TOKENS[name](fields), max_chars=limit, fallback=""
        )

    return _collapse_separators(_TOKEN.sub(substitute, template))


def _collapse_separators(text: str) -> str:
    """빈 토큰이 남긴 흔적을 지운다.

    채널명이나 화질을 못 받으면 그 자리가 빈 문자열이 되어 ``260921__제목_()`` 이
    나온다. 치환이 끝난 뒤 통째로 접는다 — 어느 구분자가 어느 토큰 것인지 따지지
    않는다. 연속한 구분자는 맨 앞 것만 남기고, 속이 빈 괄호 짝은 지운다.
    """
    while True:
        shorter = _SEPARATOR_RUN.sub(lambda m: m.group(0)[0], _EMPTY_PAIR.sub("", text))
        if shorter == text:
            return text.strip(_SEPARATORS)
        text = shorter


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
