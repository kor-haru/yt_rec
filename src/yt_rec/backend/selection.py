"""자동 녹화 대상 채널 ID 저장.

자격증명이 아니다. JSON 파일에 채널 ID 목록만 둔다.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from yt_rec.recording.options import default_settings_path

__all__ = ["SelectionStore", "MemorySelectionStore", "FileSelectionStore", "default_selection_path"]


class SelectionStore(Protocol):
    def load(self) -> tuple[str, ...]: ...

    def save(self, channel_ids: Sequence[str]) -> None: ...


class MemorySelectionStore:
    def __init__(self, channel_ids: Sequence[str] = ()) -> None:
        self._ids = tuple(dict.fromkeys(channel_ids))

    def load(self) -> tuple[str, ...]:
        return self._ids

    def save(self, channel_ids: Sequence[str]) -> None:
        self._ids = tuple(dict.fromkeys(channel_ids))


def default_selection_path() -> Path:
    return default_settings_path().with_name("watched_channels.json")


class FileSelectionStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_selection_path()

    def load(self) -> tuple[str, ...]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ()
        if isinstance(raw, dict):
            values = raw.get("channel_ids") or []
        else:
            values = raw
        if not isinstance(values, list):
            return ()
        return tuple(dict.fromkeys(str(item) for item in values if item))

    def save(self, channel_ids: Sequence[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"channel_ids": list(dict.fromkeys(channel_ids))}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
