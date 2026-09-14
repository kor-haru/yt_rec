"""애플리케이션 진입점.

사용법::

    yt-rec                       # 실제 백엔드. 미연결이면 `연결 안 됨`
    python -m yt_rec --stub empty        # 빈 상태 더미
    python -m yt_rec --stub populated    # 채널·녹화·완료 더미
    python -m yt_rec --stub scenario     # 시작→진행→완료→오류 시나리오 재생
    python -m yt_rec --stub flood        # 초당 100건 진행 이벤트 부하
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QTimer, Qt
from PySide6.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QLabel, QMenu, QPushButton,
    QStyle, QSystemTrayIcon, QVBoxLayout,
)

from PySide6.QtWebEngineWidgets import QWebEngineView

from .backend import create_backend_source
from .logs import redact
from .recording.options import load_settings
from .state import commands as cmd, events as ev
from .state.models import CompletionStatus, LogEntry, NotificationStatus, Severity, StopReason, WatchState
from .state.store import AppState, EventSource
from .state.stub import PRESETS, StubEventSource, recording_lifecycle
from .ui.main_window import MainWindow
from .ui.settings_store import APPLICATION, ORGANIZATION, WindowSettings

# Configure/import WebEngine before the first QApplication, including --stub and
# smoke entrypoints. Importing alone creates no profile or network work.
QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)

__all__ = ["main", "build_application", "AppContext", "parse_args"]

STUB_MODES = (*PRESETS.keys(), "scenario", "flood")


@dataclass
class AppContext:
    """기동해 놓은 객체 묶음. 참조를 살려 두기 위해 호출자가 들고 있는다."""

    app: QApplication
    state: AppState
    window: MainWindow
    source: EventSource | None = None
    notifications: NotificationSession | None = None


class NotificationSession(QObject):
    """Glue the native receiver to the existing queued backend and GUI state.

    Only this adapter asserts trusted=True; it accepts no JSON/URL/probe command.
    Closing the browser hides it. The receiver lives until the application exits.
    """

    def __init__(self, state: AppState, source: EventSource, window: MainWindow) -> None:
        super().__init__(window)
        self.state, self.source, self.window = state, source, window
        self.receiver = None
        self.browser = None
        self._stopped = False
        self._received = False
        state.command_requested.connect(self.handle_command)

    def start(self) -> None:
        if self._stopped or self.receiver is not None:
            return
        self._status("connecting", "YouTube 알림 수신기를 시작하는 중입니다.")
        try:
            from .backend.push_receiver import YouTubePushReceiver

            self.receiver = YouTubePushReceiver(self)
            self.receiver.notification_received.connect(self._notification)
            self.receiver.status_changed.connect(self._status)
            self.receiver.start()
        except Exception as exc:
            # No fallback discovery: startup failure must remain visible and safe.
            if self.receiver is not None:
                self.receiver.stop()
                self.receiver = None
            self._status("error", f"알림 수신기를 시작하지 못했습니다. 앱을 다시 실행하고 로그를 확인하세요: {redact(str(exc))}")

    def _notification(self, notice: object) -> None:
        if self._stopped:
            return
        # BackendSource rechecks the exact type, ID and non-synthetic boundary.
        if self.source.receive_notification(notice, trusted=True):
            self._received = True
            self._status("ready", "")

    def _status(self, code: str, detail: str) -> None:
        if self._stopped and code != "stopped":
            return
        if code in ("ready", "received"):
            detail = ("알림 대기(이 세션에서 수신 이력 있음)" if self._received
                      else "알림 대기(수신 이력 없음)") + " · 준비 상태이며 모든 방송 수신을 보장하지 않습니다."
        status = NotificationStatus(code, redact(detail))
        self.state.post_event(ev.NotificationStatusChanged(status))
        self.source.publish(ev.LogAppended(LogEntry(
            at=datetime.now(timezone.utc), severity=Severity.ERROR if code == "error" else Severity.INFO,
            source="notification-receiver", message=f"{code}: {status.detail}",
        )))

    def handle_command(self, command: object) -> None:
        if self._stopped:
            return
        if not isinstance(command, (cmd.OpenNotificationBrowser, cmd.OpenNotificationSettings)):
            self.source.handle_command(command)
            return
        if self.receiver is None:
            self.start()
        if self.receiver is None:
            return
        if self.browser is None:
            self.browser = QDialog(self.window)
            self.browser.setWindowTitle("YouTube 로그인·알림 설정 — yt-rec")
            self.browser.resize(1000, 760)
            layout = QVBoxLayout(self.browser)
            note = QLabel("계정 메뉴와 같은 YouTube 계정으로 로그인하세요. 이 창을 닫아도 알림 수신은 계속됩니다.", self.browser)
            note.setWordWrap(True)
            layout.addWidget(note)
            controls = QHBoxLayout()
            check = QPushButton("알림 상태 확인", self.browser)
            check.clicked.connect(self.receiver.inspect_registration)
            controls.addWidget(check)
            controls.addStretch()
            layout.addLayout(controls)
            view = QWebEngineView(self.browser)
            view.setPage(self.receiver.page)
            layout.addWidget(view, 1)
        self.browser.show()
        self.browser.raise_()
        self.browser.activateWindow()
        if isinstance(command, cmd.OpenNotificationSettings):
            self.receiver.open_settings()
        else:
            self.receiver.open_browser()

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self.browser is not None:
            self.browser.hide()
            self.browser.deleteLater()
            self.browser = None
        if self.receiver is not None:
            self.receiver.stop()


class DesktopSession(QObject):
    """Tray lifetime and asynchronous, non-destructive application shutdown."""

    def __init__(self, context: AppContext) -> None:
        super().__init__(context.window)
        self.context = context
        self._shutdown: threading.Thread | None = None
        self._shutdown_error: Exception | None = None
        self.stopped = False
        self._last_errors = context.state.error_count
        self._last_stop = context.state.watch.stop_reason
        self._started_at = datetime.now(timezone.utc)
        self._last_completed = {item.recording_id for item in context.state.completed}
        self._last_notice = float("-inf")
        self.options = load_settings()
        window = context.window
        window.desktop_managed = True
        window.exit_requested.connect(self.shutdown)
        # A menu exit remains available on systems without a tray.
        menu = window.menuBar().addMenu("앱")
        menu.addAction("종료", window.request_exit)
        context.app.setQuitOnLastWindowClosed(False)
        icon = context.app.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay)
        window.setWindowIcon(icon)
        self.tray: QSystemTrayIcon | None = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(icon, self)
            tray_menu = QMenu(window)
            tray_menu.addAction("yt-rec 열기", self.show_window)
            tray_menu.addAction("로그 보기", window.open_logs)
            tray_menu.addSeparator()
            tray_menu.addAction("종료", window.request_exit)
            self.tray.setContextMenu(tray_menu)
            self.tray.setToolTip("yt-rec — 자동 녹화")
            self.tray.activated.connect(self._activated)
            self.tray.messageClicked.connect(window.open_logs)
            self.tray.show()
            window.tray_available = True
        context.state.errors_changed.connect(self._on_errors)
        context.state.watch_changed.connect(self._on_watch)
        context.state.completed_changed.connect(self._on_completed)
        settings_changed = getattr(context.state, "settings_changed", None)
        if settings_changed is not None:
            settings_changed.connect(self._on_settings)
        self._shutdown_timer = QTimer(self)
        self._shutdown_timer.setInterval(100)
        self._shutdown_timer.timeout.connect(self._check_shutdown)
        window.installEventFilter(self)

    def show_initial(self) -> None:
        if not (self.context.window.tray_available and getattr(self.options, "start_hidden", False)):
            self.show_window()

    def show_window(self) -> None:
        window = self.context.window
        if window.isMinimized():
            # Remove only minimization; a maximized window must stay maximized.
            window.setWindowState(window.windowState() & ~Qt.WindowState.WindowMinimized)
        window.show()
        window.raise_()
        window.activateWindow()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if watched is self.context.window and event.type() == QEvent.Type.WindowStateChange:
            # Let the native minimize finish before hiding. Recheck when delivered
            # so a quick restore or setting change cannot hide a restored window.
            QTimer.singleShot(0, self._apply_minimize_to_tray)
        return super().eventFilter(watched, event)

    def _apply_minimize_to_tray(self) -> None:
        window = self.context.window
        if self.stopped or self._shutdown is not None or window.exiting or not window.isMinimized():
            return
        if (getattr(self.options, "minimize_to_tray", False)
                and self.tray is not None and QSystemTrayIcon.isSystemTrayAvailable()):
            window.hide()

    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.show_window()

    def _on_settings(self, options: object) -> None:
        self.options = options
        self._apply_minimize_to_tray()

    def _notify(self, text: str) -> None:
        if (self.tray is not None and getattr(self.options, "notifications_enabled", True)
                and time.monotonic() - self._last_notice >= 5):
            self._last_notice = time.monotonic()
            self.tray.showMessage("yt-rec", text, QSystemTrayIcon.MessageIcon.Warning)

    def _on_completed(self, items: tuple) -> None:
        for item in items:
            if (item.recording_id not in self._last_completed
                    and item.status in (CompletionStatus.FAILED, CompletionStatus.PARTIAL)
                    and item.finished_at is not None and item.finished_at >= self._started_at):
                self._notify("녹화를 완전히 저장하지 못했습니다. 보관함과 로그를 확인해 주세요.")
        self._last_completed = {item.recording_id for item in items}

    def _on_errors(self, total: int, _unseen: int) -> None:
        if total > self._last_errors:
            # Do not copy backend messages containing account data into OS notifications.
            self._notify("새 오류가 발생했습니다. 로그에서 내용을 확인해 주세요.")
        self._last_errors = total

    def _on_watch(self, watch: object) -> None:
        reason = watch.stop_reason
        if (watch.state is WatchState.STOPPED and reason != self._last_stop
                and reason in (StopReason.AUTH_EXPIRED, StopReason.QUOTA_EXCEEDED,
                               StopReason.NETWORK_DOWN, StopReason.BACKEND_DOWN)):
            self._notify("채널 감시가 중단되었습니다. 앱에서 계정과 로그를 확인해 주세요.")
        self._last_stop = reason

    def shutdown(self) -> None:
        if self._shutdown is not None:
            return
        if self.context.notifications is not None:
            self.context.notifications.stop()
        source = self.context.source
        if source is None or isinstance(source, StubEventSource):
            if source is not None:
                source.stop()
            self._finish_shutdown()
            return
        # The Qt timer belongs to the GUI thread; all waiting happens off it.
        source.begin_shutdown()
        self._shutdown = threading.Thread(target=self._stop_source, name="yt-rec-shutdown")
        self._shutdown.start()
        self._shutdown_timer.start()

    def _stop_source(self) -> None:
        try:
            self.context.source.stop()
        except Exception as exc:
            self._shutdown_error = exc

    def _check_shutdown(self) -> None:
        if self._shutdown is None or self._shutdown.is_alive():
            return
        self._shutdown_timer.stop()
        if self._shutdown_error is not None:
            self.context.window.statusBar().showMessage(
                "종료 실패 — 새 알림·녹화 시작 중단. 앱 → 종료로 다시 시도하세요."
            )
            # Rearm only the existing exit action. BackendSource and the receiver
            # are already stopping/stopped; enabling normal commands is unsafe.
            self.context.window.exiting = False
            self.context.window.centralWidget().setEnabled(False)
            self._shutdown = None
            self._shutdown_error = None
            return
        self._finish_shutdown()

    def _finish_shutdown(self) -> None:
        self.stopped = True
        if self.tray is not None:
            self.tray.hide()
        self.context.app.quit()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="yt-rec",
        description="선택한 YouTube 채널의 라이브를 시작 지점부터 자동 녹화한다.",
    )
    parser.add_argument(
        "--stub",
        choices=STUB_MODES,
        help="백엔드 대신 스텁 이벤트 소스를 붙인다(화면 개발용).",
    )
    parser.add_argument(
        "--emit-interval-ms",
        type=int,
        default=200,
        help="화면 갱신을 묶는 간격. 0이면 묶지 않는다. 기본 200ms.",
    )
    parser.add_argument("--smoke-test", type=Path, metavar="REPORT.json",
                        help="계정·사용자 설정을 건드리지 않고 GUI와 포함 도구를 검사한다.")
    return parser.parse_args(argv)


def build_application(
    argv: list[str] | None = None,
    *,
    app: QApplication | None = None,
    settings: WindowSettings | None = None,
) -> AppContext:
    """QApplication과 창을 구성해 돌려준다. 이벤트 루프는 돌리지 않는다."""
    args = parse_args(argv)

    if app is None:
        app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setOrganizationName(ORGANIZATION)
    app.setApplicationName(APPLICATION)
    app.setApplicationDisplayName("yt-rec")

    # 백엔드가 붙기 전까지 연결 상태는 `연결 안 됨`이 기본이다.
    state = AppState(emit_interval_ms=args.emit_interval_ms)
    window = MainWindow(state, settings=settings or WindowSettings())

    source: EventSource | None
    notifications = None
    if args.stub:
        stub = StubEventSource()
        state.attach(stub)
        _start_stub(stub, args.stub)
        source = stub
    else:
        source = create_backend_source(event_only=True)
        state.attach(source)
        source.start()
        notifications = NotificationSession(state, source, window)
        notifications.start()

    return AppContext(app=app, state=state, window=window, source=source, notifications=notifications)


def _start_stub(source: StubEventSource, mode: str) -> None:
    if mode in PRESETS:
        # 창이 보인 뒤에 주입해야 첫 그리기 경로까지 확인할 수 있다.
        QTimer.singleShot(0, lambda: source.load_preset(mode))
    elif mode == "scenario":
        QTimer.singleShot(0, lambda: source.play(recording_lifecycle()))
    elif mode == "flood":
        # 반복 타이머 하나로 초당 100건을 계속 밀어 넣는다.
        QTimer.singleShot(0, lambda: source.start_flood(hz=100))
    else:  # pragma: no cover - argparse가 먼저 걸러낸다
        raise ValueError(f"알 수 없는 스텁 모드: {mode}")


def main(argv: list[str] | None = None) -> int:
    from .recording.binaries import prepare_bundled_environment
    prepare_bundled_environment()
    args = parse_args(argv)
    if args.smoke_test is not None:
        from .smoke import run_smoke
        return run_smoke(args.smoke_test)
    context = build_application(argv)
    desktop = DesktopSession(context)
    desktop.show_initial()
    try:
        return context.app.exec()
    finally:
        if context.notifications is not None:
            context.notifications.stop()
            # External app.quit() can bypass the normal async shutdown. Destroy
            # views/pages before profiles while the Qt application still exists.
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        if context.source is not None and not desktop.stopped:
            context.source.stop()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
