from types import SimpleNamespace

import pytest
from PySide6.QtGui import QKeySequence
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog, QLineEdit, QVBoxLayout, QMessageBox

from yt_rec.app import NotificationSession
from yt_rec.ui.main_window import MainWindow


URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


@pytest.fixture
def paste_session(qapp, state, window_settings, request):
    clipboard = qapp.clipboard()
    original = clipboard.text()
    clipboard.setText(getattr(request, "param", ""))
    window = MainWindow(state, settings=window_settings)
    recorded = []
    session = NotificationSession(state, SimpleNamespace(record_pasted=recorded.append), window)
    window.show()
    window.activateWindow()
    qapp.processEvents()
    yield window, session, recorded
    session.stop()
    window.close()
    window.deleteLater()
    qapp.processEvents()
    clipboard.setText(original)


def paste(qapp, widget):
    widget.setFocus()
    QTest.keySequence(widget, QKeySequence(QKeySequence.StandardKey.Paste))
    qapp.processEvents()


@pytest.mark.parametrize("paste_session", [URL], indirect=True)
def test_startup_does_not_record_existing_clipboard(paste_session):
    _window, _session, recorded = paste_session
    assert recorded == []


def test_copy_only_does_not_record(qapp, paste_session):
    _window, _session, recorded = paste_session
    qapp.clipboard().setText(URL)
    qapp.processEvents()
    assert recorded == []


def test_explicit_paste_records_once_per_action(qapp, paste_session):
    window, _session, recorded = paste_session
    qapp.clipboard().setText(URL)
    qapp.processEvents()
    assert recorded == []
    paste(qapp, window)
    assert recorded == ["dQw4w9WgXcQ"]
    # Repeating the same explicit request still reaches backend deduplication.
    paste(qapp, window)
    assert recorded == ["dQw4w9WgXcQ", "dQw4w9WgXcQ"]


@pytest.mark.parametrize("text", ["", "memo", "https://www.youtube.com/@channel"])
def test_invalid_paste_does_not_record(qapp, paste_session, text):
    window, _session, recorded = paste_session
    qapp.clipboard().setText(text)
    paste(qapp, window)
    assert recorded == []


@pytest.mark.parametrize("dialog_parent", [True, False])
def test_other_window_paste_keeps_text_input(qapp, paste_session, dialog_parent):
    window, _session, recorded = paste_session
    dialog = QDialog(window if dialog_parent else None)
    edit = QLineEdit(dialog)
    QVBoxLayout(dialog).addWidget(edit)
    dialog.show()
    dialog.activateWindow()
    qapp.processEvents()
    qapp.clipboard().setText(URL)
    paste(qapp, edit)
    assert edit.text() == URL
    assert recorded == []
    dialog.close()
    dialog.deleteLater()


def test_main_window_editor_keeps_text_paste(qapp, paste_session):
    window, _session, recorded = paste_session
    edit = QLineEdit(window.centralWidget())
    edit.show()
    qapp.clipboard().setText(URL)
    paste(qapp, edit)
    assert edit.text() == URL
    assert recorded == []


@pytest.mark.parametrize("inactive_state", ["hidden", "minimized", "stopped"])
def test_inactive_session_does_not_record(qapp, paste_session, inactive_state):
    window, session, recorded = paste_session
    if inactive_state == "hidden":
        window.hide()
    elif inactive_state == "minimized":
        window.showMinimized()
    else:
        session.stop()
    qapp.processEvents()
    qapp.clipboard().setText(URL)
    paste(qapp, window)
    assert recorded == []


@pytest.mark.parametrize("answer", [QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No])
def test_confirmation_is_forwarded_on_gui_with_safe_default(paste_session, monkeypatch, answer):
    window, session, _ = paste_session
    replies = []
    session.source.confirm_pasted = lambda *args: replies.append(args)
    def question(parent, title, text, buttons, default):
        assert parent is window and "previous.mp4" in text
        assert default == QMessageBox.StandardButton.No
        return answer
    monkeypatch.setattr(QMessageBox, "question", question)
    session._confirm_pasted("dQw4w9WgXcQ", "token", ("previous.mp4",))
    assert replies == [("dQw4w9WgXcQ", "token", answer == QMessageBox.StandardButton.Yes)]
    session._paste_result("이미 대기 중입니다")
    assert window.statusBar().currentMessage() == "이미 대기 중입니다"


def test_shutdown_during_confirmation_cannot_accept_new_recording(paste_session, monkeypatch):
    _, session, _ = paste_session
    replies = []
    session.source.confirm_pasted = lambda *args: replies.append(args)
    def question(*_):
        session.stop()
        return QMessageBox.StandardButton.Yes
    monkeypatch.setattr(QMessageBox, "question", question)
    session._confirm_pasted("dQw4w9WgXcQ", "token", ("previous.mp4",))
    assert replies == [("dQw4w9WgXcQ", "token", False)]
