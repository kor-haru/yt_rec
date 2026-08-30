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
from PySide6.QtGui import QCloseEvent, QShowEvent
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from ..state.models import ConnectionState, QuotaStatus, StopReason, WatchState, WatchStatus
from ..state.store import AppState
from .dashboard import Dashboard
from .dialogs import (
    AccountDialog,
    ArchiveDialog,
    ChannelsDialog,
    LogDialog,
    SettingsDialog,
)
from .formatting import format_countdown, stop_reason_text, watch_badge_text
from .settings_store import WindowSettings
from .widgets import Badge, ElidedLabel

__all__ = ["MainWindow", "APP_TITLE", "STYLESHEET"]

APP_TITLE = "yt-rec"

MIN_WINDOW_WIDTH = 360
"""창 최소 너비의 하한.

실제 최소 너비는 상단 바·대시보드·상태 표시줄이 요구하는 값과 이 값 중 큰
쪽으로 정한다. 글꼴과 로케일에 따라 버튼 폭이 달라지므로 상수 하나로 박으면
어떤 환경에서는 상단 바가 잘린다(실측: 한국어 기본 글꼴에서 상단 바는 435px 를
요구했고, 360px 로 강제했을 때 감시 배지가 `시 중 3채` 로 잘렸다).
"""

MIN_WINDOW_HEIGHT = 320

BADGE_WIDTH_SAMPLE = "감시 중 999채널"
"""최소 너비를 계산할 때 쓰는 감시 배지의 최장 문구.

배지는 짧아서 말줄임이 안 된다. 찌그러지면 글자가 그냥 사라진다. 그래서
최소 너비가 배지를 담을 수 있어야 하는데, **지금 표시된 문구로 계산하면 안
된다**. 문구가 길어질 때마다 최소 너비가 커지고, 최소 너비가 커지면 Qt 가 창을
그만큼 넓힌다. 실측: 백엔드가 죽은 상태(376px)로 저장하고 재실행 후 연결되면
398px 로 튀어 **복원된 창 크기가 무효화됐다**(#6 수용 기준 위반). 그래서 최장
문구로 한 번만 계산하고 이후에는 다시 계산하지 않는다.
"""

STATUS_NEXT_CHECK_SAMPLE = "다음 확인 23시간 59분 후"
"""`다음 확인` 칸이 항상 담을 수 있어야 하는 최장 문구."""

STATUS_ERRORS_SAMPLE = "오류 9999건 (새 9999건)"
"""오류 칸이 항상 담을 수 있어야 하는 최장 문구.

장시간 구동에서 누적 오류가 네 자리가 되는 것은 정상 범위다. 이 칸이 좁아지면
`오류 1234건 (새 1234건)` 이 `오류 123` 이 되어 **틀린 숫자**가 표시된다.
"""

# quota 칸에는 같은 하한을 두지 않는다. `quota 1,234,567 / 10,000,000` 은 이
# 글꼴에서 336px 이고, 세 칸의 최장 문구를 모두 담으려면 창 최소 너비가 674px
# 까지 올라간다. 그러면 사용자가 창을 그보다 좁게 쓸 수 없다. 그래서 quota 만
# 말줄임을 허용하고(`quota 1,234,567 / 10,0…`) 전문은 툴팁에 남긴다. 말줄임돼도
# 앞쪽의 `사용량` 은 남으므로 한눈에 보는 값이 사라지지 않고, 무엇보다 잘렸다는
# 사실이 화면에 드러난다.

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
        self._child_windows: dict[str, QDialog] = {}
        # 최소 크기는 처음 보일 때 한 번 더 잡는다. 상태 표시줄의 크기 조절
        # 손잡이가 show() 시점에야 폭을 보고하므로(실측 439px → 462px), 생성
        # 시점의 값만 믿으면 상태 표시줄이 창보다 넓어져 손잡이가 잘린다.
        self._minimum_size_settled = False

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
        self.dashboard.stop_requested.connect(self._confirm_stop)

        state.connection_changed.connect(self._on_connection)
        state.command_rejected.connect(self._on_command_rejected)
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
        # 가로 정책이 Ignored 라서 레이아웃이 0px 까지 줄일 수 있다. 그러면 안내
        # 문구가 말줄임 표시도 없이 통째로 사라진다. 최소한 `…` 는 남겨 두어
        # 툴팁으로 전문을 볼 수 있다는 것이 드러나게 한다.
        self.watch_detail_label.setMinimumWidth(
            self.watch_detail_label.minimumSizeHint().width()
        )
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

        **표시 내용이 바뀔 때 다시 부르지 않는다.** 생성 시와 처음 보일 때
        딱 두 번 부르고, 두 번 모두 같은 최장 문구로 계산하므로 값은 실행마다
        같다.

        ``setMinimumSize`` 로 레이아웃 요구보다 작은 값을 강제하면 Qt 가
        위젯을 요구 폭 아래로 찌그러뜨린다. 말줄임하는 라벨은 `…` 로 줄지만
        평범한 :class:`QLabel` 은 아무 표시 없이 글자를 잘라 낸다. 상태 표시줄
        라벨이 그 경우였다 — 실측: quota ``1,234,567/10,000,000`` 과 오류
        ``1234`` 건일 때 레이아웃은 674px 를 요구했는데 최소 너비가 376px 이라
        상태 표시줄이 창 밖으로 밀려 ``오류 1234건 (새 1234건)`` 이 ``오류 123``
        으로 잘렸고, offscreen 환경에서는 아예 0px 만 보였다. **틀린 숫자가
        표시된 것이므로 말줄임과 성격이 다르다.**

        그래서 세 곳을 모두 본다. 상태 표시줄 라벨은 이제 말줄임할 수 있으므로
        최소 너비를 밀어 올리지 않고, 대신 창을 좁혔을 때 조용히 잘리는 대신
        `…` 가 남는다. 계산에 상태 표시줄을 포함시키는 것은 나중에 말줄임하지
        못하는 위젯이 여기 붙었을 때 다시 조용히 잘리지 않게 하기 위한 것이다.

        계산은 :data:`BADGE_WIDTH_SAMPLE` 처럼 **표시될 수 있는 최장 문구**로
        한다. 지금 표시된 문구로 다시 계산하면 최소 너비가 커지면서 창이 스스로
        넓어져 복원된 창 크기가 무효가 된다.
        """
        saved_text, saved_kind = self.watch_badge.text(), self.watch_badge.kind()
        self.watch_badge.set_state(BADGE_WIDTH_SAMPLE, saved_kind)
        self.top_bar.layout().activate()
        try:
            needed = max(
                self.top_bar.minimumSizeHint().width(),
                self.dashboard.minimumSizeHint().width(),
                self.statusBar().minimumSizeHint().width(),
                MIN_WINDOW_WIDTH,
            )
        finally:
            self.watch_badge.set_state(saved_text, saved_kind)
            self.top_bar.layout().activate()
        self.setMinimumSize(needed, MIN_WINDOW_HEIGHT)

    def _build_status_bar(self) -> None:
        status = QStatusBar(self)
        status.setSizeGripEnabled(True)
        self.setStatusBar(status)

        self.next_check_label = self._status_label(
            "다음 확인 —", "statusNextCheck", status, floor=STATUS_NEXT_CHECK_SAMPLE
        )
        self.quota_label = self._status_label("quota —", "statusQuota", status)
        self.error_label = self._status_label(
            "오류 0건", "statusErrors", status, floor=STATUS_ERRORS_SAMPLE
        )

        status.addWidget(self.next_check_label)
        status.addWidget(self.quota_label)
        status.addPermanentWidget(self.error_label)

    def _status_label(
        self,
        text: str,
        name: str,
        status: QStatusBar,
        *,
        floor: str = "",
    ) -> ElidedLabel:
        """상태 표시줄 라벨 한 개.

        :class:`~yt_rec.ui.widgets.ElidedLabel` 인 것이 중요하다. 평범한
        :class:`QLabel` 은 폭이 모자라면 넘치는 글자를 아무 표시 없이 잘라 내
        **틀린 값**을 보여 준다. 여기 담기는 것은 quota 사용량과 오류 건수이므로
        한 글자만 사라져도 값이 달라진다.

        정책은 ``Preferred`` 다. 자리가 있으면 문구 전체를 요구하고, 모자라면
        `…` 로 줄어들 뿐 상태 표시줄이 창보다 넓어지지는 않는다.

        ``floor`` 를 주면 그 문구가 들어갈 만큼은 항상 확보한다. 그 칸은 창을
        최소 너비까지 좁혀도 말줄임되지 않고, 값이 바뀔 때 칸 폭이 흔들리지도
        않는다. 이 하한이 모여 창 최소 너비를 결정한다.
        """
        label = ElidedLabel(text, status)
        label.setObjectName(name)
        label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        if floor:
            label.setMinimumWidth(label.fontMetrics().horizontalAdvance(floor))
        return label

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
        if connection is ConnectionState.CONNECTING:
            # `연결 중` 인데 빨간 오류 색과 `기동되지 않았습니다` 를 함께 보여
            # 주면, 아직 진행 중인 일을 이미 실패한 것으로 단정하는 셈이 된다.
            kind = "warn"
            detail = "백엔드에 연결하는 중입니다."
        elif connection is not ConnectionState.CONNECTED:
            if watch.stop_reason is StopReason.AUTH_EXPIRED:
                kind = "error"
                detail = stop_reason_text(watch.stop_reason)
            elif self._state.backend_attached:
                kind = "warn"
                detail = "계정이 연결되지 않았습니다. 계정에서 Google 로그인을 하세요."
            else:
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
        # 최소 너비는 여기서 다시 계산하지 않는다. 배지 문구가 길어질 때마다
        # 최소 너비가 커지면 Qt 가 창을 그만큼 넓혀 복원된 창 크기를 무효로
        # 만든다. 대신 생성 시 최장 문구(BADGE_WIDTH_SAMPLE)로 한 번 잡는다.

    def _on_errors(self, total: int, unseen: int) -> None:
        text = f"오류 {total}건"
        if unseen:
            text += f" (새 {unseen}건)"
        self.error_label.setText(text)
        # ElidedLabel.setText 가 전문을 툴팁으로 넣어 준다. 좁은 폭에서 말줄임돼도
        # 값을 확인할 수 있어야 하므로 전문을 지우지 않고 안내만 덧붙인다.
        self.error_label.setToolTip(f"{text}\n`로그`에서 자세한 내용을 확인할 수 있습니다.")

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
    def open_channels(self) -> QDialog:
        return self._open(ChannelsDialog)

    def open_archive(self) -> QDialog:
        return self._open(ArchiveDialog)

    def open_settings(self) -> QDialog:
        return self._open(SettingsDialog)

    def open_logs(self) -> QDialog:
        dialog = self._open(LogDialog)
        # 로그를 열면 미확인 오류 배지를 해제한다(#12 수용 기준의 기반).
        self._state.mark_errors_seen()
        return dialog

    def open_account(self) -> QDialog:
        return self._open(AccountDialog)

    def _confirm_stop(self, recording_id: str) -> None:
        recording = next(
            (item for item in self._state.recordings if item.recording_id == recording_id),
            None,
        )
        title = recording.title if recording is not None else recording_id
        answer = QMessageBox.question(
            self,
            "녹화 중지",
            f"‘{title}’ 녹화를 중지할까요?\n지금까지 받은 내용은 파일로 저장됩니다.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._state.stop_recording(recording_id, reason="사용자가 중지했습니다")

    def _on_command_rejected(self, _command: object, reason: str) -> None:
        # 모달 상자는 테스트와 조작을 막는다. 상태 표시줄에만 알린다.
        bar = self.statusBar()
        if bar is not None:
            bar.showMessage(reason, 8000)

    @property
    def child_windows(self) -> dict[str, QDialog]:
        """열려 있는 하위 화면. 테스트에서 확인용."""
        return self._child_windows

    def _open(self, factory: type[QDialog]) -> QDialog:
        key = factory.__name__
        dialog = self._child_windows.get(key)
        if dialog is None:
            dialog = factory(self._state, self)
            # 닫으면 위젯까지 없앤다. 이 속성이 없으면 창을 여닫을 때마다
            # 다이얼로그가 메인 창의 자식으로 그대로 쌓인다(실측: 5회 개폐 후
            # 고아 QDialog 자식 5개). `child_windows` 만 보는 검사로는 딕셔너리가
            # 비어 있어 문제가 드러나지 않는다.
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
            dialog.finished.connect(lambda _r, k=key: self._child_windows.pop(k, None))
            # 부모가 먼저 사라지는 등 finished 를 거치지 않는 경로도 있다.
            dialog.destroyed.connect(lambda _obj=None, k=key: self._child_windows.pop(k, None))
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

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt 명명 규칙
        """다시 보일 때 남은 시간 재렌더링을 되살린다.

        :meth:`closeEvent` 가 타이머를 멈추므로 짝이 되는 곳이 필요하다. 없으면
        창을 닫았다가 다시 열었을 때 카운트다운이 그 값에 얼어붙는다. 여기서도
        백엔드를 조회하지 않는다 — 이미 받아 둔 절대 시각으로 문자열만 다시
        만든다.
        """
        super().showEvent(event)
        if not self._minimum_size_settled:
            # 크기 조절 손잡이가 이제 폭을 보고한다. 한 번만 다시 잡는다.
            self._minimum_size_settled = True
            self._apply_minimum_size()
        if not self._countdown_repaint_timer.isActive():
            self._countdown_repaint_timer.start()
        self._repaint_countdowns()
