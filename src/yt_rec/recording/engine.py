"""녹화 엔진.

주어진 video id 하나를 방송 시작 지점부터 녹화해 재생 가능한 단일 파일로 마무리한다.

설계에서 중요한 결정 세 가지:

* **조각 재시도 상한은 유한하다.** 방송 종료 시점에 마지막 조각 한두 개가 서버에서
  사라지는데, 상한이 없으면 존재하지 않는 조각을 영원히 다시 요청하며 정지한다
  (실측: 29만 회 재시도 / 7시간). 유한 상한이면 죽은 조각을 건너뛰고 병합까지 끝난다.
* **메타데이터는 시작할 때 확보한다.** 방송 종료 직후 영상이 멤버 전용으로 바뀌면
  제목을 조회할 수 없어 파일명을 만들지 못한다. 그래서 시작 시점에 받아 두고,
  마무리에서는 보관된 값만 쓴다.
* **중간 파일은 병합 검증에 성공한 뒤에만 지운다.** 검증이 실패하면 남겨 두어야
  나중에 손으로라도 살릴 수 있다.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
from datetime import tzinfo
from functools import partial
from pathlib import Path
from typing import Callable, Iterable

from .binaries import Toolchain, resolve_toolchain
from .errors import DenialCategory, MetadataUnavailableError, classify_error
from .events import (
    FragmentRetried,
    FragmentSkipped,
    LogLine,
    MetadataReady,
    ProgressReported,
    RecordingEvent,
    RecordingFinished,
    RecordingResult,
    RecordingStatus,
    StallDetected,
    StatusChanged,
)
from .merge import (
    MediaVerification,
    find_intermediates,
    merge_streams,
    verify_media,
)
from .metadata import LiveMetadata, fetch_metadata
from .naming import reserve_unique_path
from .options import RecordingOptions
from .progress import (
    PROGRESS_TEMPLATE,
    LineSplitter,
    StallDetector,
    parse_fragment_retry,
    parse_progress_line,
    parse_skipped_fragment,
)

__all__ = ["RecordingEngine", "STATE_FILENAME"]

STATE_FILENAME = "state.json"
LOG_FILENAME = "yt-dlp.log"

_MEDIA_SUFFIXES = frozenset({".mp4", ".mkv", ".webm", ".m4a", ".mka", ".m4v"})

#: yt-dlp 후처리 단계 표시. 이 동안에는 진행률이 나오지 않는다.
_POSTPROCESSOR_LINE = re.compile(
    r"^\[(Merger|Fixup\w*|ExtractAudio|Metadata|VideoRemuxer|VideoConvertor|"
    r"MoveFiles|EmbedThumbnail|SponsorBlock)\]",
    re.IGNORECASE,
)

#: 경로 전체 길이 상한. Windows MAX_PATH(260)에서 여유를 둔다.
_MAX_PATH_CHARS = 250

EventCallback = Callable[[RecordingEvent], None]


class RecordingEngine:
    """video id 하나를 녹화해 마무리하는 엔진.

    한 인스턴스는 한 번에 한 건만 녹화한다. 여러 건을 동시에 받으려면 인스턴스를
    여러 개 두면 된다. :meth:`record` 는 블로킹이므로 GUI 는 별도 스레드에서 부르고,
    :meth:`request_stop` 을 UI 스레드에서 불러 멈춘다.
    """

    def __init__(
        self,
        options: RecordingOptions,
        *,
        toolchain: Toolchain | None = None,
        on_event: EventCallback | None = None,
        tz: tzinfo | None = None,
    ) -> None:
        self.options = options
        self._toolchain = toolchain
        self._listeners: list[EventCallback] = []
        self._tz = tz
        self._stop = threading.Event()
        self._process: subprocess.Popen | None = None
        self._process_lock = threading.Lock()
        if on_event is not None:
            self._listeners.append(on_event)

    # -- 사건 통지 -------------------------------------------------------------

    def add_listener(self, callback: EventCallback) -> None:
        """상태 변화를 받을 콜백을 등록한다. GUI 는 여기에 시그널 방출을 붙인다."""
        self._listeners.append(callback)

    def remove_listener(self, callback: EventCallback) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    def _emit(self, event: RecordingEvent) -> None:
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:  # noqa: BLE001 - 청취자 오류가 녹화를 멈추면 안 된다
                pass

    # -- 도구 ------------------------------------------------------------------

    @property
    def toolchain(self) -> Toolchain:
        if self._toolchain is None:
            self._toolchain = resolve_toolchain()
        return self._toolchain

    def work_dir_for(self, video_id: str) -> Path:
        return self.options.resolved_work_root() / video_id

    def source_url(self, video_id: str) -> str:
        return self.options.url_template.format(video_id=video_id)

    # -- 공개 진입점 -----------------------------------------------------------

    def request_stop(self) -> None:
        """진행 중인 녹화를 멈춘다. 지금까지 받은 내용은 병합해 마무리한다."""
        self._stop.set()
        with self._process_lock:
            process = self._process
        if process is not None:
            _terminate(process)

    def record(self, video_id: str) -> RecordingResult:
        """``video_id`` 를 녹화하고 결과를 돌려준다. 예외 대신 결과로 실패를 알린다."""
        self._stop.clear()
        started_at = time.time()
        work_dir = self.work_dir_for(video_id)
        work_dir.mkdir(parents=True, exist_ok=True)

        # 1) 메타데이터 선확보 — 파일명 재료는 지금 잡아 두어야 한다.
        self._emit(StatusChanged(video_id=video_id, status=RecordingStatus.FETCHING_METADATA))
        metadata, denial = self._secure_metadata(video_id, work_dir)
        if denial is not None and not metadata:
            result = RecordingResult(
                video_id=video_id,
                status=RecordingStatus.DENIED,
                metadata=LiveMetadata.placeholder_for(video_id),
                work_dir=work_dir,
                denial=denial,
                started_at=started_at,
                finished_at=time.time(),
                message=f"녹화를 시작할 수 없다 ({denial.value})",
            )
            self._save_state(work_dir, result)
            self._emit(StatusChanged(video_id=video_id, status=RecordingStatus.DENIED, detail=result.message))
            self._emit(RecordingFinished(video_id=video_id, result=result))
            return result

        assert metadata is not None
        self._emit(MetadataReady(video_id=video_id, metadata=metadata))
        self._save_state(
            work_dir,
            RecordingResult(
                video_id=video_id,
                status=RecordingStatus.RECORDING,
                metadata=metadata,
                work_dir=work_dir,
                started_at=started_at,
            ),
        )

        # 2) 다운로드
        self._emit(StatusChanged(video_id=video_id, status=RecordingStatus.RECORDING))
        download = self._run_download(video_id, work_dir)

        # 3) 마무리 — 정상 종료든 정지든 같은 경로를 탄다.
        return self._finalize(
            video_id=video_id,
            metadata=metadata,
            work_dir=work_dir,
            started_at=started_at,
            stalled=download.stalled,
            skipped_fragments=download.skipped_fragments,
            downloaded_bytes=download.downloaded_bytes,
            download_message=download.message,
            denial=download.denial,
        )

    def recover_pending(self) -> list[RecordingResult]:
        """마무리되지 않은 채 남은 녹화를 찾아 병합·검증까지 끝낸다.

        프로세스가 죽거나 강제 종료된 뒤 다시 켰을 때 부르면 된다.
        """
        root = self.options.resolved_work_root()
        if not root.is_dir():
            return []

        results: list[RecordingResult] = []
        for work_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            # 한 건이 실패해도 나머지 복구는 계속한다.
            try:
                state = self._load_state(work_dir)
                if state and _is_terminal(state.get("status")):
                    continue
                metadata = LiveMetadata.load(work_dir) or LiveMetadata.placeholder_for(
                    work_dir.name
                )
                if not find_intermediates(work_dir, work_dir.name) and not _completed_output(
                    work_dir, work_dir.name
                ):
                    continue
                results.append(
                    self._finalize(
                        video_id=work_dir.name,
                        metadata=metadata,
                        work_dir=work_dir,
                        started_at=float((state or {}).get("started_at") or time.time()),
                        stalled=True,
                        skipped_fragments=tuple((state or {}).get("skipped_fragments") or ()),
                        downloaded_bytes=(state or {}).get("downloaded_bytes"),
                        download_message="이전 실행에서 마무리되지 않은 녹화를 복구했다",
                        denial=None,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                self._emit(
                    LogLine(video_id=work_dir.name, text=f"[yt-rec] 복구 실패: {exc}")
                )
        return results

    # -- 메타데이터 ------------------------------------------------------------

    def _secure_metadata(
        self, video_id: str, work_dir: Path
    ) -> tuple[LiveMetadata | None, DenialCategory | None]:
        """메타데이터를 확보해 보관한다.

        이미 보관된 값이 있으면 그것을 쓴다(재개). 조회가 영구적으로 막힌 상태면
        범주를 함께 돌려 호출자가 녹화를 포기하게 한다.
        """
        stored = LiveMetadata.load(work_dir)
        if stored is not None and not stored.placeholder:
            return stored, None

        try:
            metadata = fetch_metadata(
                video_id,
                self.source_url(video_id),
                self.toolchain,
                work_dir,
                extractor_retries=self.options.extractor_retries,
            )
        except MetadataUnavailableError as exc:
            category = exc.category
            if category is DenialCategory.UNKNOWN:
                # 못 알아본 실패는 일단 진행해 본다. 파일명은 video id 로 만든다.
                placeholder = stored or LiveMetadata.placeholder_for(video_id)
                placeholder.save(work_dir)
                self._emit(
                    LogLine(video_id=video_id, text=f"[yt-rec] 메타데이터 조회 실패: {exc}")
                )
                return placeholder, None
            if stored is not None:
                # 보관본이 있으면 조회가 막혀도 그대로 진행할 수 있다.
                return stored, None
            self._emit(LogLine(video_id=video_id, text=f"[yt-rec] {exc}"))
            return None, category

        metadata.save(work_dir)
        return metadata, None

    # -- 다운로드 --------------------------------------------------------------

    def build_download_argv(self, video_id: str) -> list[str]:
        """yt-dlp 명령줄. 테스트에서 인자 구성을 그대로 확인할 수 있게 공개한다."""
        options = self.options
        argv = [
            str(self.toolchain.ytdlp),
            "--ignore-config",
            "--no-colors",
            "--encoding",
            "utf-8",
            "--newline",
            "--no-playlist",
            "--no-mtime",
            # 조각은 통째로만 목적지에 이어붙으므로, .part 없이 써도 중간 파일이 온전하다.
            # 정지 후 복구가 단순해진다.
            "--no-part",
            "-f",
            options.format_selector(),
            "--merge-output-format",
            options.container,
            "--retries",
            str(options.total_retries),
            "--fragment-retries",
            str(options.fragment_retries),
            "--extractor-retries",
            str(options.extractor_retries),
            "--retry-sleep",
            f"fragment:{options.fragment_retry_sleep}",
            # 상한에 걸린 조각은 건너뛰고 나머지를 마저 받는다(yt-dlp 기본값이지만 명시한다).
            "--skip-unavailable-fragments",
            "--concurrent-fragments",
            str(max(1, options.concurrent_fragments)),
            "--ffmpeg-location",
            str(self.toolchain.ffmpeg_dir),
            "--progress-template",
            PROGRESS_TEMPLATE,
            "-o",
            "%(id)s.%(ext)s",
        ]
        if options.live_from_start:
            argv.append("--live-from-start")
        argv.extend(options.extra_ytdlp_args)
        argv.append(self.source_url(video_id))
        return argv

    def _run_download(self, video_id: str, work_dir: Path) -> _DownloadOutcome:
        argv = self.build_download_argv(video_id)
        detector = StallDetector(self.options.stall_timeout_seconds)
        outcome = _DownloadOutcome()
        log_path = work_dir / LOG_FILENAME

        creationflags = 0
        if os.name == "nt":
            # GUI 빌드(pythonw/윈도우 모드)에서 녹화마다 콘솔 창이 번쩍이지 않게 한다.
            creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

        try:
            process = subprocess.Popen(
                argv,
                cwd=str(work_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                bufsize=0,
                creationflags=creationflags,
            )
        except OSError as exc:
            outcome.returncode = -1
            outcome.message = f"yt-dlp 를 실행하지 못했다: {exc}"
            self._emit(LogLine(video_id=video_id, text=f"[yt-rec] {outcome.message}"))
            return outcome
        with self._process_lock:
            self._process = process

        # 첫 조각을 받기 전에 멈추는 경우도 있으므로 시계를 지금부터 돌린다.
        detector.note_activity()

        lines: queue.Queue = queue.Queue()
        reader = threading.Thread(
            target=_pump_output, args=(process, lines), name=f"ytdlp-{video_id}", daemon=True
        )
        reader.start()

        per_format_bytes: dict[str, int] = {}
        stopping = False
        try:
            with log_path.open("a", encoding="utf-8") as log:
                while True:
                    try:
                        line = lines.get(timeout=1.0)
                    except queue.Empty:
                        line = ""
                    if line is None:
                        break
                    if line:
                        self._handle_line(
                            video_id, line, detector, outcome, per_format_bytes, log
                        )
                    if stopping:
                        continue
                    if self._stop.is_set():
                        stopping = True
                        outcome.message = "사용자 요청으로 중단했다"
                        _terminate(process)
                    elif detector.is_stalled():
                        stopping = True
                        outcome.stalled = True
                        idle = detector.idle_seconds
                        outcome.message = f"{idle:.0f}초 동안 진전이 없어 정지로 판정했다"
                        self._emit(StallDetected(video_id=video_id, idle_seconds=idle))
                        self._emit(
                            StatusChanged(
                                video_id=video_id,
                                status=RecordingStatus.STALLED,
                                detail=outcome.message,
                            )
                        )
                        _terminate(process)
        except Exception as exc:  # noqa: BLE001 - 감시 중 오류로 녹화를 잃으면 안 된다
            # 로그 파일을 못 열거나 디스크가 찼을 때도 여기로 온다. 자식을 반드시 끊고
            # 지금까지 받은 중간 파일로 마무리 단계를 밟게 한다.
            outcome.message = outcome.message or f"녹화 감시 중 오류: {exc}"
            self._emit(LogLine(video_id=video_id, text=f"[yt-rec] {outcome.message}"))
        finally:
            reader.join(timeout=10)
            try:
                # 정상 종료라면 곧 끝난다. 아니면 강제로 끊는다 — 무기한 기다리지 않는다.
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                _terminate(process)
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    pass
            with self._process_lock:
                self._process = None

        outcome.returncode = process.returncode
        outcome.downloaded_bytes = sum(per_format_bytes.values()) or None
        if outcome.returncode != 0 and not outcome.message:
            outcome.message = f"yt-dlp 종료 코드 {outcome.returncode}"
        return outcome

    def _handle_line(
        self,
        video_id: str,
        line: str,
        detector: StallDetector,
        outcome: _DownloadOutcome,
        per_format_bytes: dict[str, int],
        log,
    ) -> None:
        snapshot = parse_progress_line(line)
        if snapshot is not None:
            # 진행 줄은 초당 여러 번 나온다. 로그 파일에는 남기지 않고 사건으로만 알린다.
            detector.note(snapshot.advance_key)
            if snapshot.downloaded_bytes is not None:
                per_format_bytes[snapshot.format_id or "?"] = snapshot.downloaded_bytes
            self._emit(ProgressReported(video_id=video_id, snapshot=snapshot))
            return

        log.write(line + "\n")
        self._emit(LogLine(video_id=video_id, text=line))

        if _POSTPROCESSOR_LINE.match(line):
            # 후처리(병합·타임스탬프 보정)에는 진행률이 나오지 않는다. 몇 GB 를 다시
            # 쓰는 동안 진행 신호가 없다고 정지로 오판해 끊으면 안 된다.
            detector.note_activity()
            return

        retry = parse_fragment_retry(line)
        if retry is not None:
            index, attempt, limit = retry
            self._emit(
                FragmentRetried(
                    video_id=video_id,
                    fragment_index=index,
                    attempt=attempt,
                    max_attempts=limit,
                )
            )
            return

        skipped = parse_skipped_fragment(line)
        if skipped is not None:
            outcome.skipped_fragments.append(skipped)
            # 조각을 건너뛰는 것도 진전이다. 정지 시계를 되돌린다.
            detector.note_activity()
            self._emit(FragmentSkipped(video_id=video_id, fragment_index=skipped))
            return

        if outcome.denial is None:
            category = _classify_line(line)
            if category is not None:
                outcome.denial = category

    # -- 마무리 ----------------------------------------------------------------

    def _finalize(
        self,
        *,
        video_id: str,
        metadata: LiveMetadata,
        work_dir: Path,
        started_at: float,
        stalled: bool,
        skipped_fragments: Iterable[int],
        downloaded_bytes: int | None,
        download_message: str,
        denial: DenialCategory | None,
    ) -> RecordingResult:
        skipped = tuple(sorted(set(int(i) for i in skipped_fragments)))
        fail = partial(
            self._conclude,
            video_id=video_id,
            metadata=metadata,
            work_dir=work_dir,
            status=RecordingStatus.FAILED,
            started_at=started_at,
            stalled=stalled,
            skipped=skipped,
            downloaded_bytes=downloaded_bytes,
            denial=denial,
            verification=None,
            output_path=None,
        )
        try:
            return self._finalize_inner(
                video_id=video_id,
                metadata=metadata,
                work_dir=work_dir,
                started_at=started_at,
                stalled=stalled,
                skipped=skipped,
                downloaded_bytes=downloaded_bytes,
                download_message=download_message,
                denial=denial,
            )
        except Exception as exc:  # noqa: BLE001 - 마무리 실패로 예외를 던지지 않는다
            # 여기서 예외를 흘리면 GUI 워커 스레드가 상태 없이 죽는다.
            # 중간 파일은 정리하지 않았으므로 손으로 살릴 수 있다.
            return fail(message=f"마무리 중 오류: {exc}")

    def _finalize_inner(
        self,
        *,
        video_id: str,
        metadata: LiveMetadata,
        work_dir: Path,
        started_at: float,
        stalled: bool,
        skipped: tuple[int, ...],
        downloaded_bytes: int | None,
        download_message: str,
        denial: DenialCategory | None,
    ) -> RecordingResult:
        # 이전 시도가 남긴 복구본은 지운다. 남겨 두면 이번에 받은 중간 파일을
        # 병합하지 않고 낡은 파일을 결과로 내보내게 된다.
        for stale in work_dir.glob(f"{video_id}.recovered.*"):
            stale.unlink(missing_ok=True)

        # 중간 파일이 남아 있다면 그것이 원본이다. yt-dlp 는 스스로 병합에 성공하면
        # 중간 파일을 지우므로, 남아 있다는 것은 마무리가 안 됐다는 뜻이다.
        sources = find_intermediates(work_dir, video_id)
        merged = None if sources else _completed_output(work_dir, video_id)

        if merged is None:
            self._emit(StatusChanged(video_id=video_id, status=RecordingStatus.MERGING))
            if not sources:
                return self._conclude(
                    video_id=video_id,
                    metadata=metadata,
                    work_dir=work_dir,
                    status=RecordingStatus.DENIED if denial else RecordingStatus.FAILED,
                    started_at=started_at,
                    stalled=stalled,
                    skipped=skipped,
                    downloaded_bytes=downloaded_bytes,
                    denial=denial,
                    verification=None,
                    output_path=None,
                    message=download_message or "받은 파일이 없다",
                )
            candidate = work_dir / f"{video_id}.recovered.{self.options.container}"
            try:
                merged = merge_streams(sources, candidate, self.toolchain)
            except Exception as exc:  # noqa: BLE001 - 병합 실패도 결과로 보고한다
                # 중간 파일 병합이 안 되면 yt-dlp 가 남긴 결과라도 써 본다.
                merged = _completed_output(work_dir, video_id)
                if merged is None:
                    return self._conclude(
                        video_id=video_id,
                        metadata=metadata,
                        work_dir=work_dir,
                        status=RecordingStatus.FAILED,
                        started_at=started_at,
                        stalled=stalled,
                        skipped=skipped,
                        downloaded_bytes=downloaded_bytes,
                        denial=denial,
                        verification=None,
                        output_path=None,
                        message=f"병합 실패: {exc}",
                    )

        # 검증
        self._emit(StatusChanged(video_id=video_id, status=RecordingStatus.VERIFYING))
        verification = verify_media(merged, self.toolchain, deep=self.options.verify_deep)

        if not verification.playable:
            # 중간 파일은 남긴다. 손으로라도 살릴 수 있어야 한다.
            return self._conclude(
                video_id=video_id,
                metadata=metadata,
                work_dir=work_dir,
                status=RecordingStatus.FAILED,
                started_at=started_at,
                stalled=stalled,
                skipped=skipped,
                downloaded_bytes=downloaded_bytes,
                denial=denial,
                verification=verification,
                output_path=None,
                message="병합 검증 실패: " + ", ".join(verification.issues),
            )

        # 최종 파일명은 보관된 메타데이터로 정한다. 지금 다시 조회하지 않는다.
        suffix = merged.suffix or f".{self.options.container}"
        basename = metadata.basename(
            self.options.filename_template,
            max_title_chars=self.options.max_title_chars,
            tz=self._tz,
        )
        basename = _fit_to_path_limit(self.options.output_dir, basename, suffix)
        final_path = reserve_unique_path(self.options.output_dir, basename, suffix)
        try:
            os.replace(merged, final_path)
        except OSError:
            # work 디렉터리를 다른 볼륨에 둔 경우 rename 이 안 된다. 복사로 옮긴다.
            try:
                shutil.move(str(merged), str(final_path))
            except OSError as exc:
                final_path.unlink(missing_ok=True)  # 예약해 둔 빈 파일을 치운다
                return self._conclude(
                    video_id=video_id,
                    metadata=metadata,
                    work_dir=work_dir,
                    status=RecordingStatus.FAILED,
                    started_at=started_at,
                    stalled=stalled,
                    skipped=skipped,
                    downloaded_bytes=downloaded_bytes,
                    denial=denial,
                    verification=verification,
                    output_path=None,
                    message=f"결과 파일을 옮기지 못했다: {exc}",
                )

        if not self.options.keep_intermediates:
            _cleanup_intermediates(work_dir, video_id)

        status = RecordingStatus.COMPLETED if verification.complete else RecordingStatus.PARTIAL
        message = download_message
        if not verification.complete:
            detail = ", ".join(verification.issues) or "누락 구간 있음"
            message = f"부분 복구: {detail}" + (f" ({message})" if message else "")
        return self._conclude(
            video_id=video_id,
            metadata=metadata,
            work_dir=work_dir,
            status=status,
            started_at=started_at,
            stalled=stalled,
            skipped=skipped,
            downloaded_bytes=downloaded_bytes,
            # 결과 파일을 만들었으므로 다시 시도할 이유가 없다.
            denial=None,
            verification=verification,
            output_path=final_path,
            message=message,
        )

    def _conclude(
        self,
        *,
        video_id: str,
        metadata: LiveMetadata,
        work_dir: Path,
        status: RecordingStatus,
        started_at: float,
        stalled: bool,
        skipped: tuple[int, ...],
        downloaded_bytes: int | None,
        denial: DenialCategory | None,
        verification: MediaVerification | None,
        output_path: Path | None,
        message: str,
    ) -> RecordingResult:
        result = RecordingResult(
            video_id=video_id,
            status=status,
            metadata=metadata,
            work_dir=work_dir,
            output_path=output_path,
            verification=verification,
            denial=denial,
            stalled=stalled,
            skipped_fragments=skipped,
            downloaded_bytes=downloaded_bytes,
            started_at=started_at,
            finished_at=time.time(),
            message=message,
        )
        self._save_state(work_dir, result)
        self._emit(StatusChanged(video_id=video_id, status=status, detail=message))
        self._emit(RecordingFinished(video_id=video_id, result=result))
        return result

    # -- 상태 파일 -------------------------------------------------------------

    @staticmethod
    def _save_state(work_dir: Path, result: RecordingResult) -> Path:
        work_dir.mkdir(parents=True, exist_ok=True)
        target = work_dir / STATE_FILENAME
        target.write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return target

    @staticmethod
    def _load_state(work_dir: Path) -> dict | None:
        try:
            return json.loads((work_dir / STATE_FILENAME).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None


class _DownloadOutcome:
    """다운로드 단계에서 모은 사실."""

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.stalled = False
        self.skipped_fragments: list[int] = []
        self.downloaded_bytes: int | None = None
        self.denial: DenialCategory | None = None
        self.message = ""


def _fit_to_path_limit(directory: Path, basename: str, suffix: str) -> str:
    """경로 전체가 Windows 상한을 넘지 않도록 이름을 줄인다.

    출력 디렉터리가 깊으면 설정한 제목 길이만으로는 부족하다. 다 받아 놓은 녹화를
    이름이 길다는 이유로 잃으면 안 된다.
    """
    room = _MAX_PATH_CHARS - len(str(directory)) - len(suffix) - len(" (999)") - 1
    if len(basename) <= room:
        return basename
    trimmed = basename[: max(8, room)].rstrip(" .　")
    return trimmed or "recording"


def _is_terminal(status: str | None) -> bool:
    """상태 파일에 적힌 문자열이 끝난 상태인가. 모르는 값이면 끝나지 않은 것으로 본다."""
    try:
        return RecordingStatus(status).terminal
    except ValueError:
        return False


def _classify_line(line: str) -> DenialCategory | None:
    """오류로 보이는 줄에서만 범주를 뽑는다."""
    if "ERROR" not in line.upper():
        return None
    category = classify_error(line)
    return None if category is DenialCategory.UNKNOWN else category


def _completed_output(work_dir: Path, video_id: str) -> Path | None:
    """yt-dlp 가 스스로 병합해 놓은 결과 파일. 없으면 ``None``."""
    if not work_dir.is_dir():
        return None
    for path in sorted(work_dir.iterdir()):
        if not path.is_file() or path.stat().st_size == 0:
            continue
        if path.suffix.lower() not in _MEDIA_SUFFIXES:
            continue
        if path.stem in (video_id, f"{video_id}.recovered"):
            return path
    return None


def _cleanup_intermediates(work_dir: Path, video_id: str) -> None:
    """검증에 성공한 뒤에만 부른다. 메타데이터·상태·로그는 남긴다."""
    for path in list(work_dir.iterdir()):
        if not path.is_file():
            continue
        name = path.name
        if not name.startswith(f"{video_id}."):
            continue
        if name in (STATE_FILENAME, LOG_FILENAME):
            continue
        try:
            path.unlink()
        except OSError:
            pass
    for leftover in work_dir.glob(f"{video_id}*-Frag*"):
        try:
            leftover.unlink()
        except OSError:
            pass


def _pump_output(process: subprocess.Popen, sink: queue.Queue) -> None:
    """자식 출력을 줄 단위로 큐에 넣는다. 끝나면 ``None`` 을 넣어 알린다."""
    splitter = LineSplitter()
    stream = process.stdout
    try:
        while stream is not None:
            chunk = stream.read1(65536) if hasattr(stream, "read1") else stream.read(65536)
            if not chunk:
                break
            for line in splitter.feed(chunk):
                sink.put(line)
        for line in splitter.flush():
            sink.put(line)
    except (OSError, ValueError):
        pass
    finally:
        sink.put(None)


def _terminate(process: subprocess.Popen) -> None:
    """자식과 그 자식(ffmpeg)까지 멈춘다.

    조각은 통째로만 목적지에 이어붙으므로, 중간에 끊어도 지금까지 받은 파일은 온전하다.
    그래서 우아한 종료에 매달리지 않는다. 반대로 후처리용 ffmpeg 손자 프로세스가
    살아남으면 work 디렉터리 파일을 붙잡아 병합·정리가 실패하므로 트리째 끊는다.
    """
    if process.poll() is not None:
        return

    if os.name == "nt":
        # taskkill 로 자식 트리까지 정리한다. CTRL_BREAK 는 콘솔 없는 GUI 빌드에서
        # 동작하지 않아 기다리는 시간만 낭비한다.
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,  # type: ignore[attr-defined]
            )
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            process.send_signal(signal.SIGINT)
            process.wait(timeout=15)
            return
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass

    try:
        process.wait(timeout=10)
        return
    except subprocess.TimeoutExpired:
        pass
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
