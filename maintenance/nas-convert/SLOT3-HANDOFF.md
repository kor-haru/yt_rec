# One-shot 4K → slot3 handoff

Issue [#125](https://github.com/kor-haru/yt_rec/issues/125). `resume_slot3.py` is a small Windows-only
waiter for the current `\\192.168.173.100\e\_e\_4K` pass. It does not modify the converter, media,
saved job, progress history, encoding settings, or the active encode.

After review, deploy the helper to `C:\Users\haru\nas-convert\resume_slot3.py` and arm it **while
the original 4K engine is still alive**, using that engine's actual PID (not its venv launcher PID):

```powershell
$converterDir = 'C:\Users\haru\nas-convert'
Start-Process -FilePath "$converterDir\.venv\Scripts\python.exe" -ArgumentList @(
    '-u', "$converterDir\resume_slot3.py", '24468'
) -WorkingDirectory $converterDir -WindowStyle Hidden -RedirectStandardOutput "$converterDir\slot3-handoff.stdout.log" -RedirectStandardError "$converterDir\slot3-handoff.stderr.log" -PassThru
```

Do not run the blocked `nas-convert.exe`, clear `stop.request` manually, or restart the active
converter. Runtime messages also go through the converter's existing `convert.log`.

The waiter snapshots the saved job and stop marker (bytes, identity, size and mtime). It waits for
the original engine to exit, then requires released shared lock/current-job, correct 4K folder,
a new `finished_at` since arming, `current=null`, all entries terminal, and matching zero-pending/
zero-running summary. A known corrupt-source failure remains recorded: terminal processing is not
the same as every file succeeding. Missing/malformed/stale/partial progress cancels, not launches.

Immediately before handoff it atomically creates the shared lock with its own PID, then rechecks
completion, unchanged intent, and lock ownership. On Windows, renaming the unchanged pause to a
per-engine claim allows only one waiter to consume it. The watchdog stays blocked by the live
helper's lock while the moved pause is verified; a new pause/job before commit cancels without
overwriting that intent. If pause restoration fails, the helper retains its live lock and retries
every 20 seconds until the pause is restored or a new pause appears; it does not expose an unpaused
watchdog on cancellation. Do not kill/reboot a helper while restoration is pending: its live lock is
the safety barrier until `stop.request` exists again. A retained `stop.request.handoff-<pid>` means
a newer pause was preserved or consumed-claim cleanup failed; inspect its log before another handoff.

Commit is the successful moved-pause identity and final saved-job/own-lock validation. The helper
then drops only its own lock, leaving the existing five-minute watchdog to resume the saved job on
its next tick (normally within five minutes). It never launches or retries a converter itself,
avoiding competing launchers and Windows venv parent-PID assumptions. New job/pause intent after
commit is handled normally by the existing watchdog/runner. Exit codes: `0` = handoff committed
to the watchdog, `1` = cancelled; `0` does not mean slot3 has already started or every 4K file succeeded.
If an existing launcher fails, normal watchdog retries remain in place; this helper adds no launch path.

The hidden waiter survives closing this chat. It is **not reboot-persistent**; do not reboot while
relying on this one-shot handoff. Safe deployment/arming and real launch verification are separate
from the mocked tests:

```powershell
& 'D:\_dev\workspace\yt_rec\.venv\Scripts\python.exe' -m pytest tests/test_nas_slot3_handoff.py -q
```

Tests run in the repository's normal CI suite, use only temporary local files and a stub converter,
and never import the external converter or start/encode NAS media.
