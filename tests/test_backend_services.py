from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone

from backend_fakes import FakeAuth, FakeRecorder, FakeYouTube
from yt_rec.backend.archive import ArchiveStore
from yt_rec.backend.controller import WatchController
from yt_rec.backend.selection import MemorySeenStore, MemorySelectionStore
from yt_rec.backend.source import BackendSource, create_backend_source
from yt_rec.backend.tokens import MemoryTokenStore
from yt_rec.logs import LogStore, sanitize_event
from yt_rec.recording.options import RecordingOptions, load_settings, save_settings
from yt_rec.state import commands as cmd
from yt_rec.state import events as ev
from yt_rec.state.models import CompletedRecording, CompletionStatus, LogEntry, Severity, WatchedChannel

from test_archive import save_record


def controller(options, emit):
    return WatchController(
        emit=emit, auth=FakeAuth(), tokens=MemoryTokenStore(),
        selection=MemorySelectionStore(), recorder=FakeRecorder(),
        youtube_factory=lambda _: FakeYouTube(), options=options,
        settings_saver=lambda _: None,
    )


def test_loaded_diagnostic_notes_and_save_errors_are_redacted():
    archive = sanitize_event(ev.CompletedChanged((CompletedRecording(
        recording_id="vid", title="title", note="refresh_token=private",
    ),)))
    assert archive.completed[0].note == "refresh_token=[REDACTED]"
    error = sanitize_event(ev.SettingsSaveFailed("token=private"))
    assert error.message == "token=[REDACTED]"
    assert sanitize_event(ev.ArchiveDismissFinished(error="token=private")).error == "token=[REDACTED]"


def test_archive_dismiss_rechecks_missing_active_and_unfinished_records(state, tmp_path):
    paths = [save_record(tmp_path, name) for name in ("missing", "restored", "active", "unfinished")]
    store = ArchiveStore(tmp_path / "roots.json")
    store.remember(tmp_path)
    control = controller(RecordingOptions(output_dir=tmp_path), lambda _: None)
    source = BackendSource(control, poll_interval_ms=0, archive_store=store)
    state.attach(source)
    state.command_requested.connect(source.handle_command)
    source.handle_command(cmd.RefreshArchive())
    confirmed = state.archive
    assert len(confirmed) == 4 and all(item.file_missing for item in confirmed)
    (tmp_path / "restored.mp4").write_bytes(b"restored after confirmation")
    control._recorder.recording.add("active")
    save_record(tmp_path, "unfinished", status="recording")
    protected = {path: path.read_bytes() for path in paths}
    results = []
    state.archive_dismiss_finished.connect(results.append)
    assert state.dismiss_archive(confirmed, missing_only=True)
    assert results[-1] == ev.ArchiveDismissFinished(removed_count=1)
    assert {item.recording_id for item in state.archive} == {"active", "restored"}
    assert all(path.read_bytes() == before for path, before in protected.items())
    assert (tmp_path / "restored.mp4").read_bytes() == b"restored after confirmation"
    source.handle_command(cmd.RefreshArchive())
    assert {item.recording_id for item in state.archive} == {"active", "restored"}
    assert {item.recording_id for item in ArchiveStore(store.path).load()} == {"active", "restored"}


def test_archive_dismiss_does_not_hide_new_generation_or_active_manual_selection(state, tmp_path):
    save_record(tmp_path, "vid")
    store = ArchiveStore(tmp_path / "roots.json")
    store.remember(tmp_path)
    control = controller(RecordingOptions(output_dir=tmp_path), lambda _: None)
    source = BackendSource(control, poll_interval_ms=0, archive_store=store)
    state.attach(source)
    state.command_requested.connect(source.handle_command)
    state.refresh_archive()
    old = state.archive
    save_record(tmp_path, "vid", finished_at=3000)
    state.dismiss_archive(old)
    assert len(state.archive) == 1
    control._recorder.recording.add("vid")
    state.dismiss_archive(state.archive)
    assert len(state.archive) == 1
    assert not store.dismissed_path.exists()


def test_archive_dismiss_failure_does_not_change_ui_and_session_failure_stays_hidden(state, tmp_path, monkeypatch):
    store = ArchiveStore(tmp_path / "roots.json")
    source = BackendSource(controller(RecordingOptions(output_dir=tmp_path), lambda _: None),
                           poll_interval_ms=0, archive_store=store)
    state.attach(source)
    state.command_requested.connect(source.handle_command)
    failure = CompletedRecording(recording_id="vid", title="startup failed",
                                 status=CompletionStatus.FAILED, finished_at=datetime.now(timezone.utc))
    source.publish(ev.RecordingFinished(failure))
    results = []
    state.archive_dismiss_finished.connect(results.append)
    with monkeypatch.context() as patch:
        def cannot_save(_items):
            raise PermissionError("token=private")
        patch.setattr(store, "dismiss", cannot_save)
        state.dismiss_archive(state.archive)
    assert results[-1].removed_count == 0 and "[REDACTED]" in results[-1].error
    assert state.archive == (failure,)
    state.dismiss_archive(state.archive)
    assert results[-1] == ev.ArchiveDismissFinished(removed_count=1)
    assert not state.archive
    state.refresh_archive()
    assert not state.archive


def test_archive_dismiss_without_persistent_store_reports_failure(state, tmp_path):
    source = BackendSource(controller(RecordingOptions(output_dir=tmp_path), lambda _: None), poll_interval_ms=0)
    state.attach(source)
    state.command_requested.connect(source.handle_command)
    results = []
    state.archive_dismiss_finished.connect(results.append)
    state.dismiss_archive(())
    assert results[-1].error
    assert results[-1].removed_count == 0


def test_channel_error_does_not_expose_authorization_in_state(state):
    state.apply(ev.ChannelsChanged((WatchedChannel(
        channel_id="UC1", name="channel",
        last_check_result="조회 실패: Authorization: Bearer FAKE-ONLY-SECRET",
    ),)))
    assert "FAKE-ONLY-SECRET" not in state.channels[0].last_check_result
    assert "[REDACTED]" in state.channels[0].last_check_result


def test_archive_refresh_keeps_a_startup_failure_without_state_file(state, tmp_path):
    failure = CompletedRecording(
        recording_id="vid", title="unable to start", status=CompletionStatus.FAILED,
        finished_at=datetime.now(timezone.utc), note="yt-dlp is not installed",
    )
    source = BackendSource(controller(RecordingOptions(output_dir=tmp_path), lambda _: None),
                           poll_interval_ms=0, archive_store=ArchiveStore(tmp_path / "roots.json"))
    state.attach(source)
    source.publish(ev.RecordingFinished(failure))
    source.handle_command(cmd.RefreshArchive())
    assert state.completed == (failure,)
    assert state.archive == (failure,)
    source.publish(ev.RecordingFinished(CompletedRecording(
        recording_id="vid", title="successful retry", output_path=str(tmp_path / "video.mp4"),
    )))
    assert source._unsaved_results == {}


def test_backend_persists_redacted_logs_and_reloads_archive(tmp_path, qapp):
    output = tmp_path / "recordings"
    directory = output / ".yt-rec" / "vid"
    directory.mkdir(parents=True)
    video = output / "video.mp4"
    video.write_bytes(b"media")
    (directory / "state.json").write_text(json.dumps({
        "video_id": "vid", "status": "completed", "output_path": str(video),
        "metadata": {"title": "Recorded"}, "verification": {"duration": 42},
        "finished_at": 1,
    }), encoding="utf-8")
    options = RecordingOptions(output_dir=output, log_retention_days=30)
    records = LogStore(tmp_path / "logs")
    source = BackendSource(controller(options, lambda _: None), poll_interval_ms=0,
                           log_store=records, archive_store=ArchiveStore(tmp_path / "roots.json"))
    events = []
    source.event_ready.connect(events.append)
    source.publish(ev.SettingsChanged(options))
    source.publish(ev.LogAppended(LogEntry(
        at=datetime.now(timezone.utc), severity=Severity.ERROR,
        source="test", message="access_token=should-not-appear",
    )))
    source.handle_command(cmd.RefreshArchive())
    assert records.read_recent()[0].message == "access_token=[REDACTED]"
    assert "should-not-appear" not in (tmp_path / "logs" / "yt-rec.log").read_text(encoding="utf-8")
    archive = [event for event in events if isinstance(event, ev.CompletedChanged)][-1]
    assert archive.completed[0].duration.total_seconds() == 42
    assert archive.completed[0].total_bytes == 5
    assert records._file.retention_days == 30
    source.stop()


def test_worker_failure_does_not_discard_following_command(tmp_path, qapp):
    source = BackendSource(controller(RecordingOptions(output_dir=tmp_path), lambda _: None),
                           background=True, poll_interval_ms=0)
    done = threading.Event()
    source.start()
    source._run(lambda: (_ for _ in ()).throw(ValueError("token=secret")))
    source._run(done.set)
    try:
        assert done.wait(2)
        assert source._worker.is_alive()
    finally:
        source.stop()
    assert not source._worker.is_alive()


def test_shutdown_waits_for_running_work_and_discards_pending_commands(tmp_path, qapp):
    source = BackendSource(controller(RecordingOptions(output_dir=tmp_path), lambda _: None),
                           background=True, poll_interval_ms=0)
    entered, release = threading.Event(), threading.Event()
    called = []
    source.start()
    source._run(lambda: (entered.set(), release.wait(3)))
    assert entered.wait(2)
    source._run(lambda: called.append("queued"))
    source.begin_shutdown()
    waiter = threading.Thread(target=source.stop)
    waiter.start()
    time.sleep(0.05)
    assert waiter.is_alive()
    release.set()
    waiter.join(2)
    assert not waiter.is_alive()
    assert called == []
    source._run(lambda: called.append("after shutdown"))
    assert called == []


def test_autostart_failure_and_disk_failure_keep_old_settings(tmp_path, qapp, monkeypatch):
    settings_path = tmp_path / "settings.json"
    options = RecordingOptions(output_dir=tmp_path)
    save_settings(options, settings_path)
    monkeypatch.setattr("yt_rec.backend.source.load_settings", lambda: load_settings(settings_path))
    monkeypatch.setattr("yt_rec.backend.source.default_settings_path", lambda: settings_path)
    monkeypatch.setattr("yt_rec.backend.source.FileSeenStore", MemorySeenStore)
    monkeypatch.setattr("yt_rec.backend.source.FileSelectionStore", MemorySelectionStore)
    monkeypatch.setattr("yt_rec.backend.source.default_token_store", MemoryTokenStore)
    monkeypatch.setattr("yt_rec.backend.source.GoogleAuth", FakeAuth)
    monkeypatch.setattr("yt_rec.backend.source.ArchiveStore", lambda: ArchiveStore(tmp_path / "roots.json"))
    monkeypatch.setattr("yt_rec.backend.source.LogStore", lambda **kw: LogStore(tmp_path / "logs", **kw))
    def disk_error(_):
        raise OSError("disk full")
    monkeypatch.setattr("yt_rec.backend.source.save_settings", disk_error)
    calls = []
    monkeypatch.setattr("yt_rec.desktop.set_autostart", calls.append)
    source = create_backend_source(background=False, poll_interval=0, event_only=False)
    events = []
    source.event_ready.connect(events.append)
    source.handle_command(cmd.UpdateSettings({"autostart": True}))
    assert calls == [True, False]
    assert load_settings(settings_path).autostart is False
    assert source._controller._recorder.options.autostart is False
    assert isinstance(events[-1], ev.SettingsSaveFailed)
    assert "disk full" in events[-1].message
    monkeypatch.setattr("yt_rec.desktop.set_autostart", disk_error)
    source.handle_command(cmd.UpdateSettings({"autostart": True}))
    assert isinstance(events[-1], ev.SettingsSaveFailed)
    source.stop()
