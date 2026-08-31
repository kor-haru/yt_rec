from __future__ import annotations

from datetime import datetime, timezone

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from yt_rec.state import events as ev
from yt_rec.state.models import LogEntry, Severity
from yt_rec.state.store import AppState
from yt_rec.ui.logs import LogDialog


def append_log(
    state: AppState,
    minute: int,
    severity: Severity,
    message: str,
    source: str,
) -> None:
    state.apply(
        ev.LogAppended(
            LogEntry(
                at=datetime(2026, 8, 31, 12, minute, tzinfo=timezone.utc),
                severity=severity,
                message=message,
                source=source,
            )
        )
    )


def populated_dialog(state: AppState) -> LogDialog:
    append_log(state, 0, Severity.INFO, "감시를 시작했습니다", "monitor")
    append_log(state, 1, Severity.WARNING, "Network slow", "youtube")
    append_log(state, 2, Severity.ERROR, "녹화\n실패", "recorder")
    return LogDialog(state)


def test_초기_로그를_최신순_4열로_표시한다(state: AppState) -> None:
    dialog = populated_dialog(state)

    assert dialog.model.rowCount() == 3
    assert [dialog.model.headerData(i, Qt.Orientation.Horizontal) for i in range(4)] == [
        "시각",
        "수준",
        "대상",
        "메시지",
    ]
    assert dialog.model.data(dialog.model.index(0, 3)) == "녹화 실패"
    assert dialog.model.data(dialog.model.index(2, 3)) == "감시를 시작했습니다"
    dialog.close()


def test_수준_3종과_메시지_부분일치_검색이_동작한다(state: AppState) -> None:
    dialog = populated_dialog(state)

    assert dialog.proxy.rowCount() == 3
    dialog.level_filter.setCurrentIndex(1)
    assert dialog.proxy.rowCount() == 2
    dialog.level_filter.setCurrentIndex(2)
    assert dialog.proxy.rowCount() == 1
    dialog.level_filter.setCurrentIndex(0)
    assert dialog.proxy.rowCount() == 3

    dialog.search_edit.setText("NETWORK")
    assert dialog.proxy.rowCount() == 1
    assert dialog.proxy.data(dialog.proxy.index(0, 3)) == "Network slow"
    dialog.close()


def test_선택_행을_한_줄로_클립보드에_복사한다(state: AppState) -> None:
    dialog = populated_dialog(state)
    dialog.show()
    QApplication.processEvents()
    clipboard = QApplication.clipboard()
    clipboard.setText("기존 내용")

    assert not dialog.copy_button.isEnabled()
    dialog.copy_selected()
    assert clipboard.text() == "기존 내용"
    dialog.table.selectRow(0)
    QApplication.processEvents()
    assert dialog.copy_button.isEnabled()
    assert dialog.copy_shortcut.key() == QKeySequence(QKeySequence.StandardKey.Copy)

    dialog.table.setFocus()
    QTest.keyClick(dialog.table, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    QApplication.processEvents()
    copied = clipboard.text()
    assert "[오류] recorder: 녹화 실패" in copied
    assert "\n" not in copied and "\r" not in copied
    dialog.close()


def test_열린_동안_새_로그가_맨_앞에_반영된다(state: AppState) -> None:
    dialog = populated_dialog(state)
    append_log(state, 3, Severity.INFO, "새 로그", "monitor")

    assert dialog.model.rowCount() == 4
    assert dialog.model.data(dialog.model.index(0, 3)) == "새 로그"
    dialog.close()
