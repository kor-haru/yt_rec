"""Bounded native-notification metadata, independent of recording results."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from yt_rec.state.models import NotificationHistoryEntry

MAX_HISTORY = 500
MAX_FILE_BYTES = 48 * 1024 * 1024


@dataclass(frozen=True)
class ReceivedNotification:
    """Internal native display text only. Never carries Notification.data."""

    received_at: datetime
    title: str
    body: str
    synthetic: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.received_at, datetime) or self.received_at.utcoffset() is None:
            raise ValueError("알림 수신 시각에 시간대가 필요합니다")
        if (not isinstance(self.title, str) or len(self.title) > 4096
                or not isinstance(self.body, str) or len(self.body) > 16384):
            raise ValueError("알림 표시 내용의 길이가 올바르지 않습니다")


class NotificationHistoryStore:
    """Only a successfully loaded file may be replaced. Caller owns worker I/O."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._loaded = False

    def load(self) -> tuple[NotificationHistoryEntry, ...]:
        try:
            with self.path.open("rb") as stream:
                data = stream.read(MAX_FILE_BYTES + 1)
        except FileNotFoundError:
            data = b"[]"
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("알림 이력 파일 크기 초과")
        raw = json.loads(data)
        if not isinstance(raw, list) or len(raw) > MAX_HISTORY:
            raise ValueError("알림 이력 형식 오류")
        entries = []
        for row in raw:
            if not isinstance(row, dict) or set(row) != {"entry_id", "received_at", "title", "body"}:
                raise ValueError("알림 이력 필드 오류")
            entry_id = row["entry_id"]
            if not isinstance(entry_id, str) or len(entry_id) != 32 or any(c not in "0123456789abcdef" for c in entry_id):
                raise ValueError("알림 이력 ID 오류")
            notice = ReceivedNotification(datetime.fromisoformat(row["received_at"]), row["title"], row["body"])
            entries.append(NotificationHistoryEntry(entry_id, notice.received_at, notice.title, notice.body))
        if len({item.entry_id for item in entries}) != len(entries):
            raise ValueError("중복 알림 이력 ID")
        self._loaded = True
        return tuple(sorted(entries, key=lambda item: item.received_at, reverse=True))

    def save(self, entries: tuple[NotificationHistoryEntry, ...]) -> None:
        if not self._loaded:
            raise OSError("읽지 못한 기존 알림 이력은 덮어쓰지 않습니다")
        if len(entries) > MAX_HISTORY:
            raise ValueError("알림 이력 상한 초과")
        raw = [dict(entry_id=item.entry_id, received_at=item.received_at.isoformat(),
                    title=item.title, body=item.body) for item in entries]
        data = json.dumps(raw, ensure_ascii=False).encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("알림 이력 파일 크기 초과")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".notification-history-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)
