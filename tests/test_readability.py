"""보조 문구가 실제로 읽히는지 검증.

실측 회귀: 스타일시트에 ``color: palette(dark)`` 를 박았더니 다크 테마에서
안내 문구와 카드 메타 정보(채널명·경과 시간·크기·화질)가 배경과 같은 색이 되어
통째로 보이지 않았다. 컬러 이모지만 보이고 글자는 사라졌다. 이슈 #6 의
`빈 상태 안내 문구 표시` 수용 기준이 위젯에 문자열만 들어 있다고 충족되는 것이
아니므로, 실제 대비를 계산해 확인한다.
"""

from __future__ import annotations

import pytest
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

from yt_rec.state.store import AppState
from yt_rec.state.stub import StubEventSource
from yt_rec.ui.main_window import STYLESHEET, MainWindow
from yt_rec.ui.settings_store import WindowSettings

MIN_CONTRAST = 3.0
"""보조 문구의 최소 대비비. WCAG 의 큰 글자·보조 텍스트 기준."""


def _relative_luminance(color: QColor) -> float:
    def channel(value: int) -> float:
        srgb = value / 255
        return srgb / 12.92 if srgb <= 0.03928 else ((srgb + 0.055) / 1.055) ** 2.4

    return (
        0.2126 * channel(color.red())
        + 0.7152 * channel(color.green())
        + 0.0722 * channel(color.blue())
    )


def _composite(foreground: QColor, background: QColor) -> QColor:
    """알파가 있는 글자색을 배경 위에 합성한 실제 색."""
    alpha = foreground.alphaF()
    return QColor(
        round(foreground.red() * alpha + background.red() * (1 - alpha)),
        round(foreground.green() * alpha + background.green() * (1 - alpha)),
        round(foreground.blue() * alpha + background.blue() * (1 - alpha)),
    )


def contrast_ratio(foreground: QColor, background: QColor) -> float:
    drawn = _composite(foreground, background)
    lo, hi = sorted((_relative_luminance(drawn), _relative_luminance(background)))
    return (hi + 0.05) / (lo + 0.05)


def test_스타일시트가_테마_의존_고정색을_쓰지_않는다() -> None:
    """``palette(dark)`` 는 라이트 테마 기준 색이라 다크 테마에서 사라진다."""
    assert "color: palette(dark)" not in STYLESHEET


@pytest.mark.parametrize(
    "label_name",
    ["recording_empty", "channels_empty", "completed_empty"],
)
def test_빈_상태_안내_문구가_배경과_구분된다(
    qapp: QApplication, window_settings: WindowSettings, label_name: str
) -> None:
    state = AppState(emit_interval_ms=0)
    window = MainWindow(state, settings=window_settings)
    window.show()
    QApplication.processEvents()

    label = getattr(window.dashboard, label_name)
    assert label.text(), "안내 문구가 비어 있다"
    assert label.isVisible()

    background = label.palette().color(label.backgroundRole())
    ratio = contrast_ratio(label.text_color(), background)
    assert ratio >= MIN_CONTRAST, (
        f"{label_name} 대비비가 {ratio:.2f} 로 너무 낮다 "
        f"(글자 {label.text_color().name(QColor.NameFormat.HexArgb)}, "
        f"배경 {background.name()})"
    )

    window.close()
    state.deleteLater()


def test_카드_메타_문구가_배경과_구분된다(
    qapp: QApplication, window_settings: WindowSettings
) -> None:
    state = AppState(emit_interval_ms=0)
    window = MainWindow(state, settings=window_settings)
    window.show()
    source = StubEventSource()
    state.attach(source)
    source.load_preset("populated")
    QApplication.processEvents()

    rows = list(window.dashboard.recording_rows().values())
    rows += list(window.dashboard.completed_rows().values())
    assert rows

    for row in rows:
        label = row.meta_label
        assert label.text()
        background = label.palette().color(label.backgroundRole())
        ratio = contrast_ratio(label.text_color(), background)
        assert ratio >= MIN_CONTRAST, f"메타 문구 대비비 {ratio:.2f}"

    for channel_row in window.dashboard.channel_rows().values():
        label = channel_row.result_label
        background = label.palette().color(label.backgroundRole())
        assert contrast_ratio(label.text_color(), background) >= MIN_CONTRAST

    window.close()
    source.stop()
    state.detach(source)
    state.deleteLater()


def test_본문_문구는_흐리게_처리하지_않는다(
    qapp: QApplication, window_settings: WindowSettings
) -> None:
    """제목은 보조 문구가 아니므로 전체 대비를 유지해야 한다."""
    state = AppState(emit_interval_ms=0)
    window = MainWindow(state, settings=window_settings)
    window.show()
    source = StubEventSource()
    state.attach(source)
    source.load_preset("populated")
    QApplication.processEvents()

    for row in window.dashboard.recording_rows().values():
        assert not row.title_label.is_muted()
        background = row.title_label.palette().color(row.title_label.backgroundRole())
        assert contrast_ratio(row.title_label.text_color(), background) >= 4.5

    window.close()
    source.stop()
    state.detach(source)
    state.deleteLater()
