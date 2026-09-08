from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from yt_rec.backend.recorder import translate_engine_event
from yt_rec.logs import LogStore, redact, sanitize_event
from yt_rec.recording.binaries import Toolchain
from yt_rec.recording.engine import RecordingEngine
from yt_rec.recording.events import RecordingStatus, StatusChanged
from yt_rec.recording.merge import MediaVerification
from yt_rec.recording.metadata import LiveMetadata
from yt_rec.recording.options import RecordingOptions
from yt_rec.state import events as ev
from yt_rec.state.models import CompletedRecording, LogEntry, Recording, Severity
from yt_rec.state.store import AppState, EventSource
from yt_rec.ui.logs import LogDialog


def entry(message: str, *, at: datetime | None = None) -> LogEntry:
    return LogEntry(at=at or datetime.now(timezone.utc), severity=Severity.ERROR,
                    source="recorder", message=message)


@pytest.mark.parametrize("message,secret", [
    ('request https://google.test/auth?code=URL-SECRET&state=STATE-SECRET', "URL-SECRET"),
    ('request https%3A%2F%2Fgoogle.test%2Fauth%3Fcode=ENCODED-SECRET', "ENCODED-SECRET"),
    ('Authorization: Bearer HEADER-SECRET', "HEADER-SECRET"),
    ('Basic BASE64-SECRET', "BASE64-SECRET"),
    ('{"refresh_token":\n "REFRESH-SECRET", "other":"ALSO-SECRET"}', "REFRESH-SECRET"),
    ("error: {'access_token': 'ACCESS-SECRET'}", "ACCESS-SECRET"),
    ('client_secret="CLIENT-SECRET"', "CLIENT-SECRET"),
    ('YT_REC_GOOGLE_CLIENT_SECRET=ENV-SECRET', "ENV-SECRET"),
    ('code_verifier: PKCE-SECRET', "PKCE-SECRET"),
    ('Cookie: SID=COOKIE-SECRET; arbitrary=SECOND-SECRET', "SECOND-SECRET"),
    ('.youtube.com\tTRUE\t/\tTRUE\t1999999999\tSID\tNETSCAPE-SECRET', "NETSCAPE-SECRET"),
    ('failed with ya29.ACCESS-SECRET', "ACCESS-SECRET"),
    ('failed with 1//REFRESH-SECRET', "REFRESH-SECRET"),
    ("CalledProcessError: ['yt-dlp', '--password', 'CLI-SECRET']", "CLI-SECRET"),
    ('private_key: -----BEGIN PRIVATE KEY-----\nPRIVATE-SECRET', "PRIVATE-SECRET"),
])
def test_credentials_are_removed_before_log_storage(message, secret, tmp_path):
    safe = redact(message)
    assert secret not in safe
    assert redact(safe) == safe
    store = LogStore(tmp_path)
    store.append(replace(entry(message), source=message))
    store.close()
    text = (tmp_path / "yt-rec.log").read_text(encoding="utf-8")
    assert secret not in text
    assert secret not in repr(store.read_recent())


def test_engine_redacts_before_recording_file_and_event_then_global_store(tmp_path, state):
    raw = "ERROR: Cookie: SID=SECRET-ENGINE; OTHER=SECOND-ENGINE"

    class DiagnosticEngine(RecordingEngine):
        def build_download_argv(self, video_id):
            return [sys.executable, "-c", f"print({raw!r})"]

    store = LogStore(tmp_path / "logs")
    observed = []

    def collect(event):
        observed.append(event)
        for ui_event in translate_engine_event(event, title="test", channel_id="", channel_name="", quality=""):
            if isinstance(ui_event, ev.LogAppended):
                store.append(ui_event.entry)
            state.apply(ui_event)

    engine = DiagnosticEngine(RecordingOptions(output_dir=tmp_path), on_event=collect)
    work_dir = tmp_path / "recording"
    work_dir.mkdir()
    engine._run_download("test-id", work_dir)
    engine._emit(StatusChanged(video_id="test-id", status=RecordingStatus.STALLED,
                               detail="access_token=EXCEPTION-SECRET"))
    store.close()
    assert state.logs and state.logs[0].severity is Severity.ERROR
    evidence = repr(observed) + repr(state.snapshot())
    evidence += (work_dir / "yt-dlp.log").read_text(encoding="utf-8")
    evidence += (tmp_path / "logs" / "yt-rec.log").read_text(encoding="utf-8")
    assert "SECRET-ENGINE" not in evidence and "SECOND-ENGINE" not in evidence
    assert "EXCEPTION-SECRET" not in evidence


def test_state_boundary_sanitizes_errors_without_changing_archive_path(state):
    path = "D:/recordings/movie.mp4"
    done = CompletedRecording(recording_id="video", title="test", output_path=path,
                              note="OAuth error: refresh_token=STATE-SECRET")
    for event in (
        ev.LogAppended(entry("client_secret=STATE-SECRET")),
        ev.RecordingStarted(Recording(recording_id="video", title="test", detail="token=STATE-SECRET")),
        ev.RecordingProgress(recording_id="video", reported_bytes=17,
                             reported_elapsed=timedelta(seconds=3), detail="Cookie:STATE-SECRET"),
        ev.RecordingFinished(done),
    ):
        assert "STATE-SECRET" not in repr(sanitize_event(event))
        state.apply(event)
        assert "STATE-SECRET" not in repr(state.snapshot())
    assert state.completed[0].output_path == path
    assert done.note.endswith("STATE-SECRET"), "입력 데이터는 바꾸지 않는다"


def test_persisted_failure_diagnostics_are_redacted_without_rewriting_media_metadata(tmp_path):
    engine = RecordingEngine(RecordingOptions(output_dir=tmp_path))
    metadata = LiveMetadata(video_id="video", webpage_url="https://media.test/?key=MEDIA-DATA")
    verification = MediaVerification(path=tmp_path / "video.mp4", playable=False, complete=False,
                                      issues=("token=VERIFY-SECRET",), demux_errors=("Cookie: DEMUX-SECRET",))
    result = engine._conclude(
        video_id="video", metadata=metadata, work_dir=tmp_path,
        status=RecordingStatus.FAILED, started_at=0, stalled=False, skipped=(),
        downloaded_bytes=None, denial=None, verification=verification,
        output_path=None, message="client_secret=FAILURE-SECRET",
    )
    saved = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert "VERIFY-SECRET" not in repr(saved) and "DEMUX-SECRET" not in repr(saved)
    assert "FAILURE-SECRET" not in repr(saved)
    assert saved["metadata"]["webpage_url"] == metadata.webpage_url
    assert result.metadata is metadata


def test_reload_latest_1000_and_ignore_corrupt_tail(tmp_path, qapp):
    store = LogStore(tmp_path)
    for index in range(1020):
        store.append(entry(f"message-{index}"))
    store.close()
    with (tmp_path / "yt-rec.log").open("a", encoding="utf-8") as stream:
        stream.write('{"at":')
    reopened = LogStore(tmp_path)
    source = EventSource()
    source.log_store = reopened
    state = AppState(emit_interval_ms=0)
    state.attach(source)
    assert len(state.logs) == 1000
    assert state.logs[0].message == "message-1019"
    assert state.logs[-1].message == "message-20"
    assert state.unseen_error_count == 0
    assert Path(state.log_directory) == tmp_path
    reopened.append(entry("after restart"))
    assert reopened.read_recent()[0].message == "after restart"
    state.detach(source)
    reopened.close()
    state.deleteLater()


def test_rotation_bounds_utf8_bytes_and_reload_across_backups(tmp_path):
    store = LogStore(tmp_path, max_bytes=2048, backup_count=2)
    for index in range(80):
        store.append(entry(f"{index}:" + "한" * 50))
    store.close()
    paths = list(tmp_path.glob("yt-rec.log*"))
    assert len(paths) == 3
    assert all(path.stat().st_size <= 2048 for path in paths)
    recent = store.read_recent()
    assert recent[0].message.startswith("79:")
    assert len(recent) > len((tmp_path / "yt-rec.log").read_text(encoding="utf-8").splitlines())
    assert [int(item.message.split(":")[0]) for item in recent] == sorted(
        (int(item.message.split(":")[0]) for item in recent), reverse=True,
    )


def test_oversize_message_keeps_file_bounded_and_json_readable(tmp_path):
    store = LogStore(tmp_path, max_bytes=1024)
    store.append(replace(entry("한" * 10000), source="\x00" * 10000))
    store.close()
    assert (tmp_path / "yt-rec.log").stat().st_size <= 1024
    assert len(store.read_recent()) == 1


def test_retention_removes_only_owned_old_logs_and_daily_rotation(tmp_path, monkeypatch):
    now = time.time()
    old = now - 15 * 86400
    for name in ("yt-rec.log", "yt-rec.log.1", "metadata.json", "movie.mp4"):
        path = tmp_path / name
        path.write_text("old", encoding="utf-8")
        os.utime(path, (old, old))
    store = LogStore(tmp_path, retention_days=14)
    assert not (tmp_path / "yt-rec.log").exists()
    assert not (tmp_path / "yt-rec.log.1").exists()
    assert (tmp_path / "metadata.json").exists() and (tmp_path / "movie.mp4").exists()
    store.append(entry("today"))
    monkeypatch.setattr("yt_rec.logs.time.time", lambda: now + 86400)
    store.append(entry("tomorrow", at=datetime.fromtimestamp(now + 86400, timezone.utc)))
    assert (tmp_path / "yt-rec.log.1").exists()
    store.close()


def test_retention_setting_filters_old_records_and_is_validated(tmp_path):
    store = LogStore(tmp_path)
    store.append(entry("old", at=datetime.now(timezone.utc) - timedelta(days=3)))
    store.append(entry("recent"))
    store.set_retention_days(1)
    assert [item.message for item in store.read_recent()] == ["recent"]
    with pytest.raises(ValueError):
        store.set_retention_days(0)
    store.close()


def test_recovery_prunes_expired_finished_recording_logs_but_keeps_metadata(tmp_path):
    work = tmp_path / ".yt-rec" / "finished-video"
    work.mkdir(parents=True)
    (work / "state.json").write_text(json.dumps({"status": "completed"}), encoding="utf-8")
    (work / "metadata.json").write_text("{}", encoding="utf-8")
    log = work / "yt-dlp.log"
    log.write_text("old diagnostic", encoding="utf-8")
    old = time.time() - 20 * 86400
    os.utime(log, (old, old))
    tools = Toolchain(ytdlp=Path("yt-dlp"), ffmpeg=Path("ffmpeg"), ffprobe=Path("ffprobe"))
    RecordingEngine(RecordingOptions(output_dir=tmp_path), toolchain=tools).recover_pending()
    assert not log.exists()
    assert (work / "metadata.json").exists() and (work / "state.json").exists()


def test_disk_failure_is_reported_to_caller(tmp_path, monkeypatch):
    store = LogStore(tmp_path)

    def fail():
        raise OSError("disk full")

    monkeypatch.setattr(store._file, "_open", fail)
    with pytest.raises(OSError, match="disk full"):
        store.append(entry("test"))
    store.close()


def test_log_folder_button_opens_global_location(tmp_path, state, monkeypatch):
    store = LogStore(tmp_path)
    source = EventSource()
    source.log_store = store
    state.attach(source)
    opened = []
    monkeypatch.setattr("yt_rec.ui.logs.QDesktopServices.openUrl", lambda url: opened.append(url.toLocalFile()) or True)
    dialog = LogDialog(state)
    assert dialog.open_folder_button.isEnabled()
    dialog.open_folder_button.click()
    assert Path(opened[0]) == tmp_path
    dialog.close()
    store.close()


@pytest.mark.parametrize("heartbeat_interval_ms", [16, 50])
def test_500_logs_per_second_keep_ui_responsive_and_storage_bounded(
    tmp_path, qapp, heartbeat_interval_ms,
):
    seconds = float(os.environ.get("YT_REC_LOG_SOAK_SECONDS", "3"))
    store = LogStore(tmp_path, max_bytes=128 * 1024)
    state = AppState(emit_interval_ms=200)
    dialog = LogDialog(state)
    dialog.show()
    QApplication.processEvents()
    # Qt may coalesce delayed timeouts. Probe frequency is not UI throughput;
    # both normal and coarser probes must enforce the same maximum stall.
    beats = []
    heartbeat = QTimer()
    heartbeat.setInterval(heartbeat_interval_ms)
    heartbeat.timeout.connect(lambda: beats.append(time.monotonic()))
    heartbeat.start()
    started = time.monotonic()
    count = 0
    for batch in range(round(seconds * 5)):
        for _ in range(100):
            safe = store.append(entry(f"message-{count}"))
            state.apply(ev.LogAppended(safe))
            count += 1
        remaining_ms = max(0, int((started + (batch + 1) / 5 - time.monotonic()) * 1000))
        QTest.qWait(remaining_ms)
    state.flush()
    QApplication.processEvents()
    finished = time.monotonic()
    heartbeat.stop()
    store.close()
    assert count == round(seconds * 5) * 100
    assert dialog.model.rowCount() == min(count, 1000)
    # Include startup and the tail: an absent/stopped heartbeat must not pass.
    assert beats, "UI heartbeat never ran"
    samples = [started, *beats, finished]
    max_gap = max(right - left for left, right in zip(samples, samples[1:]))
    assert max_gap < 0.25, f"UI event loop blocked for {max_gap:.3f}s"
    assert finished - started < seconds + 0.25, "fixed log workload missed its deadline"
    assert sum(path.stat().st_size for path in tmp_path.glob("yt-rec.log*")) <= 5 * 128 * 1024
    print(f"logs={count}, seconds={time.monotonic() - started:.2f}, heartbeat_max_gap={max_gap:.3f}s")
    dialog.close()
    state.deleteLater()
