"""Normal application wiring with fake native input, API and recording I/O.

These tests do not prove delivery by YouTube; receiver origin/ID validation is
covered separately. The app still uses the real queued source/controller/recorder.
"""

from __future__ import annotations

import time
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QPushButton

from test_notification_source import VIDEO, flush, notice, until
from test_notification_source import production_backend as production_backend
from yt_rec import app as application
from yt_rec.backend.notifications import LiveNotification
from yt_rec.state import events as ev
from yt_rec.state.models import ConnectionState, NotificationStatus


@pytest.fixture
def notification_app(production_backend, fake_push_receiver, monkeypatch, qapp, window_settings):
    captured = []

    def factory(**kwargs):
        assert kwargs == {"event_only": True}
        result = production_backend(**kwargs)
        captured.append(result)
        return result[0]

    monkeypatch.setattr(application, "create_backend_source", factory)
    context = application.build_application(["--emit-interval-ms", "0"], app=qapp, settings=window_settings)
    until(qapp, lambda: context.state.connection is ConnectionState.CONNECTED)
    flush(context.source, qapp)
    try:
        yield context, captured[0]
    finally:
        context.notifications.stop()
        context.window.close()


def test_normal_app_never_polls_on_start_selections_settings_or_ticks(notification_app, qapp):
    context, (source, api, engines, selected, _, _) = notification_app
    assert source._controller.event_only
    assert source._poll_timer is None
    assert context.notifications.receiver.starts == 1
    assert "수신 이력 없음" in context.window.notification_label.text()
    assert "주기 확인 없음" == context.window.next_check_label.text()
    context.state.set_watched_channels(["UC1", "UC2"])
    context.state.update_settings(max_recordings=2, poll_interval_seconds=30)
    context.state.refresh_subscriptions()
    for _ in range(5):
        source.tick()
    flush(source, qapp)
    assert selected.load() == ("UC1", "UC2")
    assert api.find_calls == api.get_calls == []
    assert not engines and source._poll_timer is None


def test_native_notice_reaches_real_worker_engine_progress_and_seen(notification_app, qapp):
    context, (source, api, engines, _, seen, _) = notification_app
    receiver = context.notifications.receiver
    receiver.notification_received.emit(notice())
    until(qapp, lambda: VIDEO in engines)
    receiver.notification_received.emit(notice())
    engines[VIDEO].actions.put(("progress", 4096))
    until(qapp, lambda: context.state.recordings and context.state.recordings[0].reported_bytes == 4096)
    assert api.get_calls == [VIDEO] and api.find_calls == []
    assert all(thread is not qapp.thread() for thread in api.network_threads)
    assert context.notifications._received
    receiver.inspect_registration()
    assert "이 세션에서 수신 이력 있음" in context.state.notification.detail
    engines[VIDEO].actions.put(("finish", True))
    until(qapp, lambda: seen.is_done(VIDEO))
    receiver.notification_received.emit(notice())
    flush(source, qapp)
    assert api.get_calls == [VIDEO]
    assert any("첫 미디어=" in log.message and "첫 미디어=None" not in log.message and "coverage=unknown" in log.message
               for log in context.state.logs if log.source == "notification")


def test_receiver_error_never_enables_discovery(notification_app, qapp):
    context, (source, api, engines, _, _, _) = notification_app
    context.notifications.receiver.status_changed.emit("error", "연결 오류: token=secret")
    assert "secret" not in context.window.notification_label.text()
    assert context.window.watch_badge.text() == "알림 수신 오류"
    source.tick()
    context.state.update_settings(max_recordings=2)
    flush(source, qapp)
    assert source._controller.event_only and source._poll_timer is None
    assert api.find_calls == api.get_calls == [] and not engines


def test_receiver_start_failure_is_visible_without_polling(production_backend, fake_push_receiver, monkeypatch, qapp, window_settings):
    result = production_backend()
    source, api, engines, _, _, _ = result
    monkeypatch.setattr(application, "create_backend_source", lambda **_: source)
    monkeypatch.setattr(fake_push_receiver, "start", lambda _: (_ for _ in ()).throw(RuntimeError("start failed")))
    context = application.build_application(["--emit-interval-ms", "0"], settings=window_settings)
    try:
        flush(source, qapp)
        assert context.state.notification.code == "error"
        assert "시작하지 못했습니다" in context.window.notification_label.text()
        assert source._poll_timer is None and source._controller.event_only
        assert not engines and api.find_calls == api.get_calls == []
    finally:
        context.notifications.stop()
        context.window.close()


def test_synthetic_notice_cannot_start_through_app_adapter(notification_app, qapp):
    context, (source, api, engines, _, _, _) = notification_app
    context.notifications.receiver.notification_received.emit(LiveNotification(VIDEO, time.time()))
    flush(source, qapp)
    assert api.get_calls == [] and not engines
    assert not context.notifications._received


def test_browser_controls_work_without_oauth_and_closing_keeps_receiver(notification_app, qapp, monkeypatch):
    context, (source, _, _, _, _, _) = notification_app
    context.state.disconnect_account()
    until(qapp, lambda: context.state.connection is ConnectionState.DISCONNECTED)
    context.window.notification_login_button.click()
    receiver = context.notifications.receiver
    browser = context.notifications.browser
    qapp.processEvents()
    assert receiver.browser_opens == 1 and browser.isVisible()
    assert context.state.notification.code == "login_required"
    inspect = Mock(wraps=receiver.inspect_registration)
    monkeypatch.setattr(receiver, "inspect_registration", inspect)
    browser.close()
    qapp.processEvents()
    inspect.assert_called_once_with()
    assert context.state.notification.code == "ready"
    assert not browser.isVisible() and receiver.stops == 0
    context.window.notification_settings_button.click()
    assert context.notifications.browser is browser
    assert receiver.settings_opens == 1 and browser.isVisible()
    for button in browser.findChildren(QPushButton):
        if button.text() == "알림 상태 확인":
            button.click()
    assert inspect.call_count == 2
    assert context.state.notification.code == "ready"
    flush(source, qapp)
    context.notifications.stop()
    browser.close()
    assert inspect.call_count == 2  # Closing during shutdown cannot restart inspection.


def test_main_inspection_is_one_shot_without_browser_navigation_or_oauth(notification_app, qapp, monkeypatch):
    context, (source, api, engines, _, _, _) = notification_app
    context.state.disconnect_account()
    until(qapp, lambda: context.state.connection is ConnectionState.DISCONNECTED)
    receiver = context.notifications.receiver
    inspect = Mock(wraps=receiver.inspect_registration)
    monkeypatch.setattr(receiver, "inspect_registration", inspect)
    context.window.notification_check_button.click()
    inspect.assert_called_once_with()
    assert context.notifications.browser is None
    assert receiver.browser_opens == receiver.settings_opens == 0
    flush(source, qapp)
    assert api.find_calls == api.get_calls == [] and not engines
    assert source._poll_timer is None
    context.notifications.stop()
    context.window.notification_check_button.click()
    assert inspect.call_count == 1


@pytest.mark.parametrize("system,opened,expected", [
    ("win32", True, "열기를 요청"), ("win32", False, "열지 못했습니다"),
    ("darwin", False, "Windows에서 지원"), ("linux", False, "Windows에서 지원"),
])
def test_system_notification_button_only_opens_fixed_uri(notification_app, monkeypatch, system, opened, expected):
    context, (_, api, engines, _, _, _) = notification_app
    open_url = Mock(return_value=opened)
    monkeypatch.setattr(application.sys, "platform", system)
    monkeypatch.setattr(application.QDesktopServices, "openUrl", open_url)
    status = context.state.notification
    context.window.system_notification_settings_button.click()
    assert expected in context.window.statusBar().currentMessage()
    if system == "win32":
        assert open_url.call_count == 1
        assert open_url.call_args.args[0].toString() == "ms-settings:notifications"
    else:
        open_url.assert_not_called()
    assert context.state.notification == status  # Opening settings proves no permission.
    assert context.notifications.browser is None
    assert api.find_calls == api.get_calls == [] and not engines


@pytest.mark.parametrize("code,text", [
    ("connecting", "수신기 연결 중"), ("checking", "알림 확인 중"),
    ("stopped", "알림 수신 종료"), ("worker_missing", "수신 등록 없음"),
    ("worker_inactive", "수신기 준비 중"), ("unsubscribed", "푸시 등록 없음"),
    ("permission_required", "알림 권한 필요"), ("error", "알림 수신 오류"),
])
def test_notification_badge_distinguishes_setup_from_lifecycle(notification_app, code, text):
    context, _ = notification_app
    context.notifications.receiver.status_changed.emit(code, "고정 상태 안내")
    assert context.window.watch_badge.text() == text
    assert context.window.notification_check_button.isEnabled() is (code not in ("connecting", "checking", "stopped"))


def test_shutdown_stops_receiver_and_blocks_late_native_input(notification_app, monkeypatch, qapp):
    context, (source, api, engines, _, _, _) = notification_app
    monkeypatch.setattr(application, "load_settings", lambda: source._controller._recorder.options)
    monkeypatch.setattr(application.QSystemTrayIcon, "isSystemTrayAvailable", lambda: False)
    quit_on_last = qapp.quitOnLastWindowClosed()
    desktop = application.DesktopSession(context)
    receiver = context.notifications.receiver
    try:
        desktop.shutdown()
        receiver.notification_received.emit(notice())
        until(qapp, lambda: desktop.stopped)
        assert receiver.stops == 1 and source._stopping
        assert api.get_calls == [] and not engines
        context.notifications.stop()
        assert receiver.stops == 1
    finally:
        qapp.setQuitOnLastWindowClosed(quit_on_last)


def test_receiver_status_is_plain_text_and_ready_is_not_delivery(notification_app, theme, qapp):
    context, _ = notification_app
    window = context.window
    window.show()
    initial_width = window.width()
    for code in ("login_required", "permission_required", "unsubscribed", "error", "ready"):
        context.state.post_event(ev.NotificationStatusChanged(NotificationStatus(code, "<b>수신 상태</b> token=private")))
        qapp.processEvents()
        assert window.notification_label.textFormat() is Qt.TextFormat.PlainText
        assert "private" not in window.notification_label.text()
        assert window.width() == initial_width
        if code != "ready":
            assert "알림 대기" not in window.watch_badge.text()
    context.notifications.receiver.inspect_registration()
    assert "수신 이력 없음" in window.notification_label.text()


def test_settings_hide_polling_and_preserve_previous_value(notification_app, qapp):
    context, (_, api, _, _, _, _) = notification_app
    original = context.state.settings.poll_interval_seconds
    dialog = context.window.open_settings()
    assert not hasattr(dialog, "poll_interval_spin")
    assert any("주기적으로 방송을 조회하지 않습니다" in label.text() for label in dialog.findChildren(QLabel))
    dialog.max_recordings_spin.setValue(3)
    dialog.save_button.click()
    until(qapp, lambda: context.state.settings.max_recordings == 3)
    assert context.state.settings.poll_interval_seconds == original
    assert api.find_calls == []


def test_real_browser_view_receives_remaining_height(notification_app, monkeypatch, qapp, tmp_path):
    """Real QtWebEngine layout, with an off-record about:blank page and no login."""
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
    from PySide6.QtWebEngineWidgets import QWebEngineView

    context, _ = notification_app
    profile = QWebEngineProfile()
    page = QWebEnginePage(profile)
    context.notifications.receiver.page = page
    monkeypatch.setattr(application, "QWebEngineView", QWebEngineView)
    try:
        context.window.notification_login_button.click()
        browser = context.notifications.browser
        qapp.processEvents()
        view = browser.findChild(QWebEngineView)
        assert view.height() >= browser.height() - 130
        assert browser.grab().save(str(tmp_path / "notification-browser.png"))
        assert profile.isOffTheRecord() and page.url().isEmpty()
    finally:
        context.notifications.stop()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        profile.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_native_profile_is_destroyed_after_page_on_application_shutdown(production_backend, monkeypatch, qapp, tmp_path, window_settings):
    """Actual receiver/profile lifetime, but no YouTube navigation or user data."""
    from PySide6.QtCore import QCoreApplication, QEvent
    from yt_rec.backend import push_receiver

    source, api, engines, _, _, _ = production_backend()
    monkeypatch.setattr(application, "create_backend_source", lambda **_: source)
    monkeypatch.setattr(push_receiver, "default_profile_directory", lambda: tmp_path / "push-profile")
    monkeypatch.setattr(push_receiver.YouTubePushReceiver, "start", lambda _: None)
    monkeypatch.setattr(push_receiver.YouTubePushReceiver, "open_browser", lambda _: None)
    # 이 검사는 내장 QtWebEngine 수신기의 수명주기를 본다. 기본값(Chrome)이 아니라
    # 그 경로를 명시적으로 고른다 (#79 에서도 내장 경로는 지우지 않는다).
    options = source._controller._recorder.options.with_(notification_receiver="qtwebengine")
    monkeypatch.setattr(application, "load_settings", lambda: options)
    monkeypatch.setattr(application.QSystemTrayIcon, "isSystemTrayAvailable", lambda: False)
    context = application.build_application(["--emit-interval-ms", "0"], settings=window_settings)
    receiver = context.notifications.receiver
    destroyed = []
    receiver.page.destroyed.connect(lambda: destroyed.append("page"))
    receiver.profile.destroyed.connect(lambda: destroyed.append("profile"))
    quit_on_last = qapp.quitOnLastWindowClosed()
    desktop = application.DesktopSession(context)
    try:
        context.window.notification_login_button.click()
        assert receiver.page.url().isEmpty()
        assert receiver.profile.persistentStoragePath().replace("\\", "/").startswith(tmp_path.as_posix())
        desktop.shutdown()
        receiver.notification_received.emit(notice())
        until(qapp, lambda: desktop.stopped)
        # Equivalent to the final app cleanup if quit arrives before DeferredDelete.
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        assert destroyed == ["page", "profile"]
        assert not engines and api.find_calls == api.get_calls == []
    finally:
        context.notifications.stop()
        context.window.close()
        qapp.setQuitOnLastWindowClosed(quit_on_last)
