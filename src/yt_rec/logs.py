"""출력 직전 비밀값 제거와 크기·기간이 제한된 앱 로그."""

from __future__ import annotations

import json
import logging
import re
import time
from collections import deque
from dataclasses import replace
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .state.models import LogEntry

MAX_LOG_BYTES = 2 * 1024 * 1024
LOG_BACKUPS = 4
MAX_MESSAGE_CHARS = 8000

_URL = re.compile(r"https?(?:://|%3a%2f%2f)[^\s<>\"']+", re.IGNORECASE)
_CREDENTIAL = re.compile(
    r"(?i)(?<![\w])(?:YT_REC_GOOGLE_)?(?:access[_-]?token|refresh[_-]?token|"
    r"id[_-]?token|client[_-]?(?:secret|id)|code[_-]?(?:verifier|challenge)|"
    r"authorization|proxy-authorization|set-cookie|cookies?|password|passwd|"
    r"(?:x-)?api[_-]?key|private[_-]?key|oauth[_-]?token(?:[_-]?secret)?|"
    r"credentials?|session(?:id|[_-]id|[_-]token)?|token|secret|code|SID|HSID|SSID|APISID|SAPISID|"
    r"__Secure-[\w-]+)[\"']?\s*[:=]\s*"
)
_TOKEN = re.compile(
    r"(?i)\b(?:Bearer|Basic)\s+[^\s,;\"']+|"
    r"\b(?:ya29\.[\w.-]+|GOCSPX-[\w-]+|1//[\w.-]+|eyJ[\w-]+\.[\w-]+\.[\w-]+)"
)
_NETSCAPE_COOKIE = re.compile(r"(?m)^(?:#HttpOnly_)?\S+\t(?:TRUE|FALSE)\t[^\n]+$")
_SECRET_OPTION = re.compile(
    r"(?i)--(?:password|username|cookies|add-header|client-secret|access-token|refresh-token)\b"
)


def redact(text: str) -> str:
    """불신하는 진단 문자열을 가린다. URL은 쿼리뿐 아니라 전체를 숨긴다.

    credential 필드 뒤의 구조를 추측하지 않고 나머지를 숨겨 여러 줄 JSON,
    쿠키 묶음, 예외의 repr 에서 뒤따르는 비밀값이 새지 않게 한다.
    원본 미디어 메타데이터나 자격 증명 저장에는 이 함수를 쓰지 않는다.
    """
    text = _URL.sub("[URL REDACTED]", str(text))
    option = _SECRET_OPTION.search(text)
    if option is not None:
        text = text[:option.end()] + " [REDACTED]"
    match = _CREDENTIAL.search(text)
    if match is not None:
        text = text[:match.end()] + "[REDACTED]"
    text = _TOKEN.sub("[REDACTED]", text)
    return _NETSCAPE_COOKIE.sub("[COOKIE REDACTED]", text)[:MAX_MESSAGE_CHARS]


def sanitize_entry(entry: LogEntry) -> LogEntry:
    return replace(entry, message=redact(entry.message), source=redact(entry.source))


def sanitize_event(event: object) -> object:
    """화면/알림에 쓰는 진단 필드만 가린다. 파일 경로와 보관 데이터는 유지한다."""
    from .state import events as ev

    if isinstance(event, ev.LogAppended):
        return replace(event, entry=sanitize_entry(event.entry))
    if isinstance(event, ev.RecordingProgress):
        return replace(event, detail=redact(event.detail)) if event.detail else event
    if isinstance(event, ev.RecordingStarted):
        return replace(event, recording=replace(event.recording, detail=redact(event.recording.detail)))
    if isinstance(event, ev.RecordingFinished):
        return replace(event, completed=replace(event.completed, note=redact(event.completed.note)))
    if isinstance(event, ev.CompletedChanged):
        return replace(event, completed=tuple(
            replace(item, note=redact(item.note)) for item in event.completed
        ))
    if isinstance(event, ev.SettingsSaveFailed):
        return replace(event, message=redact(event.message))
    if isinstance(event, ev.NotificationStatusChanged):
        return replace(event, status=replace(event.status, detail=redact(event.status.detail)))
    if isinstance(event, (ev.ArchiveDismissFinished, ev.ArchiveDeleteFinished)):
        return replace(event, error=redact(event.error))
    if isinstance(event, ev.ChannelsChanged):
        return replace(event, channels=tuple(
            replace(channel, last_check_result=redact(channel.last_check_result))
            for channel in event.channels
        ))
    return event


class RotatingLog(RotatingFileHandler):
    """UTF-8 바이트 상한과 일별 회전. 파일 5개(각 2MiB), 기본 14일 보관.

    표준 핸들러의 lock 으로 녹화 스레드들의 쓰기와 회전을 직렬화한다.
    날짜/기간 정리는 이 핸들러의 정확한 파일명만 대상으로 한다.
    """

    def __init__(
        self, path: Path, *, retention_days: int = 14,
        max_bytes: int = MAX_LOG_BYTES, backup_count: int = LOG_BACKUPS,
    ) -> None:
        if max_bytes < 512 or backup_count < 1:
            raise ValueError("로그 파일 상한은 512바이트 이상, 보관 파일 수는 1 이상이어야 합니다")
        path.parent.mkdir(parents=True, exist_ok=True)
        super().__init__(path, maxBytes=max_bytes, backupCount=backup_count,
                         encoding="utf-8", delay=True)
        self._day = int(path.stat().st_mtime // 86400) if path.exists() else int(time.time() // 86400)
        self.set_retention_days(retention_days)

    def set_retention_days(self, days: int) -> None:
        if not 1 <= days <= 365:
            raise ValueError("로그 보관 기간은 1~365일이어야 합니다")
        self.acquire()
        try:
            self.retention_days = days
            self._prune()
        finally:
            self.release()

    def _prune(self) -> None:
        cutoff = time.time() - self.retention_days * 86400
        for index in range(self.backupCount + 1):
            path = Path(self.baseFilename + (f".{index}" if index else ""))
            try:
                if path.stat().st_mtime >= cutoff:
                    continue
                if index == 0 and self.stream is not None:
                    self.stream.close()
                    self.stream = None
                path.unlink()
            except FileNotFoundError:
                pass

    def shouldRollover(self, record: logging.LogRecord) -> bool:  # noqa: N802
        day = int(time.time() // 86400)
        if day != self._day:
            self._day = day
            return True
        if self.stream is None:
            self.stream = self._open()
        self.stream.seek(0, 2)
        return self.stream.tell() + len((self.format(record) + "\n").encode("utf-8")) > self.maxBytes

    def _open(self):
        stream = super()._open()
        # 강제 종료로 마지막 줄이 잘렸어도 다음 정상 항목과 붙이지 않는다.
        with open(self.baseFilename, "rb") as reader:
            if reader.seek(0, 2):
                reader.seek(-1, 2)
                if reader.read(1) != b"\n":
                    stream.write("\n")
                    stream.flush()
        return stream

    def doRollover(self) -> None:  # noqa: N802
        super().doRollover()
        self._prune()

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802
        # 호출자가 저장 실패를 UI에 알린다. 표준 logging의 조용한 유실은 피한다.
        raise

    def write(self, text: str) -> None:
        text = text.rstrip("\n").encode("utf-8")[:self.maxBytes - 1].decode("utf-8", "ignore")
        self.handle(logging.LogRecord("yt-rec", logging.INFO, "", 0, text, (), None))

    def __enter__(self) -> RotatingLog:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class LogStore:
    """앱 전체 JSONL 로그. 최근 1000건은 재시작 때 최신순으로 읽는다."""

    def __init__(
        self, directory: Path | None = None, *, retention_days: int = 14,
        max_bytes: int = MAX_LOG_BYTES, backup_count: int = LOG_BACKUPS,
    ) -> None:
        from .recording.options import default_settings_path

        self.directory = Path(directory) if directory is not None else default_settings_path().parent / "logs"
        self._file = RotatingLog(self.directory / "yt-rec.log", retention_days=retention_days,
                                 max_bytes=max_bytes, backup_count=backup_count)

    def append(self, entry: LogEntry) -> LogEntry:
        entry = sanitize_entry(entry)
        # JSON 이스케이프(문자당 최대 6바이트) 후에도 한 행이 파일 상한을 넘지 않는다.
        room = (self._file.maxBytes - 200) // 12
        entry = replace(entry, message=entry.message[:room], source=entry.source[:room])
        data = {"at": entry.at.isoformat(), "severity": entry.severity.value,
                "source": entry.source, "message": entry.message}
        self._file.write(json.dumps(data, ensure_ascii=False))
        return entry

    def read_recent(self, limit: int = 1000) -> tuple[LogEntry, ...]:
        from .state.models import LogEntry, Severity

        recent: deque[LogEntry] = deque(maxlen=limit)
        cutoff = time.time() - self._file.retention_days * 86400
        self._file.acquire()
        try:
            for index in range(self._file.backupCount, -1, -1):
                path = Path(self._file.baseFilename + (f".{index}" if index else ""))
                try:
                    with path.open(encoding="utf-8", errors="replace") as stream:
                        for line in stream:
                            try:
                                data = json.loads(line)
                                at = datetime.fromisoformat(data["at"])
                                if at.tzinfo is None:
                                    at = at.replace(tzinfo=timezone.utc)
                                if at.timestamp() < cutoff:
                                    continue
                                recent.append(sanitize_entry(LogEntry(
                                    at=at, severity=Severity(data["severity"]),
                                    source=str(data.get("source", "")), message=str(data["message"]),
                                )))
                            except (ValueError, KeyError, TypeError, OverflowError):
                                continue  # 강제 종료로 잘린 마지막 행은 건너뛴다.
                except FileNotFoundError:
                    continue
        finally:
            self._file.release()
        return tuple(reversed(recent))

    def set_retention_days(self, days: int) -> None:
        self._file.set_retention_days(days)

    def close(self) -> None:
        self._file.close()
