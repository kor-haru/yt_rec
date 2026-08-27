"""구독 채널 선택 화면. 목록은 QListView 가상 스크롤이다."""

from __future__ import annotations

from PySide6.QtCore import QAbstractListModel, QModelIndex, QSortFilterProxyModel, Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from ..state.models import ConnectionState, Subscription
from ..state.store import AppState
from .account import AccountPane
from .widgets import set_muted

__all__ = ["ChannelsDialog", "SubscriptionListModel", "SubscriptionFilter"]


class SubscriptionListModel(QAbstractListModel):
    toggled = Signal(str, bool)
    """channel_id, selected"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._items: tuple[Subscription, ...] = ()

    def set_subscriptions(self, items: tuple[Subscription, ...]) -> None:
        self.beginResetModel()
        self._items = items
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        if parent.isValid():
            return 0
        return len(self._items)

    def at(self, row: int) -> Subscription | None:
        if 0 <= row < len(self._items):
            return self._items[row]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):  # noqa: N802
        item = self.at(index.row())
        if item is None or not index.isValid():
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            badge = "  [조회 불가]" if item.unavailable else ""
            return f"{item.name}{badge}"
        if role == Qt.ItemDataRole.ToolTipRole:
            return item.channel_id
        if role == Qt.ItemDataRole.CheckStateRole:
            return Qt.CheckState.Checked if item.selected else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.UserRole:
            return item.channel_id
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:  # noqa: N802
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return (
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsUserCheckable
        )

    def setData(self, index: QModelIndex, value, role: int = Qt.ItemDataRole.EditRole) -> bool:  # noqa: N802
        item = self.at(index.row())
        if item is None or role != Qt.ItemDataRole.CheckStateRole:
            return False
        selected = value in (Qt.CheckState.Checked, int(Qt.CheckState.Checked), True)
        self.toggled.emit(item.channel_id, bool(selected))
        return False


class SubscriptionFilter(QSortFilterProxyModel):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._query = ""
        self._mode = "all"
        self.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)

    def set_query(self, text: str) -> None:
        self._query = text.strip()
        self._refresh_filter()

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self._refresh_filter()

    def _refresh_filter(self) -> None:
        # Qt 6.10+ 는 invalidateFilter 가 deprecated. 6.7 은 beginFilterChange 가 없다.
        begin = getattr(self, "beginFilterChange", None)
        end = getattr(self, "endFilterChange", None)
        if begin is not None and end is not None:
            begin()
            end()
            return
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:  # noqa: N802
        model = self.sourceModel()
        if not isinstance(model, SubscriptionListModel):
            return True
        item = model.at(source_row)
        if item is None:
            return False
        if self._mode == "selected" and not item.selected:
            return False
        if self._mode == "unselected" and item.selected:
            return False
        if self._query:
            needle = self._query.casefold()
            hay = f"{item.name}\n{item.channel_id}".casefold()
            if needle not in hay:
                return False
        return True


class ChannelsDialog(QDialog):
    """대시보드 `채널 관리` 로 여는 화면."""

    def __init__(self, state: AppState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = state
        self.setWindowTitle("채널 관리 — yt-rec")
        self.setObjectName("ChannelsDialog")
        self.setMinimumSize(520, 420)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)

        self.pane = AccountPane(state, self, show_reload=True)
        layout.addWidget(self.pane)

        self.search_edit = QLineEdit(self)
        self.search_edit.setObjectName("channelSearch")
        self.search_edit.setPlaceholderText("채널 이름 또는 ID")
        self.search_edit.textChanged.connect(self._on_search)
        layout.addWidget(self.search_edit)

        filters = QHBoxLayout()
        self.filter_group = QButtonGroup(self)
        self.filter_all = QRadioButton("전체", self)
        self.filter_selected = QRadioButton("선택됨만", self)
        self.filter_unselected = QRadioButton("미선택만", self)
        self.filter_all.setObjectName("filterAll")
        self.filter_selected.setObjectName("filterSelected")
        self.filter_unselected.setObjectName("filterUnselected")
        self.filter_all.setChecked(True)
        for button in (self.filter_all, self.filter_selected, self.filter_unselected):
            self.filter_group.addButton(button)
            filters.addWidget(button)
        filters.addStretch(1)
        self.filter_all.toggled.connect(lambda on: on and self._set_mode("all"))
        self.filter_selected.toggled.connect(lambda on: on and self._set_mode("selected"))
        self.filter_unselected.toggled.connect(lambda on: on and self._set_mode("unselected"))
        layout.addLayout(filters)

        self.model = SubscriptionListModel(self)
        self.proxy = SubscriptionFilter(self)
        self.proxy.setSourceModel(self.model)
        self.model.toggled.connect(self._on_toggled)

        self.list_view = QListView(self)
        self.list_view.setObjectName("channelList")
        self.list_view.setModel(self.proxy)
        self.list_view.setUniformItemSizes(True)
        self.list_view.setSelectionMode(QListView.SelectionMode.SingleSelection)
        layout.addWidget(self.list_view, 1)

        self.summary_label = QLabel(self)
        self.summary_label.setObjectName("selectionSummary")
        set_muted(self.summary_label)
        layout.addWidget(self.summary_label)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("닫기")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        state.subscriptions_changed.connect(self._on_subscriptions)
        state.connection_changed.connect(self._on_connection)
        self._on_subscriptions(state.subscriptions)
        self._on_connection(state.connection)

    @property
    def state(self) -> AppState:
        return self._state

    def _on_search(self, text: str) -> None:
        self.proxy.set_query(text)

    def _set_mode(self, mode: str) -> None:
        self.proxy.set_mode(mode)

    def _on_subscriptions(self, subscriptions: tuple[Subscription, ...]) -> None:
        self.model.set_subscriptions(subscriptions)
        selected = sum(1 for item in subscriptions if item.selected)
        total = len(subscriptions)
        text = f"{selected}개 선택 / 구독 {total}개"
        if selected == 0:
            text += "  ·  선택된 채널이 없으면 감시를 시작하지 않습니다"
        self.summary_label.setText(text)

    def _on_connection(self, connection: ConnectionState) -> None:
        enabled = connection is ConnectionState.CONNECTED
        self.search_edit.setEnabled(enabled)
        self.list_view.setEnabled(enabled)

    def _on_toggled(self, channel_id: str, selected: bool) -> None:
        current: list[str] = []
        seen: set[str] = set()
        for item in self._state.subscriptions:
            want = selected if item.channel_id == channel_id else item.selected
            if want and item.channel_id not in seen:
                current.append(item.channel_id)
                seen.add(item.channel_id)
        if selected and channel_id not in seen:
            current.append(channel_id)
        self._state.set_watched_channels(current)
