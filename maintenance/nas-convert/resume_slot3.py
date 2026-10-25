"""One-shot Windows handoff: finish the active 4K pass, then resume saved slot3."""

from __future__ import annotations

import argparse
import json
import ntpath
import os
import sys
import time
from datetime import datetime
from pathlib import Path

CONVERTER_DIR = Path(r"C:\Users\haru\nas-convert")
SOURCE_FOLDER = r"\\192.168.173.100\e\_e\_4K"
SAVED_FOLDER = r"\\192.168.173.222\slot3"
PROGRESS_NAME = "progress-192-168-173-100-e-e-4k.json"


def snapshot(path: Path) -> tuple:
    """Include identity/mtime as well as bytes so a renewed pause cancels this request."""
    before = path.stat()
    data = path.read_bytes()
    after = path.stat()

    def signature(s):
        return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)

    if signature(before) != signature(after):
        raise ValueError(f"file changed while reading: {path.name}")
    return (data, *signature(after))


def read_state(path: Path) -> dict:
    state = json.loads(path.read_bytes())
    if not isinstance(state, dict):
        raise ValueError(f"invalid object: {path.name}")
    return state


def same_folder(value: object, expected: str) -> bool:
    return isinstance(value, str) and ntpath.normcase(ntpath.normpath(value)) == ntpath.normcase(expected)


def finished_state(state: dict, armed_at: datetime, previous_finish: object) -> bool:
    if not same_folder(state.get("folder"), SOURCE_FOLDER) or "current" not in state or state["current"] is not None:
        return False
    finished = state.get("finished_at")
    try:
        if finished == previous_finish or datetime.strptime(finished, "%Y-%m-%d %H:%M:%S") < armed_at:
            return False
    except (TypeError, ValueError):
        return False
    items = state.get("items")
    if not isinstance(items, list) or not items:
        return False
    terminal = {"done", "skipped", "failed"}
    if any(not isinstance(it, dict) or not isinstance(it.get("status"), str)
           or it["status"] not in terminal or not isinstance(it.get("file"), str) or not it["file"] for it in items):
        return False
    counts = {status: sum(it["status"] == status for it in items)
              for status in ("pending", "running", "done", "skipped", "failed")}
    return state.get("summary") == counts


def run(converter, engine_pid: int) -> int:
    """Return 0 when committed to the existing watchdog, or 1 on cancellation."""
    c = converter
    claimed = c.STOP_FILE.with_name(f"stop.request.handoff-{engine_pid}")
    moved = False
    own_lock = False
    committed = False
    try:
        if engine_pid <= 0 or not c.pid_alive(engine_pid):
            raise ValueError("original 4K engine must be alive when arming")
        if claimed.exists():
            raise ValueError("handoff already claimed")
        job_before, stop_before = snapshot(c.JOB_DEF), snapshot(c.STOP_FILE)
        job = json.loads(job_before[0])
        if not isinstance(job, dict) or job.get("folders") != [SAVED_FOLDER]:
            raise ValueError("saved job is not exactly slot3")
        progress = c.LOCAL_DIR / PROGRESS_NAME
        initial = read_state(progress)
        if not same_folder(initial.get("folder"), SOURCE_FOLDER):
            raise ValueError("initial progress does not belong to the 4K folder")
        previous_finish = initial.get("finished_at")
        armed_at = datetime.now().replace(microsecond=0)

        def intent_unchanged() -> None:
            if snapshot(c.JOB_DEF) != job_before or snapshot(c.STOP_FILE) != stop_before:
                raise ValueError("saved job or stop request changed; preserving new intent")

        def completed() -> None:
            if (c.LOCK.exists() and not own_lock) or c.JOB.exists():
                raise ValueError("shared lock/current-job was not released")
            if not finished_state(read_state(progress), armed_at, previous_finish):
                raise ValueError("4K pass did not finish freshly with every item terminal")

        c.log(f"slot3 handoff armed: original 4K pid={engine_pid}; stop request stays intact")
        while True:
            intent_unchanged()
            if not c.pid_alive(engine_pid):
                break
            time.sleep(20)
        completed()
        # Keep the watchdog blocked while a moved pause is validated; unlike acquire_lock, x is atomic.
        with c.LOCK.open("x", encoding="ascii") as lock:
            lock.write(str(os.getpid()))
        own_lock = True
        intent_unchanged()
        completed()
        if c.LOCK.read_text(encoding="ascii").strip() != str(os.getpid()):
            raise ValueError("handoff no longer owns the shared lock")
        if claimed.exists():
            raise ValueError("handoff already claimed")
        c.STOP_FILE.rename(claimed)  # Windows rename refuses an existing destination.
        moved = True
        if snapshot(claimed) != stop_before or c.STOP_FILE.exists() or snapshot(c.JOB_DEF) != job_before:
            raise ValueError("operator intent changed during claim")
        if c.LOCK.read_text(encoding="ascii").strip() != str(os.getpid()):
            raise ValueError("shared lock ownership changed during claim")
        # Commit: the unchanged pause was consumed for the unchanged slot3 job. Later intent wins normally.
        committed = True
        try:
            claimed.unlink()
            moved = False
        except OSError as exc:
            c.log(f"handoff committed; consumed pause retained at {claimed}: {exc}")
        c.log("slot3 handoff committed; existing five-minute watchdog resumes the saved job on its next tick")
        return 0
    except (OSError, ValueError) as exc:
        while moved and not committed and not c.STOP_FILE.exists():
            try:
                claimed.rename(c.STOP_FILE)  # Never overwrite a newer pause on Windows.
                moved = False
            except OSError as restore_error:
                c.log(f"slot3 pause restore failed; retaining live lock and retrying in 20s: {restore_error}")
                time.sleep(20)
        c.log(f"slot3 handoff cancelled: {exc}")
        return 1
    finally:
        # A different process's lock must never be removed, including on cancellation.
        try:
            if own_lock and c.LOCK.read_text(encoding="ascii").strip() == str(os.getpid()):
                c.LOCK.unlink()
        except OSError as exc:
            c.log(f"handoff lock cleanup failed; watchdog can reclaim it after this helper exits: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("engine_pid", type=int, help="the currently running 4K engine PID")
    args = parser.parse_args()
    sys.path.insert(0, str(CONVERTER_DIR))
    import convert_slot1_a as converter

    return run(converter, args.engine_pid)


if __name__ == "__main__":
    raise SystemExit(main())
