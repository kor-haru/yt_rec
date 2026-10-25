# External converter maintenance

Issue [#123](https://github.com/kor-haru/yt_rec/issues/123): append newly discovered videos after each
processed-file boundary without changing encode or replacement policy.

`refresh-after-file.patch` targets the independent converter at `C:\Users\haru\nas-convert`.
It updates `convert_slot1_a.py`, its self-check `test_convert.py`, and its `AGENTS.md`; the converter
is not vendored into this repository. The patch was prepared from the production snapshot on
2026-10-25 in an isolated staging directory. Baseline SHA-256:

| File | SHA-256 |
| --- | --- |
| `convert_slot1_a.py` | `265e3ed7dd6d34b416fafc78d6144d202e0fadd4df2d70ec85ad6cb1bbf29c55` |
| `test_convert.py` | `4cf7be26171f3eef9002eac76c8f3039c0fda59128511a0e1c3f2b1b20861973` |
| `AGENTS.md` | `f7a39fc737e7993b98baea7c1da5fab0917a64f8aaad707cf8f8a83cd5254f05` |

Request a graceful stop, then wait until the converter and its child ffmpeg have exited after the
current file's validation/replacement. Check baseline hashes and apply this patch only while paused;
never kill the active encode or automatically resume after deployment. The patch paths are relative
to the external converter directory, not this repository. `git apply --check` there is read-only.

Validation (uses the existing interpreter; no NAS encoding):

```powershell
& 'C:\Users\haru\nas-convert\.venv\Scripts\python.exe' '<staging-or-converter-directory>\test_convert.py'
```

The tests use local temporary files and mocked media processing. They cover FIFO append/resume, recursive
and final-file discovery, progress counts, failed/skipped history, renamed outputs across resume,
legacy MP4/MKV output inference (without suppressing ambiguous candidates), policy retries,
interruption, and copy-stability deferral. No quality or
codec settings change. A newly detected file waits for five seconds of unchanged size/mtime; a paused
copy can still look stable, so copy to a non-video extension and rename only after completion.
