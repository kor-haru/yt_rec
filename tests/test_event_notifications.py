from __future__ import annotations

import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, fields

import pytest

from yt_rec.backend.notifications import LiveNotification, NotificationRecorder, YouTubeSignal
from yt_rec.backend.selection import FileSeenStore, MemorySeenStore, MemorySelectionStore
from yt_rec.backend.youtube import LiveBroadcast, YouTubeApi
from yt_rec.backend.recorder import EngineRecorder
from yt_rec.recording.engine import RecordingEngine
from yt_rec.recording.events import RecordingResult, RecordingStatus
from yt_rec.recording.metadata import LiveMetadata
from yt_rec.recording.options import RecordingOptions

from backend_fakes import FakeResponse, ScriptedSession

VIDEO = "notify00001"
OTHER = "notify00002"
THIRD = "notify00003"


class Api:
    def __init__(self):
        self.calls = []
        self.lives = {v: LiveBroadcast(v, "UC1", "live") for v in (VIDEO, OTHER, THIRD)}
        self.error = None

    def get_live(self, video_id):
        self.calls.append(video_id)
        if self.error:
            raise self.error
        return self.lives.get(video_id)


class Recorder:
    def __init__(self, limit=1):
        self.limit = limit
        self.active = set()
        self.calls = []
        self.error = None

    def is_recording(self, video_id):
        return video_id in self.active

    def start(self, video_id, **metadata):
        if self.error:
            raise self.error
        if len(self.active) >= self.limit:
            return False
        self.active.add(video_id)
        self.calls.append((video_id, metadata))
        return True


def setup_handler(*, seen=None, limit=1):
    api = Api()
    recorder = Recorder(limit)
    selection = MemorySelectionStore(["UC1"])
    updates = []
    handler = NotificationRecorder(
        youtube=lambda: api, selection=selection, recorder=recorder, seen=seen,
        on_update=updates.append, clock=lambda: 20.0,
    )
    return handler, api, recorder, selection, updates


def notice(video_id=VIDEO, *, at=10.0):
    return LiveNotification(video_id, at)


def test_idle_has_zero_queries_or_background_pollers(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("notification handler must not create a thread/timer")

    monkeypatch.setattr(threading.Thread, "start", forbidden)
    handler, api, recorder, _, _ = setup_handler()
    for _ in range(100):
        # Even repeated empty explicit drains cannot issue an API request.
        handler.resume()
    assert api.calls == recorder.calls == []
    assert handler.pending_video_ids == ()


def test_immediate_handoff_is_not_media_or_full_coverage():
    handler, api, recorder, _, updates = setup_handler()
    result = handler.receive(notice())
    assert api.calls == [VIDEO]
    assert recorder.calls[0] == (VIDEO, {"channel_id": "UC1", "channel_name": "", "title": "live"})
    assert result.status == "handed"
    assert result.notification.received_at == 10
    assert result.notification.synthetic is True
    assert result.handed_to_recorder_at == 20
    assert result.first_media_at is None
    handler.report_progress(VIDEO, 0, at=21)
    handler.report_progress(VIDEO, 50, at=19)  # stale previous attempt
    assert len(updates) == 1
    handler.report_progress(VIDEO, 50, at=22)
    handler.report_progress(VIDEO, 100, at=23)
    assert len(updates) == 2
    assert updates[-1].first_media_at == 22
    assert updates[-1].coverage == "unknown"
    assert updates[-1].coverage_reason


@pytest.mark.parametrize("selected,live", [([], True), (["UC2"], True), (["UC1"], False)])
def test_unselected_and_nonlive_never_start(selected, live):
    handler, api, recorder, selection, _ = setup_handler()
    selection.save(selected)
    if not live:
        api.lives.clear()
    assert handler.receive(notice()).status == "ignored"
    assert recorder.calls == []
    assert len(api.calls) == bool(selected)


def test_selection_is_rechecked_after_metadata_request():
    handler, api, recorder, selection, _ = setup_handler()
    original = api.get_live

    def changed(video_id):
        selection.save([])
        return original(video_id)

    api.get_live = changed
    assert handler.receive(notice()).status == "ignored"
    assert recorder.calls == []


def test_duplicate_concurrent_and_out_of_order_notices_only_start_once():
    handler, api, recorder, _, _ = setup_handler(limit=10)
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda n: handler.receive(notice(at=float(n))), range(40)))
    assert {r.status for r in results} == {"handed"}
    assert api.calls == [VIDEO]
    assert len(recorder.calls) == 1


def test_queue_deduplicates_and_drains_only_on_slot_release():
    handler, api, recorder, _, _ = setup_handler()
    handler.receive(notice())
    assert handler.receive(notice(OTHER)).status == "queued"
    assert handler.receive(notice(OTHER, at=1)).status == "queued"
    handler.receive(notice(THIRD))
    assert handler.pending_video_ids == (OTHER, THIRD)
    assert api.calls == [VIDEO, OTHER, THIRD]
    recorder.active.remove(VIDEO)
    assert len(recorder.calls) == 1  # Releasing a fake slot alone cannot poll.
    handler.recording_finished(VIDEO, True)
    assert [call[0] for call in recorder.calls] == [VIDEO, OTHER]
    assert handler.pending_video_ids == (THIRD,)
    assert api.calls.count(OTHER) == 2  # Fresh live/selection check at dequeue.
    recorder.active.remove(OTHER)
    handler.recording_finished(OTHER, True)
    assert [call[0] for call in recorder.calls] == [VIDEO, OTHER, THIRD]


@pytest.mark.parametrize("cancel", ["ended", "unselected"])
def test_queue_rechecks_current_live_and_selection_and_records_reason(cancel):
    handler, api, recorder, selection, updates = setup_handler()
    handler.receive(notice())
    handler.receive(notice(OTHER))
    if cancel == "ended":
        api.lives.pop(OTHER)
    else:
        selection.save(["UC2"])
    recorder.active.remove(VIDEO)
    handler.recording_finished(VIDEO, True)
    assert handler.pending_video_ids == ()
    assert [call[0] for call in recorder.calls] == [VIDEO]
    assert updates[-1].status == "ignored" and updates[-1].reason


@pytest.mark.parametrize("failure", ["metadata", "start", "after_start"])
def test_failed_attempt_is_retryable_only_on_new_event(failure):
    seen = MemorySeenStore()
    handler, api, recorder, _, updates = setup_handler(seen=seen)
    if failure == "metadata":
        api.error = RuntimeError("access_token=topsecret")
    elif failure == "start":
        recorder.error = RuntimeError("access_token=topsecret")
    handler.receive(notice())
    if failure == "after_start":
        recorder.active.remove(VIDEO)
        handler.recording_finished(VIDEO, False)
    assert updates[-1].status == "failed"
    assert "topsecret" not in updates[-1].reason
    assert not seen.is_done(VIDEO) and VIDEO not in seen._started
    calls = len(api.calls)
    handler.resume()
    assert len(api.calls) == calls  # no retry loop
    api.error = recorder.error = None
    assert handler.receive(notice(at=30)).status == "handed"


def test_success_survives_handler_restart_but_stale_started_does_not(tmp_path):
    path = tmp_path / "seen.json"
    seen = FileSeenStore(path)
    handler, _, recorder, _, _ = setup_handler(seen=seen)
    handler.receive(notice())
    recorder.active.remove(VIDEO)
    handler.recording_finished(VIDEO, True)
    restarted, api, _, _, _ = setup_handler(seen=FileSeenStore(path))
    assert restarted.receive(notice()).status == "ignored"
    assert api.calls == []
    restarted._seen.mark_started(OTHER)
    again, _, _, _, _ = setup_handler(seen=FileSeenStore(path))
    assert again.receive(notice(OTHER)).status == "handed"


def test_explicit_reconnect_drains_without_query_while_disconnected():
    handler, api, recorder, _, _ = setup_handler()
    handler._youtube = lambda: None
    assert handler.receive(notice()).status == "queued"
    handler.resume()
    assert api.calls == recorder.calls == []
    handler._youtube = lambda: api
    handler.resume()
    assert api.calls == [VIDEO]
    assert len(recorder.calls) == 1


def test_connection_is_rechecked_after_metadata_request():
    handler, api, recorder, _, _ = setup_handler()
    original = api.get_live

    def disconnected(video_id):
        handler._youtube = lambda: None
        return original(video_id)

    api.get_live = disconnected
    assert handler.receive(notice()).status == "queued"
    assert recorder.calls == [] and handler.pending_video_ids == (VIDEO,)
    handler.resume()
    assert api.calls == [VIDEO]
    api.get_live = original
    handler._youtube = lambda: api
    handler.resume()
    assert api.calls == [VIDEO, VIDEO] and len(recorder.calls) == 1


def test_signal_is_immutable_synthetic_by_default_and_has_no_payload():
    signal = YouTubeSignal(10.0)
    assert signal.synthetic is True
    assert {field.name for field in fields(signal)} == {"received_at", "synthetic"}
    with pytest.raises(FrozenInstanceError):
        signal.received_at = 20.0


@pytest.mark.parametrize("stamp", [-1, float("inf"), float("nan"), "10", None, True])
def test_signal_rejects_invalid_received_time(stamp):
    with pytest.raises(ValueError, match="수신 시각"):
        YouTubeSignal(stamp)


def test_observer_failure_is_visible_redacted_and_does_not_undo_recording(caplog):
    handler, _, recorder, _, _ = setup_handler()

    def broken(result):
        raise RuntimeError("access_token=hidden-secret")

    handler._on_update = broken
    assert handler.receive(notice()).status == "handed"
    assert VIDEO in recorder.active and VIDEO in handler._active
    assert "알림 상태 전달 실패" in caplog.text
    assert "hidden-secret" not in caplog.text


def test_real_recorder_post_release_hook_drains_queue_after_failed_engine(tmp_path):
    entered = queue.Queue()
    release = threading.Event()
    updates = []

    class SyntheticEngine(RecordingEngine):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, ownership_root=tmp_path / "locks", **kwargs)

        def _record(self, video_id, started_at):
            entered.put(video_id)
            if video_id == VIDEO:
                assert release.wait(10)
            return RecordingResult(
                video_id=video_id, status=RecordingStatus.FAILED,
                metadata=LiveMetadata.placeholder_for(video_id), work_dir=self.work_dir_for(video_id),
            )

    recorder = EngineRecorder(
        RecordingOptions(output_dir=tmp_path / "synthetic", max_recordings=1),
        lambda event: None, engine_cls=SyntheticEngine,
        on_result=lambda video_id, ok: handler.recording_finished(video_id, ok),
    )
    api = Api()
    handler = NotificationRecorder(
        youtube=lambda: api, selection=MemorySelectionStore(["UC1"]),
        recorder=recorder, on_update=updates.append,
    )
    try:
        assert handler.receive(notice()).status == "handed"
        assert entered.get(timeout=10) == VIDEO
        assert handler.receive(notice(OTHER)).status == "queued"
        release.set()
        assert entered.get(timeout=10) == OTHER
        recorder.join_all(timeout=10)
        assert handler.pending_video_ids == ()
        assert api.calls == [VIDEO, OTHER, OTHER]
        assert all(update.first_media_at is None for update in updates)
    finally:
        release.set()
        recorder.join_all(timeout=10)


@pytest.mark.parametrize("video_id", ["../danger", "https://youtu.be/notify00001", "", "x" * 12])
def test_notification_requires_one_canonical_video_id(video_id):
    with pytest.raises(ValueError):
        LiveNotification(video_id, 10)


@pytest.mark.parametrize("state", ["live", "upcoming", "none", "ended", "missing_start", "wrong_id", "missing_channel", "title_only"])
def test_single_video_api_is_strict_and_makes_exactly_one_request(state):
    item = {
        "id": VIDEO,
        "snippet": {"channelId": "UC1", "title": "LIVE!", "liveBroadcastContent": "live"},
        "liveStreamingDetails": {"actualStartTime": "2026-09-09T01:00:00Z"},
    }
    if state in ("upcoming", "none"):
        item["snippet"]["liveBroadcastContent"] = state
    if state == "ended":
        item["liveStreamingDetails"]["actualEndTime"] = "2026-09-09T02:00:00Z"
    if state in ("missing_start", "title_only"):
        item["liveStreamingDetails"].clear()
    if state == "wrong_id":
        item["id"] = OTHER
    if state == "missing_channel":
        item["snippet"].pop("channelId")
    if state == "title_only":
        item["snippet"].pop("liveBroadcastContent")
    session = ScriptedSession({"videos": [FakeResponse(200, {"items": [item]})]})
    api = YouTubeApi(session)
    result = api.get_live(VIDEO)
    assert (result is not None) == (state == "live")
    assert len(session.calls) == 1
    assert session.calls[0][0].endswith("/videos")
    assert session.calls[0][1] == {"part": "snippet,liveStreamingDetails", "id": VIDEO}
    assert api.quota_used == 1
