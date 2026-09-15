from __future__ import annotations

import os
import shutil
import stat
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from yt_rec.backend import archive as backend
from yt_rec.backend.archive import ArchiveStore
from yt_rec.backend.source import BackendSource
from yt_rec.logs import sanitize_event
from yt_rec.recording.options import RecordingOptions
from yt_rec.state import commands as cmd, events as ev
from yt_rec.state.models import CompletionStatus, Recording
from yt_rec.state.store import EventSource
from yt_rec.ui.archive import ArchiveDialog

from test_archive import save_record
from test_backend_services import controller


class TrashFile:
    def __init__(self, path):
        self.path = path

    def fileName(self):
        return self.path

    def errorString(self):
        return "test trash failure"

    def moveToTrash(self):
        raise AssertionError("Tests must not use the native user trash")


@pytest.fixture(autouse=True)
def no_native_trash(monkeypatch):
    monkeypatch.setattr(backend, "QFile", TrashFile)


def archive(tmp_path, **changes):
    output = tmp_path / "output"
    state_path = save_record(output, "vid", **changes)
    media = output / "vid.mp4"
    media.write_bytes(b"test media")
    store = ArchiveStore(tmp_path / "roots.json")
    store.remember(output)
    return store, media, state_path


def attach(state, store, output):
    control = controller(RecordingOptions(output_dir=output), lambda _: None)
    source = BackendSource(control, poll_interval_ms=0, archive_store=store)
    state.attach(source)
    state.command_requested.connect(source.handle_command)
    state.refresh_archive()
    results = []
    state.archive_delete_finished.connect(results.append)
    return source, results


@pytest.mark.parametrize("status", ["completed", "partial"])
def test_trash_success_preserves_originals_and_restart(state, tmp_path, monkeypatch, status):
    store, media, state_path = archive(tmp_path, status=status)
    fragment = state_path.parent / "vid.f137.mp4"
    fragment.write_bytes(b"fragment")
    seen = tmp_path / "seen-videos.json"
    seen.write_bytes(b'["vid"]')
    protected = {path: path.read_bytes() for path in (state_path, fragment, seen, store.path)}
    source, results = attach(state, store, media.parent)
    selected, = state.archive
    calls = []
    trash = tmp_path / "test-trash.mp4"
    def move(file):
        calls.append(file.fileName())
        assert state.archive == (selected,)
        assert not store.dismissed_path.exists()
        Path(file.fileName()).rename(trash)
        return True
    monkeypatch.setattr(TrashFile, "moveToTrash", move)
    assert state.delete_archive_file(selected)
    assert calls == [media.as_posix()]
    assert results == [ev.ArchiveDeleteFinished(True, True)]
    assert not state.archive and not state.completed
    assert not media.exists() and trash.read_bytes() == b"test media"
    assert all(path.read_bytes() == before for path, before in protected.items())
    assert ArchiveStore(store.path).load() == ()
    state.delete_archive_file(selected)
    assert len(calls) == 1 and results[-1].error
    state.detach(source)


def test_trash_failure_never_falls_back_to_permanent_delete(state, tmp_path, monkeypatch):
    store, media, _ = archive(tmp_path)
    source, results = attach(state, store, media.parent)
    selected, = state.archive
    calls = []
    def fail(file):
        calls.append(file.fileName())
        return False
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Permanent delete fallback")
    with monkeypatch.context() as patch:
        patch.setattr(TrashFile, "moveToTrash", fail)
        patch.setattr(Path, "unlink", forbidden)
        patch.setattr(os, "unlink", forbidden)
        patch.setattr(shutil, "rmtree", forbidden)
        state.delete_archive_file(selected)
    assert calls == [media.as_posix()]
    assert results[-1].error and not results[-1].file_trashed
    assert "영구 삭제로 재시도하지 않습니다" in results[-1].error
    assert state.archive == (selected,) and media.read_bytes() == b"test media"
    assert not store.dismissed_path.exists()
    state.detach(source)


def test_history_save_failure_after_trash_remains_retryable(state, tmp_path, monkeypatch):
    store, media, state_path = archive(tmp_path)
    before = state_path.read_bytes()
    source, results = attach(state, store, media.parent)
    selected, = state.archive
    trash = tmp_path / "test-trash.mp4"
    def move(file):
        Path(file.fileName()).rename(trash)
        return True
    def fail(_items):
        raise OSError("token=private disk full")
    monkeypatch.setattr(TrashFile, "moveToTrash", move)
    with monkeypatch.context() as patch:
        patch.setattr(store, "dismiss", fail)
        state.delete_archive_file(selected)
    result = results[-1]
    assert result.file_trashed and not result.history_removed
    assert "이력 저장에 실패" in result.error and "[REDACTED]" in result.error
    assert "private" not in result.error
    assert state.archive[0].file_missing
    assert state.completed == state.archive
    assert ArchiveStore(store.path).load()[0].file_missing
    assert trash.read_bytes() == b"test media" and state_path.read_bytes() == before
    assert state.dismiss_archive(state.archive)
    assert not state.archive and not ArchiveStore(store.path).load()
    state.detach(source)


@pytest.mark.parametrize("change", [
    "active", "recording", "merging", "failed", "state", "file", "same_size_file", "missing", "forged", "dismissed",
])
def test_backend_rejects_active_and_changed_selection(state, tmp_path, change):
    store, media, state_path = archive(tmp_path)
    source, results = attach(state, store, media.parent)
    selected, = state.archive
    if change == "active":
        source._controller._recorder.recording.add("vid")
    elif change in {"recording", "merging", "failed"}:
        save_record(media.parent, "vid", status=change)
    elif change == "state":
        save_record(media.parent, "vid", message="changed without changing archive key")
    elif change == "file":
        media.write_bytes(b"changed media")
    elif change == "same_size_file":
        media.rename(tmp_path / "old.mp4")
        media.write_bytes(b"test media")
    elif change == "missing":
        media.rename(tmp_path / "moved.mp4")
    elif change == "forged":
        selected = replace(selected, deletion_token=())
    elif change == "dismissed":
        store.dismiss((selected,))
    before = state_path.read_bytes()
    state.delete_archive_file(selected)
    assert results[-1].error and not results[-1].file_trashed
    assert state_path.read_bytes() == before
    state.detach(source)


@pytest.mark.parametrize("kind", ["outside", "directory", "state", "fragment", "custom_work", "shared", "hardlink", "traversal"])
def test_trash_rejects_unsafe_stored_paths(tmp_path, kind):
    store, media, state_path = archive(tmp_path)
    target = media
    if kind == "outside":
        target = tmp_path / "outside.mp4"
        target.write_bytes(b"outside")
    elif kind == "directory":
        target = media.parent / "directory.mp4"
        target.mkdir()
    elif kind == "state":
        target = state_path
    elif kind == "fragment":
        target = state_path.parent / "vid.f137.mp4"
        target.write_bytes(b"fragment")
    elif kind == "custom_work":
        store.remember(tmp_path / "elsewhere", work_root=media.parent)
    elif kind == "shared":
        save_record(media.parent, "other", output_path=str(media))
    elif kind == "hardlink":
        os.link(media, tmp_path / "hardlink.mp4")
    elif kind == "traversal":
        target = media.parent / ".." / media.parent.name / media.name
    save_record(media.parent, "vid", output_path=str(target))
    selected = next(item for item in store.load() if item.recording_id == "vid")
    before = {path: path.read_bytes() for path in (media, state_path)}
    with pytest.raises((ValueError, OSError)):
        store.trash(selected)
    assert all(path.read_bytes() == contents for path, contents in before.items())


@pytest.mark.parametrize("link_kind", ["file", "state", "junction"])
def test_real_links_and_junctions_are_rejected(tmp_path, link_kind):
    store, media, state_path = archive(tmp_path)
    if link_kind == "junction":
        if os.name != "nt":
            pytest.skip("Windows junction")
        link = tmp_path / "junction"
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(media.parent)],
                       check=True, capture_output=True)
        store = ArchiveStore(tmp_path / "linked-roots.json")
        store.remember(link)
        save_record(media.parent, "vid", output_path=str(link / media.name))
    else:
        original = media if link_kind == "file" else state_path
        moved = tmp_path / original.name
        original.rename(moved)
        try:
            original.symlink_to(moved)
        except OSError as exc:
            pytest.skip(f"Symlink privilege unavailable: {exc}")
    selected, = store.load()
    with pytest.raises(ValueError, match="링크|정션"):
        store.trash(selected)


def test_corrupt_exclusions_block_trash_without_losing_roots(tmp_path):
    store, media, _ = archive(tmp_path)
    store.dismissed_path.write_text("broken", encoding="utf-8")
    store = ArchiveStore(store.path)
    selected, = store.load()
    with pytest.raises(ValueError, match="제외목록"):
        store.trash(selected)
    assert media.read_bytes() == b"test media"
    assert store.dismissed_path.read_text(encoding="utf-8") == "broken"


@pytest.mark.parametrize("kind", ["file_reparse", "parent_reparse", "state_link"])
def test_link_flags_rejected_without_symlink_privilege(tmp_path, monkeypatch, kind):
    store, media, state_path = archive(tmp_path)
    selected, = store.load()
    original = Path.lstat
    rejected = {"file_reparse": media, "parent_reparse": media.parent, "state_link": state_path}[kind]
    def lstat(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == rejected:
            return SimpleNamespace(st_mode=stat.S_IFLNK if kind == "state_link" else info.st_mode,
                                   st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return info
    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(ValueError, match="링크|정션"):
        store.trash(selected)


def test_trash_rejects_network_volume_without_calling_trash(tmp_path, monkeypatch):
    store, media, _ = archive(tmp_path)
    selected, = store.load()
    calls = []
    monkeypatch.setattr(backend, "_remote_volume", lambda path: path == media)
    monkeypatch.setattr(TrashFile, "moveToTrash", lambda file: calls.append(file.fileName()) or True)
    with pytest.raises(ValueError, match="네트워크·NAS"):
        store.trash(selected)
    assert not calls and media.read_bytes() == b"test media"
    assert not store.dismissed_path.exists()


@pytest.mark.parametrize("raw", [r"\\192.168.173.222\slot1\__a\vid.mp4", "//nirvanas/slot1/vid.mp4"])
def test_unc_paths_count_as_remote_volumes(raw):
    assert backend._remote_volume(Path(raw))


def test_ancestor_reparse_above_output_dir_does_not_block_trash(tmp_path, monkeypatch):
    store, media, _ = archive(tmp_path)
    selected, = store.load()
    original = Path.lstat
    outside = media.parent.parent
    def lstat(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if Path(path) == outside:
            return SimpleNamespace(st_mode=info.st_mode,
                                   st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return info
    monkeypatch.setattr(Path, "lstat", lstat)
    trash = tmp_path / "test-trash.mp4"
    monkeypatch.setattr(TrashFile, "moveToTrash", lambda file: Path(file.fileName()).rename(trash) or True)
    store.trash(selected)
    assert not media.exists() and trash.read_bytes() == b"test media"


def test_confirmation_cancel_default_and_selected_command(state, tmp_path, monkeypatch):
    store, media, _ = archive(tmp_path)
    item, = store.load()
    state.attach(EventSource())
    state.apply(ev.CompletedChanged((item,)))
    dialog = ArchiveDialog(state)
    dialog.table.selectRow(0)
    commands = []
    state.command_requested.connect(commands.append)
    def cancel(box):
        assert item.title in box.text()
        assert item.output_path in box.informativeText()
        assert "휴지통" in box.text() and "영구 삭제로 재시도하지 않습니다" in box.informativeText()
        assert "네트워크·NAS" in box.informativeText()
        assert box.textFormat() is Qt.TextFormat.PlainText
        assert box.defaultButton().text() == box.escapeButton().text() == "취소"
        box.defaultButton().click()
        return 0
    monkeypatch.setattr(QMessageBox, "exec", cancel)
    dialog.delete_selected()
    assert not commands and state.archive == (item,)
    assert media.read_bytes() == b"test media"
    def accept(box):
        next(button for button in box.buttons() if button.text() == "휴지통으로 이동").click()
        return 0
    monkeypatch.setattr(QMessageBox, "exec", accept)
    dialog.delete_selected()
    assert commands == [cmd.DeleteArchiveFile(item)]
    assert not dialog.delete_button.isEnabled() and not dialog.dismiss_button.isEnabled()
    state.apply(ev.ArchiveDeleteFinished(error="이동 실패"))
    assert dialog.action_label.text() == "이동 실패" and dialog.delete_button.isEnabled()
    state.apply(ev.ArchiveDeleteFinished(True, False, "파일 이동 후 이력 저장 실패"))
    assert "이력 저장 실패" in dialog.action_label.text()
    state.apply(ev.RecordingStarted(Recording(recording_id="vid", title="active")))
    assert not dialog.delete_button.isEnabled()
    dialog.close()


def test_delete_result_redaction():
    result = sanitize_event(ev.ArchiveDeleteFinished(True, False, "token=private"))
    assert result == ev.ArchiveDeleteFinished(True, False, "token=[REDACTED]")
