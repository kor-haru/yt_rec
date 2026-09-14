"""받은 알림의 로컬 시각·제목·본문. 파일과 웹에는 직접 접근하지 않는다."""

from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHeaderView, QLabel, QMessageBox,
    QPlainTextEdit, QPushButton, QSplitter, QTableView, QVBoxLayout, QWidget,
)

from ..state.models import NotificationHistoryEntry
from ..state.store import AppState
from .formatting import to_local


class NotificationHistoryModel(QAbstractTableModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.items: tuple[NotificationHistoryEntry, ...] = ()

    def rowCount(self, parent=QModelIndex()):  # noqa: N802
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent=QModelIndex()):  # noqa: N802
        return 0 if parent.isValid() else 2

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if index.isValid() and role == Qt.ItemDataRole.DisplayRole:
            item = self.items[index.row()]
            return (to_local(item.received_at).strftime("%Y-%m-%d %H:%M:%S"),
                    " ".join(item.title.splitlines()) or "(제목 없음)")[index.column()]
        return None

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return ("받은 시각 (로컬)", "YouTube 알림 제목")[section]
        return None


class NotificationHistoryDialog(QDialog):
    def __init__(self, state: AppState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = state
        self.setWindowTitle("알림 이력 — yt-rec")
        self.setMinimumSize(440, 400)
        self.resize(720, 540)
        layout = QVBoxLayout(self)
        note = QLabel("앱 전용 브라우저가 받은 알림입니다. 녹화 성공 이력과는 다릅니다.\n최신 500건을 보관합니다. 저장 전의 과거 알림은 불러오지 않습니다.", self)
        note.setWordWrap(True)
        layout.addWidget(note)
        self.error_label = QLabel(self)
        self.error_label.setWordWrap(True)
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.error_label)
        splitter = QSplitter(Qt.Orientation.Vertical, self)
        self.model = NotificationHistoryModel(self)
        self.table = QTableView(splitter)
        self.table.setAccessibleName("받은 알림 목록")
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 180)
        self.detail = QPlainTextEdit(splitter)
        self.detail.setReadOnly(True)
        self.detail.setAccessibleName("선택한 알림 제목과 본문 전문")
        self.detail.setPlaceholderText("알림을 선택하면 제목과 본문 전체를 읽을 수 있습니다.")
        splitter.addWidget(self.table)
        splitter.addWidget(self.detail)
        splitter.setSizes([230, 200])
        layout.addWidget(splitter, 1)
        self.count_label = QLabel(self)
        layout.addWidget(self.count_label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        self.delete_button = QPushButton("선택 이력 삭제", self)
        buttons.addButton(self.delete_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("닫기")
        buttons.rejected.connect(self.reject)
        self.delete_button.clicked.connect(self._delete)
        layout.addWidget(buttons)
        self.table.selectionModel().selectionChanged.connect(self._selection_changed)
        state.notification_history_changed.connect(self._update)
        self._update(state.notification_history)

    def _selected(self) -> NotificationHistoryEntry | None:
        rows = self.table.selectionModel().selectedRows()
        return self.model.items[rows[0].row()] if rows else None

    def _update(self, items: tuple[NotificationHistoryEntry, ...]) -> None:
        previous = self._selected()
        self.model.beginResetModel()
        self.model.items = items
        self.model.endResetModel()
        if items:
            row = next((i for i, item in enumerate(items) if previous and item.entry_id == previous.entry_id), 0)
            self.table.selectRow(row)
        self.count_label.setText(f"{len(items)}건 · 최신순" if items else "아직 받은 알림 이력이 없습니다.")
        self.error_label.setText(self._state.notification_history_error)
        self.error_label.setVisible(bool(self._state.notification_history_error))
        self._selection_changed()

    def _selection_changed(self, *_args) -> None:
        item = self._selected()
        self.delete_button.setEnabled(item is not None and self._state.backend_attached)
        if item is None:
            self.detail.clear()
        else:
            self.detail.setPlainText(
                f"받은 시각 (로컬): {to_local(item.received_at).strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"제목\n{item.title}\n\n본문\n{item.body}"
            )

    def _delete(self) -> None:
        item = self._selected()
        if item is not None and QMessageBox.question(
            self, "알림 이력 삭제", "선택한 알림 이력 한 건을 삭제할까요?\n녹화 파일과 진행 중인 녹화는 바뀌지 않습니다. 이력 삭제는 되돌릴 수 없습니다.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes:
            self._state.delete_notification_history(item.entry_id)
