"""대시보드 본문 — `녹화 중` / `감시 중 채널` / `최근 완료` 세 섹션.

이 이슈(#6)에서는 각 행을 최소한의 자리 표시자로 그린다. 다만 스텁 데이터를
주입하면 실제로 그려지도록 :class:`~yt_rec.state.store.AppState` 시그널에
연결해 두었다. 카드 조작(중지 버튼 등)과 풍부한 표현은 #9, #10이 채운다.

**폴링 없음**: 이 위젯은 백엔드도 파일시스템도 조회하지 않는다. 표시하는
크기와 경과 시간은 상태 모델에 담긴 `녹화 프로세스가 보고한 값` 그대로다.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..state.models import (
    CompletedRecording,
    CompletionStatus,
    ConnectionState,
    Recording,
    RecordingState,
    WatchedChannel,
    WatchState,
    WatchStatus,
)
from ..state.store import AppState
from .formatting import (
    completion_status_text,
    format_bytes,
    format_countdown,
    format_duration,
    format_timestamp,
    recording_state_text,
    stop_reason_text,
)
from .widgets import Badge, CollapsibleSection, ElidedLabel, set_muted, sync_rows

__all__ = ["Dashboard", "RecordingRow", "ChannelRow", "CompletedRow"]

SECTION_RECORDING = "recording"
SECTION_CHANNELS = "channels"
SECTION_COMPLETED = "completed"

EMPTY_RECORDING = "진행 중인 녹화가 없습니다. 감시 중인 채널에서 라이브가 시작되면 여기에 표시됩니다."
EMPTY_CHANNELS = "감시 중인 채널이 없습니다. `채널 관리`에서 자동 녹화할 채널을 선택하세요."
EMPTY_COMPLETED = "완료된 녹화가 없습니다. 첫 녹화가 끝나면 여기에 요약이 표시됩니다."
EMPTY_CHANNELS_DISCONNECTED = (
    "백엔드에 연결되지 않아 감시 상태를 알 수 없습니다. 앱을 다시 시작해 보세요."
)
EMPTY_CHANNELS_CONNECTING = (
    "백엔드에 연결하는 중입니다. 감시 중인 채널은 연결이 끝나면 표시됩니다."
)
# `연결 중` 에 `다시 시작해 보세요` 를 보여 주면 아직 진행 중인 일을 실패로
# 단정하고 엉뚱한 조치를 권하는 셈이 된다.

_RECORDING_BADGE_KIND = {
    RecordingState.STARTING: "neutral",
    RecordingState.RECORDING: "ok",
    RecordingState.RETRYING: "warn",
    RecordingState.STALLED: "error",
    RecordingState.STOPPING: "neutral",
}

_COMPLETION_BADGE_KIND = {
    CompletionStatus.COMPLETED: "ok",
    CompletionStatus.PARTIAL: "warn",
    CompletionStatus.FAILED: "error",
    CompletionStatus.MISSING: "warn",
}


class _Row(QWidget):
    """행 위젯 공통. 좌우로 늘어나되 가로 스크롤을 만들지 않는다."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("row")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        # 스타일시트(`QWidget#row`)의 아래 테두리를 실제로 그리게 한다. 평범한
        # QWidget 은 이 속성이 없으면 스타일시트의 배경·테두리를 아예 칠하지
        # 않는다. 스타일시트에 규칙만 써 두고 이 줄을 빠뜨리면 조용히 무시되어
        # 행 구분선이 한 픽셀도 나오지 않는다(픽셀 측정으로 확인).
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)


class RecordingRow(_Row):
    """진행 중 녹화 한 건."""

    stop_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._recording_id = ""
        self.title_label = ElidedLabel("", self)
        self.title_label.setObjectName("rowTitle")
        self.meta_label = ElidedLabel("", self, muted=True)
        self.meta_label.setObjectName("rowMeta")
        self.badge = Badge("", self)
        self.stop_button = QPushButton("중지", self)
        self.stop_button.setObjectName("stopButton")
        self.stop_button.clicked.connect(self._request_stop)

        grid = QGridLayout(self)
        grid.setContentsMargins(6, 4, 6, 4)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(2)
        grid.addWidget(self.title_label, 0, 0)
        grid.addWidget(self.badge, 0, 1, Qt.AlignmentFlag.AlignRight)
        grid.addWidget(self.stop_button, 0, 2, Qt.AlignmentFlag.AlignRight)
        grid.addWidget(self.meta_label, 1, 0, 1, 3)
        grid.setColumnStretch(0, 1)

    def _request_stop(self) -> None:
        if self._recording_id:
            self.stop_requested.emit(self._recording_id)

    def update_from(self, recording: Recording) -> None:
        self._recording_id = recording.recording_id
        self.title_label.setText(recording.title)
        # 크기·경과 시간은 녹화 프로세스가 보고한 값. stat으로 재지 않는다.
        parts = [
            recording.channel_name or "채널 미상",
            format_duration(recording.reported_elapsed),
            format_bytes(recording.reported_bytes),
        ]
        if recording.quality:
            parts.append(recording.quality)
        if recording.detail:
            parts.append(recording.detail)
        self.meta_label.setText("  ·  ".join(p for p in parts if p))
        self.badge.set_state(
            recording_state_text(recording.state),
            _RECORDING_BADGE_KIND.get(recording.state, "neutral"),
        )


class ChannelRow(_Row):
    """감시 중 채널 한 건. 남은 시간은 로컬 시계로 다시 그린다."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._channel: WatchedChannel | None = None

        self.name_label = ElidedLabel("", self)
        self.name_label.setObjectName("rowTitle")
        self.countdown_label = QLabel("—", self)
        self.countdown_label.setObjectName("rowCountdown")
        set_muted(self.countdown_label)
        self.result_label = ElidedLabel("", self, muted=True)
        self.result_label.setObjectName("rowMeta")

        grid = QGridLayout(self)
        grid.setContentsMargins(6, 4, 6, 4)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(2)
        grid.addWidget(self.name_label, 0, 0)
        grid.addWidget(self.countdown_label, 0, 1, Qt.AlignmentFlag.AlignRight)
        grid.addWidget(self.result_label, 1, 0, 1, 2)
        grid.setColumnStretch(0, 1)

    def update_from(self, channel: WatchedChannel) -> None:
        self._channel = channel
        self.name_label.setText(channel.name or channel.channel_id)
        result = channel.last_check_result or "아직 확인하지 않음"
        when = format_timestamp(channel.last_check_at)
        self.result_label.setText(f"마지막 확인 {when}  ·  {result}")
        self.refresh_countdown()

    def refresh_countdown(self) -> None:
        """이미 받아 둔 다음 확인 시각을 기준으로 남은 시간만 다시 그린다."""
        deadline = self._channel.next_check_at if self._channel else None
        self.countdown_label.setText(format_countdown(deadline))


class CompletedRow(_Row):
    """최근 완료 한 건. #10 보관함이 같은 모델을 더 자세히 그린다."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.title_label = ElidedLabel("", self)
        self.title_label.setObjectName("rowTitle")
        self.meta_label = ElidedLabel("", self, muted=True)
        self.meta_label.setObjectName("rowMeta")
        self.badge = Badge("", self)

        grid = QGridLayout(self)
        grid.setContentsMargins(6, 4, 6, 4)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(2)
        grid.addWidget(self.title_label, 0, 0)
        grid.addWidget(self.badge, 0, 1, Qt.AlignmentFlag.AlignRight)
        grid.addWidget(self.meta_label, 1, 0, 1, 2)
        grid.setColumnStretch(0, 1)

    def update_from(self, item: CompletedRecording) -> None:
        self.title_label.setText(item.title)
        parts = [
            format_timestamp(item.finished_at),
            item.channel_name or "채널 미상",
            format_duration(item.duration),
            format_bytes(item.total_bytes),
        ]
        if item.note:
            parts.append(item.note)
        self.meta_label.setText("  ·  ".join(p for p in parts if p))
        self.badge.set_state(
            completion_status_text(item.status),
            _COMPLETION_BADGE_KIND.get(item.status, "neutral"),
        )


class Dashboard(QWidget):
    """세 섹션을 세로로 쌓은 대시보드 본문."""

    manage_channels_requested = Signal()
    open_archive_requested = Signal()
    stop_requested = Signal(str)

    def __init__(self, state: AppState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = state
        self._recording_rows: dict[str, QWidget] = {}
        self._channel_rows: dict[str, QWidget] = {}
        self._completed_rows: dict[str, QWidget] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(12)

        self.recording_section = CollapsibleSection("녹화 중", SECTION_RECORDING, self)
        self.channels_section = CollapsibleSection("감시 중 채널", SECTION_CHANNELS, self)
        self.completed_section = CollapsibleSection("최근 완료", SECTION_COMPLETED, self)

        self.manage_channels_button = QPushButton("채널 관리", self)
        self.manage_channels_button.clicked.connect(self.manage_channels_requested)
        self.channels_section.add_action_widget(self.manage_channels_button)

        self.open_archive_button = QPushButton("보관함 열기", self)
        self.open_archive_button.clicked.connect(self.open_archive_requested)
        self.completed_section.add_action_widget(self.open_archive_button)

        self.recording_empty = self._empty_label(EMPTY_RECORDING)
        self.channels_empty = self._empty_label(EMPTY_CHANNELS)
        self.completed_empty = self._empty_label(EMPTY_COMPLETED)

        self.recording_list = self._list_container(self.recording_section, self.recording_empty)
        self.channels_list = self._list_container(self.channels_section, self.channels_empty)
        self.completed_list = self._list_container(self.completed_section, self.completed_empty)

        layout.addWidget(self.recording_section)
        layout.addWidget(self.channels_section)
        layout.addWidget(self.completed_section)
        layout.addStretch(1)

        state.recordings_changed.connect(self._on_recordings)
        state.channels_changed.connect(self._on_channels)
        state.completed_changed.connect(self._on_completed)
        state.watch_changed.connect(self._on_watch)
        state.connection_changed.connect(self._on_connection)

        # 최초 1회는 현재 스냅샷으로 채운다. 이후에는 시그널로만 갱신된다.
        self._on_recordings(state.recordings)
        self._on_channels(state.channels)
        self._on_completed(state.completed)
        self._on_watch(state.watch)

    # ------------------------------------------------------------------
    @property
    def sections(self) -> tuple[CollapsibleSection, ...]:
        return (self.recording_section, self.channels_section, self.completed_section)

    def recording_rows(self) -> dict[str, RecordingRow]:
        """``recording_id`` → 행 위젯. 화면 이슈와 테스트가 행을 찾을 때 쓴다."""
        return dict(self._recording_rows)

    def channel_rows(self) -> dict[str, ChannelRow]:
        return dict(self._channel_rows)

    def completed_rows(self) -> dict[str, CompletedRow]:
        return dict(self._completed_rows)

    def _empty_label(self, text: str) -> ElidedLabel:
        label = ElidedLabel(text, muted=True)
        label.setObjectName("emptyState")
        label.setWordWrap(False)
        return label

    def _list_container(self, section: CollapsibleSection, empty: ElidedLabel) -> QVBoxLayout:
        holder = QWidget(section)
        rows = QVBoxLayout(holder)
        rows.setContentsMargins(0, 0, 0, 0)
        rows.setSpacing(2)
        section.content_layout().addWidget(empty)
        section.content_layout().addWidget(holder)
        return rows

    # ------------------------------------------------------------------
    def _make_recording_row(self, _recording: Recording) -> RecordingRow:
        row = RecordingRow()
        row.stop_requested.connect(self.stop_requested)
        return row

    def _on_recordings(self, recordings) -> None:
        items = list(recordings)
        sync_rows(
            self.recording_list,
            items,
            key_of=lambda r: r.recording_id,
            create=self._make_recording_row,
            update=lambda w, r: w.update_from(r),
            registry=self._recording_rows,
        )
        self.recording_empty.setVisible(not items)

    def _on_channels(self, channels) -> None:
        items = list(channels)
        sync_rows(
            self.channels_list,
            items,
            key_of=lambda c: c.channel_id,
            create=lambda _c: ChannelRow(),
            update=lambda w, c: w.update_from(c),
            registry=self._channel_rows,
        )
        self.channels_empty.setVisible(not items)

    def _on_completed(self, completed) -> None:
        items = list(completed)
        sync_rows(
            self.completed_list,
            items,
            key_of=lambda c: c.recording_id,
            create=lambda _c: CompletedRow(),
            update=lambda w, c: w.update_from(c),
            registry=self._completed_rows,
        )
        self.completed_empty.setVisible(not items)

    def _on_watch(self, watch: WatchStatus) -> None:
        """채널 섹션의 빈 상태 문구를 고른다. 문구를 정하는 곳은 여기 하나다.

        비연결이 가장 먼저다. 백엔드가 없으면 감시 요약 자체를 신뢰할 수 없어
        `채널을 선택하세요` 같은 안내는 원인도 조치도 틀린 말이 된다.
        """
        if self._channel_rows:
            return
        if self._state.connection is ConnectionState.CONNECTING:
            self.channels_empty.setText(EMPTY_CHANNELS_CONNECTING)
        elif self._state.connection is not ConnectionState.CONNECTED:
            self.channels_empty.setText(EMPTY_CHANNELS_DISCONNECTED)
        elif watch.state is WatchState.STOPPED and watch.stop_reason is not None:
            self.channels_empty.setText(f"{stop_reason_text(watch.stop_reason)} — {EMPTY_CHANNELS}")
        else:
            self.channels_empty.setText(EMPTY_CHANNELS)

    def _on_connection(self, _connection: ConnectionState) -> None:
        # 연결이 바뀌면 문구를 다시 고른다. 판정은 _on_watch() 한 곳에서만
        # 하므로 페이로드가 아니라 저장소의 현재 값을 함께 본다. 다시
        # 연결됐을 때 비연결 안내가 그대로 남지 않는 것도 이 경로 덕이다.
        self._on_watch(self._state.watch)

    # ------------------------------------------------------------------
    def refresh_countdowns(self) -> None:
        """채널 행의 남은 시간 표시만 다시 그린다.

        백엔드를 조회하지 않는다. 이미 받아 둔 `다음 확인 시각`과 로컬 시계로
        문자열만 다시 만든다.
        """
        for row in self._channel_rows.values():
            row.refresh_countdown()
