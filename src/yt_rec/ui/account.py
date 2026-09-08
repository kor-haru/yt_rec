"""계정 연결 화면."""

from __future__ import annotations

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..backend.oauth import ClientConfigError, import_client_config, load_client_config
from ..state.models import ConnectionState, Severity, StopReason, WatchState
from ..state.store import AppState
from .formatting import format_timestamp, stop_reason_text
from .widgets import ElidedLabel

__all__ = ["AccountPane", "AccountDialog"]


class AccountPane(QWidget):
    """연결 상태, 연결/해제, 다시 불러오기."""

    def __init__(
        self,
        state: AppState,
        parent: QWidget | None = None,
        *,
        show_reload: bool = True,
    ) -> None:
        super().__init__(parent)
        self._state = state

        self.status_label = ElidedLabel("", self, muted=True)
        self.status_label.setObjectName("accountStatus")
        self.status_label.setWordWrap(True)

        self.connect_button = QPushButton("연결", self)
        self.connect_button.setObjectName("connectButton")
        self.connect_button.clicked.connect(self._connect)

        self.disconnect_button = QPushButton("연결 해제", self)
        self.disconnect_button.setObjectName("disconnectButton")
        self.disconnect_button.clicked.connect(self._disconnect)

        self.reload_button = QPushButton("다시 불러오기", self)
        self.reload_button.setObjectName("reloadButton")
        self.reload_button.clicked.connect(self._reload)
        self.reload_button.setVisible(show_reload)

        self.session_only = QCheckBox("이번 실행에서만 로그인 유지", self)
        self.session_only.setObjectName("sessionOnlyLogin")
        self.session_only.setToolTip("보안 저장소를 사용할 수 없을 때 선택하세요. 앱을 종료하면 다시 로그인해야 합니다.")

        self.error_label = QLabel("", self)
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setWordWrap(True)
        self.error_label.setObjectName("accountError")

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.addWidget(self.connect_button)
        buttons.addWidget(self.disconnect_button)
        buttons.addWidget(self.reload_button)
        buttons.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.status_label)
        layout.addWidget(self.session_only)
        layout.addLayout(buttons)
        layout.addWidget(self.error_label)

        state.connection_changed.connect(self._refresh)
        state.account_changed.connect(self._refresh)
        state.watch_changed.connect(self._refresh)
        state.logs_changed.connect(self._refresh)
        self._refresh()

    def _connect(self) -> None:
        self._state.connect_account(session_only=self.session_only.isChecked())

    def _disconnect(self) -> None:
        self._state.disconnect_account()

    def _reload(self) -> None:
        self._state.refresh_subscriptions()

    def _refresh(self, *_payload: object) -> None:
        connection = self._state.connection
        account = self._state.account
        self.session_only.setEnabled(connection is ConnectionState.DISCONNECTED)
        latest_error = next(
            (entry.message for entry in self._state.logs if entry.severity is Severity.ERROR), ""
        )
        self.error_label.setText(latest_error if connection is ConnectionState.DISCONNECTED else "")
        self.error_label.setVisible(bool(self.error_label.text()))
        if connection is ConnectionState.CONNECTING:
            self.status_label.setText("Google 계정에 연결하는 중입니다.")
            self.connect_button.setEnabled(False)
            self.disconnect_button.setEnabled(False)
            self.reload_button.setEnabled(False)
            return
        watch = self._state.watch
        if connection is ConnectionState.CONNECTED:
            synced = format_timestamp(account.last_synced_at)
            label = account.label or "연결됨"
            text = f"{label}  ·  마지막 동기화 {synced}"
            if watch.state is WatchState.STOPPED and watch.stop_reason not in (
                None,
                StopReason.NO_CHANNELS,
            ):
                extra = stop_reason_text(watch.stop_reason)
                if extra:
                    text = f"{label}  ·  {extra}"
            self.status_label.setText(text)
            self.connect_button.setEnabled(False)
            self.disconnect_button.setEnabled(True)
            self.reload_button.setEnabled(True)
            return
        if watch.stop_reason is StopReason.AUTH_EXPIRED:
            self.status_label.setText("계정 인증이 만료되었습니다. 다시 연결하세요.")
        elif watch.stop_reason is StopReason.NETWORK_DOWN:
            self.status_label.setText(
                "네트워크에 연결할 수 없습니다. 저장된 인증은 유지했습니다."
            )
        else:
            self.status_label.setText(
                "연결을 누르면 브라우저에서 로그인할 Google 계정을 선택합니다."
            )
        self.connect_button.setEnabled(True)
        self.disconnect_button.setEnabled(False)
        self.reload_button.setEnabled(False)


class AccountDialog(QDialog):
    """상단 `계정` 버튼으로 여는 화면."""

    def __init__(self, state: AppState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = state
        self.setWindowTitle("계정 — yt-rec")
        self.setObjectName("AccountDialog")
        self.setMinimumSize(480, 360)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        guidance = QLabel(
            "앱을 등록한 개발자 계정과 다른 Google 계정으로 로그인할 수 있습니다.\n"
            "브라우저에서 원하는 계정 또는 '다른 계정 사용'을 선택하세요. 계정을 바꾸려면 먼저 연결을 해제하세요.\n"
            "앱이 Testing 상태이면 Google Auth Platform → Audience → Test users에 로그인할 계정을 추가해야 합니다.",
            self,
        )
        guidance.setTextFormat(Qt.TextFormat.PlainText)
        guidance.setWordWrap(True)
        layout.addWidget(guidance)

        self.config_status = QLabel("", self)
        self.config_status.setObjectName("oauthConfigStatus")
        self.config_status.setTextFormat(Qt.TextFormat.PlainText)
        self.config_status.setWordWrap(True)
        layout.addWidget(self.config_status)

        self.import_button = QPushButton("OAuth JSON 가져오기", self)
        self.import_button.setObjectName("importOAuthConfig")
        self.import_button.clicked.connect(self._import_config)
        platform_button = QPushButton("Google Auth Platform 열기", self)
        platform_button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl("https://console.cloud.google.com/auth/overview"))
        )
        setup_buttons = QHBoxLayout()
        setup_buttons.addWidget(self.import_button)
        setup_buttons.addWidget(platform_button)
        layout.addLayout(setup_buttons)

        self.pane = AccountPane(state, self, show_reload=False)
        layout.addWidget(self.pane)
        state.connection_changed.connect(self._refresh_config)
        self._refresh_config()
        layout.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("닫기")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _refresh_config(self, *_payload: object) -> None:
        self.import_button.setEnabled(self._state.connection is ConnectionState.DISCONNECTED)
        try:
            load_client_config()
        except ClientConfigError as extra:
            self.config_status.setText(str(extra))
        else:
            self.config_status.setText("Google 로그인 설정이 준비되었습니다.")

    def _import_config(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, "Google OAuth JSON 선택", "", "JSON 파일 (*.json)")
        if not filename:
            return
        try:
            import_client_config(filename)
        except ClientConfigError as extra:
            QMessageBox.warning(self, "로그인 설정을 가져오지 못했습니다", str(extra))
            return
        self._refresh_config()

    @property
    def state(self) -> AppState:
        return self._state
