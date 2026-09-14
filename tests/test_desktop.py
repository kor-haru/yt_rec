from __future__ import annotations

import ctypes
import json
import os
import plistlib
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QDialog, QMessageBox, QSystemTrayIcon

from yt_rec import app as application, desktop
from yt_rec.app import AppContext, DesktopSession, NotificationSession
from yt_rec.backend.selection import MemorySeenStore, MemorySelectionStore
from yt_rec.backend.source import BackendSource
from yt_rec.recording.options import RecordingOptions
from yt_rec.state.events import RecordingStarted, SettingsChanged, SettingsSaveFailed
from yt_rec.state.models import CompletedRecording, CompletionStatus, Recording
from yt_rec.ui.main_window import MainWindow


@pytest.mark.parametrize("backend", [False, True])
def test_completed_shutdown_leaves_real_qt_event_loop(tmp_path, backend):
    script = '''
import json, sys
from pathlib import Path
from types import SimpleNamespace
from PySide6.QtCore import QSettings, QTimer
from PySide6.QtWidgets import QApplication, QSystemTrayIcon
from yt_rec import app as module
from yt_rec.app import AppContext, DesktopSession
from yt_rec.backend.selection import MemorySelectionStore, MemorySeenStore
from yt_rec.backend.source import BackendSource
from yt_rec.recording.options import RecordingOptions
from yt_rec.state.store import AppState
from yt_rec.state.stub import StubEventSource
from yt_rec.ui.main_window import MainWindow
from yt_rec.ui.settings_store import WindowSettings

directory = Path(sys.argv[1])
options = RecordingOptions(output_dir=directory)
module.load_settings = lambda: options
QSystemTrayIcon.isSystemTrayAvailable = lambda: False
app = QApplication([])
state = AppState(emit_interval_ms=0)
window = MainWindow(state, settings=WindowSettings(QSettings(str(directory / "window.ini"), QSettings.IniFormat)))
if sys.argv[2] == "True":
    controller = SimpleNamespace(
        event_only=True, _options=options, _recorder=None,
        _selection=MemorySelectionStore(), _seen=MemorySeenStore(), start=lambda: None,
    )
    source = BackendSource(controller, background=True)
    source.start()
else:
    source = StubEventSource()
desktop = DesktopSession(AppContext(app, state, window, source))
desktop.show_initial()
result = {"watchdog": False}

def cleanup_failed_test():
    result.update(watchdog=True, stopped=desktop.stopped, exiting=window.exiting,
                  central_enabled=window.centralWidget().isEnabled())
    # Release only this test window after capturing the quit-veto evidence.
    window.desktop_managed = False
    window.close()
    app.quit()

QTimer.singleShot(1000, cleanup_failed_test)
QTimer.singleShot(0, window.request_exit)
result["exit_code"] = app.exec()
source.stop()
result["stopped"] = desktop.stopped
result["worker_alive"] = bool(getattr(source, "_worker", None) and source._worker.is_alive())
print(json.dumps(result))
'''
    env = os.environ.copy()
    for key in ("APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOME",
                "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        directory = tmp_path / key
        directory.mkdir()
        env[key] = str(directory)
    env["QT_QPA_PLATFORM"] = "offscreen"
    process = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), str(backend)],
        env=env, capture_output=True, text=True, timeout=10,
    )
    assert process.returncode == 0, process.stderr
    result = json.loads(process.stdout)
    assert result == {"watchdog": False, "exit_code": 0, "stopped": True, "worker_alive": False}


@pytest.mark.parametrize("frozen", [False, True])
def test_recording_icon_is_shared_by_application_dialog_window_and_tray(
    qapp, window_settings, monkeypatch, tmp_path, frozen,
):
    if frozen:
        package = tmp_path / "bundle/_internal/yt_rec"
        shutil.copytree(Path(application.__file__).with_name("assets"), package / "assets")
        monkeypatch.setattr(application, "__file__", str(package / "app.py"))
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", str(package.parent), raising=False)
    original = qapp.windowIcon()
    qapp.setWindowIcon(QIcon())
    tray_type = MagicMock()
    tray_type.isSystemTrayAvailable.return_value = True
    monkeypatch.setattr(application, "QSystemTrayIcon", tray_type)
    monkeypatch.setattr(application, "load_settings", lambda: SimpleNamespace(start_hidden=True))
    context = application.build_application(["--stub", "empty"], app=qapp, settings=window_settings)
    dialog = QDialog()
    try:
        icon = qapp.windowIcon()
        assert not icon.isNull()
        assert context.window.windowIcon().cacheKey() == icon.cacheKey()
        assert dialog.windowIcon().cacheKey() == icon.cacheKey()
        session = DesktopSession(context)
        assert context.window.windowIcon().cacheKey() == icon.cacheKey()
        assert tray_type.call_args.args[0].cacheKey() == icon.cacheKey()
        assert session.tray is tray_type.return_value
    finally:
        dialog.close()
        context.source.stop()
        context.window.desktop_managed = False
        context.window.close()
        qapp.setWindowIcon(original)


@pytest.mark.parametrize("system,result", [("win32", 0), ("win32", -1), ("linux", 0)])
def test_taskbar_identity_is_windows_only_and_checks_hresult(monkeypatch, system, result):
    setter = MagicMock(return_value=result)
    loader = MagicMock(return_value=SimpleNamespace(SetCurrentProcessExplicitAppUserModelID=setter))
    monkeypatch.setattr(ctypes, "WinDLL", loader, raising=False)
    monkeypatch.setattr(sys, "platform", system)
    if result < 0:
        with pytest.raises(OSError, match="작업 표시줄"):
            desktop.set_app_id()
    else:
        desktop.set_app_id()
    if system == "win32":
        setter.assert_called_once_with("io.github.kor-haru.yt-rec")
        assert setter.argtypes == [ctypes.c_wchar_p]
        assert setter.restype is ctypes.c_long
    else:
        loader.assert_not_called()


def test_taskbar_identity_precedes_qapplication_lookup(qapp, window_settings, monkeypatch):
    calls = []
    original = qapp.windowIcon()
    monkeypatch.setattr(application, "set_app_id", lambda: calls.append("id"))

    def instance():
        assert calls == ["id"]
        return qapp

    monkeypatch.setattr(application.QApplication, "instance", instance)
    context = application.build_application(["--stub", "empty"], settings=window_settings)
    context.source.stop()
    context.window.close()
    qapp.setWindowIcon(original)


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


def _session(qapp, state, window_settings, monkeypatch, available, source=None, options=None):
    tray_type = MagicMock()
    tray_type.ActivationReason = QSystemTrayIcon.ActivationReason
    tray_type.isSystemTrayAvailable.return_value = available
    monkeypatch.setattr("yt_rec.app.QSystemTrayIcon", tray_type)
    monkeypatch.setattr("yt_rec.app.load_settings", lambda: options or SimpleNamespace(start_hidden=True, notifications_enabled=True))
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


@pytest.mark.parametrize("available", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
def test_minimize_option_keeps_source_and_receiver_running(
    qapp, state, window_settings, monkeypatch, tmp_path, fake_push_receiver, available, enabled,
):
    source = MagicMock()
    options = RecordingOptions(output_dir=tmp_path, minimize_to_tray=enabled)
    window, session, _tray = _session(
        qapp, state, window_settings, monkeypatch, available, source, options,
    )
    notifications = NotificationSession(state, source, window)
    session.context.notifications = notifications
    notifications.start()
    session.show_initial()
    assert window.isVisible()  # minimize preference is not start_hidden
    window.showMinimized()  # exercise Qt's real WindowStateChange delivery
    qapp.processEvents()
    assert window.isMinimized()
    assert window.isHidden() is (available and enabled)
    state.apply(RecordingStarted(Recording("id", "Live")))
    assert state.recordings and window._countdown_repaint_timer.isActive()
    source.stop.assert_not_called()
    source.begin_shutdown.assert_not_called()
    assert notifications.receiver.starts == 1 and notifications.receiver.stops == 0
    assert not notifications._stopped and not session.stopped and not window.exiting
    session.show_window()
    qapp.processEvents()
    assert window.isVisible() and not window.isMinimized()
    notifications.stop()
    window.desktop_managed = False
    window.close()


@pytest.mark.parametrize("maximized", [False, True])
@pytest.mark.parametrize("restore", ["click", "double_click", "menu"])
def test_minimize_tray_restore_preserves_window_state(
    qapp, state, window_settings, monkeypatch, tmp_path, maximized, restore,
):
    options = RecordingOptions(output_dir=tmp_path, minimize_to_tray=True)
    window, session, tray = _session(qapp, state, window_settings, monkeypatch, True, options=options)
    window.showMaximized() if maximized else window.showNormal()
    qapp.processEvents()
    window.showMinimized()
    qapp.processEvents()
    assert window.isHidden()
    if restore == "menu":
        tray.setContextMenu.call_args.args[0].actions()[0].trigger()
    else:
        reason = (QSystemTrayIcon.ActivationReason.Trigger if restore == "click"
                  else QSystemTrayIcon.ActivationReason.DoubleClick)
        tray.activated.connect.call_args.args[0](reason)
    qapp.processEvents()
    assert window.isVisible() and not window.isMinimized()
    assert window.isMaximized() is maximized
    window.desktop_managed = False
    window.close()


def test_saved_minimize_setting_applies_immediately_not_on_failure(
    qapp, state, window_settings, monkeypatch, tmp_path,
):
    options = RecordingOptions(output_dir=tmp_path)
    window, session, _tray = _session(qapp, state, window_settings, monkeypatch, True, options=options)
    session.show_initial()
    state.apply(SettingsChanged(options.with_(minimize_to_tray=True)))
    assert window.isVisible() and not window.isMinimized()
    window.showMinimized()
    qapp.processEvents()
    assert window.isHidden()
    state.apply(SettingsSaveFailed("disk full"))
    assert window.isHidden() and session.options.minimize_to_tray is True
    state.apply(SettingsChanged(options))
    assert window.isHidden()  # Changing preferences must not reopen a hidden window.
    session.show_window()
    window.showMinimized()
    qapp.processEvents()
    assert window.isVisible() and window.isMinimized()
    state.apply(SettingsChanged(options.with_(minimize_to_tray=True)))
    assert window.isHidden() and window.isMinimized()
    session.show_window()
    window.desktop_managed = False
    window.close()


@pytest.mark.parametrize("settings_event", [False, True])
def test_disabled_minimize_option_never_reopens_a_close_to_tray_window(
    qapp, state, window_settings, monkeypatch, tmp_path, settings_event,
):
    options = RecordingOptions(output_dir=tmp_path)
    window, session, _tray = _session(qapp, state, window_settings, monkeypatch, True, options=options)
    session.show_initial()
    window.showMinimized()
    if settings_event:
        qapp.processEvents()
    window.close()
    assert window.isHidden()
    if settings_event:
        state.apply(SettingsChanged(options.with_(notifications_enabled=False)))
    qapp.processEvents()
    assert window.isHidden() and not session.stopped
    session.show_window()
    window.desktop_managed = False
    window.close()


@pytest.mark.parametrize("change", ["restore", "disable", "tray_lost", "shutdown"])
def test_deferred_minimize_rechecks_before_hiding(
    qapp, state, window_settings, monkeypatch, tmp_path, change,
):
    options = RecordingOptions(output_dir=tmp_path, minimize_to_tray=True)
    window, session, _tray = _session(qapp, state, window_settings, monkeypatch, True, options=options)
    session.show_initial()
    window.showMinimized()
    if change == "restore":
        session.show_window()
    elif change == "disable":
        state.apply(SettingsChanged(options.with_(minimize_to_tray=False)))
    elif change == "tray_lost":
        monkeypatch.setattr("yt_rec.app.QSystemTrayIcon.isSystemTrayAvailable", lambda: False)
    else:
        session.shutdown()
    qapp.processEvents()
    assert window.isVisible()
    assert window.isMinimized() is (change != "restore")
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
    assert window.desktop_managed
    assert calls[0] == ("begin", gui_thread)
    qapp.processEvents()
    release.set()
    session._shutdown.join(3)
    session._check_shutdown()
    assert session.stopped
    assert calls[1][1] != gui_thread
    window.desktop_managed = False
    window.close()


@pytest.mark.parametrize("available", [False, True])
@pytest.mark.parametrize("event_only", [False, True])
def test_failed_shutdown_stays_disabled_and_menu_retry_waits_for_recorder(
    qapp, state, window_settings, monkeypatch, tmp_path, fake_push_receiver,
    available, event_only,
):
    started, retry_entered, release = (threading.Event() for _ in range(3))
    joins = []

    def join_recordings(*, timeout):
        joins.append(timeout)
        if len(joins) == 1:
            raise RuntimeError("test recorder cleanup failure")
        retry_entered.set()
        assert release.wait(3), "test recorder was not released"

    recorder = MagicMock()
    recorder.join_all.side_effect = join_recordings
    controller = SimpleNamespace(
        event_only=event_only, _options=RecordingOptions(output_dir=tmp_path),
        _recorder=recorder, _selection=MemorySelectionStore(), _seen=MemorySeenStore(),
        start=started.set, tick=MagicMock(), handle_command=MagicMock(),
    )
    source = BackendSource(controller, background=True, poll_interval_ms=60000)
    state.attach(source)
    window, session, tray = _session(qapp, state, window_settings, monkeypatch, available, source)
    notifications = NotificationSession(state, source, window)
    session.context.notifications = notifications
    notifications.start()
    source.start()
    assert started.wait(3)
    original_timer = source._poll_timer
    original_worker = source._worker
    state.apply(RecordingStarted(Recording("finishing", "Live")))
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.StandardButton.Yes)
    try:
        window.request_exit()
        session._shutdown.join(3)
        session._check_shutdown()
        qapp.processEvents()
        assert source._stopping and not original_worker.is_alive()
        assert notifications._stopped and notifications.receiver.stops == 1
        assert state.notification.code == "stopped"
        assert not window.centralWidget().isEnabled()
        assert window.desktop_managed
        assert "새 알림" in window.statusBar().currentMessage()
        assert "다시 시도" in window.statusBar().currentMessage()
        assert not session.stopped and not session._shutdown_timer.isActive()
        qapp.quit.assert_not_called()
        # A queued/stale timeout must not treat a failed attempt as success.
        session._check_shutdown()
        qapp.quit.assert_not_called()
        source.handle_command(object())
        source.tick()
        notifications.start()
        notifications.receiver.notification_received.emit(object())
        assert not source.receive_notification(object(), trusted=True)
        controller.handle_command.assert_not_called()
        controller.tick.assert_not_called()
        recorder.start.assert_not_called()
        assert source._poll_timer is original_timer
        assert original_timer is None or not original_timer.isActive()
        assert notifications.receiver.starts == 1
        assert state.recordings[0].recording_id == "finishing"
        # Reuse the real menu action; disabled content must not trap the exit.
        exit_action = window.menuBar().actions()[0].menu().actions()[0]
        assert exit_action.isEnabled()
        exit_action.trigger()
        assert retry_entered.wait(3)
        assert session._shutdown_timer.isActive()
        session._check_shutdown()
        qapp.processEvents()
        assert window.exiting and not session.stopped
        assert window.desktop_managed
        assert not window.centralWidget().isEnabled()
        qapp.quit.assert_not_called()
        release.set()
        session._shutdown.join(3)
        session._check_shutdown()
        assert joins == [None, None]
        assert session.stopped and not session._shutdown_timer.isActive()
        qapp.quit.assert_called_once()
        assert source._worker is original_worker and not original_worker.is_alive()
        assert notifications.receiver.starts == notifications.receiver.stops == 1
        if available:
            tray.hide.assert_called_once()
    finally:
        release.set()
        if session._shutdown is not None:
            session._shutdown.join(3)
        source.stop()
        notifications.stop()
        state.detach(source)
        window.desktop_managed = False
        window.close()
