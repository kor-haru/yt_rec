from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from yt_rec.backend import archive as backend
from yt_rec.state import commands as cmd, events as ev
from yt_rec.state.models import CompletedRecording, CompletionStatus
from yt_rec.state.store import MAX_COMPLETED, AppState, EventSource
from yt_rec.ui.archive import ArchiveDialog


def save_record(root: Path, video_id: str, **changes: object) -> Path:
    path = root / ".yt-rec" / video_id / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "video_id": video_id,
        "status": "completed",
        "metadata": {"title": "한글／제목 🎧", "channel": "테스트 채널"},
        "output_path": str(root / f"{video_id}.mp4"),
        "verification": {"duration": 3600.5, "issues": []},
        "downloaded_bytes": 9_999_999,
        "started_at": 1_000,
        "finished_at": 1_120,
        **changes,
    }
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_history_restores_real_size_media_duration_missing_partial_and_bad_records(tmp_path: Path) -> None:
    save_record(tmp_path, "complete")
    (tmp_path / "complete.mp4").write_bytes(b"media")
    save_record(tmp_path, "partial", status="partial", skipped_fragments=[4, 5], verification={"duration": 20, "issues": ["끝 구간 검증 실패"]})
    (tmp_path / "partial.mp4").write_bytes(b"partial")
    save_record(tmp_path, "missing")
    save_record(tmp_path, "failed", status="failed", output_path=None, verification=None, message="병합 실패")
    save_record(tmp_path, "active", status="recording")
    bad = save_record(tmp_path, "bad")
    bad.write_text("broken json", encoding="utf-8")
    items = {item.recording_id: item for item in backend.load_archive(tmp_path)}
    assert len(items) == 5
    assert items["complete"].total_bytes == 5
    assert items["complete"].duration == timedelta(seconds=3600.5)
    assert items["complete"].title == "한글／제목 🎧"
    assert items["complete"].finished_at.tzinfo is not None
    assert items["missing"].status is CompletionStatus.MISSING
    assert items["missing"].output_path == str(tmp_path / "missing.mp4")
    assert items["missing"].total_bytes == -1
    assert items["partial"].status is CompletionStatus.PARTIAL
    assert "4, 5" in items["partial"].note and "정확한 누락 시각" in items["partial"].note
    assert "끝 구간 검증 실패" in items["partial"].note
    assert items["failed"].status is CompletionStatus.FAILED
    assert items["failed"].duration == timedelta()
    assert items["bad"].status is CompletionStatus.FAILED


def test_relative_engine_paths_do_not_depend_on_current_working_directory(tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "recordings"
    save_record(output, "video", output_path="recordings/video.mp4")
    (output / "video.mp4").write_bytes(b"ok")
    monkeypatch.chdir(tmp_path.parent)
    item, = backend.load_archive(output)
    assert item.status is CompletionStatus.COMPLETED
    assert Path(item.output_path) == output / "video.mp4"


def test_output_roots_survive_restart_and_custom_work_root(tmp_path: Path) -> None:
    old = tmp_path / "old"
    new = tmp_path / "new"
    state_path = save_record(old, "old-video")
    (old / "old-video.mp4").write_bytes(b"old")
    save_record(new, "new-video")
    custom = tmp_path / "work"
    custom.mkdir()
    state_path.parent.rename(custom / "old-video")
    registry = tmp_path / "config" / "archive-roots.json"
    store = backend.ArchiveStore(registry)
    store.remember(old, work_root=custom)
    store.remember(new)
    store.remember(new)
    reopened = backend.ArchiveStore(registry)
    assert len(reopened.load()) == 2
    assert len(json.loads(registry.read_text(encoding="utf-8"))) == 2
    (old / "old-video.mp4").unlink()
    items = {item.recording_id: item for item in reopened.load()}
    assert items["old-video"].status is CompletionStatus.MISSING


def test_shared_custom_work_folder_resolves_original_relative_output(tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "old"
    state_path = save_record(output, "video", output_path="old/video.mp4", work_dir="work/video")
    (output / "video.mp4").write_bytes(b"old")
    custom = tmp_path / "work"
    custom.mkdir()
    state_path.parent.rename(custom / "video")
    monkeypatch.chdir(tmp_path.parent)
    item, = backend.load_archive(tmp_path / "new", work_root=custom)
    assert item.output_path == str(output / "video.mp4")
    assert item.status is CompletionStatus.COMPLETED


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_file_manager_routes_preserve_unicode_and_spaces(tmp_path: Path, monkeypatch, platform: str) -> None:
    path = tmp_path / "한글 🎧 ／ 파일.mp4"
    path.write_bytes(b"ok")
    launched = []
    monkeypatch.setattr(backend.sys, "platform", platform)
    monkeypatch.setattr(backend.subprocess, "Popen", lambda args, **kwargs: launched.append(args))
    monkeypatch.setattr(backend.subprocess, "run", lambda args, **kwargs: launched.append(args))
    monkeypatch.setattr(backend.subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    monkeypatch.setattr(backend.os, "startfile", lambda name: launched.append(name), raising=False)
    monkeypatch.setenv("SystemRoot", str(tmp_path / "Windows"))
    backend.open_archive_path(str(path), reveal=True)
    backend.open_archive_path(str(path))
    if platform == "win32":
        explorer = tmp_path / "Windows/explorer.exe"
        assert launched == [f'"{explorer}" /select,"{path}"', str(path)]
    elif platform == "darwin":
        assert launched == [["open", "-R", str(path)], ["open", str(path)]]
    else:
        assert launched[0][0] == "dbus-send"
        assert launched[0][-2:] == [f"array:string:{path.as_uri()}", "string:"]
        assert launched[1] == ["xdg-open", str(path)]


def test_windows_reveal_never_searches_cwd_or_path_for_explorer(tmp_path, monkeypatch):
    path = tmp_path / "한글, 영상.mp4"
    path.write_bytes(b"media")
    fake = tmp_path / "explorer.exe"
    fake.write_bytes(b"not an executable; must never be selected")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))
    system_root = tmp_path / "Windows"
    monkeypatch.setenv("SystemRoot", str(system_root))
    monkeypatch.setattr(backend.sys, "platform", "win32")
    monkeypatch.setattr(backend.subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    launched = []
    monkeypatch.setattr(backend.subprocess, "Popen", lambda argv, **kwargs: launched.append((argv, kwargs)))
    backend.open_archive_path(str(path), reveal=True)
    explorer = system_root / "explorer.exe"
    assert launched == [(f'"{explorer}" /select,"{path}"', {"creationflags": 0, "shell": False})]
    assert fake.read_bytes() == b"not an executable; must never be selected"


@pytest.mark.parametrize("system_root", [None, "", "relative", "../Windows"])
def test_windows_reveal_rejects_missing_or_relative_system_directory(tmp_path, monkeypatch, system_root):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"media")
    monkeypatch.setattr(backend.sys, "platform", "win32")
    if system_root is None:
        monkeypatch.delenv("SystemRoot", raising=False)
    else:
        monkeypatch.setenv("SystemRoot", system_root)
    launched = []
    monkeypatch.setattr(backend.subprocess, "Popen", lambda *args, **kwargs: launched.append(args))
    with pytest.raises(OSError, match="Windows"):
        backend.open_archive_path(str(path), reveal=True)
    assert launched == []


@pytest.mark.parametrize("name", ["recording.mp4", "recording with spaces.mp4", "한글방송.mp4", "comma,only.mp4", "한글, 방송 🎧.mp4"])
def test_windows_explorer_quotes_only_the_path_not_the_select_switch(tmp_path: Path, monkeypatch, name: str) -> None:
    path = tmp_path / name
    path.write_bytes(b"test recording")
    launched = []
    monkeypatch.setattr(backend.sys, "platform", "win32")
    monkeypatch.setattr(backend.subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    monkeypatch.setenv("SystemRoot", str(tmp_path / "Windows"))
    monkeypatch.setattr(backend.subprocess, "Popen", lambda args, **kwargs: launched.append((args, kwargs)))

    backend.open_archive_path(str(path), reveal=True)

    args, options = launched[0]
    command_line = args if isinstance(args, str) else subprocess.list2cmdline(args)
    explorer = tmp_path / "Windows" / "explorer.exe"
    prefix = f'"{explorer}" /select,'
    assert command_line.startswith(prefix)
    assert command_line[len(prefix):].strip() == f'"{path}"'
    assert not options.get("shell", False)
    assert options["creationflags"] == backend.subprocess.CREATE_NO_WINDOW


def test_linux_selection_encodes_uri_without_shell_or_array_delimiter_injection(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "한글 🎧, #100% '방송'.mp4"
    path.write_bytes(b"ok")
    launched = []
    monkeypatch.setattr(backend.sys, "platform", "linux")
    monkeypatch.setattr(backend.subprocess, "run", lambda args, **kwargs: launched.append((args, kwargs)))
    backend.open_archive_path(str(path), reveal=True)
    assert len(launched) == 1
    argv, options = launched[0]
    assert "--session" in argv and "--print-reply" in argv
    assert "--reply-timeout=2000" in argv
    assert argv[-3:] == ["org.freedesktop.FileManager1.ShowItems", f"array:string:{path.as_uri()}", "string:"]
    assert all(character not in argv[-2] for character in (" ", ",", "#", "'", "🎧"))
    assert "%2C" in argv[-2] and "%23" in argv[-2] and "%25" in argv[-2]
    assert options == {"check": True, "timeout": 3, "capture_output": True}


@pytest.mark.parametrize("failure", [
    FileNotFoundError("dbus-send missing"),
    subprocess.CalledProcessError(1, "dbus-send"),
    subprocess.TimeoutExpired("dbus-send", 3),
])
def test_linux_selection_falls_back_to_parent_for_unavailable_service(tmp_path: Path, monkeypatch, failure: Exception) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"ok")
    launched = []
    monkeypatch.setattr(backend.sys, "platform", "linux")
    def run(argv, **kwargs):
        launched.append(argv)
        if argv[0] == "dbus-send":
            raise failure
    monkeypatch.setattr(backend.subprocess, "run", run)
    backend.open_archive_path(str(path), reveal=True)
    assert len(launched) == 2
    assert launched[1] == ["xdg-open", str(path.parent)]


def test_file_actions_reject_missing_files_urls_and_executables(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        backend.open_archive_path(str(tmp_path / "missing.mp4"))
    for path in ("https://example.com/movie.mp4", "relative.mp4", str(tmp_path / "program.exe")):
        with pytest.raises(ValueError):
            backend.open_archive_path(path)


def test_file_manager_failure_is_reported_as_oserror(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"ok")
    monkeypatch.setattr(backend.sys, "platform", "linux")
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, "xdg-open")
    monkeypatch.setattr(backend.subprocess, "run", fail)
    with pytest.raises(OSError, match="파일 관리자를 열지 못했습니다"):
        backend.open_archive_path(str(path), reveal=True)


def test_archive_does_not_trim_history_and_commands_work_before_login(state: AppState) -> None:
    source = EventSource()
    state.attach(source)
    sent = []
    state.command_requested.connect(sent.append)
    items = tuple(CompletedRecording(str(i), str(i)) for i in range(MAX_COMPLETED + 25))
    state.apply(ev.CompletedChanged(items))
    assert len(state.archive) == MAX_COMPLETED + 25
    assert len(state.completed) == MAX_COMPLETED
    assert state.snapshot().archive == items
    assert state.refresh_archive()
    assert state.open_recording_path("C:/movie.mp4", reveal=True)
    assert sent == [cmd.RefreshArchive(), cmd.OpenRecordingPath("C:/movie.mp4", reveal=True)]
    done = CompletedRecording("new", "새 녹화")
    state.apply(ev.RecordingFinished(done))
    assert len(state.archive) == MAX_COMPLETED + 26
    state.detach(source)


def test_archive_search_sort_badges_actions_and_live_refresh(state: AppState) -> None:
    source = EventSource()
    state.attach(source)
    sent = []
    state.command_requested.connect(sent.append)
    timestamp = datetime(2026, 9, 1, tzinfo=timezone.utc)
    items = tuple(CompletedRecording(
        recording_id=str(i), title=f"방송 {i:03}／🎧", channel_name="Mix Channel" if i % 2 else "채널",
        finished_at=timestamp + timedelta(days=i), total_bytes=i * 10, duration=timedelta(seconds=60),
        status=(CompletionStatus.COMPLETED, CompletionStatus.PARTIAL, CompletionStatus.FAILED, CompletionStatus.MISSING)[i % 4],
        output_path=f"C:/녹화/{i} 방송 🎧.mp4", note="받지 못한 조각: 8, 9" if i % 4 == 1 else "",
    ) for i in range(205))
    state.apply(ev.CompletedChanged(items))
    dialog = ArchiveDialog(state)
    assert dialog.model.rowCount() == 205
    assert dialog.model.columnCount() == 6
    assert sent == [cmd.RefreshArchive()]
    assert dialog.proxy.data(dialog.proxy.index(0, 1)) == "방송 204／🎧"
    dialog.table.sortByColumn(4, Qt.SortOrder.AscendingOrder)
    assert dialog.proxy.data(dialog.proxy.index(0, 4)) == "0 B"
    dialog.table.sortByColumn(1, Qt.SortOrder.AscendingOrder)
    assert dialog.proxy.data(dialog.proxy.index(1, 1)) == "방송 001／🎧"
    dialog.search_edit.setText("mIX cHAN")
    assert dialog.proxy.rowCount() == 102
    dialog.search_edit.setText("방송 001／🎧")
    assert dialog.proxy.rowCount() == 1
    dialog.table.selectRow(0)
    assert dialog.proxy.data(dialog.proxy.index(0, 5)) == "부분 복구"
    assert "8, 9" in dialog.detail_label.text()
    dialog.copy_selected()
    assert QApplication.clipboard().text() == items[1].output_path
    dialog.play_selected()
    dialog.reveal_selected()
    assert sent[-2:] == [cmd.OpenRecordingPath(items[1].output_path), cmd.OpenRecordingPath(items[1].output_path, reveal=True)]
    dialog.search_edit.setText("방송 003／🎧")
    dialog.table.selectRow(0)
    assert not dialog.play_button.isEnabled()
    assert not dialog.reveal_button.isEnabled()
    assert dialog.copy_button.isEnabled()
    assert dialog.proxy.data(dialog.proxy.index(0, 5)) == "파일 없음"
    dialog.search_edit.setText("[")
    assert dialog.proxy.rowCount() == 0
    dialog.search_edit.clear()
    state.apply(ev.RecordingFinished(CompletedRecording("new", "new recording")))
    assert dialog.proxy.rowCount() == 206
    dialog.close()
    state.detach(source)


@pytest.mark.integration
def test_saved_media_duration_and_file_size_match_ffprobe(tmp_path: Path, make_clip, toolchain) -> None:
    clip = make_clip("verified.mp4", seconds=2)
    result = subprocess.run([str(toolchain.ffprobe), "-v", "error", "-show_entries", "format=duration,size", "-of", "json", str(clip)], capture_output=True, check=True)
    media = json.loads(result.stdout)["format"]
    save_record(tmp_path, "video", output_path=str(clip), verification={"duration": float(media["duration"]), "issues": []})
    item, = backend.load_archive(tmp_path)
    assert item.total_bytes == int(media["size"])
    assert item.duration.total_seconds() == float(media["duration"])
