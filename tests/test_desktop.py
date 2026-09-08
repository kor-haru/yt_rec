from __future__ import annotations

import plistlib
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PySide6.QtWidgets import QMessageBox

from yt_rec import desktop
from yt_rec.app import AppContext, DesktopSession
from yt_rec.state.events import RecordingStarted
from yt_rec.state.models import CompletedRecording, CompletionStatus, Recording
from yt_rec.ui.main_window import MainWindow


def test_windows_startup_only_changes_our_value(monkeypatch):
    registry = MagicMock()
    monkeypatch.setitem(sys.modules, "winreg", registry)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(desktop, "startup_command", lambda: [r"C:\My App\yt-rec.exe"])
    desktop.set_autostart(True)
    assert registry.SetValueEx.call_args.args[1:] == (
        "yt-rec", 0, registry.REG_SZ, '"C:\\My App\\yt-rec.exe"'
    )
    desktop.set_autostart(False)
    assert registry.DeleteValue.call_args.args[1] == "yt-rec"


@pytest.mark.parametrize("system", ["darwin", "linux"])
def test_native_startup_files_round_trip(system, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", system)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    command = [str(tmp_path / "App Space/yt-rec"), "-m", "yt_rec"]
    monkeypatch.setattr(desktop, "startup_command", lambda: command)
    desktop.set_autostart(True)
    if system == "darwin":
        target = tmp_path / "Library/LaunchAgents/io.github.kor-haru.yt-rec.plist"
        assert plistlib.loads(target.read_bytes())["ProgramArguments"] == command
    else:
        target = tmp_path / ".config/autostart/yt-rec.desktop"
        assert "Exec=" + " ".join(map(desktop._desktop_argument, command)) in target.read_text()
    desktop.set_autostart(False)
    assert not target.exists()
    desktop.set_autostart(False)


def test_desktop_exec_quotes_and_rejects_newlines():
    assert desktop._desktop_argument('a%b $c"d') == '"a%%b \\\\$c\\\\"d"'
    with pytest.raises(ValueError):
        desktop._desktop_argument("path\nExec=bad")


def _session(qapp, state, window_settings, monkeypatch, available, source=None):
    tray_type = MagicMock()
    tray_type.isSystemTrayAvailable.return_value = available
    monkeypatch.setattr("yt_rec.app.QSystemTrayIcon", tray_type)
    monkeypatch.setattr("yt_rec.app.load_settings", lambda: SimpleNamespace(start_hidden=True, notifications_enabled=True))
    monkeypatch.setattr(qapp, "quit", MagicMock())
    window = MainWindow(state, settings=window_settings)
    session = DesktopSession(AppContext(qapp, state, window, source))
    return window, session, tray_type.return_value


def test_tray_close_hides_and_explicit_exit_confirms(qapp, state, window_settings, monkeypatch):
    window, session, _tray = _session(qapp, state, window_settings, monkeypatch, True)
    session.show_initial()
    assert not window.isVisible()
    session.show_window()
    window.close()
    assert not window.isVisible() and not session.stopped
    state.apply(RecordingStarted(Recording("id", "Live")))
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.No)
    window.request_exit()
    assert not window.exiting
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
    window.request_exit()
    assert session.stopped
    window.desktop_managed = False
    window.close()


def test_without_tray_hidden_start_still_shows_and_close_exits(qapp, state, window_settings, monkeypatch):
    window, session, _tray = _session(qapp, state, window_settings, monkeypatch, False)
    session.show_initial()
    assert window.isVisible()
    window.close()
    assert session.stopped
    window.desktop_managed = False
    window.close()


def test_tray_reopen_preserves_maximized_window(qapp, state, window_settings, monkeypatch):
    window, session, _tray = _session(qapp, state, window_settings, monkeypatch, True)
    window.showMaximized()
    window.hide()
    session.show_window()
    assert window.isMaximized()
    window.desktop_managed = False
    window.close()


def test_notifications_obey_toggle_and_never_include_raw_log(qapp, state, window_settings, monkeypatch):
    window, session, tray = _session(qapp, state, window_settings, monkeypatch, True)
    session._on_errors(1, 1)
    tray.showMessage.assert_called_once()
    session._on_settings(SimpleNamespace(notifications_enabled=False))
    session._on_errors(2, 2)
    tray.showMessage.assert_called_once()
    window.desktop_managed = False
    window.close()


def test_top_error_badge_caps_display_and_preserves_exact_count(qapp, state, window_settings):
    window = MainWindow(state, settings=window_settings)
    window._on_errors(124, 123)
    assert window.log_button.text() == "로그 99+"
    assert "123" in window.log_button.toolTip()
    window._on_errors(124, 0)
    assert window.log_button.text() == "로그"
    window.close()


def test_recording_failure_notifies_but_restored_history_does_not(qapp, state, window_settings, monkeypatch):
    window, session, tray = _session(qapp, state, window_settings, monkeypatch, True)
    old = CompletedRecording("old", "old", status=CompletionStatus.FAILED,
                             finished_at=datetime.now(timezone.utc) - timedelta(days=1))
    session._on_completed((old,))
    tray.showMessage.assert_not_called()
    new = CompletedRecording("new", "new", status=CompletionStatus.FAILED,
                             finished_at=datetime.now(timezone.utc))
    session._on_completed((new, old))
    tray.showMessage.assert_called_once()
    session._on_completed((new, old))
    tray.showMessage.assert_called_once()
    window.desktop_managed = False
    window.close()


def test_shutdown_wait_does_not_block_gui(qapp, state, window_settings, monkeypatch):
    release = threading.Event()
    gui_thread = threading.get_ident()
    calls = []
    source = SimpleNamespace(
        begin_shutdown=lambda: calls.append(("begin", threading.get_ident())),
        stop=lambda: (calls.append(("stop", threading.get_ident())), release.wait(3)),
    )
    window, session, _tray = _session(qapp, state, window_settings, monkeypatch, False, source)
    window.request_exit()
    assert not session.stopped
    assert calls[0] == ("begin", gui_thread)
    qapp.processEvents()
    release.set()
    session._shutdown.join(3)
    session._check_shutdown()
    assert session.stopped
    assert calls[1][1] != gui_thread
    window.desktop_managed = False
    window.close()
