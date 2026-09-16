"""Chromium flags for the YouTube push profile. Import before QtWebEngine.

``Local State`` (OSCrypt) must live in the same tree as ``storage/GCM Store``.
The directory is Qt ``GenericDataLocation`` / yt-rec / youtube-push — the same
path ``default_profile_directory`` uses. Do not substitute LOCALAPPDATA or
XDG_DATA_HOME; those miss the macOS profile.

GCM Store is reset only from the real GUI ``main()`` on Windows, and only
once. A failed delete does not write the marker. Stub/smoke/import leave
cookies and GCM bytes alone. ``DEPRECATED_ENDPOINT`` is Google's retired GCM
URL and is not disabled here.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from PySide6.QtCore import QStandardPaths

_GCM_RESET_MARKER = ".gcm-os-crypt-reset-1"


def profile_root() -> Path:
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericDataLocation)
    if not base:
        raise RuntimeError("알림 브라우저 저장 위치를 찾을 수 없습니다")
    return Path(base) / "yt-rec" / "youtube-push"


def reset_gcm_store_once(root: Path) -> bool:
    """Remove an unreadable GCM Store once. Cookies stay. Marker only after success."""
    marker = root / _GCM_RESET_MARKER
    if marker.exists():
        return False
    gcm = root / "storage" / "GCM Store"
    if gcm.is_dir():
        try:
            shutil.rmtree(gcm)
        except OSError:
            return False
    try:
        root.mkdir(parents=True, exist_ok=True)
        marker.write_text("gcm store reset after os_crypt decrypt failure\n", encoding="utf-8")
    except OSError:
        return False
    return True


def maybe_reset_gcm_store() -> None:
    if sys.platform != "win32":
        return
    reset_gcm_store_once(profile_root())


def configure_webengine_process() -> None:
    root = profile_root()
    root.mkdir(parents=True, exist_ok=True)
    flag = f"--user-data-dir={root}"
    current = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "").strip()
    if "--user-data-dir=" in current:
        return
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = f"{current} {flag}".strip() if current else flag
