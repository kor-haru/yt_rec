"""화면 이슈들이 공유하는 기본 위젯.

전부 Qt Widgets다. QML은 쓰지 않는다.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPalette
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

__all__ = [
    "ElidedLabel",
    "Badge",
    "CollapsibleSection",
    "sync_rows",
    "clear_layout",
    "set_muted",
    "MUTED_ALPHA",
]

MUTED_ALPHA = 170
"""보조 문구의 불투명도(0~255)."""


def muted_color(widget: QWidget, *, alpha: int = MUTED_ALPHA) -> QColor:
    """현재 테마의 본문 색에 투명도만 준 보조 문구 색.

    스타일시트에 ``color: palette(dark)`` 같은 고정 색을 쓰면 안 된다.
    ``dark`` 는 라이트 테마 기준의 어두운 색이라, 다크 테마에서는 배경과
    구분되지 않아 안내 문구가 통째로 보이지 않는다(실측 확인).
    """
    color = QColor(widget.palette().color(widget.foregroundRole()))
    color.setAlpha(alpha)
    return color


def set_muted(label: QLabel, *, alpha: int = MUTED_ALPHA) -> None:
    """일반 :class:`QLabel` 을 보조 문구 색으로 바꾼다."""
    palette = label.palette()
    palette.setColor(QPalette.ColorRole.WindowText, muted_color(label, alpha=alpha))
    label.setPalette(palette)


class ElidedLabel(QLabel):
    """폭이 모자라면 말줄임(…)으로 줄여 그리는 라벨.

    가로 방향 크기 정책이 ``Ignored`` 라서 긴 제목이 창을 밀어내지 않는다.
    덕분에 창을 좁혀도 가로 스크롤이 생기지 않는다.
    """

    def __init__(
        self,
        text: str = "",
        parent: QWidget | None = None,
        *,
        mode: Qt.TextElideMode = Qt.TextElideMode.ElideRight,
        muted: bool = False,
    ) -> None:
        super().__init__(text, parent)
        self._mode = mode
        self._muted = muted
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def is_muted(self) -> bool:
        return self._muted

    def text_color(self) -> QColor:
        """실제로 글자를 그릴 때 쓰는 색."""
        if self._muted:
            return muted_color(self)
        return QColor(self.palette().color(self.foregroundRole()))

    def setText(self, text: str) -> None:  # noqa: N802 - Qt 명명 규칙
        super().setText(text)
        # 전체 문구는 툴팁으로 남겨 잘려도 확인할 수 있게 한다.
        self.setToolTip(text)

    def elided_text(self, width: int | None = None) -> str:
        """지금 폭(또는 지정 폭)에서 실제로 그려질 문자열. 테스트에서 쓴다."""
        available = self.contentsRect().width() if width is None else width
        return self.fontMetrics().elidedText(self.text(), self._mode, max(available, 0))

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt 명명 규칙
        hint = super().minimumSizeHint()
        height = max(hint.height(), self.fontMetrics().height())
        return QSize(self.fontMetrics().horizontalAdvance("…") + 4, height)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt 명명 규칙
        hint = super().sizeHint()
        # 글꼴 높이보다 낮게 잡히면 글자가 잘린다.
        return QSize(hint.width(), max(hint.height(), self.fontMetrics().height()))

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 명명 규칙
        painter = QPainter(self)
        painter.setPen(self.text_color())
        rect = self.contentsRect()
        painter.drawText(rect, self.alignment(), self.elided_text(rect.width()))


class Badge(QLabel):
    """상태 배지. ``kind`` 동적 속성으로 스타일시트가 색을 고른다."""

    def __init__(self, text: str = "", parent: QWidget | None = None, *, kind: str = "neutral") -> None:
        super().__init__(text, parent)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setObjectName("badge")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # 배지는 짧은 상태 문구다. 줄이면 말줄임 없이 잘리므로 자연 폭을 지킨다.
        self.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        self.set_kind(kind)

    def set_kind(self, kind: str) -> None:
        """``ok`` / ``warn`` / ``error`` / ``neutral`` 중 하나."""
        self.setProperty("kind", kind)
        self.style().unpolish(self)
        self.style().polish(self)

    def kind(self) -> str:
        return str(self.property("kind"))

    def set_state(self, text: str, kind: str) -> None:
        self.setText(text)
        self.set_kind(kind)


class CollapsibleSection(QWidget):
    """제목을 눌러 접고 펼 수 있는 영역.

    접힘 상태는 :attr:`key` 로 :class:`~yt_rec.ui.settings_store.WindowSettings` 에
    저장된다.
    """

    toggled = Signal(bool)
    """payload: 접혔으면 ``True``"""

    def __init__(
        self,
        title: str,
        key: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.key = key
        self._collapsed = False

        self._header = QToolButton(self)
        self._header.setObjectName("sectionHeader")
        self._header.setText(title)
        self._header.setCheckable(True)
        self._header.setChecked(True)
        self._header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._header.setArrowType(Qt.ArrowType.DownArrow)
        self._header.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._header.clicked.connect(self._on_header_clicked)

        self._actions = QWidget(self)
        self._actions_layout = QHBoxLayout(self._actions)
        self._actions_layout.setContentsMargins(0, 0, 0, 0)
        self._actions_layout.setSpacing(6)

        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(8)
        header_row.addWidget(self._header)
        header_row.addStretch(1)
        header_row.addWidget(self._actions)

        self._content = QFrame(self)
        self._content.setObjectName("sectionContent")
        self._content.setFrameShape(QFrame.Shape.NoFrame)
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(8, 4, 8, 8)
        self._content_layout.setSpacing(6)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(2)
        outer.addLayout(header_row)
        outer.addWidget(self._content)

    # ------------------------------------------------------------------
    @property
    def title(self) -> str:
        return self._header.text()

    def content_layout(self) -> QVBoxLayout:
        """섹션 본문에 위젯을 넣을 레이아웃."""
        return self._content_layout

    def add_action_widget(self, widget: QWidget) -> None:
        """제목 줄 오른쪽에 버튼 등을 붙인다(`채널 관리`, `보관함 열기`)."""
        self._actions_layout.addWidget(widget)

    def is_collapsed(self) -> bool:
        return self._collapsed

    def set_collapsed(self, collapsed: bool) -> None:
        collapsed = bool(collapsed)
        if collapsed == self._collapsed:
            # 최초 복원 시에도 위젯 표시 상태는 맞춰 둔다.
            self._content.setVisible(not collapsed)
            self._header.setChecked(not collapsed)
            self._header.setArrowType(
                Qt.ArrowType.RightArrow if collapsed else Qt.ArrowType.DownArrow
            )
            return
        self._collapsed = collapsed
        self._content.setVisible(not collapsed)
        self._header.setChecked(not collapsed)
        self._header.setArrowType(
            Qt.ArrowType.RightArrow if collapsed else Qt.ArrowType.DownArrow
        )
        self.toggled.emit(collapsed)

    def toggle(self) -> None:
        self.set_collapsed(not self._collapsed)

    def _on_header_clicked(self) -> None:
        self.set_collapsed(not self._header.isChecked())


def clear_layout(layout: QLayout) -> None:
    """레이아웃의 자식 위젯을 모두 제거한다."""
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.setParent(None)
            widget.deleteLater()


def sync_rows(
    layout: QVBoxLayout,
    items: Sequence[object],
    *,
    key_of: Callable[[object], str],
    create: Callable[[object], QWidget],
    update: Callable[[QWidget, object], None],
    registry: dict[str, QWidget],
) -> None:
    """목록 데이터에 맞춰 행 위젯을 최소 변경으로 맞춘다.

    통째로 다시 만들면 초당 여러 번 갱신될 때 위젯 생성·파괴가 계속 일어난다.
    키가 같은 행은 재사용하고 값만 갱신해 장시간 구동에서도 메모리가 늘지
    않게 한다.
    """
    wanted = [key_of(item) for item in items]
    wanted_set = set(wanted)

    for key in list(registry):
        if key not in wanted_set:
            widget = registry.pop(key)
            layout.removeWidget(widget)
            widget.setParent(None)
            widget.deleteLater()

    for index, (key, item) in enumerate(zip(wanted, items)):
        widget = registry.get(key)
        if widget is None:
            widget = create(item)
            registry[key] = widget
        update(widget, item)
        if layout.indexOf(widget) != index:
            layout.insertWidget(index, widget)


def hline(parent: QWidget | None = None) -> QFrame:
    """가로 구분선."""
    line = QFrame(parent)
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFrameShadow(QFrame.Shadow.Plain)
    line.setObjectName("hline")
    return line


def labelled_row(pairs: Iterable[tuple[str, QWidget]], parent: QWidget | None = None) -> QWidget:
    """``[(라벨, 위젯), ...]`` 을 한 줄로 배치한 컨테이너."""
    container = QWidget(parent)
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(6)
    for text, widget in pairs:
        if text:
            layout.addWidget(QLabel(text, container))
        layout.addWidget(widget)
    return container
