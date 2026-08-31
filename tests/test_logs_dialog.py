from __future__ import annotations

from datetime import datetime, timezone

from PySide6.QtCore import QItemSelectionModel, Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QHeaderView

from yt_rec.state import events as ev
from yt_rec.state.models import LogEntry, Severity
from yt_rec.state.store import MAX_LOGS, AppState
from yt_rec.ui.logs import LogDialog, LogTableModel


def append_log(
    state: AppState,
    minute: int,
    severity: Severity,
    message: str,
    source: str,
    *,
    second: int = 0,
) -> None:
    state.apply(
        ev.LogAppended(
            LogEntry(
                at=datetime(2026, 8, 31, 12, minute, second, tzinfo=timezone.utc),
                severity=severity,
                message=message,
                source=source,
            )
        )
    )


def populated_dialog(state: AppState) -> LogDialog:
    append_log(state, 0, Severity.INFO, "감시를 시작했습니다", "monitor", second=1)
    append_log(state, 1, Severity.WARNING, "Net\nwork slow", "youtube", second=2)
    append_log(state, 2, Severity.ERROR, "녹화\n실패", "recorder", second=3)
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
    assert dialog.model.data(dialog.model.index(0, 0)).endswith("02:03")
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

    dialog.search_edit.setText("NET WORK")
    assert dialog.proxy.rowCount() == 1
    assert dialog.proxy.data(dialog.proxy.index(0, 3)) == "Net work slow"
    dialog.proxy.set_mode("error")
    assert dialog.proxy.rowCount() == 0
    dialog.proxy.set_mode(None)
    assert dialog.proxy.rowCount() == 1
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
    assert copied.split()[1].count(":") == 2
    assert "\n" not in copied and "\r" not in copied
    dialog.close()


def test_증분_추가_뒤에도_선택과_복사가_유지된다(state: AppState) -> None:
    dialog = populated_dialog(state)
    dialog.table.selectRow(1)
    QApplication.processEvents()
    assert dialog.proxy.data(dialog.table.selectionModel().selectedRows()[0].siblingAtColumn(3)) == (
        "Net work slow"
    )

    append_log(state, 3, Severity.INFO, "새 로그", "monitor")

    assert dialog.model.rowCount() == 4
    assert dialog.model.data(dialog.model.index(0, 3)) == "새 로그"
    selected = dialog.table.selectionModel().selectedRows()
    assert len(selected) == 1
    assert dialog.proxy.data(selected[0].siblingAtColumn(3)) == "Net work slow"
    assert dialog.copy_button.isEnabled()
    dialog.copy_selected()
    assert "[경고] youtube: Net work slow" in QApplication.clipboard().text()
    dialog.close()


def test_prepend와_tail_trim만_증분_신호를_쓴다() -> None:
    old = tuple(
        LogEntry(
            at=datetime(2026, 8, 31, 12, 0, row, tzinfo=timezone.utc),
            severity=Severity.INFO,
            message=str(row),
        )
        for row in range(3)
    )
    model = LogTableModel()
    model.set_logs(old)
    inserted = QSignalSpy(model.rowsInserted)
    removed = QSignalSpy(model.rowsRemoved)
    reset = QSignalSpy(model.modelReset)

    model.set_logs(old)
    assert inserted.count() == removed.count() == reset.count() == 0

    new = LogEntry(
        at=datetime(2026, 8, 31, 12, 1, tzinfo=timezone.utc),
        severity=Severity.WARNING,
        message="new",
    )
    model.set_logs((new, *old[:-1]))
    assert inserted.count() == 1
    assert removed.count() == 1
    assert reset.count() == 0

    unrelated = tuple(
        LogEntry(
            at=datetime(2026, 8, 31, 13, 0, row, tzinfo=timezone.utc),
            severity=Severity.ERROR,
            message=f"other-{row}",
        )
        for row in range(3)
    )
    model.set_logs(unrelated)
    assert reset.count() == 1


def test_필터된_여러_행을_화면_순서대로_복사한다(state: AppState) -> None:
    rows = (
        (Severity.ERROR, "keep oldest"),
        (Severity.ERROR, "discard"),
        (Severity.WARNING, "keep middle"),
        (Severity.INFO, "keep hidden"),
        (Severity.ERROR, "keep newest"),
    )
    for minute, (severity, message) in enumerate(rows):
        append_log(state, minute, severity, message, f"source-{minute}")
    dialog = LogDialog(state)
    dialog.level_filter.setCurrentIndex(1)
    dialog.search_edit.setText("KEEP")

    assert [dialog.proxy.data(dialog.proxy.index(row, 3)) for row in range(3)] == [
        "keep newest",
        "keep middle",
        "keep oldest",
    ]
    visible_times = [dialog.proxy.data(dialog.proxy.index(row, 0)) for row in range(3)]
    assert visible_times == sorted(visible_times, reverse=True)
    selection = dialog.table.selectionModel()
    flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
    selection.select(dialog.proxy.index(2, 0), flags)
    selection.select(dialog.proxy.index(0, 0), flags)
    dialog.copy_selected()

    copied = QApplication.clipboard().text().splitlines()
    assert "keep newest" in copied[0]
    assert "source-4" in copied[0]
    assert "keep oldest" in copied[1]
    assert "source-0" in copied[1]
    dialog.close()


def test_200ms_배치로_대량_로그를_한번에_그린다(qapp) -> None:
    state = AppState(emit_interval_ms=200)
    dialog = LogDialog(state)
    changed = QSignalSpy(state.logs_changed)
    for row in range(MAX_LOGS + 20):
        append_log(state, row % 60, Severity.INFO, f"message-{row}", "batch")

    assert changed.wait(1500)
    assert changed.count() == 1
    assert dialog.model.rowCount() == MAX_LOGS
    assert dialog.proxy.data(dialog.proxy.index(0, 3)) == f"message-{MAX_LOGS + 19}"
    header = dialog.table.horizontalHeader()
    assert all(
        header.sectionResizeMode(column) is not QHeaderView.ResizeMode.ResizeToContents
        for column in range(3)
    )
    assert header.sectionResizeMode(3) is QHeaderView.ResizeMode.Stretch
    dialog.close()
    state.deleteLater()
