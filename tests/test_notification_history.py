"""SYNTHETIC fixtures only; no real notifications, accounts or recording files."""

import json
import os
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QDialogButtonBox, QMessageBox

from test_notification_app import notification_app as notification_app
from test_notification_source import VIDEO, flush, notice, until
from test_notification_source import production_backend as production_backend
from yt_rec.backend import notification_history as module
from yt_rec.state import events as ev
from yt_rec.state.models import NotificationHistoryEntry
from yt_rec.ui.formatting import to_local
from yt_rec.ui.notification_history import NotificationHistoryDialog

STAMP = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def arrival(**kwargs):
    return replace(module.ReceivedNotification(STAMP, "SYNTHETIC title", "SYNTHETIC body", synthetic=False), **kwargs)


def entry(number=1, **kwargs):
    return replace(NotificationHistoryEntry(f"{number:032x}", STAMP + timedelta(seconds=number), "SYNTHETIC title", "SYNTHETIC body"), **kwargs)


def test_store_roundtrip_minimal_fields_and_newest_first(tmp_path):
    path = tmp_path / "notification-history.json"
    store = module.NotificationHistoryStore(path)
    assert store.load() == () and not path.exists()
    items = (entry(1, title="SYNTHETIC <b>문자 그대로</b>", body="첫 줄\n다음 줄"), entry(2))
    store.save(items)
    assert module.NotificationHistoryStore(path).load() == items[::-1]
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert all(set(row) == {"entry_id", "received_at", "title", "body"} for row in raw)
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("raw", [b"{", b"null", b"[{}]", b"\xff", b'{"token":"SYNTHETIC-secret"}'])
def test_corrupt_file_never_overwritten(tmp_path, raw):
    path = tmp_path / "notification-history.json"
    path.write_bytes(raw)
    store = module.NotificationHistoryStore(path)
    with pytest.raises((ValueError, TypeError)):
        store.load()
    with pytest.raises(OSError):
        store.save((entry(),))
    assert path.read_bytes() == raw


def test_atomic_write_failure_preserves_original(tmp_path, monkeypatch):
    path = tmp_path / "history.json"
    store = module.NotificationHistoryStore(path)
    store.load()
    store.save((entry(),))
    original = path.read_bytes()
    monkeypatch.setattr(module.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("SYNTHETIC")))
    with pytest.raises(OSError):
        store.save((entry(2),))
    assert path.read_bytes() == original and not list(tmp_path.glob("*.tmp"))


def test_file_and_history_count_limits(tmp_path, monkeypatch):
    path = tmp_path / "history.json"
    path.write_bytes(b"[] ")
    monkeypatch.setattr(module, "MAX_FILE_BYTES", 2)
    with pytest.raises(ValueError):
        module.NotificationHistoryStore(path).load()
    store = module.NotificationHistoryStore(tmp_path / "new.json")
    store.load()
    with pytest.raises(ValueError):
        store.save(tuple(entry(i) for i in range(module.MAX_HISTORY + 1)))


@pytest.mark.parametrize("changes", [{"title": "x" * 4097}, {"body": "x" * 16385}, {"received_at": STAMP.replace(tzinfo=None)}])
def test_input_bounds(changes):
    with pytest.raises(ValueError):
        arrival(**changes)


def test_native_arrival_to_state_dialog_disk_and_restart(notification_app, production_backend, qapp):
    context, (source, api, engines, *_rest) = notification_app
    context.notifications.receiver.notification_arrived.emit(arrival())
    flush(source, qapp)
    assert len(context.state.notification_history) == 1
    assert api.get_calls == api.find_calls == [] and not engines  # No video identified.
    assert context.notifications._received
    context.window.notification_history_button.click()
    dialog = context.window._child_windows[NotificationHistoryDialog.__name__]
    assert dialog.model.rowCount() == 1 and "SYNTHETIC body" in dialog.detail.toPlainText()
    source.begin_shutdown()
    source.stop()
    restarted = production_backend()[0]
    restarted.start()
    flush(restarted, qapp)
    assert restarted._history == context.state.notification_history


def test_storage_failure_does_not_stop_recording(notification_app, monkeypatch, qapp):
    context, (source, api, engines, *_rest) = notification_app
    monkeypatch.setattr(source._history_store, "save", lambda *_: (_ for _ in ()).throw(OSError("SYNTHETIC secret must not log")))
    context.notifications.receiver.notification_arrived.emit(arrival())
    context.notifications.receiver.notification_received.emit(notice())
    until(qapp, lambda: VIDEO in engines)
    flush(source, qapp)
    assert len(context.state.notification_history) == 1 and context.state.notification_history_error
    assert api.get_calls == [VIDEO] and api.find_calls == []
    assert all("secret must not log" not in item.message and "SYNTHETIC body" not in item.message for item in context.state.logs)


def test_corrupt_history_does_not_block_start_or_replace_file(production_backend, qapp):
    source = production_backend()[0]
    path = source._history_store.path
    path.write_bytes(b"{SYNTHETIC broken original")
    source.start()
    flush(source, qapp)
    assert source._controller._connected and source._history_error
    assert source.record_notification_history(arrival(), trusted=True)
    flush(source, qapp)
    assert len(source._history) == 1 and path.read_bytes() == b"{SYNTHETIC broken original"


def test_synthetic_untrusted_stopped_and_live_inputs_cannot_create_history(notification_app, qapp):
    context, (source, *_rest) = notification_app
    assert not source.record_notification_history(arrival())
    assert not source.record_notification_history(arrival(synthetic=True), trusted=True)
    assert not source.record_notification_history(notice(), trusted=True)
    context.notifications.receiver.notification_arrived.emit(arrival(synthetic=True))
    context.notifications.stop()
    context.notifications.receiver.notification_arrived.emit(arrival())
    flush(source, qapp)
    assert context.state.notification_history == ()
    assert not source._history_store.path.exists()


def test_accepted_arrival_is_saved_during_normal_shutdown(notification_app, qapp):
    context, (source, *_rest) = notification_app
    blocked, release = threading.Event(), threading.Event()
    source._run(lambda: (blocked.set(), release.wait(5)))
    assert blocked.wait(5)
    context.notifications.receiver.notification_arrived.emit(arrival())
    source.begin_shutdown()
    release.set()
    source.stop()
    restored = module.NotificationHistoryStore(source._history_store.path).load()
    assert len(restored) == 1 and restored[0].title == "SYNTHETIC title"


def test_latest_500_and_delete_only_selected_metadata(notification_app, monkeypatch, qapp):
    context, (source, *_rest) = notification_app
    source._history = tuple(entry(i) for i in range(500, 0, -1))
    context.notifications.receiver.notification_arrived.emit(arrival(received_at=STAMP + timedelta(seconds=501)))
    flush(source, qapp)
    assert len(source._history) == 500 and source._history[-1].entry_id == entry(2).entry_id
    original = source._history
    untouched = source._controller._options.output_dir / "SYNTHETIC-preserve.mp4"
    untouched.write_bytes(b"SYNTHETIC fixture, not media")
    dialog = context.window.open_notification_history()
    monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.StandardButton.No)
    dialog.delete_button.click()
    flush(source, qapp)
    assert source._history == original
    monkeypatch.setattr(QMessageBox, "question", lambda *_: QMessageBox.StandardButton.Yes)
    dialog.delete_button.click()
    flush(source, qapp)
    assert source._history == original[1:]
    assert module.NotificationHistoryStore(source._history_store.path).load() == original[1:]
    assert untouched.read_bytes() == b"SYNTHETIC fixture, not media"


def test_delete_failure_keeps_row(notification_app, monkeypatch, qapp):
    context, (source, *_rest) = notification_app
    context.notifications.receiver.notification_arrived.emit(arrival())
    flush(source, qapp)
    previous = source._history
    monkeypatch.setattr(source._history_store, "save", lambda *_: (_ for _ in ()).throw(OSError("SYNTHETIC")))
    context.state.delete_notification_history(previous[0].entry_id)
    flush(source, qapp)
    assert source._history == previous and context.state.notification_history_error


def test_empty_and_long_plain_text_are_readable(state, qapp, tmp_path):
    # Windows offscreen has no default font directory. Read an installed font
    # into this test process only; do not install fonts or change user settings.
    font_path = Path(os.environ.get("SystemRoot", "")) / "Fonts" / "malgun.ttf"
    font_id = QFontDatabase.addApplicationFont(str(font_path)) if font_path.is_file() else -1
    dialog = NotificationHistoryDialog(state)
    if font_id >= 0:
        dialog.setFont(QFont(QFontDatabase.applicationFontFamilies(font_id)[0], 10))
    dialog.show()
    qapp.processEvents()
    assert "아직" in dialog.count_label.text() and not dialog.delete_button.isEnabled()
    long = entry(title="SYNTHETIC <b>title</b> " + "긴 제목 " * 500,
                 body="SYNTHETIC <script>not executed</script>\n" + "긴 본문\n" * 2000)
    state.apply(ev.NotificationHistoryChanged((long,)))
    qapp.processEvents()
    assert long.title in dialog.detail.toPlainText() and long.body in dialog.detail.toPlainText()
    assert to_local(long.received_at).strftime("%Y-%m-%d %H:%M:%S") == dialog.model.data(dialog.model.index(0, 0))
    assert dialog.detail.isReadOnly() and dialog.detail.verticalScrollBar().maximum() > 0
    for size in [(720, 540), (440, 400)]:
        dialog.resize(*size)
        qapp.processEvents()
        for button in (dialog.delete_button, dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Close)):
            assert dialog.rect().contains(button.mapTo(dialog, QPoint(0, 0)))
            assert dialog.rect().contains(button.mapTo(dialog, button.rect().bottomRight()))
    dialog.resize(720, 540)
    qapp.processEvents()
    assert dialog.grab().save(str(tmp_path / "SYNTHETIC-notification-history.png"))
    dialog.detail.verticalScrollBar().setValue(dialog.detail.verticalScrollBar().maximum())
    qapp.processEvents()
    assert dialog.grab().save(str(tmp_path / "SYNTHETIC-notification-history-body.png"))
    dialog.close()
    if font_id >= 0:
        QFontDatabase.removeApplicationFont(font_id)
