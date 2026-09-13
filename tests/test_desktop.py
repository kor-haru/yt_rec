from __future__ import annotations

import plistlib
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PySide6.QtWidgets import QMessageBox, QSystemTrayIcon

from yt_rec import desktop
from yt_rec.app import AppContext, DesktopSession, NotificationSession
from yt_rec.backend.selection import MemorySeenStore, MemorySelectionStore
from yt_rec.backend.source import BackendSource
from yt_rec.recording.options import RecordingOptions
from yt_rec.state.events import RecordingStarted, SettingsChanged, SettingsSaveFailed
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
