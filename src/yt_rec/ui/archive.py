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
    QMessageBox,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ..state.models import CompletedRecording, CompletionStatus
from ..state.events import ArchiveDismissFinished
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
        self._dismiss_pending = False
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

        cleanup = QHBoxLayout()
        self.dismiss_button = QPushButton("이력에서 제거", self)
        self.cleanup_button = QPushButton("파일 없는 이력 정리", self)
        self.dismiss_button.setToolTip("선택한 항목을 보관함에서만 숨깁니다. 실제 파일은 삭제하지 않습니다")
        self.cleanup_button.setToolTip("전체 보관함에서 파일 없음이 확인된 항목만 정리합니다. 연결되지 않은 드라이브·접근 오류는 제외합니다")
        cleanup.addWidget(self.dismiss_button)
        cleanup.addWidget(self.cleanup_button)
        cleanup.addStretch()
        layout.addLayout(cleanup)
        self.action_label = QLabel(self)
        self.action_label.setWordWrap(True)
        self.action_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.action_label)

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
        self.dismiss_button.clicked.connect(self.dismiss_selected)
        self.cleanup_button.clicked.connect(self.cleanup_missing)
        state.archive_changed.connect(self._on_archive)
        state.recordings_changed.connect(self._selection_changed)
        state.archive_dismiss_finished.connect(self._on_dismiss_finished)
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
        can_dismiss = self._state.backend_attached and not self._dismiss_pending
        active_ids = {recording.recording_id for recording in self._state.recordings}
        self.dismiss_button.setEnabled(can_dismiss and item is not None and item.recording_id not in active_ids)
        self.cleanup_button.setEnabled(can_dismiss and bool(self._missing_items()))
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

    def _missing_items(self) -> tuple[CompletedRecording, ...]:
        active_ids = {item.recording_id for item in self._state.recordings}
        return tuple(item for item in self._state.archive
                     if item.file_missing and item.recording_id not in active_ids)

    def dismiss_selected(self) -> None:
        item = self._selected()
        if item is not None and self.dismiss_button.isEnabled():
            self._request_dismiss((item,), missing_only=False)

    def cleanup_missing(self) -> None:
        items = self._missing_items()
        if items and self.cleanup_button.isEnabled():
            self._request_dismiss(items, missing_only=True)

    def _confirm_dismiss(self, items: tuple[CompletedRecording, ...], *, missing_only: bool) -> bool:
        box = QMessageBox(self)
        box.setWindowTitle("보관함 이력 정리")
        box.setIcon(QMessageBox.Icon.Question)
        box.setTextFormat(Qt.TextFormat.PlainText)
        subject = (f"검색 결과와 관계없이 전체 보관함에서 파일이 없는 {len(items)}건의 이력을 제거할까요?"
                   if missing_only else f"‘{items[0].title}’ 항목을 이력에서 제거할까요?")
        box.setText(subject + "\n\n실제 영상과 녹화 조각은 삭제하지 않습니다.\n"
                    "제거한 항목은 새로고침하거나 앱을 다시 열어도 보관함에 나타나지 않습니다.")
        if missing_only:
            box.setInformativeText("진행 중인 녹화, 연결되지 않은 드라이브, 접근 권한 등으로 파일 유무를 확인하지 못한 항목은 제외합니다.")
        remove = box.addButton("이력에서 제거", QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton("취소", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        box.exec()
        return box.clickedButton() is remove

    def _request_dismiss(self, items: tuple[CompletedRecording, ...], *, missing_only: bool) -> None:
        if not self._confirm_dismiss(items, missing_only=missing_only):
            return
        self._dismiss_pending = True
        self.action_label.setText("보관함 이력을 정리하는 중입니다…")
        self._selection_changed()
        if not self._state.dismiss_archive(items, missing_only=missing_only):
            self._on_dismiss_finished(ArchiveDismissFinished(error="백엔드에 연결되지 않아 이력을 제거하지 못했습니다."))

    def _on_dismiss_finished(self, result: ArchiveDismissFinished) -> None:
        self._dismiss_pending = False
        if result.error:
            self.action_label.setText(result.error)
        elif result.removed_count:
            self.action_label.setText(f"{result.removed_count}건을 이력에서 제거했습니다. 실제 영상과 녹화 조각은 그대로 두었습니다.")
        else:
            self.action_label.setText("제거한 이력이 없습니다. 파일이 복원되었거나 녹화·이력 상태가 바뀌었을 수 있습니다. 새로고침 후 확인하세요.")
        self._selection_changed()
