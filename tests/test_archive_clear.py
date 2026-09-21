"""메인 화면 `최근 완료` 의 일괄 비우기 (이슈 #84).

이력만 지운다. 파일 삭제(`보관함` → `파일 삭제`)는 이 경로에 없다.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from yt_rec.backend import archive as backend
from yt_rec.backend.source import BackendSource
from yt_rec.recording.options import RecordingOptions
from yt_rec.state import events as ev
from yt_rec.state.models import CompletedRecording
from yt_rec.state.store import AppState, EventSource
from yt_rec.ui.main_window import MainWindow
from yt_rec.ui.settings_store import WindowSettings

from test_archive import save_record
from test_backend_services import controller


def make_window(state: AppState, settings: WindowSettings) -> MainWindow:
    window = MainWindow(state, settings=settings)
    window.show()
    QApplication.processEvents()
    return window


def attach(state: AppState, store: backend.ArchiveStore, output) -> BackendSource:
    """메인 창 → 명령 → 실제 보관함 저장소까지 한 경로로 잇는다."""
    control = controller(RecordingOptions(output_dir=output), lambda _event: None)
    source = BackendSource(control, poll_interval_ms=0, archive_store=store)
    state.attach(source)
    state.command_requested.connect(source.handle_command)
    state.refresh_archive()
    return source


def accept_clear(box: QMessageBox) -> int:
    next(button for button in box.buttons() if button.text() == "이력 비우기").click()
    return 0


def test_완료_이력_비우기가_파일을_남기고_목록만_지운다(state, tmp_path, window_settings, monkeypatch):
    output = tmp_path / "출력 폴더"
    records = [save_record(output, "one"), save_record(output, "two", finished_at=1_200)]
    media = [output / "one.mp4", output / "two.mp4"]
    for path in media:
        path.write_bytes(b"real media")
    fragment = records[0].parent / "one.f137.mp4"
    fragment.write_bytes(b"recoverable fragment")
    protected = {path: path.read_bytes() for path in (*records, *media, fragment)}
    store = backend.ArchiveStore(tmp_path / "roots.json")
    store.remember(output)
    attach(state, store, output)
    window = make_window(state, window_settings)
    button = window.dashboard.clear_completed_button
    assert button.isEnabled()
    assert len(state.archive) == 2

    shown = {}
    def accept_confirmation(box):
        shown["text"] = f"{box.text()}\n{box.informativeText()}"
        assert box.defaultButton().text() == "취소"
        assert box.textFormat() is Qt.TextFormat.PlainText
        return accept_clear(box)
    monkeypatch.setattr(QMessageBox, "exec", accept_confirmation)
    button.click()

    assert "2건" in shown["text"]
    assert "저장된 영상 파일은 삭제하지 않습니다" in shown["text"]
    assert state.archive == () and not window.dashboard.completed_rows()
    assert not button.isEnabled()
    # 제외목록만 늘었다. 원본 기록·영상·조각은 한 바이트도 바뀌지 않았다.
    assert store.load() == ()
    assert backend.ArchiveStore(store.path).load() == ()
    assert all(path.read_bytes() == data for path, data in protected.items())
    assert "2건을 비웠습니다" in window.statusBar().currentMessage()
    window.close()


def test_제외목록을_읽지_못하면_비우기를_거절하고_사유를_알린다(state, tmp_path, window_settings, monkeypatch):
    output = tmp_path / "output"
    record = save_record(output, "one")
    media = output / "one.mp4"
    media.write_bytes(b"real media")
    path = tmp_path / "roots.json"
    backend.ArchiveStore(path).remember(output)
    dismissed = path.with_suffix(".dismissed.json")
    dismissed.write_text("깨진 제외목록", encoding="utf-8")
    store = backend.ArchiveStore(path)
    assert store.dismiss_error
    attach(state, store, output)
    window = make_window(state, window_settings)
    monkeypatch.setattr(QMessageBox, "exec", accept_clear)
    window.dashboard.clear_completed_button.click()

    assert dismissed.read_text(encoding="utf-8") == "깨진 제외목록"
    assert record.exists() and media.read_bytes() == b"real media"
    assert len(state.archive) == 1
    assert window.dashboard.clear_completed_button.isEnabled()
    assert "제외목록" in window.statusBar().currentMessage()
    window.close()


def test_비울_이력이_없으면_비우기가_눌리지_않는다(state, window_settings, monkeypatch):
    state.attach(EventSource())
    window = make_window(state, window_settings)
    button = window.dashboard.clear_completed_button
    assert not button.isEnabled()
    commands = []
    state.command_requested.connect(commands.append)
    monkeypatch.setattr(QMessageBox, "exec", lambda _box: pytest.fail("비울 이력이 없는데 확인창을 띄웠다"))
    button.click()
    window._confirm_clear_completed()
    assert not commands

    state.apply(ev.CompletedChanged((CompletedRecording("vid", "완료된 방송"),)))
    assert button.isEnabled()
    window.close()
