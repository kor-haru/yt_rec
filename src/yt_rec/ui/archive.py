"""완료 녹화 보관함. 파일 조회와 실행은 AppState 명령으로만 요청한다."""

from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ..state.models import CompletedRecording, CompletionStatus
from ..state.store import AppState
from .formatting import completion_status_text, format_bytes, format_duration, to_local


class ArchiveTableModel(QAbstractTableModel):
    _HEADERS = ("날짜", "방송 제목", "채널", "길이", "파일 크기", "상태")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._items: tuple[CompletedRecording, ...] = ()

    def set_items(self, items: tuple[CompletedRecording, ...]) -> None:
        if items != self._items:
            self.beginResetModel()
            self._items = items
            self.endResetModel()

    def at(self, row: int) -> CompletedRecording | None:
        return self._items[row] if 0 <= row < len(self._items) else None

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._items)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._HEADERS)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        item = self.at(index.row())
        if not index.isValid() or item is None or not 0 <= index.column() < 6:
            return None
        column = index.column()
        if role == Qt.ItemDataRole.ToolTipRole:
            return "\n".join(text for text in (item.title, item.output_path, item.note) if text)
        if role == Qt.ItemDataRole.UserRole:
            return (
                item.finished_at.timestamp() if item.finished_at else 0,
                item.title.casefold(), item.channel_name.casefold(),
                item.duration.total_seconds(), item.total_bytes, completion_status_text(item.status),
            )[column]
        if role == Qt.ItemDataRole.DisplayRole:
            local = to_local(item.finished_at)
            return (
                local.strftime("%Y-%m-%d %H:%M") if local else "—",
                " ".join(item.title.splitlines()), " ".join(item.channel_name.splitlines()) or "—",
                format_duration(item.duration) if item.duration.total_seconds() > 0 else "—",
                format_bytes(item.total_bytes), completion_status_text(item.status),
            )[column]
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole and 0 <= section < 6:
            return self._HEADERS[section]
        return None


class ArchiveFilter(QSortFilterProxyModel):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSortRole(Qt.ItemDataRole.UserRole)
        self.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.setFilterKeyColumn(-1)

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:  # noqa: N802
        model = self.sourceModel()
        query = self.filterRegularExpression()
        return any(query.match(str(model.data(model.index(source_row, column, source_parent)))).hasMatch() for column in (1, 2))


class ArchiveDialog(QDialog):
    def __init__(self, state: AppState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = state
        self.setObjectName("ArchiveDialog")
        self.setWindowTitle("보관함 — yt-rec")
        self.setMinimumSize(720, 440)
        layout = QVBoxLayout(self)
        heading = QLabel("보관함", self)
        heading.setObjectName("dialogHeading")
        layout.addWidget(heading)

        filters = QHBoxLayout()
        search_label = QLabel("제목·채널 &검색:", self)
        self.search_edit = QLineEdit(self)
        self.search_edit.setPlaceholderText("제목 또는 채널 이름 일부 입력")
        self.search_edit.setAccessibleName("보관함 제목·채널 검색")
        search_label.setBuddy(self.search_edit)
        filters.addWidget(search_label)
        filters.addWidget(self.search_edit, 1)
        self.refresh_button = QPushButton("새로고침", self)
        self.refresh_button.setToolTip("저장된 이력과 파일 존재 여부를 다시 확인합니다")
        self.refresh_button.setEnabled(state.backend_attached)
        filters.addWidget(self.refresh_button)
        layout.addLayout(filters)

        self.model = ArchiveTableModel(self)
        self.proxy = ArchiveFilter(self)
        self.proxy.setSourceModel(self.model)
        self.table = QTableView(self)
        self.table.setAccessibleName("완료 녹화 보관함 목록")
        self.table.setModel(self.proxy)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, Qt.SortOrder.DescendingOrder)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column, width in ((0, 150), (2, 110), (3, 80), (4, 85), (5, 85)):
            self.table.setColumnWidth(column, width)
        layout.addWidget(self.table, 1)

        self.count_label = QLabel(self)
        layout.addWidget(self.count_label)
        self.detail_label = QLabel("녹화를 선택하면 저장 위치와 복구 내용을 볼 수 있습니다.", self)
        self.detail_label.setWordWrap(True)
        self.detail_label.setTextFormat(Qt.TextFormat.PlainText)
        self.detail_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.detail_label.setMaximumHeight(110)
        layout.addWidget(self.detail_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        self.play_button = QPushButton("재생", self)
        self.reveal_button = QPushButton("파일 위치 열기", self)
        self.copy_button = QPushButton("경로 복사", self)
        for button in (self.play_button, self.reveal_button, self.copy_button):
            buttons.addButton(button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("닫기")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.search_edit.textChanged.connect(self._search)
        self.refresh_button.clicked.connect(state.refresh_archive)
        self.table.selectionModel().selectionChanged.connect(self._selection_changed)
        self.table.doubleClicked.connect(self.play_selected)
        self.play_button.clicked.connect(self.play_selected)
        self.reveal_button.clicked.connect(self.reveal_selected)
        self.copy_button.clicked.connect(self.copy_selected)
        state.archive_changed.connect(self._on_archive)
        self._on_archive(state.archive)
        if state.backend_attached:
            state.refresh_archive()

    @property
    def state(self) -> AppState:
        return self._state

    def _selected(self) -> CompletedRecording | None:
        rows = self.table.selectionModel().selectedRows()
        return self.model.at(self.proxy.mapToSource(rows[0]).row()) if rows else None

    def _on_archive(self, items: tuple[CompletedRecording, ...]) -> None:
        selected = self._selected()
        self.model.set_items(items)
        if selected is not None:
            for row, item in enumerate(items):
                if (item.recording_id, item.output_path) == (selected.recording_id, selected.output_path):
                    proxy_index = self.proxy.mapFromSource(self.model.index(row, 0))
                    if proxy_index.isValid():
                        self.table.selectRow(proxy_index.row())
                    break
        self._selection_changed()
        self._count()

    def _search(self, text: str) -> None:
        self.proxy.setFilterFixedString(text.strip())
        self._selection_changed()
        self._count()

    def _count(self) -> None:
        total = self.model.rowCount()
        self.count_label.setText(f"{self.proxy.rowCount()} / {total}건" if total else "아직 저장된 녹화가 없습니다.")

    def _selection_changed(self, *_args: object) -> None:
        item = self._selected()
        has_path = bool(item and item.output_path)
        can_open = has_path and item.status in {CompletionStatus.COMPLETED, CompletionStatus.PARTIAL} and self._state.backend_attached
        self.play_button.setEnabled(can_open)
        self.reveal_button.setEnabled(has_path and item.status is not CompletionStatus.MISSING and self._state.backend_attached)
        self.copy_button.setEnabled(has_path)
        if item is None:
            self.detail_label.setText("녹화를 선택하면 저장 위치와 복구 내용을 볼 수 있습니다.")
        else:
            self.detail_label.setText("\n".join(text for text in (item.output_path or "최종 파일 없음", item.note) if text))
        self.detail_label.setToolTip(self.detail_label.text())

    def play_selected(self, *_args: object) -> None:
        item = self._selected()
        if item and item.output_path and self.play_button.isEnabled():
            self._state.open_recording_path(item.output_path)

    def reveal_selected(self) -> None:
        item = self._selected()
        if item and item.output_path and self.reveal_button.isEnabled():
            self._state.open_recording_path(item.output_path, reveal=True)

    def copy_selected(self) -> None:
        item = self._selected()
        if item and item.output_path:
            QApplication.clipboard().setText(item.output_path)
