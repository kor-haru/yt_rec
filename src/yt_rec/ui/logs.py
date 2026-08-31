"""앱 상태에 쌓인 로그를 조회하고 복사하는 화면."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
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

from ..state.models import LogEntry, Severity
from ..state.store import AppState
from .formatting import severity_text, to_local

__all__ = ["LogDialog", "LogFilter", "LogTableModel", "format_log_timestamp"]


def _one_line(text: str) -> str:
    return " ".join(text.splitlines())


def format_log_timestamp(value: datetime) -> str:
    """분 단위 이력과 달리 로그 발생 순서를 구분할 수 있게 초까지 표시한다."""
    local = to_local(value)
    return "—" if local is None else local.strftime("%m-%d %H:%M:%S")


class LogTableModel(QAbstractTableModel):
    _HEADERS = ("시각", "수준", "대상", "메시지")

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._items: tuple[LogEntry, ...] = ()

    def set_logs(self, items: tuple[LogEntry, ...]) -> None:
        if items == self._items:
            return

        old = self._items
        inserted_count: int | None = None
        overlap = 0
        for candidate in range(len(items) + 1):
            candidate_overlap = len(items) - candidate
            if candidate_overlap > len(old):
                continue
            if candidate_overlap == 0 and old and items:
                continue
            if items[candidate:] == old[:candidate_overlap]:
                inserted_count = candidate
                overlap = candidate_overlap
                break

        if inserted_count is None:
            self.beginResetModel()
            self._items = items
            self.endResetModel()
            return

        if inserted_count:
            self.beginInsertRows(QModelIndex(), 0, inserted_count - 1)
            self._items = items[:inserted_count] + self._items
            self.endInsertRows()

        removed_count = len(old) - overlap
        if removed_count:
            first = len(self._items) - removed_count
            self.beginRemoveRows(QModelIndex(), first, len(self._items) - 1)
            self._items = self._items[:first]
            self.endRemoveRows()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._items)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._HEADERS)

    def at(self, row: int) -> LogEntry | None:
        if 0 <= row < len(self._items):
            return self._items[row]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):  # noqa: N802
        item = self.at(index.row())
        if item is None or not index.isValid():
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            if index.column() == 0:
                return format_log_timestamp(item.at)
            if index.column() == 1:
                return severity_text(item.severity)
            if index.column() == 2:
                return _one_line(item.source)
            if index.column() == 3:
                return _one_line(item.message)
            return None
        if role == Qt.ItemDataRole.UserRole:
            return item
        if role == Qt.ItemDataRole.ToolTipRole and index.column() == 3:
            return item.message
        return None

    def headerData(  # noqa: N802
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ):
        if (
            orientation == Qt.Orientation.Horizontal
            and role == Qt.ItemDataRole.DisplayRole
            and 0 <= section < len(self._HEADERS)
        ):
            return self._HEADERS[section]
        return None

    def copy_text(self, row: int) -> str:
        item = self.at(row)
        if item is None:
            return ""
        source = _one_line(item.source) or "—"
        return (
            f"{format_log_timestamp(item.at)} "
            f"[{severity_text(item.severity)}] {source}: {_one_line(item.message)}"
        )


class LogFilter(QSortFilterProxyModel):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._mode = "all"
        self._query = ""

    def set_mode(self, mode: str | None) -> None:
        mode = mode if mode in {"all", "warning", "error"} else "all"
        if mode == self._mode:
            return
        begin = getattr(self, "beginFilterChange", None)
        end = getattr(self, "endFilterChange", None)
        if begin is not None and end is not None:
            begin()
            self._mode = mode
            end()
            return
        self._mode = mode
        self.invalidateFilter()

    def set_query(self, text: str) -> None:
        query = text.strip().casefold()
        if query == self._query:
            return
        begin = getattr(self, "beginFilterChange", None)
        end = getattr(self, "endFilterChange", None)
        if begin is not None and end is not None:
            begin()
            self._query = query
            end()
            return
        self._query = query
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:  # noqa: N802
        model = self.sourceModel()
        if not isinstance(model, LogTableModel):
            return True
        item = model.at(source_row)
        if item is None:
            return False
        if self._mode == "warning" and item.severity is Severity.INFO:
            return False
        if self._mode == "error" and item.severity is not Severity.ERROR:
            return False
        return not self._query or self._query in _one_line(item.message).casefold()


class LogDialog(QDialog):
    """대시보드 `로그`로 여는 실시간 로그 뷰어."""

    def __init__(self, state: AppState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = state
        self.setWindowTitle("로그 — yt-rec")
        self.setObjectName("LogDialog")
        self.setMinimumSize(640, 420)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)

        heading = QLabel("로그", self)
        heading.setObjectName("dialogHeading")
        layout.addWidget(heading)

        filters = QHBoxLayout()
        level_label = QLabel("&수준:", self)
        self.level_filter = QComboBox(self)
        self.level_filter.setObjectName("logLevelFilter")
        self.level_filter.setAccessibleName("로그 수준 필터")
        self.level_filter.addItem("전체", "all")
        self.level_filter.addItem("경고 이상", "warning")
        self.level_filter.addItem("오류만", "error")
        level_label.setBuddy(self.level_filter)
        filters.addWidget(level_label)
        filters.addWidget(self.level_filter)

        search_label = QLabel("&메시지 검색:", self)
        self.search_edit = QLineEdit(self)
        self.search_edit.setObjectName("logSearch")
        self.search_edit.setAccessibleName("로그 메시지 검색")
        self.search_edit.setPlaceholderText("메시지 부분일치")
        search_label.setBuddy(self.search_edit)
        filters.addWidget(search_label)
        filters.addWidget(self.search_edit, 1)
        layout.addLayout(filters)

        self.model = LogTableModel(self)
        self.proxy = LogFilter(self)
        self.proxy.setSourceModel(self.model)

        self.table = QTableView(self)
        self.table.setObjectName("logTable")
        self.table.setAccessibleName("로그 목록")
        self.table.setModel(self.proxy)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 130)
        self.table.setColumnWidth(1, 70)
        self.table.setColumnWidth(2, 120)
        layout.addWidget(self.table, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        self.copy_button = QPushButton("선택 행 복사", self)
        self.copy_button.setObjectName("copyLog")
        self.copy_button.setAccessibleName("선택한 로그 복사")
        self.copy_button.setEnabled(False)
        buttons.addButton(self.copy_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("닫기")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.copy_shortcut = QShortcut(QKeySequence.StandardKey.Copy, self)
        self.copy_shortcut.activated.connect(self.copy_selected)
        self.copy_button.clicked.connect(self.copy_selected)
        self.table.selectionModel().selectionChanged.connect(self._update_copy_enabled)
        self.level_filter.currentIndexChanged.connect(self._on_level_changed)
        self.search_edit.textChanged.connect(self._on_query_changed)
        state.logs_changed.connect(self._on_logs)
        self._on_logs(state.logs)

    @property
    def state(self) -> AppState:
        return self._state

    def _on_level_changed(self, index: int) -> None:
        self.proxy.set_mode(self.level_filter.itemData(index))
        self._update_copy_enabled()

    def _on_query_changed(self, text: str) -> None:
        self.proxy.set_query(text)
        self._update_copy_enabled()

    def _on_logs(self, logs: tuple[LogEntry, ...]) -> None:
        self.model.set_logs(logs)
        self._update_copy_enabled()

    def _update_copy_enabled(self, *_args: object) -> None:
        self.copy_button.setEnabled(bool(self.table.selectionModel().selectedRows()))

    def copy_selected(self) -> None:
        selected = sorted(
            self.table.selectionModel().selectedRows(), key=lambda index: index.row()
        )
        if not selected:
            return
        lines = [
            self.model.copy_text(self.proxy.mapToSource(index).row()) for index in selected
        ]
        text = "\n".join(line for line in lines if line)
        if text:
            QApplication.clipboard().setText(text)
