"""Chromium flags for the YouTube push profile. Import before QtWebEngine.

GCM tokens live in ``storage/GCM Store`` and are encrypted with OSCrypt.
Qt stores cookies and GCM under ``…/youtube-push/storage``, but Chromium
looks for ``Local State`` in ``--user-data-dir``. Those must be the same
folder. Chromium 140's OsCryptAsync/app-bound path expects Chrome's
elevation service; QtWebEngineProcess is not chrome.exe, so DPAPI returns
``0x57`` and GCM cannot restore the token.

Disable OsCryptAsync so sync DPAPI can mint a key. Reset the GCM Store
once after that flag is in place. Cookies stay. ``DEPRECATED_ENDPOINT``
is Google's retired GCM URL and is not disabled here.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from PySide6.QtCore import QStandardPaths

_GCM_RESET_MARKER = ".gcm-os-crypt-reset-2"
_OSCRYPT_FEATURE = "OsCryptAsync"


def profile_root() -> Path:
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericDataLocation)
    if not base:
        raise RuntimeError("알림 브라우저 저장 위치를 찾을 수 없습니다")
    return Path(base) / "yt-rec" / "youtube-push"


def chromium_user_data_dir() -> Path:
    return profile_root() / "storage"


def reset_gcm_store_once(root: Path) -> bool:
    """Remove an unreadable GCM Store once. Cookies stay. Marker only after success."""
    marker = root / _GCM_RESET_MARKER
    if marker.exists():
        return False
    targets = [root / "storage" / "GCM Store", root / "GCM Store"]
    for gcm in targets:
        if not gcm.is_dir():
            continue
        try:
            shutil.rmtree(gcm)
        except OSError:
            return False
    try:
        root.mkdir(parents=True, exist_ok=True)
        marker.write_text("gcm store reset after os_crypt 0x57\n", encoding="utf-8")
    except OSError:
        return False
    return True


def maybe_reset_gcm_store() -> None:
    if sys.platform != "win32":
        return
    reset_gcm_store_once(profile_root())


def _with_user_data_and_oscrypt(current: str, user_data: Path) -> str:
    tokens = current.split() if current else []
    if not any(token.startswith("--user-data-dir=") for token in tokens):
        tokens.append(f"--user-data-dir={user_data}")
    index = next((i for i, token in enumerate(tokens) if token.startswith("--disable-features=")), None)
    if index is None:
        tokens.append(f"--disable-features={_OSCRYPT_FEATURE}")
        return " ".join(tokens)
    names = [name for name in tokens[index].split("=", 1)[1].split(",") if name]
    if _OSCRYPT_FEATURE not in names:
        names.append(_OSCRYPT_FEATURE)
        tokens[index] = "--disable-features=" + ",".join(names)
    return " ".join(tokens)


def configure_webengine_process() -> None:
    root = profile_root()
    data = chromium_user_data_dir()
    root.mkdir(parents=True, exist_ok=True)
    data.mkdir(parents=True, exist_ok=True)
    current = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "").strip()
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = _with_user_data_and_oscrypt(current, data)
