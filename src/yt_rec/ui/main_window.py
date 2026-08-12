"""메인 창 — 상단 바 / 대시보드 / 하단 상태 표시줄.

이슈 #6이 정한 순서를 그대로 따른다.

1. 상단 바 — 앱 이름, 감시 상태 배지, 설정·로그·계정 진입 버튼
2. `녹화 중` 섹션
3. `감시 중 채널` 섹션 (`채널 관리` 버튼)
4. `최근 완료` 섹션 (`보관함 열기` 버튼)
5. 하단 상태 표시줄 — 다음 확인까지 남은 시간, API quota, 누적 오류 수

창은 :class:`~yt_rec.state.store.AppState` 의 시그널만 구독한다. 백엔드를
주기적으로 조회하는 코드도, 파일 크기를 stat으로 재는 코드도 없다.
"""

from __future__ import annotations

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from ..state.models import ConnectionState, QuotaStatus, WatchState, WatchStatus
from ..state.store import AppState
from .dashboard import Dashboard
from .dialogs import (
    AccountDialog,
    ArchiveDialog,
    ChannelsDialog,
    LogDialog,
    PlaceholderDialog,
    SettingsDialog,
)
from .formatting import format_countdown, stop_reason_text, watch_badge_text
from .settings_store import WindowSettings
from .widgets import Badge, ElidedLabel

__all__ = ["MainWindow", "APP_TITLE", "STYLESHEET"]

APP_TITLE = "yt-rec"

MIN_WINDOW_WIDTH = 360
"""창 최소 너비의 하한.

실제 최소 너비는 상단 바가 요구하는 값과 이 값 중 큰 쪽으로 정한다. 글꼴과
로케일에 따라 버튼 폭이 달라지므로 상수 하나로 박으면 어떤 환경에서는 상단
바가 잘린다(실측: 한국어 기본 글꼴에서 상단 바는 435px 를 요구했고, 360px 로
강제했을 때 감시 배지가 `시 중 3채` 로 잘렸다).
"""

MIN_WINDOW_HEIGHT = 320

COUNTDOWN_REPAINT_MS = 1000
"""남은 시간 표시를 다시 그리는 간격.

**백엔드 폴링이 아니다.** 백엔드가 준 `다음 확인 시각`(절대 시각)과 로컬
시계만으로 문자열을 다시 만든다. 이 타이머는 상태를 조회하지 않는다.
"""

STYLESHEET = """
QLabel#appTitle { font-size: 15px; font-weight: 600; }
QLabel#badge {
    padding: 2px 8px;
    border-radius: 8px;
    border: 1px solid palette(mid);
    font-size: 11px;
}
QLabel#badge[kind="ok"]      { background: #1f6f3f; color: #ffffff; border-color: #1f6f3f; }
QLabel#badge[kind="warn"]    { background: #8a5a00; color: #ffffff; border-color: #8a5a00; }
QLabel#badge[kind="error"]   { background: #8c2f2f; color: #ffffff; border-color: #8c2f2f; }
QLabel#badge[kind="neutral"] { background: palette(midlight); color: palette(text); }
QToolButton#sectionHeader { font-size: 13px; font-weight: 600; border: none; padding: 4px 2px; }
QFrame#sectionContent { border: 1px solid palette(mid); border-radius: 6px; }
QLabel#rowTitle { font-weight: 600; }
QLabel#rowMeta { font-size: 11px; }
QLabel#rowCountdown { font-size: 11px; }
QLabel#emptyState { font-style: italic; padding: 6px 2px; }
QLabel#dialogHeading { font-size: 15px; font-weight: 600; }
QWidget#row { border-bottom: 1px solid palette(midlight); }
QWidget#topBar QPushButton { padding: 4px 10px; }
"""
# 보조 문구 색은 스타일시트에 고정하지 않는다. `palette(dark)` 같은 값은
# 라이트 테마 기준이라 다크 테마에서 배경과 겹쳐 글자가 보이지 않는다.
# 대신 :func:`~yt_rec.ui.widgets.set_muted` 로 현재 테마 본문 색에 투명도만 준다.


class MainWindow(QMainWindow):
    """앱의 단일 메인 창."""

    def __init__(
        self,
        state: AppState,
        *,
        settings: WindowSettings | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._state = state
        self._settings = settings if settings is not None else WindowSettings()
        self._child_windows: dict[str, PlaceholderDialog] = {}

        self.setWindowTitle(APP_TITLE)
        self.resize(760, 620)
        self.setStyleSheet(STYLESHEET)

        central = QWidget(self)
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)

        self.top_bar = self._build_top_bar(central)
        central_layout.addWidget(self.top_bar)

        self.scroll_area = QScrollArea(central)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        # 가로 스크롤은 만들지 않는다. 넘치는 텍스트는 말줄임으로 처리한다.
        self.scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.dashboard = Dashboard(state, self.scroll_area)
        self.scroll_area.setWidget(self.dashboard)
        central_layout.addWidget(self.scroll_area, 1)

        self.setCentralWidget(central)

        self._build_status_bar()
        self._apply_minimum_size()

        self.dashboard.manage_channels_requested.connect(self.open_channels)
        self.dashboard.open_archive_requested.connect(self.open_archive)

        state.connection_changed.connect(self._on_connection)
        state.watch_changed.connect(self._on_watch)
        state.errors_changed.connect(self._on_errors)
        state.quota_changed.connect(self._on_quota)

        # 남은 시간 표시 재렌더링. 백엔드를 조회하지 않는다.
        self._countdown_repaint_timer = QTimer(self)
        self._countdown_repaint_timer.setInterval(COUNTDOWN_REPAINT_MS)
        self._countdown_repaint_timer.timeout.connect(self._repaint_countdowns)
        self._countdown_repaint_timer.start()

        self._restore_window_state()

        # 현재 스냅샷으로 첫 화면을 채운다.
        self._on_connection(state.connection)
        self._on_watch(state.watch)
        self._on_errors(state.error_count, state.unseen_error_count)
        self._on_quota(state.quota)

    # ------------------------------------------------------------------
    # 구성
    # ------------------------------------------------------------------
    def _build_top_bar(self, parent: QWidget) -> QWidget:
        bar = QWidget(parent)
        bar.setObjectName("topBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(8)

        self.app_title_label = QLabel(APP_TITLE, bar)
        self.app_title_label.setObjectName("appTitle")
        layout.addWidget(self.app_title_label)

        self.watch_badge = Badge("연결 안 됨", bar, kind="neutral")
        layout.addWidget(self.watch_badge)

        self.watch_detail_label = ElidedLabel("", bar, muted=True)
        self.watch_detail_label.setObjectName("rowMeta")
        layout.addWidget(self.watch_detail_label, 1)

        self.settings_button = QPushButton("설정", bar)
        self.settings_button.clicked.connect(self.open_settings)
        layout.addWidget(self.settings_button)

        self.log_button = QPushButton("로그", bar)
        self.log_button.clicked.connect(self.open_logs)
        layout.addWidget(self.log_button)

        self.account_button = QPushButton("계정", bar)
        self.account_button.clicked.connect(self.open_account)
        layout.addWidget(self.account_button)

        return bar

    def _apply_minimum_size(self) -> None:
        """어떤 부분도 잘리지 않는 최소 크기를 창에 건다.

        ``setMinimumSize`` 로 레이아웃 요구보다 작은 값을 강제하면 Qt 가
        위젯을 요구 폭 아래로 찌그러뜨려 글자가 말줄임 없이 잘린다. 그래서
        상단 바·대시보드가 요구하는 폭에서 계산한다.
        """
        needed = max(
            self.top_bar.minimumSizeHint().width(),
            self.dashboard.minimumSizeHint().width(),
            MIN_WINDOW_WIDTH,
        )
        self.setMinimumSize(needed, MIN_WINDOW_HEIGHT)

    def _build_status_bar(self) -> None:
        status = QStatusBar(self)
        status.setSizeGripEnabled(True)
        self.setStatusBar(status)

        self.next_check_label = QLabel("다음 확인 —", status)
        self.next_check_label.setObjectName("statusNextCheck")
        self.quota_label = QLabel("quota —", status)
        self.quota_label.setObjectName("statusQuota")
        self.error_label = QLabel("오류 0건", status)
        self.error_label.setObjectName("statusErrors")

        status.addWidget(self.next_check_label)
        status.addWidget(self.quota_label)
        status.addPermanentWidget(self.error_label)

    # ------------------------------------------------------------------
    # 상태 반영
    # ------------------------------------------------------------------
    def _on_connection(self, connection: ConnectionState) -> None:
        self._refresh_badge(connection, self._state.watch)

    def _on_watch(self, watch: WatchStatus) -> None:
        self._refresh_badge(self._state.connection, watch)
        self._repaint_countdowns()

    def _refresh_badge(self, connection: ConnectionState, watch: WatchStatus) -> None:
        text = watch_badge_text(connection, watch)
        if connection is not ConnectionState.CONNECTED:
            kind = "error"
            detail = "백엔드가 기동되지 않았습니다. 감시·녹화가 진행되지 않습니다."
        elif watch.state is WatchState.WATCHING:
            kind = "ok"
            detail = ""
        else:
            kind = "warn"
            detail = stop_reason_text(watch.stop_reason)
        self.watch_badge.set_state(text, kind)
        self.watch_badge.setToolTip(detail or text)
        self.watch_detail_label.setText(detail)
        # 배지 문구 길이가 바뀌면 상단 바가 요구하는 폭도 바뀐다
        # (`연결 안 됨` → `감시 중 3채널`). 다시 계산하지 않으면 잘린다.
        self._apply_minimum_size()

    def _on_errors(self, total: int, unseen: int) -> None:
        text = f"오류 {total}건"
        if unseen:
            text += f" (새 {unseen}건)"
        self.error_label.setText(text)
        self.error_label.setToolTip("`로그`에서 자세한 내용을 확인할 수 있습니다.")

    def _on_quota(self, quota: QuotaStatus) -> None:
        if quota.limit:
            self.quota_label.setText(f"quota {quota.used:,} / {quota.limit:,}")
        else:
            self.quota_label.setText(f"quota {quota.used:,}")

    def _repaint_countdowns(self) -> None:
        """남은 시간 문자열만 다시 만든다. 상태 조회는 하지 않는다."""
        watch = self._state.watch
        if self._state.connection is not ConnectionState.CONNECTED:
            self.next_check_label.setText("다음 확인 —")
        else:
            self.next_check_label.setText(f"다음 확인 {format_countdown(watch.next_check_at)}")
        self.dashboard.refresh_countdowns()

    # ------------------------------------------------------------------
    # 진입점
    # ------------------------------------------------------------------
    def open_channels(self) -> PlaceholderDialog:
        return self._open(ChannelsDialog)

    def open_archive(self) -> PlaceholderDialog:
        return self._open(ArchiveDialog)

    def open_settings(self) -> PlaceholderDialog:
        return self._open(SettingsDialog)

    def open_logs(self) -> PlaceholderDialog:
        dialog = self._open(LogDialog)
        # 로그를 열면 미확인 오류 배지를 해제한다(#12 수용 기준의 기반).
        self._state.mark_errors_seen()
        return dialog

    def open_account(self) -> PlaceholderDialog:
        return self._open(AccountDialog)

    @property
    def child_windows(self) -> dict[str, PlaceholderDialog]:
        """열려 있는 하위 화면. 테스트에서 확인용."""
        return self._child_windows

    def _open(self, factory: type[PlaceholderDialog]) -> PlaceholderDialog:
        key = factory.__name__
        dialog = self._child_windows.get(key)
        if dialog is None:
            dialog = factory(self._state, self)
            dialog.finished.connect(lambda _r, k=key: self._child_windows.pop(k, None))
            self._child_windows[key] = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        return dialog

    # ------------------------------------------------------------------
    # 창 상태 저장·복원
    # ------------------------------------------------------------------
    def _restore_window_state(self) -> None:
        geometry = self._settings.geometry()
        if geometry is not None:
            self.restoreGeometry(geometry)
        window_state = self._settings.window_state()
        if window_state is not None:
            self.restoreState(window_state)
        for section in self.dashboard.sections:
            section.set_collapsed(self._settings.section_collapsed(section.key))
            section.toggled.connect(
                lambda collapsed, key=section.key: self._on_section_toggled(key, collapsed)
            )

    def _on_section_toggled(self, key: str, collapsed: bool) -> None:
        self._settings.set_section_collapsed(key, collapsed)

    def save_window_state(self) -> None:
        """창 기하와 섹션 접힘 상태를 저장한다."""
        self._settings.set_geometry(self.saveGeometry())
        self._settings.set_window_state(self.saveState())
        for section in self.dashboard.sections:
            self._settings.set_section_collapsed(section.key, section.is_collapsed())
        self._settings.sync()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt 명명 규칙
        self.save_window_state()
        self._countdown_repaint_timer.stop()
        super().closeEvent(event)
