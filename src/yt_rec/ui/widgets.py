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
    "ELLIPSIS",
    "can_elide",
    "visible_text_width",
    "drawn_text",
]

MUTED_ALPHA = 170
"""보조 문구의 불투명도(0~255)."""

ELLIPSIS = "…"
"""말줄임 표시. `잘렸다`는 사실이 화면에 드러나는지 판단하는 기준이다."""


def can_elide(label: QLabel) -> bool:
    """폭이 모자랄 때 말줄임할 수 있는 라벨인지.

    위젯 클래스 이름이 아니라 **능력**으로 판단한다. 말줄임을 못 하는 라벨은
    넘치는 글자를 아무 표시 없이 잘라 버리므로, 담긴 문구가 온전히 그려져야만
    한다. 이 구분을 클래스 목록으로 적어 두면 화면 이슈가 새 라벨 종류를
    들여올 때 검사에서 조용히 빠진다(실측: 배지 잘림 회귀 테스트가 ``Badge``
    만 순회해서, 같은 결함을 가진 상태 표시줄 라벨을 통과시켰다).
    """
    return callable(getattr(label, "elided_text", None))


def visible_text_width(label: QLabel) -> int:
    """이 라벨의 글자가 실제로 나타날 수 있는 폭.

    라벨 자신의 폭만 보면 안 된다. 부모가 창보다 넓어지면 라벨은 제 폭을
    받아 놓고도 창 밖으로 밀려 화면에서 사라진다(실측: 상태 표시줄이 674px 를
    요구하는데 창이 376px 이라 오류 라벨이 0px 만 보였다). 그래서 잘리고 남은
    영역까지 본다.
    """
    if not label.isVisible():
        return 0
    region = label.visibleRegion()
    if region.isEmpty():
        return 0
    return max(region.boundingRect().intersected(label.contentsRect()).width(), 0)


def drawn_text(label: QLabel) -> str:
    """지금 이 순간 화면에 실제로 나타나는 문자열.

    두 단계를 그대로 따라간다. 먼저 라벨이 자기 폭에 맞춰 그리려고 만드는
    문자열(말줄임할 수 있으면 말줄임한 것, 못 하면 원문)을 구하고, 그 다음
    부모·창에 잘려 나가고 남는 부분만 취한다.

    `무엇이 보이는가` 를 위젯 종류와 무관하게 한 가지 방법으로 묻기 위한
    함수다. 원문과 다르면서 :data:`ELLIPSIS` 로 끝나지도 않으면 사용자는
    잘렸다는 사실조차 모른 채 **틀린 값**을 읽는다.
    """
    metrics = label.fontMetrics()
    painted = label.text()
    if can_elide(label):
        painted = label.elided_text(max(label.contentsRect().width(), 0))

    available = visible_text_width(label)
    if metrics.horizontalAdvance(painted) <= available:
        return painted
    cut = len(painted)
    while cut and metrics.horizontalAdvance(painted[:cut]) > available:
        cut -= 1
    return painted[:cut]


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
