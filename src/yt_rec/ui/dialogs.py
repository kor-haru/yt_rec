"""대시보드에서 진입하는 화면들의 자리 표시자.

각 화면의 내용은 별도 이슈가 채운다. 이 이슈(#6)는 진입점이 실제로 열린다는
것까지만 보장한다. 화면 담당자는 여기 있는 클래스를 실제 구현으로 바꾸고
:data:`PLACEHOLDER_SCREENS` 등록만 갱신하면 된다.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ..state.store import AppState
from .widgets import set_muted

__all__ = [
    "PlaceholderDialog",
    "ChannelsDialog",
    "ArchiveDialog",
    "SettingsDialog",
    "LogDialog",
    "AccountDialog",
]


class PlaceholderDialog(QDialog):
    """`아직 구현되지 않음`을 알리는 빈 화면.

    상태 저장소를 함께 받아 두므로, 실제 구현은 이 클래스를 대체하면서
    같은 생성자 시그니처를 유지하면 된다.
    """

    #: 창 제목
    screen_title = "화면"
    #: 이 화면을 채울 이슈 번호
    issue = 0
    #: 안내 문구
    summary = ""

    def __init__(self, state: AppState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = state
        self.setWindowTitle(f"{self.screen_title} — yt-rec")
        self.setObjectName(f"{type(self).__name__}")
        self.setMinimumSize(480, 320)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)

        heading = QLabel(self.screen_title, self)
        heading.setObjectName("dialogHeading")
        layout.addWidget(heading)

        if self.summary:
            summary = QLabel(self.summary, self)
            summary.setWordWrap(True)
            layout.addWidget(summary)

        notice = QLabel(f"이 화면은 이슈 #{self.issue}에서 구현됩니다.", self)
        notice.setObjectName("placeholderNotice")
        notice.setWordWrap(True)
        set_muted(notice)
        layout.addWidget(notice)

        layout.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        # Qt 기본 버튼 문구는 영어다. 나머지 화면이 한국어이므로 맞춰 준다.
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("닫기")
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    @property
    def state(self) -> AppState:
        return self._state


class ChannelsDialog(PlaceholderDialog):
    screen_title = "채널 관리"
    issue = 8
    summary = "계정 연결 상태와 구독 채널 중 자동 녹화 대상을 고르는 화면입니다."


class ArchiveDialog(PlaceholderDialog):
    screen_title = "보관함"
    issue = 10
    summary = "완료된 녹화를 찾아보고 파일에 접근하는 화면입니다."


class SettingsDialog(PlaceholderDialog):
    screen_title = "설정"
    issue = 11
    summary = "저장 위치와 녹화 옵션을 조정하는 다이얼로그입니다."

    def __init__(self, state: AppState, parent: QWidget | None = None) -> None:
        super().__init__(state, parent)
        # 설정은 모달로 제공하기로 정해져 있다(#11).
        self.setModal(True)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)


class LogDialog(PlaceholderDialog):
    screen_title = "로그"
    issue = 12
    summary = "오류와 동작 이력을 앱 안에서 확인하고 복사하는 화면입니다."


class AccountDialog(PlaceholderDialog):
    screen_title = "계정"
    issue = 8
    summary = "Google 계정 연결과 해제를 다루는 화면입니다."
