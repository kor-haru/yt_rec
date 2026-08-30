"""계정 연결 화면."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..state.models import ConnectionState, StopReason, WatchState
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

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.addWidget(self.connect_button)
        buttons.addWidget(self.disconnect_button)
        buttons.addWidget(self.reload_button)
        buttons.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.status_label)
        layout.addLayout(buttons)

        state.connection_changed.connect(self._refresh)
        state.account_changed.connect(self._refresh)
        state.watch_changed.connect(self._refresh)
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
                "계정이 연결되지 않았습니다. 연결을 누르면 시스템 브라우저에서 Google 로그인을 합니다."
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
        self.setMinimumSize(480, 240)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        self.pane = AccountPane(state, self, show_reload=False)
        layout.addWidget(self.pane)
        layout.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("닫기")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @property
    def state(self) -> AppState:
        return self._state
