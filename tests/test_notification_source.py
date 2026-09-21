"""Production wiring with fake I/O: proves no real receiver or download."""

from __future__ import annotations

import queue
import threading
import time
from datetime import timedelta

import pytest
from PySide6.QtCore import QThread

from backend_fakes import FakeAuth, FakeYouTube
from yt_rec.backend import source as production
from yt_rec.backend.archive import ArchiveStore
from yt_rec.backend.notifications import LiveNotification
from yt_rec.backend.recorder import EngineRecorder
from yt_rec.backend.selection import FileSeenStore, FileSelectionStore
from yt_rec.backend.tokens import MemoryTokenStore
from yt_rec.backend.youtube import ChannelRef, LiveBroadcast, VideoState
from yt_rec.logs import LogStore
from yt_rec.recording.events import (
    ProgressReported, RecordingFinished, RecordingResult, RecordingStatus,
    StallDetected, StatusChanged,
)
from yt_rec.recording.metadata import LiveMetadata
from yt_rec.recording.options import RecordingOptions, save_settings
from yt_rec.recording.progress import ProgressSnapshot
from yt_rec.state import commands as cmd, events as ev
from yt_rec.state.models import ConnectionState, RecordingState, WatchState

VIDEO, OTHER, THIRD = "notify00001", "notify00002", "notify00003"


def until(qapp, predicate, *, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return
        time.sleep(0.005)
    assert predicate(), "expected backend event was not delivered"


def flush(source, qapp):
    done = threading.Event()
    source._run(done.set)
    until(qapp, done.is_set)
    # Worker completion guarantees emission, not delivery of queued Qt signals.
    qapp.processEvents()


@pytest.fixture
def production_backend(tmp_path, monkeypatch, qapp):
    """Use the actual factory/controller/recorder, replacing external I/O only."""
    engines = {}
    events = []
    selected = FileSelectionStore(tmp_path / "channels.json")
    selected.save(["UC1"])
    seen = FileSeenStore(tmp_path / "seen.json")
    options = RecordingOptions(output_dir=tmp_path / "recordings", max_recordings=1)
    options.output_dir.mkdir()
    settings = tmp_path / "settings.json"
    save_settings(options, settings)

    class Api(FakeYouTube):
        def __init__(self):
            super().__init__(subs=[ChannelRef("UC1", "selected"), ChannelRef("UC2", "other")])
            self.get_calls = []
            self.network_threads = []
            self.lives_by_id = {v: LiveBroadcast(v, "UC1", "test only") for v in (VIDEO, OTHER, THIRD)}
            self.block = None
            self.entered = threading.Event()
            self.release = threading.Event()

        def _network(self):
            self.network_threads.append(QThread.currentThread())

        def load_account_label(self):
            self._network()
            return super().load_account_label()

        def list_subscriptions(self):
            self._network()
            return super().list_subscriptions()

        def get_live(self, video_id):
            self._network()
            self.get_calls.append(video_id)
            if video_id == self.block:
                self.entered.set()
                assert self.release.wait(5)
            return self.lives_by_id.get(video_id)

        def get_video_state(self, video_id):
            live = self.get_live(video_id)
            return VideoState(video_id, "live", live) if live else VideoState(video_id, "none")

    class Engine:
        def __init__(self, options, on_event=None):
            self.options = options
            self.emit = on_event
            self.actions = queue.Queue()

        def clear_stop(self):
            pass

        def recover_pending(self):
            return []

        def request_stop(self):
            self.actions.put(("finish", False))

        def record(self, video_id):
            engines[video_id] = self
            while True:
                action, value = self.actions.get(timeout=10)
                if action == "progress":
                    self.emit(ProgressReported(video_id=video_id, snapshot=ProgressSnapshot(
                        status="downloading", downloaded_bytes=value, elapsed=1,
                    )))
                elif action == "stall":
                    self.emit(StallDetected(video_id=video_id, idle_seconds=30))
                elif action == "status":
                    self.emit(StatusChanged(video_id=video_id, status=value))
                else:
                    result = RecordingResult(
                        video_id=video_id, status=RecordingStatus.COMPLETED if value else RecordingStatus.FAILED,
                        metadata=LiveMetadata.placeholder_for(video_id), work_dir=self.options.output_dir,
                        finished_at=time.time(),
                    )
                    self.emit(RecordingFinished(video_id=video_id, result=result))
                    return result

    api = Api()
    monkeypatch.setattr(production, "load_settings", lambda: options)
    monkeypatch.setattr(production, "save_settings", lambda value: save_settings(value, settings))
    monkeypatch.setattr(production, "default_settings_path", lambda: settings)
    monkeypatch.setattr(production, "FileSeenStore", lambda: seen)
    monkeypatch.setattr(production, "FileSelectionStore", lambda: selected)
    monkeypatch.setattr(production, "default_token_store", lambda: MemoryTokenStore("saved-test-credentials"))
    monkeypatch.setattr(production, "GoogleAuth", FakeAuth)
    monkeypatch.setattr(production, "session_from_credentials", lambda _: object())
    monkeypatch.setattr(production, "YouTubeApi", lambda _: api)
    monkeypatch.setattr(production, "EngineRecorder", lambda *args, **kwargs: EngineRecorder(*args, engine_cls=Engine, **kwargs))
    monkeypatch.setattr(production, "ArchiveStore", lambda: ArchiveStore(tmp_path / "archive.json"))
    monkeypatch.setattr(production, "LogStore", lambda **kwargs: LogStore(tmp_path / "logs", **kwargs))
    sources = []

    def factory(**kwargs):
        source = production.create_backend_source(**kwargs)
        source.event_ready.connect(events.append)
        sources.append(source)
        return source, api, engines, selected, seen, events

    try:
        yield factory
    finally:
        api.release.set()
        for source in sources:
            source.begin_shutdown()
            source.stop()
        qapp.processEvents()


@pytest.fixture
def wired(production_backend, qapp):
    result = production_backend()
    source, _, _, _, _, events = result
    source.start()
    until(qapp, lambda: any(isinstance(e, ev.ConnectionChanged) and e.state is ConnectionState.CONNECTED for e in events))
    flush(source, qapp)
    return result


def notice(video_id=VIDEO):
    # Explicit test double for a future authenticated adapter, not delivery proof.
    return LiveNotification(video_id, time.time(), synthetic=False)


def diagnostic(events, status):
    return [e.entry for e in events if isinstance(e, ev.LogAppended)
            and e.entry.source == "notification" and f": {status};" in e.entry.message]


def test_factory_event_only_never_discovers_on_start_connect_selection_settings_or_ticks(wired, qapp):
    source, api, engines, selected, _, events = wired
    assert source._poll_timer is None
    assert isinstance(source._controller._recorder, EngineRecorder)
    source.handle_command(cmd.DisconnectAccount())
    source.handle_command(cmd.ConnectAccount(session_only=True))
    source.handle_command(cmd.SetWatchedChannels(("UC1", "UC2")))
    source.handle_command(cmd.RefreshSubscriptions())
    source.handle_command(cmd.UpdateSettings({"max_recordings": 2, "poll_interval_seconds": 300}))
    for _ in range(5):
        source.tick()
        source._run(source._controller.tick)
    flush(source, qapp)
    assert selected.load() == ("UC1", "UC2")
    assert source._controller._recorder.options.max_recordings == 2
    assert api.find_calls == api.get_calls == []
    assert not engines
    watch = [e for e in events if isinstance(e, ev.WatchStatusChanged)][-1]
    assert watch.state is WatchState.UNKNOWN and watch.next_check_at is None
    assert all(e.next_check_at is None for e in events if isinstance(e, ev.WatchStatusChanged))
    assert all(thread is not qapp.thread() for thread in api.network_threads)


def test_production_rejects_probe_synthetic_and_untrusted_inputs(wired, qapp):
    source, api, engines, _, _, events = wired
    assert not source.receive_notification(LiveNotification(VIDEO, time.time()), trusted=True)
    assert not source.receive_notification(notice())
    assert not source.receive_notification({"video_id": VIDEO, "synthetic": False}, trusted=True)
    flush(source, qapp)
    assert api.get_calls == [] and not engines
    assert len([e for e in events if isinstance(e, ev.LogAppended) and "알림 거절" in e.entry.message]) == 3
    with pytest.raises(ValueError, match="백그라운드"):
        production.create_backend_source(event_only=True, background=False)


def test_flush_delivers_logs_queued_after_the_first_gui_event_pass(wired, qapp, monkeypatch):
    source, api, engines, _, _, events = wired
    entered, release, drained = (threading.Event() for _ in range(3))

    def block_worker():
        entered.set()
        assert release.wait(5)

    run, process_events = source._run, qapp.processEvents
    source._run(block_worker)
    try:
        assert entered.wait(5)
        assert not source.receive_notification(LiveNotification(VIDEO, time.time()), trusted=True)
        assert not source.receive_notification(notice())
        assert not source.receive_notification({"video_id": VIDEO, "synthetic": False}, trusted=True)

        def mark_drained(work):
            def finish():
                work()
                drained.set()
            run(finish)

        def worker_finishes_after_event_pass():
            process_events()
            release.set()
            assert drained.wait(5)

        # Force the worker to finish between processEvents() and done.is_set().
        # Its three Qt log signals remain queued even though the FIFO is empty.
        with monkeypatch.context() as patch:
            patch.setattr(source, "_run", mark_drained)
            patch.setattr(qapp, "processEvents", worker_finishes_after_event_pass)
            flush(source, qapp)
        assert api.get_calls == [] and not engines
        assert len([e for e in events if isinstance(e, ev.LogAppended) and "알림 거절" in e.entry.message]) == 3
    finally:
        release.set()


def test_trusted_notice_handoff_actual_progress_completion_and_dedup(wired, qapp):
    source, api, engines, _, seen, events = wired
    assert source.receive_notification(notice(), trusted=True)
    until(qapp, lambda: VIDEO in engines and bool(diagnostic(events, "handed")))
    source.receive_notification(notice(), trusted=True)
    flush(source, qapp)
    assert api.get_calls == [VIDEO] and api.find_calls == []
    assert diagnostic(events, "received") and len(diagnostic(events, "handed")) == 1
    assert "첫 미디어=None" in diagnostic(events, "handed")[0].message
    # UI status/progress events are not engine byte evidence.
    source.publish(ev.RecordingProgress(VIDEO, 100, timedelta(seconds=1), state=RecordingState.STALLED))
    engines[VIDEO].actions.put(("progress", 0))
    engines[VIDEO].actions.put(("status", RecordingStatus.FETCHING_METADATA))
    engines[VIDEO].actions.put(("stall", None))
    flush(source, qapp)
    assert not diagnostic(events, "receiving")
    engines[VIDEO].actions.put(("progress", 100))
    engines[VIDEO].actions.put(("finish", True))
    until(qapp, lambda: bool(diagnostic(events, "completed")))
    assert len(diagnostic(events, "receiving")) == 1
    assert "첫 미디어=None" not in diagnostic(events, "receiving")[0].message
    assert all("coverage=unknown" in entry.message for status in ("handed", "receiving", "completed") for entry in diagnostic(events, status))
    assert FileSeenStore(seen.path).is_done(VIDEO)
    source.receive_notification(notice(), trusted=True)
    flush(source, qapp)
    assert api.get_calls == [VIDEO]
    assert all(thread is not qapp.thread() for thread in api.network_threads)


def test_slot_release_and_capacity_changes_resume_only_pending_notices(wired, qapp):
    source, api, engines, _, _, events = wired
    source.receive_notification(notice(), trusted=True)
    until(qapp, lambda: VIDEO in engines)
    source.receive_notification(notice(OTHER), trusted=True)
    source.receive_notification(notice(OTHER), trusted=True)
    until(qapp, lambda: source._notifications.pending_video_ids == (OTHER,))
    assert api.get_calls == [VIDEO, OTHER]
    source.handle_command(cmd.UpdateSettings({"max_recordings": 2}))
    until(qapp, lambda: OTHER in engines)
    source.receive_notification(notice(THIRD), trusted=True)
    until(qapp, lambda: source._notifications.pending_video_ids == (THIRD,))
    engines[VIDEO].actions.put(("finish", False))
    until(qapp, lambda: THIRD in engines)
    assert api.get_calls == [VIDEO, OTHER, OTHER, THIRD, THIRD]
    assert not source._notifications.pending_video_ids
    assert diagnostic(events, "failed")
    assert api.find_calls == []


def test_reconnect_and_selection_revalidate_pending_without_discovery(wired, qapp):
    source, api, engines, _, _, events = wired
    source.handle_command(cmd.DisconnectAccount())
    flush(source, qapp)
    source.receive_notification(notice(), trusted=True)
    until(qapp, lambda: source._notifications.pending_video_ids == (VIDEO,))
    assert api.get_calls == []
    source.handle_command(cmd.ConnectAccount(session_only=True))
    until(qapp, lambda: VIDEO in engines)
    source.receive_notification(notice(OTHER), trusted=True)
    until(qapp, lambda: source._notifications.pending_video_ids == (OTHER,))
    source.handle_command(cmd.SetWatchedChannels(("UC2",)))
    until(qapp, lambda: not source._notifications.pending_video_ids)
    assert OTHER not in engines and diagnostic(events, "ignored")
    source.receive_notification(notice(THIRD), trusted=True)
    flush(source, qapp)
    assert THIRD not in engines
    assert api.find_calls == []


def test_shutdown_during_video_check_prevents_start_and_new_input(wired, qapp):
    source, api, engines, _, seen, _ = wired
    api.block = VIDEO
    source.receive_notification(notice(), trusted=True)
    until(qapp, api.entered.is_set)
    source.begin_shutdown()
    api.release.set()
    source.stop()
    assert not engines and not seen._started
    assert not source.receive_notification(notice(OTHER), trusted=True)
    assert api.get_calls == [VIDEO] and api.find_calls == []


def test_shutdown_keeps_completion_seen_without_draining_pending(wired, qapp, monkeypatch):
    source, api, engines, _, seen, _ = wired
    source.receive_notification(notice(), trusted=True)
    until(qapp, lambda: VIDEO in engines)
    source.receive_notification(notice(OTHER), trusted=True)
    until(qapp, lambda: source._notifications.pending_video_ids == (OTHER,))
    # Simulate a completed engine releasing its slot during shutdown.
    monkeypatch.setattr(engines[VIDEO], "request_stop", lambda: engines[VIDEO].actions.put(("finish", True)))
    source.begin_shutdown()
    source.stop()
    assert FileSeenStore(seen.path).is_done(VIDEO)
    assert OTHER not in engines
    assert api.get_calls == [VIDEO, OTHER]
    assert not source._worker.is_alive()


def test_shutdown_between_notice_guard_and_recorder_start_rejects_handoff(wired, qapp, monkeypatch):
    source, api, engines, _, seen, _ = wired
    recorder = source._controller._recorder
    entered, release = threading.Event(), threading.Event()
    original = recorder.start
    def delayed_start(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(recorder, "start", delayed_start)
    try:
        source.receive_notification(notice(), trusted=True)
        until(qapp, entered.is_set)
        source.begin_shutdown()
        release.set()
        source.stop()
        assert not engines and not seen._started
        assert not source._notifications.pending_video_ids
        assert recorder.start(OTHER) is False
        assert api.get_calls == [VIDEO]
    finally:
        release.set()


def test_recorder_join_waits_for_post_slot_release_finalization(tmp_path):
    entered, release, joined = threading.Event(), threading.Event(), threading.Event()
    class Engine:
        def __init__(self, *args, **kwargs):
            pass
        def clear_stop(self):
            pass
        def record(self, video_id):
            return type("Result", (), {"succeeded": True})()
    def finalize(_video, _ok):
        entered.set()
        assert release.wait(5)
    recorder = EngineRecorder(RecordingOptions(output_dir=tmp_path), lambda _: None,
                              engine_cls=Engine, on_result=finalize)
    waiter = threading.Thread(target=lambda: (recorder.join_all(timeout=None), joined.set()))
    try:
        recorder.start(VIDEO)
        assert entered.wait(5)
        assert not recorder.is_recording(VIDEO)  # Capacity is available already.
        waiter.start()
        assert not joined.wait(0.05)
        release.set()
        waiter.join(5)
        assert joined.is_set()
    finally:
        release.set()
        recorder.join_all(timeout=5)


def test_slow_notification_lookup_does_not_block_active_engine_progress_or_completion(wired, qapp):
    source, api, engines, _, seen, events = wired
    source.receive_notification(notice(), trusted=True)
    until(qapp, lambda: VIDEO in engines)
    api.block = OTHER
    source.receive_notification(notice(OTHER), trusted=True)
    until(qapp, api.entered.is_set)
    try:
        engines[VIDEO].actions.put(("progress", 100))
        engines[VIDEO].actions.put(("finish", True))
        until(qapp, lambda: bool(diagnostic(events, "completed")), timeout=0.5)
        assert diagnostic(events, "receiving")
        assert FileSeenStore(seen.path).is_done(VIDEO)
        assert not api.release.is_set()
    finally:
        api.release.set()
    until(qapp, lambda: OTHER in engines)
    assert api.get_calls == [VIDEO, OTHER]
