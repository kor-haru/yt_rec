"""고빈도 이벤트 부하 검증 (이슈 #7).

`초당 100건의 진행 이벤트를 주입하고 GUI 응답성과 메모리 증가를 확인한다`에
해당한다. 기본 3초만 돌려 테스트 시간을 짧게 유지하고, 더 긴 침수 시험은
환경 변수로 늘린다::

    YT_REC_SOAK_SECONDS=300 pytest tests/test_throughput.py
"""

from __future__ import annotations

import gc
import os
import time

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from yt_rec.state.store import AppState
from yt_rec.state.stub import StubEventSource
from yt_rec.ui.main_window import MainWindow
from yt_rec.ui.settings_store import WindowSettings

SOAK_SECONDS = float(os.environ.get("YT_REC_SOAK_SECONDS", "3"))
EMIT_INTERVAL_MS = 200
INJECT_HZ = 100


def test_초당_100건_주입에도_갱신이_묶이고_위젯이_늘지_않는다(
    qapp: QApplication, window_settings: WindowSettings
) -> None:
    state = AppState(emit_interval_ms=EMIT_INTERVAL_MS)
    window = MainWindow(state, settings=window_settings)
    window.show()
    QApplication.processEvents()

    source = StubEventSource()
    state.attach(source)

    injected: list[int] = []
    repaints: list[int] = []
    source.event_ready.connect(lambda _e: injected.append(1))
    state.recordings_changed.connect(lambda payload: repaints.append(len(payload)))

    gc.collect()
    widgets_before = len(window.findChildren(QWidget))

    source.start_flood(recording_id="soak", hz=INJECT_HZ)

    deadline = time.monotonic() + SOAK_SECONDS
    max_stall = 0.0
    while time.monotonic() < deadline:
        tick = time.monotonic()
        QTest.qWait(50)
        max_stall = max(max_stall, time.monotonic() - tick)

    source.stop()
    QApplication.processEvents()
    gc.collect()
    widgets_after = len(window.findChildren(QWidget))

    # 실제로 대량 주입이 일어났는지 먼저 확인한다.
    assert len(injected) > SOAK_SECONDS * INJECT_HZ * 0.5, (
        f"주입이 너무 적다: {len(injected)}건"
    )

    # 갱신은 emit_interval_ms 마다 한 번으로 묶인다. 주입 건수보다 훨씬 적어야 한다.
    expected_max = SOAK_SECONDS * (1000 / EMIT_INTERVAL_MS) * 2 + 10
    assert len(repaints) <= expected_max, (
        f"갱신이 {len(repaints)}회로 묶이지 않았다 (상한 {expected_max})"
    )
    assert len(repaints) < len(injected) / 4

    # 카드는 한 장뿐이고 위젯이 늘어나지 않는다.
    assert len(window.dashboard.recording_rows()) == 1
    assert widgets_after <= widgets_before + 4, (
        f"위젯이 {widgets_before} → {widgets_after} 로 늘었다"
    )

    # 이벤트 루프가 한 번에 오래 막히지 않았다.
    assert max_stall < 1.0, f"이벤트 루프가 {max_stall:.2f}초 막혔다"

    # 마지막 보고값이 화면에 반영돼 있다.
    row = window.dashboard.recording_rows()["soak"]
    assert row.meta_label.text()
    assert state.recordings[0].reported_bytes > 0

    window.close()
    state.detach(source)
    state.deleteLater()


def test_완료_이력이_쌓여도_행_위젯은_상한을_넘지_않는다(
    qapp: QApplication, window_settings: WindowSettings
) -> None:
    """장시간 구동에서 최근 완료 섹션이 무한히 늘지 않는지 본다."""
    from datetime import timedelta

    from yt_rec.state import events as ev
    from yt_rec.state.models import CompletedRecording
    from yt_rec.state.store import MAX_COMPLETED

    state = AppState(emit_interval_ms=0)
    window = MainWindow(state, settings=window_settings)
    window.show()

    for i in range(MAX_COMPLETED + 50):
        state.apply(
            ev.RecordingFinished(
                CompletedRecording(
                    recording_id=f"c{i}",
                    title=f"완료 {i}",
                    duration=timedelta(minutes=1),
                    total_bytes=1024,
                )
            )
        )
    QApplication.processEvents()

    assert len(state.completed) == MAX_COMPLETED
    assert len(window.dashboard.completed_rows()) == MAX_COMPLETED

    window.close()
    state.deleteLater()
