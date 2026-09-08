from __future__ import annotations

import multiprocessing
import threading
from pathlib import Path

import pytest

from yt_rec.recording.binaries import Toolchain
from yt_rec.recording.engine import RecordingEngine
from yt_rec.recording.events import RecordingResult, RecordingStatus
from yt_rec.recording.metadata import LiveMetadata
from yt_rec.recording.options import RecordingOptions
from yt_rec.recording.ownership import RecordingOwnedError, VideoLease

VIDEO = "notify00001"
TOOLS = Toolchain(ytdlp=Path("unused"), ffmpeg=Path("unused"), ffprobe=Path("unused"))


class SyntheticEngine(RecordingEngine):
    """No media/network subprocess, but uses the real public engine boundary."""

    def _record(self, video_id, started_at):
        self.entered.put(("entered", str(self.options.output_dir)))
        assert self.release.wait(10), "test did not release the owner"
        return RecordingResult(
            video_id=video_id, status=RecordingStatus.COMPLETED,
            metadata=LiveMetadata.placeholder_for(video_id), work_dir=self.work_dir_for(video_id),
        )


def _record_child(output, root, gate, release, messages):
    engine = SyntheticEngine(RecordingOptions(output_dir=Path(output)), ownership_root=Path(root))
    engine.entered, engine.release = messages, release
    assert gate.wait(10)
    result = engine.record(VIDEO)
    messages.put(("result", result.status.value))


def _lease_child(root, ready):
    with VideoLease(VIDEO, root=Path(root)):
        ready.set()
        threading.Event().wait(30)  # test terminates only this synthetic child


def test_two_processes_with_different_output_folders_have_one_owner(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    gate, release, messages = ctx.Event(), ctx.Event(), ctx.Queue()
    children = [ctx.Process(target=_record_child, args=(
        str(tmp_path / name), str(tmp_path / "locks"), gate, release, messages,
    )) for name in ("gui-output", "cli-output")]
    try:
        for process in children:
            process.start()
        gate.set()
        outcomes = [messages.get(timeout=15), messages.get(timeout=15)]
        assert sum(kind == "entered" for kind, _ in outcomes) == 1
        assert ("result", "failed") in outcomes
        release.set()
        assert messages.get(timeout=15) == ("result", "completed")
        for process in children:
            process.join(10)
            assert process.exitcode == 0
    finally:
        release.set()
        for process in children:
            if process.is_alive():
                process.terminate()
            process.join(10)
        messages.close()


def test_crash_releases_lease_without_deleting_lock_file(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    child = ctx.Process(target=_lease_child, args=(str(tmp_path), ready))
    child.start()
    try:
        assert ready.wait(15)
        with pytest.raises(RecordingOwnedError):
            VideoLease(VIDEO, root=tmp_path).acquire()
        child.terminate()
        child.join(10)
        assert not child.is_alive()
        with VideoLease(VIDEO, root=tmp_path) as lease:
            assert lease.path.exists()
        assert lease.path.exists()
    finally:
        if child.is_alive():
            child.terminate()
        child.join(10)


def test_same_process_and_case_distinct_ids(tmp_path):
    with VideoLease(VIDEO, root=tmp_path):
        with pytest.raises(RecordingOwnedError):
            with VideoLease(VIDEO, root=tmp_path):
                pytest.fail("second owner entered")
        with VideoLease(VIDEO.upper(), root=tmp_path):
            pass
    with VideoLease(VIDEO, root=tmp_path):
        pass


def test_duplicate_leaves_other_output_metadata_and_artifacts_untouched(tmp_path, monkeypatch):
    options = RecordingOptions(output_dir=tmp_path / "second")
    engine = RecordingEngine(options, toolchain=TOOLS, ownership_root=tmp_path / "locks")
    work = engine.work_dir_for(VIDEO)
    work.mkdir(parents=True)
    artifact = work / "state.json"
    artifact.write_bytes(b"existing state must not change")

    def forbidden(*args, **kwargs):
        pytest.fail("duplicate reached metadata/download")

    monkeypatch.setattr(engine, "_record", forbidden)
    monkeypatch.setattr(LiveMetadata, "load", forbidden)
    with VideoLease(VIDEO, root=tmp_path / "locks"):
        result = engine.record(VIDEO)
    assert result.status is RecordingStatus.FAILED
    assert artifact.read_bytes() == b"existing state must not change"
    assert sorted(p.name for p in work.iterdir()) == ["state.json"]


def test_record_owner_blocks_recovery_even_in_other_output(tmp_path, monkeypatch):
    engine = RecordingEngine(
        RecordingOptions(output_dir=tmp_path / "other"), toolchain=TOOLS, ownership_root=tmp_path / "locks",
    )
    work = engine.work_dir_for(VIDEO)
    work.mkdir(parents=True)
    media = work / f"{VIDEO}.f137.mp4"
    media.write_bytes(b"untouched")
    monkeypatch.setattr(engine, "_finalize", lambda **_: pytest.fail("active owner's files were finalized"))
    with VideoLease(VIDEO, root=tmp_path / "locks"):
        assert engine.recover_pending() == []
    assert media.read_bytes() == b"untouched"


def test_recovery_owner_blocks_new_record_until_finalization_finishes(tmp_path, monkeypatch):
    recovery = RecordingEngine(
        RecordingOptions(output_dir=tmp_path / "old"), toolchain=TOOLS, ownership_root=tmp_path / "locks",
    )
    work = recovery.work_dir_for(VIDEO)
    work.mkdir(parents=True)
    (work / f"{VIDEO}.f137.mp4").write_bytes(b"synthetic")
    entered, release = threading.Event(), threading.Event()

    def finalize(**kwargs):
        entered.set()
        assert release.wait(10)
        return RecordingResult(
            video_id=VIDEO, status=RecordingStatus.PARTIAL,
            metadata=LiveMetadata.placeholder_for(VIDEO), work_dir=work,
        )

    monkeypatch.setattr(recovery, "_finalize", finalize)
    thread = threading.Thread(target=recovery.recover_pending)
    thread.start()
    try:
        assert entered.wait(10)
        recorder = RecordingEngine(
            RecordingOptions(output_dir=tmp_path / "new"), toolchain=TOOLS, ownership_root=tmp_path / "locks",
        )
        monkeypatch.setattr(recorder, "_record", lambda *_: pytest.fail("record entered during recovery"))
        assert recorder.record(VIDEO).status is RecordingStatus.FAILED
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive()
    with VideoLease(VIDEO, root=tmp_path / "locks"):
        pass


def test_lease_failure_fails_closed_before_engine_work(tmp_path, monkeypatch):
    bad_root = tmp_path / "not-a-directory"
    bad_root.write_bytes(b"x")
    engine = RecordingEngine(RecordingOptions(output_dir=tmp_path / "output"), ownership_root=bad_root)
    monkeypatch.setattr(engine, "_record", lambda *_: pytest.fail("recording without lease"))
    assert engine.record(VIDEO).status is RecordingStatus.FAILED
    assert not engine.options.output_dir.exists()


def test_engine_exception_releases_lease_for_next_attempt(tmp_path, monkeypatch):
    engine = RecordingEngine(RecordingOptions(output_dir=tmp_path / "output"), ownership_root=tmp_path / "locks")

    def broken(*_):
        raise RuntimeError("synthetic setup failed")

    monkeypatch.setattr(engine, "_record", broken)
    assert engine.record(VIDEO).status is RecordingStatus.FAILED
    with VideoLease(VIDEO, root=tmp_path / "locks"):
        pass


def test_invalid_id_never_reads_metadata_outside_work_dir(tmp_path, monkeypatch):
    engine = RecordingEngine(RecordingOptions(output_dir=tmp_path), ownership_root=tmp_path / "locks")
    monkeypatch.setattr(LiveMetadata, "load", lambda *_: pytest.fail("invalid ID read metadata"))
    assert engine.record("../../elsewhere").status is RecordingStatus.FAILED
    assert not (tmp_path / "locks").exists()
