"""QtWebEngine/Chromium process flags. Import before any QtWebEngine module.

GCM tokens live in the profile GCM Store and are encrypted with Chromium
OSCrypt. If ``Local State`` is in a different directory from that store, or
the DPAPI blob is unreadable, Chromium logs ``Failed to decrypt (0x57)`` and
``Failed to restore security token``. Point ``--user-data-dir`` at the same
folder as the named profile, and reset the GCM Store once so a new token can
be written. ``DEPRECATED_ENDPOINT`` is Google's retired GCM URL; this module
does not disable GCM, because push still uses it.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

_GCM_RESET_MARKER = ".gcm-os-crypt-reset-1"


def profile_root() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
    else:
        base = os.environ.get("XDG_DATA_HOME")
    if not base:
        base = str(Path.home() / ".local" / "share") if sys.platform != "win32" else str(Path.home())
    return Path(base) / "yt-rec" / "youtube-push"


def reset_gcm_store_once(root: Path) -> bool:
    """Remove an unreadable GCM Store once. Cookies and YouTube login stay."""
    marker = root / _GCM_RESET_MARKER
    if marker.exists():
        return False
    gcm = root / "storage" / "GCM Store"
    if gcm.is_dir():
        shutil.rmtree(gcm, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    marker.write_text("gcm store reset after os_crypt decrypt failure\n", encoding="utf-8")
    return True


def maybe_reset_gcm_store() -> None:
    if os.environ.get("PYTEST_CURRENT_TEST"):
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
