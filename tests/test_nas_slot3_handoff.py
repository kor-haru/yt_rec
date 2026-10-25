"""Local-only checks: no external converter import, NAS access, or media processes."""

import importlib.util
import json
import os
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


HELPER = Path(__file__).resolve().parents[1] / "maintenance/nas-convert/resume_slot3.py"
spec = importlib.util.spec_from_file_location("resume_slot3", HELPER)
handoff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(handoff)
ARMED = datetime(2026, 10, 26, 1, 0, 0)


def terminal_state():
    return {
        "folder": handoff.SOURCE_FOLDER,
        "finished_at": "2026-10-26 01:00:01",
        "current": None,
        "items": [{"file": "a.mp4", "status": "done"},
                  {"file": "b.mp4", "status": "skipped"},
                  {"file": "bad.mkv", "status": "failed", "error": "EBML header parsing failed"}],
        "summary": {"pending": 0, "running": 0, "done": 1, "skipped": 1, "failed": 1},
    }


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    api = SimpleNamespace(
        LOCAL_DIR=tmp_path, JOB_DEF=tmp_path / "job.json", STOP_FILE=tmp_path / "stop.request",
        LOCK=tmp_path / "convert.lock", JOB=tmp_path / "current-job.json",
        pid_alive=Mock(side_effect=[True, True, False]), launch_background=Mock(), log=Mock(),
    )
    api.JOB_DEF.write_text(json.dumps({"folders": [handoff.SAVED_FOLDER], "max_ratio": 1.0,
                                       "apple_tv": False, "blackout": "04:50-05:15"}), encoding="utf-8")
    api.STOP_FILE.write_bytes(b"2026-10-24 18:07:22")
    progress = tmp_path / handoff.PROGRESS_NAME
    progress.write_text(json.dumps({"folder": handoff.SOURCE_FOLDER, "current": {"file": "a.mkv"},
                                    "items": [{"file": "a.mkv", "status": "running"}]}), encoding="utf-8")
    clock = Mock(wraps=datetime)
    clock.now.return_value = ARMED
    monkeypatch.setattr(handoff, "datetime", clock)
    monkeypatch.setattr(handoff.time, "sleep", lambda _: progress.write_text(json.dumps(terminal_state()), encoding="utf-8"))
    return api, progress


def test_waits_for_original_engine_then_hands_off_once(runtime):
    api, progress = runtime
    job_before = api.JOB_DEF.read_bytes()
    assert handoff.run(api, 24468) == 0
    api.launch_background.assert_not_called()
    assert not api.STOP_FILE.exists()
    assert not api.STOP_FILE.with_name("stop.request.handoff-24468").exists()
    assert not api.LOCK.exists()
    assert api.JOB_DEF.read_bytes() == job_before
    assert json.loads(progress.read_text(encoding="utf-8"))["items"][2]["error"] == "EBML header parsing failed"


@pytest.mark.parametrize("change", [
    lambda s: s.update(folder=handoff.SAVED_FOLDER),
    lambda s: s.update(finished_at="2026-10-25 23:00:00"),
    lambda s: s.pop("finished_at"),
    lambda s: s.update(finished_at="garbage"),
    lambda s: s.update(current={"file": "still-running.mkv"}),
    lambda s: s.pop("current"),
    lambda s: s.update(items=[]),
    lambda s: s["items"][0].update(status="pending"),
    lambda s: s["items"][0].update(status="running"),
    lambda s: s["items"][0].update(status=[]),
    lambda s: s["items"][0].update(file=[]),
    lambda s: s["summary"].update(pending=1),
    lambda s: s["summary"].update(failed=0),
])
def test_incomplete_or_stale_state_never_finishes(change):
    state = terminal_state()
    change(state)
    assert not handoff.finished_state(state, ARMED, None)


def test_unchanged_finished_timestamp_is_not_new_completion():
    state = terminal_state()
    assert not handoff.finished_state(state, ARMED, state["finished_at"])


@pytest.mark.parametrize("kind", ["lock", "current-job", "pending", "malformed", "missing"])
def test_crash_or_unreleased_state_never_clears_stop(runtime, monkeypatch, kind):
    api, progress = runtime
    stop_before = handoff.snapshot(api.STOP_FILE)
    def stopped(_):
        state = terminal_state()
        if kind == "lock":
            api.LOCK.write_text("24468", encoding="utf-8")
        elif kind == "current-job":
            api.JOB.write_text("{}", encoding="utf-8")
        elif kind == "pending":
            state["items"][0]["status"] = "pending"
        elif kind == "malformed":
            progress.write_text("{", encoding="utf-8")
            return
        elif kind == "missing":
            progress.unlink()
            return
        progress.write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setattr(handoff.time, "sleep", stopped)
    assert handoff.run(api, 24468) == 1
    api.launch_background.assert_not_called()
    assert handoff.snapshot(api.STOP_FILE) == stop_before


@pytest.mark.parametrize("kind", ["job", "stop-content", "stop-metadata", "stop-missing"])
def test_changed_operator_intent_cancels_without_launch(runtime, monkeypatch, kind):
    api, _ = runtime
    def changed(_):
        if kind == "job":
            api.JOB_DEF.write_text(json.dumps({"folders": ["D:\\other"]}), encoding="utf-8")
        elif kind == "stop-content":
            api.STOP_FILE.write_bytes(b"new pause")
        elif kind == "stop-metadata":
            stamp = api.STOP_FILE.stat()
            os.utime(api.STOP_FILE, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1))
        else:
            api.STOP_FILE.unlink()
    monkeypatch.setattr(handoff.time, "sleep", changed)
    assert handoff.run(api, 24468) == 1
    api.launch_background.assert_not_called()
    if kind == "stop-content":
        assert api.STOP_FILE.read_bytes() == b"new pause"
    if kind == "job":
        assert json.loads(api.JOB_DEF.read_text(encoding="utf-8"))["folders"] == ["D:\\other"]


def test_cannot_arm_after_original_engine_exits(runtime):
    api, _ = runtime
    api.pid_alive.side_effect = None
    api.pid_alive.return_value = False
    assert handoff.run(api, 24468) == 1
    assert api.STOP_FILE.exists()
    api.launch_background.assert_not_called()


def test_active_encode_locks_are_not_touched_while_waiting(runtime, monkeypatch):
    api, progress = runtime
    api.LOCK.write_text("24468", encoding="utf-8")
    api.JOB.write_text(json.dumps({"folder": handoff.SOURCE_FOLDER}), encoding="utf-8")
    def finished(_):
        api.launch_background.assert_not_called()
        assert api.LOCK.read_text(encoding="utf-8") == "24468" and api.STOP_FILE.exists()
        api.LOCK.unlink()
        api.JOB.unlink()
        progress.write_text(json.dumps(terminal_state()), encoding="utf-8")
    monkeypatch.setattr(handoff.time, "sleep", finished)
    assert handoff.run(api, 24468) == 0


def test_missing_stop_prevents_a_second_handoff(runtime):
    api, _ = runtime
    assert handoff.run(api, 24468) == 0
    api.pid_alive.side_effect = None
    api.pid_alive.return_value = True
    assert handoff.run(api, 24468) == 1
    api.launch_background.assert_not_called()


def test_duplicate_claim_does_not_overwrite_or_launch(runtime):
    api, _ = runtime
    claim = api.STOP_FILE.with_name("stop.request.handoff-24468")
    claim.write_bytes(b"previous claim")
    assert handoff.run(api, 24468) == 1
    assert claim.read_bytes() == b"previous claim" and api.STOP_FILE.exists()
    api.launch_background.assert_not_called()


def test_new_stop_between_claim_and_commit_is_preserved(runtime, monkeypatch):
    api, _ = runtime
    original_rename = Path.rename
    def new_stop(path, target):
        result = original_rename(path, target)
        if path == api.STOP_FILE:
            path.write_bytes(b"new pause")
        return result
    monkeypatch.setattr(Path, "rename", new_stop)
    assert handoff.run(api, 24468) == 1
    assert api.STOP_FILE.read_bytes() == b"new pause"
    api.launch_background.assert_not_called()


def test_new_job_during_claim_restores_pause_without_rewriting_job(runtime, monkeypatch):
    api, _ = runtime
    original_rename = Path.rename
    before = handoff.snapshot(api.STOP_FILE)
    def new_job(path, target):
        result = original_rename(path, target)
        if path == api.STOP_FILE:
            api.JOB_DEF.write_text(json.dumps({"folders": ["D:\\new"]}), encoding="utf-8")
        return result
    monkeypatch.setattr(Path, "rename", new_job)
    assert handoff.run(api, 24468) == 1
    assert handoff.snapshot(api.STOP_FILE) == before
    assert json.loads(api.JOB_DEF.read_text(encoding="utf-8"))["folders"] == ["D:\\new"]
    api.launch_background.assert_not_called()


def test_claim_permission_failure_preserves_pause(runtime, monkeypatch):
    api, _ = runtime
    before = handoff.snapshot(api.STOP_FILE)
    def denied(*_):
        raise PermissionError("claim denied")
    monkeypatch.setattr(Path, "rename", denied)
    assert handoff.run(api, 24468) == 1
    assert handoff.snapshot(api.STOP_FILE) == before
    api.launch_background.assert_not_called()


@pytest.mark.parametrize("new_pause", [False, True])
def test_restore_failure_keeps_live_lock_until_pause_exists(runtime, monkeypatch, new_pause):
    api, _ = runtime
    original_rename = Path.rename
    original_sleep = handoff.time.sleep
    restore_attempts = []
    guarded_retries = []

    def interrupted(path, target):
        if path == api.STOP_FILE:
            result = original_rename(path, target)
            api.JOB_DEF.write_text(json.dumps({"folders": ["D:\\new"]}), encoding="utf-8")
            return result
        restore_attempts.append(path)
        if len(restore_attempts) == 1:
            raise PermissionError("transient restore denial")
        return original_rename(path, target)

    def retry_sleep(seconds):
        if restore_attempts:
            guarded_retries.append(api.LOCK.read_text(encoding="ascii") == str(os.getpid())
                                   and not api.STOP_FILE.exists())
            if new_pause:
                api.STOP_FILE.write_bytes(b"new operator pause")
        else:
            original_sleep(seconds)

    monkeypatch.setattr(Path, "rename", interrupted)
    monkeypatch.setattr(handoff.time, "sleep", retry_sleep)
    assert handoff.run(api, 24468) == 1
    assert guarded_retries == [True]
    assert api.STOP_FILE.read_bytes() == (b"new operator pause" if new_pause else b"2026-10-24 18:07:22")
    assert len(restore_attempts) == (1 if new_pause else 2)
    assert not api.LOCK.exists()
    api.launch_background.assert_not_called()


def test_new_pause_moved_during_claim_is_protected_from_watchdog(runtime, monkeypatch):
    api, _ = runtime
    original_rename = Path.rename
    observed = []
    def renewed_pause(path, target):
        if path == api.STOP_FILE:
            path.write_bytes(b"renewed pause")
            observed.append(api.LOCK.read_text(encoding="ascii") == str(os.getpid()))
        result = original_rename(path, target)
        # A normal watchdog cannot acquire a shared lock held by this live helper.
        observed.append(api.LOCK.exists())
        return result
    monkeypatch.setattr(Path, "rename", renewed_pause)
    assert handoff.run(api, 24468) == 1
    assert all(observed) and api.STOP_FILE.read_bytes() == b"renewed pause"
    assert not api.LOCK.exists()
    api.launch_background.assert_not_called()


def test_lost_shared_lock_before_claim_is_not_deleted(runtime, monkeypatch):
    api, _ = runtime
    original_read = Path.read_text
    def replaced_owner(path, *args, **kwargs):
        if path == api.LOCK:
            path.write_text("999999", encoding="ascii")
        return original_read(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", replaced_owner)
    assert handoff.run(api, 24468) == 1
    assert api.LOCK.read_text(encoding="ascii") == "999999"
    assert api.STOP_FILE.exists()
    api.launch_background.assert_not_called()


def test_exclusive_lock_race_does_not_overwrite_other_owner(runtime, monkeypatch):
    api, _ = runtime
    original_open = Path.open
    def occupied(path, mode="r", *args, **kwargs):
        if path == api.LOCK and mode == "x":
            with original_open(path, "w", encoding="ascii") as lock:
                lock.write("999999")
        return original_open(path, mode, *args, **kwargs)
    monkeypatch.setattr(Path, "open", occupied)
    assert handoff.run(api, 24468) == 1
    assert api.LOCK.read_text(encoding="ascii") == "999999" and api.STOP_FILE.exists()


def test_job_change_after_commit_is_preserved_for_watchdog_not_direct_launch(runtime, monkeypatch):
    api, _ = runtime
    original_unlink = Path.unlink
    def next_intent(path, *args, **kwargs):
        if path.name == "stop.request.handoff-24468":
            assert api.LOCK.read_text(encoding="ascii") == str(os.getpid())
            api.JOB_DEF.write_text(json.dumps({"folders": ["D:\\new"]}), encoding="utf-8")
        return original_unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", next_intent)
    assert handoff.run(api, 24468) == 0
    assert json.loads(api.JOB_DEF.read_text(encoding="utf-8"))["folders"] == ["D:\\new"]
    assert not api.LOCK.exists() and not api.STOP_FILE.exists()
    api.launch_background.assert_not_called()
