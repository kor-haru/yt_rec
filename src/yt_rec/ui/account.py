"""계정 연결 화면."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

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

        self.connect_button = QPushButton("Google 로그인", self)
        self.connect_button.setObjectName("connectButton")
        self.connect_button.clicked.connect(self._connect)

        self.disconnect_button = QPushButton("브라우저 로그아웃", self)
        self.disconnect_button.setObjectName("disconnectButton")
        self.disconnect_button.clicked.connect(self._disconnect)

        self.reload_button = QPushButton("다시 불러오기", self)
        self.reload_button.setObjectName("reloadButton")
        self.reload_button.clicked.connect(self._reload)
        self.reload_button.setVisible(show_reload)

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
        login_note = QLabel(
            "앱이 연 Chrome에서 로그인하면 구독 채널을 가져옵니다. "
            "방송 알림도 같은 로그인을 사용합니다.", self
        )
        login_note.setTextFormat(Qt.TextFormat.PlainText)
        login_note.setWordWrap(True)
        layout.addWidget(login_note)
        layout.addLayout(buttons)
        layout.addWidget(self.error_label)

        state.connection_changed.connect(self._refresh)
        state.account_changed.connect(self._refresh)
        state.watch_changed.connect(self._refresh)
        state.logs_changed.connect(self._refresh)
        self._refresh()

    def _connect(self) -> None:
        self._state.connect_account()

    def _disconnect(self) -> None:
        self._state.disconnect_account()

    def _reload(self) -> None:
        self._state.refresh_subscriptions()

    def _refresh(self, *_payload: object) -> None:
        connection = self._state.connection
        account = self._state.account
        latest_account_log = next(
            (entry for entry in self._state.logs if entry.source == "browser-account"), None
        )
        error = (
            latest_account_log.message
            if latest_account_log is not None
            and latest_account_log.severity is Severity.ERROR
            and connection is ConnectionState.DISCONNECTED
            else ""
        )
        self.error_label.setText(error)
        self.error_label.setVisible(bool(self.error_label.text()))
        self.connect_button.setText(
            "계정 전환" if connection is ConnectionState.CONNECTED else "Google 로그인"
        )
        if connection is ConnectionState.CONNECTING:
            self.status_label.setText("브라우저 로그인과 구독 채널을 확인하는 중입니다.")
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
            self.connect_button.setEnabled(True)
            self.disconnect_button.setEnabled(True)
            self.reload_button.setEnabled(True)
            return
        if watch.stop_reason is StopReason.AUTH_EXPIRED:
            self.status_label.setText("브라우저 로그인이 만료되었습니다. Google 로그인을 눌러 주세요.")
        elif watch.stop_reason is StopReason.NETWORK_DOWN:
            self.status_label.setText(
                "네트워크에 연결할 수 없습니다. 연결을 확인한 뒤 다시 불러오세요."
            )
        else:
            self.status_label.setText(
                "Google 로그인을 누르면 앱 전용 Chrome이 열립니다."
            )
        self.connect_button.setEnabled(True)
        self.disconnect_button.setEnabled(False)
        self.reload_button.setEnabled(True)


class AccountDialog(QDialog):
    """상단 `계정` 버튼으로 여는 화면."""

    def __init__(self, state: AppState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = state
        self.setWindowTitle("계정 — yt-rec")
        self.setObjectName("AccountDialog")
        self.setMinimumSize(480, 300)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        guidance = QLabel(
            "1. Google 로그인을 눌러 열린 YouTube에서 로그인하세요.\n"
            "2. 로그인 후 앱의 채널 관리에서 자동 녹화할 채널을 선택하세요.\n\n"
            "평소 쓰는 Chrome과 별개의 앱 전용 창입니다. 로그인은 이 창에 유지됩니다. "
            "계정을 바꾸려면 계정 전환을 누르고 YouTube의 프로필 메뉴에서 선택하세요.",
            self,
        )
        guidance.setTextFormat(Qt.TextFormat.PlainText)
        guidance.setWordWrap(True)
        layout.addWidget(guidance)

        self.pane = AccountPane(state, self)
        layout.addWidget(self.pane)
        layout.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("닫기")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @property
    def state(self) -> AppState:
        return self._state
