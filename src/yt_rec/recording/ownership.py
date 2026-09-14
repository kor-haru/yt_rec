"""Same-user, video-wide ownership shared by GUI, CLI and recovery.

OS locks disappear on process exit. Lock files intentionally remain: unlinking
one would let a new opener lock a different inode while another owner is active.
Older running versions do not hold this lease and cannot be protected retroactively.
The existing engine does not yet guarantee descendant termination on parent crash;
this lease alone cannot prove an orphaned yt-dlp/ffmpeg process has stopped.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
from pathlib import Path
from typing import BinaryIO

from .options import default_settings_path


class RecordingOwnedError(RuntimeError):
    """Another recording/recovery already owns this video; leave it untouched."""


def validate_video_id(video_id: str) -> None:
    # Also support short synthetic IDs used by the local engine/CLI tests.
    if not isinstance(video_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", video_id):
        raise ValueError("영상 ID 형식이 올바르지 않습니다")


def default_ownership_root() -> Path:
    return default_settings_path().parent / "recording-locks"


class VideoLease:
    def __init__(self, video_id: str, *, root: Path | None = None) -> None:
        validate_video_id(video_id)
        # Preserve case-sensitive video identity on case-insensitive filesystems.
        key = hashlib.sha256(video_id.encode("ascii")).hexdigest()
        self.path = (root if root is not None else default_ownership_root()) / f"{key}.lock"
        self._file: BinaryIO | None = None

    def acquire(self) -> None:
        if self._file is not None:
            raise RuntimeError("lease already acquired")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                # Windows permits locking a byte beyond EOF: do not write to a
                # byte another process may have locked, even for initialization.
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise RecordingOwnedError("다른 녹화 또는 복구가 이 영상을 사용 중입니다") from exc
            raise  # Storage/permission failures must fail closed, not disable locking.
        self._file = handle

    def close(self) -> None:
        if self._file is not None:
            handle, self._file = self._file, None
            handle.close()  # Closing releases the OS lock, including on exceptions.

    def __enter__(self) -> VideoLease:
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
