from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from PySide6.QtCore import QStandardPaths

from yt_rec.backend.push_receiver import default_profile_directory
from yt_rec.webengine_boot import configure_webengine_process, chromium_user_data_dir, reset_gcm_store_once


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
from unittest.mock import Mock
from PySide6.QtCore import QStandardPaths
QStandardPaths.writableLocation = lambda _: sys.argv[1]
before = os.environ.get('QTWEBENGINE_CHROMIUM_FLAGS')
from yt_rec import app
root = __import__('pathlib').Path(sys.argv[1]) / 'yt-rec' / 'youtube-push'
data = root / 'storage'
after = os.environ.get('QTWEBENGINE_CHROMIUM_FLAGS') or ''
assert f'--user-data-dir={data}' in after
assert 'OsCryptAsync' in after
if before:
    assert before in after
if sys.argv[2] == 'True':
    application = SimpleNamespace(
        exec=lambda: 0, setOrganizationName=Mock(), setApplicationName=Mock(),
    )
    lock = SimpleNamespace(
        acquire=lambda: True, close=Mock(), activate_requested=SimpleNamespace(connect=Mock()),
    )
    app.QApplication = SimpleNamespace(instance=lambda: application)
    app.InstanceLock = Mock(return_value=lock)
    app.set_app_id = lambda: None
    context = SimpleNamespace(app=application, notifications=None, source=None)
    app.build_application = lambda argv, *, app: context
    desktop = SimpleNamespace(show_initial=lambda: None, show_window=lambda: None)
    app.DesktopSession = lambda _: desktop
    assert app.main(['--stub', 'empty']) == 0
    app.InstanceLock.assert_called_once_with(application)
    lock.activate_requested.connect.assert_called_once_with(desktop.show_window)
    lock.close.assert_called_once_with()
""", str(tmp_path), str(start_stub)],
        env=env, cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )
    assert {str(path.relative_to(profile)).replace('\\', '/'): path.read_bytes()
            for path in profile.rglob("*") if path.is_file()} == contents
    assert result.returncode == 0, result.stderr


def test_gcm_store_reset_writes_marker_only_after_success(tmp_path: Path) -> None:
    gcm = tmp_path / "storage" / "GCM Store"
    gcm.mkdir(parents=True)
    (gcm / "CURRENT").write_bytes(b"stale")
    cookies = tmp_path / "storage" / "Cookies"
    cookies.write_bytes(b"keep")
    assert reset_gcm_store_once(tmp_path) is True
    assert not gcm.exists()
    assert cookies.read_bytes() == b"keep"
    assert (tmp_path / ".gcm-os-crypt-reset-2").is_file()
    gcm.mkdir()
    (gcm / "CURRENT").write_bytes(b"new")
    assert reset_gcm_store_once(tmp_path) is False
    assert (gcm / "CURRENT").read_bytes() == b"new"


def test_gcm_store_reset_does_not_mark_when_delete_fails(tmp_path: Path, monkeypatch) -> None:
    gcm = tmp_path / "storage" / "GCM Store"
    gcm.mkdir(parents=True)
    (gcm / "CURRENT").write_bytes(b"stale")

    def boom(path):
        raise OSError("SYNTHETIC delete failure")

    monkeypatch.setattr("yt_rec.webengine_boot.shutil.rmtree", boom)
    assert reset_gcm_store_once(tmp_path) is False
    assert not (tmp_path / ".gcm-os-crypt-reset-2").exists()
    assert (gcm / "CURRENT").read_bytes() == b"stale"


def test_configure_points_user_data_at_storage_and_disables_oscrypt_async(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "yt_rec.webengine_boot.QStandardPaths.writableLocation",
        lambda _: str(tmp_path),
    )
    monkeypatch.delenv("QTWEBENGINE_CHROMIUM_FLAGS", raising=False)
    configure_webengine_process()
    flags = os.environ["QTWEBENGINE_CHROMIUM_FLAGS"]
    data = chromium_user_data_dir()
    assert data == tmp_path / "yt-rec" / "youtube-push" / "storage"
    assert f"--user-data-dir={data}" in flags
    assert "--disable-features=OsCryptAsync" in flags
    monkeypatch.setenv("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --disable-features=Foo")
    configure_webengine_process()
    flags = os.environ["QTWEBENGINE_CHROMIUM_FLAGS"]
    assert "--disable-gpu" in flags
    assert "Foo" in flags
    assert "OsCryptAsync" in flags
    assert flags.count("--user-data-dir=") == 1
    assert flags.count("OsCryptAsync") == 1
