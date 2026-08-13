"""보조 문구가 실제로 읽히는지 검증.

실측 회귀: 스타일시트에 ``color: palette(dark)`` 를 박았더니 다크 테마에서
안내 문구와 카드 메타 정보(채널명·경과 시간·크기·화질)가 배경과 같은 색이 되어
통째로 보이지 않았다. 컬러 이모지만 보이고 글자는 사라졌다. 이슈 #6 의
`빈 상태 안내 문구 표시` 수용 기준이 위젯에 문자열만 들어 있다고 충족되는 것이
아니므로, 실제 대비를 계산해 확인한다.

**여기 있는 검사는 ``theme`` 픽스처로 라이트·다크 두 팔레트에서 각각 돈다.**
``QT_QPA_PLATFORM=offscreen`` 은 팔레트를 라이트로 고정하므로, 픽스처가 어두운
팔레트를 직접 주입하지 않으면 이 파일은 사고가 났던 조건을 한 번도 재현하지
못한다(tests/conftest.py 의 `다크 테마` 참고).
"""

from __future__ import annotations

import pytest
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from yt_rec.state import events as ev
from yt_rec.state.models import ConnectionState
from yt_rec.state.store import AppState
from yt_rec.state.stub import StubEventSource
from yt_rec.ui.dashboard import EMPTY_CHANNELS_DISCONNECTED
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


def test_다크_테마_검사가_실제로_어두운_팔레트에서_돈다(
    qapp: QApplication, theme: str
) -> None:
    """가드 자체가 무력해지지 않게 한다.

    이 파일의 검사들이 `다크 테마 회귀 방지`라고 적혀 있으면서 실제로는 라이트
    팔레트만 측정하던 것이 문제였다. ``theme`` 픽스처가 조용히 아무것도 하지
    않게 되면 여기서 먼저 걸린다.
    """
    background = QApplication.palette().color(QPalette.ColorRole.Window)
    foreground = QApplication.palette().color(QPalette.ColorRole.WindowText)
    background_luminance = _relative_luminance(background)
    if theme == "dark":
        assert background_luminance < 0.1, (
            f"다크로 돌린다면서 배경이 {background.name()} (휘도 "
            f"{background_luminance:.3f}) 다 — 팔레트 주입이 먹지 않았다"
        )
        assert _relative_luminance(foreground) > background_luminance
    else:
        assert background_luminance > 0.5, (
            f"라이트 기준 배경이 {background.name()} 다"
        )
        assert _relative_luminance(foreground) < background_luminance


@pytest.mark.parametrize(
    "label_name",
    ["recording_empty", "channels_empty", "completed_empty"],
)
def test_빈_상태_안내_문구가_배경과_구분된다(
    qapp: QApplication, theme: str, window_settings: WindowSettings, label_name: str
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


def test_비연결_안내_문구가_실제로_읽힌다(
    qapp: QApplication, theme: str, window_settings: WindowSettings
) -> None:
    """위젯에 문자열이 들어 있는 것과 화면에서 읽히는 것은 다르다.

    이 문구는 `왜 아무 일도 일어나지 않는가` 를 알리는 유일한 안내다. 배경과
    같은 색이거나 첫 낱말부터 말줄임되면 문자열 단언은 통과해도 사용자는
    원인을 알 수 없다. 실제 대비와 그려지는 문자열까지 확인한다.
    """
    state = AppState(emit_interval_ms=0)
    window = MainWindow(state, settings=window_settings)
    window.show()
    # 연결됐다가 끊긴 경로. 저장소가 감시 통지를 뒤이어 방출한다.
    state.apply(ev.ConnectionChanged(ConnectionState.CONNECTED))
    state.apply(ev.ConnectionChanged(ConnectionState.DISCONNECTED))
    QApplication.processEvents()

    label = window.dashboard.channels_empty
    assert label.text() == EMPTY_CHANNELS_DISCONNECTED
    assert label.isVisible()

    background = label.palette().color(label.backgroundRole())
    ratio = contrast_ratio(label.text_color(), background)
    assert ratio >= MIN_CONTRAST, (
        f"비연결 안내 대비비가 {ratio:.2f} 로 너무 낮다 "
        f"(글자 {label.text_color().name(QColor.NameFormat.HexArgb)}, "
        f"배경 {background.name()})"
    )

    # 창을 최소 너비까지 좁혀도 원인이 드러나는 만큼은 그려져야 한다.
    window.resize(window.minimumWidth(), 480)
    QApplication.processEvents()
    drawn = label.elided_text()
    assert "백엔드에 연결되지" in drawn, f"안내가 {drawn!r} 로만 그려진다"

    window.close()
    state.deleteLater()


def test_카드_메타_문구가_배경과_구분된다(
    qapp: QApplication, theme: str, window_settings: WindowSettings
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
    qapp: QApplication, theme: str, window_settings: WindowSettings
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
