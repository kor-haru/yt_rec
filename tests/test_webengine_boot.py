from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from PySide6.QtCore import QStandardPaths

from yt_rec.backend.push_receiver import default_profile_directory


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_profile_directory_preserves_qt_location(monkeypatch, tmp_path: Path, platform):
    base = tmp_path / "Qt 사용자 데이터"
    calls = []

    def location(kind):
        calls.append(kind)
        return str(base)

    monkeypatch.setattr(QStandardPaths, "writableLocation", location)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "not-qt"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "not-qt"))
    with monkeypatch.context() as platform_patch:
        platform_patch.setattr(sys, "platform", platform)
        directory = default_profile_directory()
    assert directory == base / "yt-rec" / "youtube-push"
    assert calls == [QStandardPaths.StandardLocation.GenericDataLocation]
    assert not directory.exists()


def test_missing_qt_location_does_not_create_an_alternate_profile(monkeypatch):
    monkeypatch.setattr(QStandardPaths, "writableLocation", lambda _: "")
    with pytest.raises(RuntimeError, match="저장 위치"):
        default_profile_directory()


@pytest.mark.parametrize("start_stub", [False, True], ids=["import", "stub"])
@pytest.mark.parametrize("flags", [None, "--disable-gpu"], ids=["unset-flags", "existing-flags"])
def test_app_import_and_stub_preserve_profile(tmp_path: Path, start_stub, flags):
    """Fresh process, fake app/event loop, and dummy bytes: no browser or account."""
    profile = tmp_path / "yt-rec" / "youtube-push"
    contents = {
        "Local State": b"test-only-key",
        "storage/Cookies": b"test-only-cookie",
        "storage/user_prefs.json": b"{}",
        "storage/GCM Store/CURRENT": b"test-only-gcm",
    }
    for relative, content in contents.items():
        path = profile / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    env = os.environ.copy()
    for name in ("APPDATA", "LOCALAPPDATA", "USERPROFILE", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME"):
        env[name] = str(tmp_path)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    env["QT_QPA_PLATFORM"] = "offscreen"
    env.pop("PYTEST_CURRENT_TEST", None)  # Exercise normal startup, not a pytest-only bypass.
    env.pop("QTWEBENGINE_CHROMIUM_FLAGS", None)
    if flags is not None:
        env["QTWEBENGINE_CHROMIUM_FLAGS"] = flags
    result = subprocess.run(
        [sys.executable, "-c", """
import os
import sys
from types import SimpleNamespace
from PySide6.QtCore import QStandardPaths
QStandardPaths.writableLocation = lambda _: sys.argv[1]
before = os.environ.get('QTWEBENGINE_CHROMIUM_FLAGS')
from yt_rec import app
if sys.argv[2] == 'True':
    context = SimpleNamespace(app=SimpleNamespace(exec=lambda: 0), notifications=None, source=None)
    app.build_application = lambda argv: context
    app.DesktopSession = lambda _: SimpleNamespace(show_initial=lambda: None)
    assert app.main(['--stub', 'empty']) == 0
assert os.environ.get('QTWEBENGINE_CHROMIUM_FLAGS') == before
""", str(tmp_path), str(start_stub)],
        env=env, cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )
    assert {str(path.relative_to(profile)).replace('\\', '/'): path.read_bytes()
            for path in profile.rglob("*") if path.is_file()} == contents
    assert result.returncode == 0, result.stderr
