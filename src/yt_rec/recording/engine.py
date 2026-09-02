"""녹화 엔진.

주어진 video id 하나를 방송 시작 지점부터 녹화해 재생 가능한 단일 파일로 마무리한다.

설계에서 중요한 결정 세 가지:

* **조각 재시도 상한은 유한하다.** 방송 종료 시점에 마지막 조각 한두 개가 서버에서
  사라지는데, 상한이 없으면 존재하지 않는 조각을 영원히 다시 요청하며 정지한다
  (실측: 29만 회 재시도 / 7시간). 유한 상한이면 죽은 조각을 건너뛰고 병합까지 끝난다.
* **메타데이터는 시작할 때 확보한다.** 방송 종료 직후 영상이 멤버 전용으로 바뀌면
  제목을 조회할 수 없어 파일명을 만들지 못한다. 그래서 시작 시점에 받아 두고,
  마무리에서는 보관된 값만 쓴다.
* **중간 파일은 병합 검증에 성공하고 종료 상태를 저장한 뒤에만 지운다.** 검증이
  실패하면 남겨 두어야 나중에 손으로라도 살릴 수 있고, 상태 저장보다 먼저 지우면
  그 사이에 죽었을 때 복구가 파일을 못 찾아 녹화를 영구히 건너뛴다.
* **사용자 추가 인자는 안전 옵션을 덮어쓸 수 없다.** yt-dlp 는 뒤에 온 옵션이 이기므로
  추가 인자를 엔진 옵션 앞에 놓고, 엔진 소관 옵션이 들어오면 이유와 함께 거부한다.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import signal
import stat
import subprocess
import threading
import time
from datetime import tzinfo
from functools import partial
from pathlib import Path
from typing import Callable, Iterable

from .binaries import BinaryNotFoundError, Toolchain, resolve_toolchain
from .errors import (
    DenialCategory,
    MetadataUnavailableError,
    ToolTimeout,
    classify_error,
)
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
    select_merge_sources,
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

__all__ = ["LOCK_FILENAME", "RecordingEngine", "STATE_FILENAME"]

STATE_FILENAME = "state.json"
LOG_FILENAME = "yt-dlp.log"

#: 이 work 디렉터리를 지금 쓰고 있는 프로세스 표시. :func:`_lock_owner` 참고.
LOCK_FILENAME = "owner.lock"

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

# -- 사용자 추가 인자 방어 -------------------------------------------------------
#
# yt-dlp 는 같은 옵션이 여러 번 오면 **뒤에 온 것이 이긴다.** 그래서 사용자가 넣은
# extra_ytdlp_args 를 엔진 옵션 뒤에 붙이면 안전 장치가 그대로 무력화된다. 두 겹으로
# 막는다: (1) 엔진이 정하는 옵션이 들어오면 이유와 함께 거부하고, (2) 통과한 인자는
# 엔진 옵션보다 앞에 놓아 순서로도 지지 않게 한다.

_WHY_RETRY = (
    "재시도 상한은 설정(fragment_retries/total_retries/extractor_retries)으로만 정한다. "
    "무한 상한은 방송 종료 시 사라진 조각을 영원히 다시 요청하며 녹화를 정지시킨다"
)
_WHY_SKIP = "상한에 걸린 조각은 건너뛰어야 나머지를 마저 받아 병합까지 끝낼 수 있다"
_WHY_OUTPUT = "중간 파일 이름과 위치는 엔진이 정한다. 최종 파일명은 보관된 메타데이터로 만든다"
_WHY_PART = "중간 파일 복구가 .part 없는 이름에 기대고 있다"
_WHY_LIVE = "방송 시작 지점부터 받는지는 live_from_start 설정으로 정한다"
_WHY_PARSE = "진행률·로그를 못 읽으면 정지 판정과 조각 기록이 무너진다"
_WHY_FORMAT = "화질 상한과 컨테이너는 설정(max_height/container)으로 정한다"
_WHY_CONFIG = "사용자 설정 파일이 무한 재시도 같은 옵션을 되살릴 수 있다"
_WHY_ENGINE = "엔진이 정하는 값이다"
_WHY_END_OF_OPTIONS = (
    "단독 -- 는 이후 엔진 옵션을 URL로 만들어 재시도·출력·시작 지점 보호를 전부 무효화한다"
)

#: extra_ytdlp_args 로 덮어쓸 수 없는 옵션 -> (값을 받는가, 거부 이유).
#:
#: :meth:`RecordingEngine.build_download_argv` 가 넘기는 옵션은 모두 여기 있어야 한다
#: (테스트가 그 대응을 검사한다). 별칭과 부정형도 같은 옵션으로 취급한다.
_ENGINE_OWNED_ARGS: dict[str, tuple[bool, str]] = {
    # 재시도 상한 — 이 엔진이 존재하는 이유다(#14).
    "-R": (True, _WHY_RETRY),
    "--retries": (True, _WHY_RETRY),
    "--fragment-retries": (True, _WHY_RETRY),
    "--extractor-retries": (True, _WHY_RETRY),
    "--retry-sleep": (True, _WHY_RETRY),
    # 상한에 걸린 조각을 어떻게 할지.
    "--skip-unavailable-fragments": (False, _WHY_SKIP),
    "--no-skip-unavailable-fragments": (False, _WHY_SKIP),
    "--abort-on-unavailable-fragment": (False, _WHY_SKIP),
    "--abort-on-unavailable-fragments": (False, _WHY_SKIP),
    "--no-abort-on-unavailable-fragment": (False, _WHY_SKIP),
    "--no-abort-on-unavailable-fragments": (False, _WHY_SKIP),
    # 파일 이름과 위치.
    "-o": (True, _WHY_OUTPUT),
    "--output": (True, _WHY_OUTPUT),
    "-P": (True, _WHY_OUTPUT),
    "--paths": (True, _WHY_OUTPUT),
    "--part": (False, _WHY_PART),
    "--no-part": (False, _WHY_PART),
    # 방송 시작 지점.
    "--live-from-start": (False, _WHY_LIVE),
    "--no-live-from-start": (False, _WHY_LIVE),
    # 출력 파싱.
    "--progress-template": (True, _WHY_PARSE),
    "--newline": (False, _WHY_PARSE),
    "--progress": (False, _WHY_PARSE),
    "--no-progress": (False, _WHY_PARSE),
    "--encoding": (True, _WHY_PARSE),
    "--no-colors": (False, _WHY_PARSE),
    "--color": (True, _WHY_PARSE),
    # 화질과 컨테이너.
    "-f": (True, _WHY_FORMAT),
    "--format": (True, _WHY_FORMAT),
    "--merge-output-format": (True, _WHY_FORMAT),
    # 설정 파일.
    "--ignore-config": (False, _WHY_CONFIG),
    "--no-config": (False, _WHY_CONFIG),
    "--no-ignore-config": (False, _WHY_CONFIG),
    "--config-location": (True, _WHY_CONFIG),
    "--config-locations": (True, _WHY_CONFIG),
    # 나머지 엔진 소관.
    "-N": (True, _WHY_ENGINE),
    "--concurrent-fragments": (True, _WHY_ENGINE),
    "--ffmpeg-location": (True, _WHY_ENGINE),
    "--mtime": (False, _WHY_ENGINE),
    "--no-mtime": (False, _WHY_ENGINE),
    "--no-playlist": (False, _WHY_ENGINE),
    "--yes-playlist": (False, _WHY_ENGINE),
    "--": (False, _WHY_END_OF_OPTIONS),
}


def _match_engine_owned(token: str) -> tuple[bool, str] | None:
    """토큰이 엔진 소관 옵션이면 ``(값을 뒤에 따로 받는가, 거부 이유)``.

    ``--flag=값`` 과 ``-oNAME`` 처럼 값이 붙어 오는 형태도 잡아낸다. 값이 이미 붙어
    있으면 다음 토큰을 값으로 먹지 않는다.
    """
    if token == "--":
        return False, _WHY_END_OF_OPTIONS
    if not token.startswith("-") or token == "-":
        return None

    name, attached, _value = token.partition("=")
    entry = _ENGINE_OWNED_ARGS.get(name)
    if entry is not None:
        takes_value, reason = entry
        return takes_value and not attached, reason

    # 짧은 옵션은 값이 붙어 올 수 있다(``-oNAME``, ``-R3``). 공백이 들어 있는 토큰은
    # 다른 옵션에 딸린 값(``--postprocessor-args "-f mp4"``)이므로 건드리지 않는다.
    if not token.startswith("--") and len(token) > 2 and not any(c.isspace() for c in token):
        entry = _ENGINE_OWNED_ARGS.get(token[:2])
        if entry is not None and entry[0]:
            return False, entry[1]
    return None


def _filter_extra_args(extra: Iterable[str]) -> tuple[list[str], list[str]]:
    """추가 인자에서 엔진 소관 옵션을 걷어낸다. ``(통과한 인자, 거부 사유)``.

    값을 받는 옵션은 값 토큰까지 함께 버린다. 플래그만 버리고 값을 남기면 yt-dlp 가
    그 값을 URL 로 오해한다.
    """
    tokens = list(extra)
    accepted: list[str] = []
    rejected: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        owned = _match_engine_owned(token)
        if owned is None:
            accepted.append(token)
            continue
        takes_value, reason = owned
        dropped = [token]
        if takes_value and index < len(tokens) and not tokens[index].startswith("-"):
            dropped.append(tokens[index])
            index += 1
        rejected.append(f"추가 인자를 거부했다: {' '.join(dropped)} — {reason}")
    return accepted, rejected


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
        """진행 중인 녹화를 멈춘다. 지금까지 받은 내용은 병합해 마무리한다.

        **아직 시작하지 않은 녹화에도 걸린다.** 플래그는 :meth:`clear_stop` 을 부를
        때까지 남는다. :meth:`record` 가 시작할 때 플래그를 지우면, 워커 스레드가
        ``record()`` 를 부르기 직전에 사용자가 누른 중단이 사라져 녹화가 방송 끝까지
        (수 시간) 계속된다. 감시 루프(#3)가 ``record()`` 를 반복 호출하는 구조에서는
        호출 사이에 눌린 중단이 매번 없어진다.
        """
        self._stop.set()
        with self._process_lock:
            process = self._process
        if process is not None:
            _terminate(process)

    def stop_requested(self) -> bool:
        """중단 요청이 걸려 있는가. 스레드 안전."""
        return self._stop.is_set()

    def clear_stop(self) -> None:
        """중단 요청을 거둔다. 다시 녹화하려면 **명시적으로** 불러야 한다.

        :meth:`record` 가 알아서 지우지 않는 것은 의도한 것이다. 위 설명 참고.
        """
        self._stop.clear()

    def record(self, video_id: str) -> RecordingResult:
        """``video_id`` 를 녹화하고 결과를 돌려준다. 예외 대신 결과로 실패를 알린다.

        이 계약은 **전 경로에서** 성립해야 한다. 도구를 못 찾거나(``yt-dlp``/``ffmpeg``
        미설치) 준비 단계에서 예상하지 못한 오류가 나도 예외를 흘리지 않는다. 호출자는
        결과를 받아 표시할 뿐, 예외를 받을 준비가 되어 있지 않다 — GUI 워커 스레드에서
        예외가 나면 상태 없이 죽는다.
        """
        started_at = time.time()
        try:
            return self._record(video_id, started_at)
        except BinaryNotFoundError as exc:
            # 어느 바이너리가 없는지 그대로 담는다. 사용자가 PATH 를 고칠 수 있어야 한다.
            return self._aborted(video_id, started_at, f"녹화를 시작할 수 없다: {exc}")
        except Exception as exc:  # noqa: BLE001 - 계약상 예외를 흘리지 않는다
            return self._aborted(video_id, started_at, f"녹화 준비 중 오류: {exc}")

    def _record(self, video_id: str, started_at: float) -> RecordingResult:
        # 중단 플래그는 여기서 지우지 않는다. 지우면 record() 를 부르기 직전에 눌린
        # 중단이 사라진다. 거두려면 clear_stop() 을 명시적으로 불러야 한다.
        # 도구를 가장 먼저 해석한다. 없으면 여기서 BinaryNotFoundError 가 나고
        # record() 가 결과로 바꾼다. work 디렉터리를 만들기 전이라 흔적도 남지 않는다.
        _ = self.toolchain
        work_dir = self.work_dir_for(video_id)
        work_dir.mkdir(parents=True, exist_ok=True)
        # 이 디렉터리를 지금 쓰고 있다고 표시한다. recover_pending() 이 이 표시를 보고
        # 살아 있는 녹화의 중간 파일을 병합하지 않는다.
        _write_lock(work_dir)
        try:
            return self._record_locked(video_id, started_at, work_dir)
        finally:
            _release_lock(work_dir)

    def _record_locked(
        self, video_id: str, started_at: float, work_dir: Path
    ) -> RecordingResult:
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
            format_ids=download.format_ids,
        )

    def _aborted(self, video_id: str, started_at: float, message: str) -> RecordingResult:
        """시작하지도 못한 실패를 결과로 만든다. **상태 파일은 건드리지 않는다.**

        도구가 없거나 준비 단계에서 죽은 것은 환경 문제다. 여기서 종료 상태를 못 박으면
        이전 시도가 남긴 중간 파일을 :meth:`recover_pending` 이 영구히 건너뛴다.
        """
        work_dir = self.work_dir_for(video_id)
        result = RecordingResult(
            video_id=video_id,
            status=RecordingStatus.FAILED,
            metadata=LiveMetadata.load(work_dir) or LiveMetadata.placeholder_for(video_id),
            work_dir=work_dir,
            started_at=started_at,
            finished_at=time.time(),
            message=message,
        )
        self._emit(
            StatusChanged(video_id=video_id, status=RecordingStatus.FAILED, detail=message)
        )
        self._emit(RecordingFinished(video_id=video_id, result=result))
        return result

    def recover_pending(self) -> list[RecordingResult]:
        """마무리되지 않은 채 남은 녹화를 찾아 병합·검증까지 끝낸다.

        프로세스가 죽거나 강제 종료된 뒤 다시 켰을 때 부르면 된다.

        이미 검증까지 끝난 녹화는 결과 파일을 다시 만들지 않는다. 종료 상태를
        저장한 뒤 정리 직전에 죽었으면 남은 중간 파일만 치운다. 이 정리는
        ffmpeg/yt-dlp 없이 한다.

        **지금 녹화 중인 work 디렉터리는 건드리지 않는다.** 앱을 두 개 띄우거나 녹화
        도중에 이 함수를 부르면, 살아 있는 중간 파일을 병합해 엉뚱한 결과를 만들고
        상태 파일을 덮어쓴다. Windows 는 열린 파일 unlink 가 실패해 피해가 제한되지만
        POSIX 에서는 그대로 지워진다. 그래서 소유권 표시(:data:`LOCK_FILENAME`)를 보고
        살아 있는 소유자가 있으면 건너뛴다.
        """
        root = self.options.resolved_work_root()
        if not root.is_dir():
            return []

        # 끝나지 않은 녹화는 도구 없이 마무리하지 않는다. 한 건이라도 종료 상태를
        # 못 박으면 ffmpeg 를 다시 설치해도 복구가 그 녹화를 영구히 건너뛴다.
        # 이미 완료된 녹화의 찌꺼기 정리는 도구가 필요 없으므로 목록은 본다.
        toolchain_error: BinaryNotFoundError | None = None
        try:
            _ = self.toolchain
        except BinaryNotFoundError as exc:
            toolchain_error = exc

        results: list[RecordingResult] = []
        postpone = False
        for work_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            # 한 건이 실패해도 나머지 복구는 계속한다.
            try:
                owner = _lock_owner(work_dir)
                if owner is not None:
                    self._emit(
                        LogLine(
                            video_id=work_dir.name,
                            text=f"[yt-rec] 녹화 중이라 복구를 건너뛴다 (pid {owner})",
                        )
                    )
                    continue
                state = self._load_state(work_dir)
                if state and _is_terminal(state.get("status")):
                    self._cleanup_completed_leftovers(work_dir, state)
                    continue
                if toolchain_error is not None:
                    if _needs_finalize(work_dir, work_dir.name):
                        postpone = True
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
        if postpone and toolchain_error is not None:
            self._emit(
                LogLine(video_id="", text=f"[yt-rec] 복구를 미룬다: {toolchain_error}")
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

        extra, rejected = _filter_extra_args(self.options.extra_ytdlp_args)
        for note in rejected:
            self._emit(LogLine(video_id=video_id, text=f"[yt-rec] {note}"))
        try:
            metadata = fetch_metadata(
                video_id,
                self.source_url(video_id),
                self.toolchain,
                work_dir,
                extractor_retries=self.options.extractor_retries,
                extra_args=extra,
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
        """yt-dlp 명령줄. 테스트에서 인자 구성을 그대로 확인할 수 있게 공개한다.

        사용자 추가 인자(:attr:`RecordingOptions.extra_ytdlp_args`)는 엔진 옵션보다
        **앞에** 놓는다. yt-dlp 는 뒤에 온 옵션이 이기므로, 거부 목록
        (:data:`_ENGINE_OWNED_ARGS`)을 빠져나간 인자가 있어도 안전 옵션이 최종적으로
        이긴다. 거부한 인자는 이유와 함께 :class:`LogLine` 으로 알린다.
        """
        options = self.options
        extra, rejected = _filter_extra_args(options.extra_ytdlp_args)
        for note in rejected:
            self._emit(LogLine(video_id=video_id, text=f"[yt-rec] {note}"))

        argv = [
            str(self.toolchain.ytdlp),
            *extra,
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
        argv.append(self.source_url(video_id))
        return argv

    def _run_download(self, video_id: str, work_dir: Path) -> _DownloadOutcome:
        outcome = _DownloadOutcome()
        if self._stop.is_set():
            # record() 를 부르기 직전에 눌린 중단. yt-dlp 를 띄우지도 않는다.
            outcome.returncode = None
            outcome.message = "사용자 요청으로 중단했다"
            self._emit(LogLine(video_id=video_id, text=f"[yt-rec] {outcome.message}"))
            return outcome

        argv = self.build_download_argv(video_id)
        detector = StallDetector(self.options.stall_timeout_seconds)
        log_path = work_dir / LOG_FILENAME

        creationflags = 0
        popen_extra: dict = {}
        if os.name == "nt":
            # GUI 빌드(pythonw/윈도우 모드)에서 녹화마다 콘솔 창이 번쩍이지 않게 한다.
            creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        else:
            # 자식을 새 프로세스 그룹의 리더로 만든다. 이래야 _terminate 가 그룹째
            # 신호를 보내 후처리용 ffmpeg 손자까지 함께 끊을 수 있다.
            popen_extra["start_new_session"] = True

        try:
            process = subprocess.Popen(
                argv,
                cwd=str(work_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                bufsize=0,
                creationflags=creationflags,
                **popen_extra,
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
        # 이번 시도가 실제로 받은 포맷. 낡은 중간 파일을 걸러내는 근거가 된다(A).
        outcome.format_ids = tuple(f for f in per_format_bytes if f and f != "?")
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
        format_ids: Iterable[str] = (),
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
                format_ids=tuple(format_ids),
            )
        except BinaryNotFoundError as exc:
            # 도구가 없는 것은 환경 문제다. **종료 상태를 저장하지 않는다** — 저장하면
            # ffmpeg 를 다시 설치해도 recover_pending() 이 이 녹화를 영구히 건너뛴다.
            return fail(
                message=f"마무리를 미룬다 (도구를 찾을 수 없다): {exc}", save_state=False
            )
        except ToolTimeout as exc:
            # 마무리 단계가 물렸다. 다운로드 단계의 정지 감지기는 여기까지 오지 않으므로
            # 이 경로에서 직접 기록·표시한다(#14).
            self._emit(StallDetected(video_id=video_id, idle_seconds=exc.timeout or 0.0))
            self._emit(LogLine(video_id=video_id, text=f"[yt-rec] {exc}"))
            return fail(message=f"마무리 시간 초과: {exc}", stalled=True)
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
        format_ids: tuple[str, ...] = (),
    ) -> RecordingResult:
        # 후보를 다 모으는 것이 아니라 **이번 시도가 받은 것**만 고른다. 지난 시도가
        # 검증 실패로 남긴 중간 파일이 있는데 사용자가 화질 상한을 낮춰 다시 녹화하면
        # 포맷 id 가 달라져 낡은 파일과 새 파일이 함께 모인다(A).
        #
        # yt-dlp 가 스스로 병합에 성공하면 **이번에 받은** 중간 파일만 지운다. 지난
        # 시도의 f* 는 그대로 남는다. 남은 f* 를 "마무리가 안 됐다"로 보면 낡은
        # 트랙을 다시 묶고, 이미 있는 {id}.mp4 를 덮거나 지운다.
        candidates = find_intermediates(work_dir, video_id)
        selection = select_merge_sources(
            candidates,
            self.toolchain,
            format_ids=format_ids,
            timeout=self.options.verify_timeout_seconds,
        )
        for note in selection.excluded:
            self._emit(LogLine(video_id=video_id, text=f"[yt-rec] 병합에서 제외: {note}"))
        sources = list(selection.sources)
        completed = _completed_output(work_dir, video_id)
        if sources and completed is not None:
            # 복구 경로에는 포맷 id 가 없다. 결과물이 남은 중간 파일보다 오래되지
            # 않았으면 yt-dlp 가 이미 묶은 쪽을 쓴다.
            try:
                if all(completed.stat().st_mtime >= path.stat().st_mtime for path in sources):
                    self._emit(
                        LogLine(
                            video_id=video_id,
                            text=f"[yt-rec] 이미 병합된 결과를 쓴다: {completed.name}",
                        )
                    )
                    sources = []
            except OSError:
                pass

        # 이전 시도가 남긴 복구본은 지운다. 남겨 두면 이번에 받은 중간 파일을
        # 병합하지 않고 낡은 파일을 결과로 내보내게 된다. **후보를 모은 뒤에** 지운다 —
        # 먼저 지우면 중간 파일 없이 복구본만 남은 work 디렉터리에서 유일한 결과물을
        # 없애 버린다. 이미 있는 {id}.mp4 를 쓰기로 했으면 지우지 않는다.
        if sources:
            for stale in work_dir.glob(f"{video_id}.recovered.*"):
                stale.unlink(missing_ok=True)

        merged = None if sources else completed

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
                merged = merge_streams(
                    sources,
                    candidate,
                    self.toolchain,
                    maps=selection.maps,
                    timeout=self.options.merge_timeout_seconds,
                )
            except (BinaryNotFoundError, ToolTimeout):
                # 도구가 없는 것과 물린 것은 _finalize 가 따로 다룬다. 여기서 결과로
                # 감싸면 종료 상태가 못 박히거나 정지 사실이 묻힌다.
                raise
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
        verification = verify_media(
            merged,
            self.toolchain,
            deep=self.options.verify_deep,
            timeout=self.options.verify_timeout_seconds,
        )
        if verification.timed_out:
            # 검증이 물렸다. 다운로드 단계의 정지 감지기는 여기까지 오지 않는다.
            self._emit(
                StallDetected(
                    video_id=video_id, idle_seconds=self.options.verify_timeout_seconds
                )
            )

        if not verification.playable:
            # 중간 파일은 남긴다. 손으로라도 살릴 수 있어야 한다.
            return self._conclude(
                video_id=video_id,
                metadata=metadata,
                work_dir=work_dir,
                status=RecordingStatus.FAILED,
                started_at=started_at,
                stalled=stalled or verification.timed_out,
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
            # 중간 파일 정리는 **검증에 성공한 뒤에만** 한다(#14 수용 기준).
            # playable 만 보고 지우면 "음성 스트림이 없다", "프레임 수 불일치",
            # "패킷 검사를 마치지 못했다"처럼 누락이 확인되거나 아예 재지 못한 상태에서
            # 원본이 사라져 다시 병합할 기회가 없어진다.
            # 순서는 _conclude 안에서 지킨다 — 종료 상태를 저장한 **뒤에** 지운다.
            cleanup=verification.complete,
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
        cleanup: bool = False,
        save_state: bool = True,
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
        # 순서가 중요하다. 종료 상태를 먼저 원자적으로 저장하고, 그 다음에 중간 파일을
        # 지운다. 거꾸로 하면 그 사이에 죽었을 때 중간 파일은 사라졌는데 state.json 은
        # recording 으로 남아, recover_pending() 이 복구할 파일을 못 찾고 이 녹화를
        # 영구히 건너뛴다. 몇 시간 녹화한 결과를 잃는 경로다.
        #
        # ``save_state=False`` 는 환경 문제로 마무리를 **미루는** 경우다(도구 미설치).
        # 종료 상태를 못 박으면 도구를 다시 설치해도 복구가 이 녹화를 건너뛴다.
        if save_state:
            self._save_state(work_dir, result)
        if cleanup and save_state and not self.options.keep_intermediates:
            # 정리 실패는 결과를 뒤집지 않는다. 결과 파일은 이미 제자리에 있고
            # 상태도 저장됐다. 남은 중간 파일은 다음 recover_pending() 이 다시 치운다.
            self._try_cleanup_intermediates(work_dir, video_id, protect=output_path)
        self._emit(StatusChanged(video_id=video_id, status=status, detail=message))
        self._emit(RecordingFinished(video_id=video_id, result=result))
        return result

    def _cleanup_completed_leftovers(self, work_dir: Path, state: dict) -> None:
        """끝난 녹화의 남은 중간 파일만 치운다. 결과 파일을 다시 만들지 않는다."""
        if self.options.keep_intermediates:
            return
        if state.get("status") != RecordingStatus.COMPLETED.value:
            return
        verification = state.get("verification")
        if not isinstance(verification, dict) or verification.get("complete") is not True:
            return
        raw = state.get("output_path")
        protect = Path(raw) if raw else None
        self._try_cleanup_intermediates(work_dir, work_dir.name, protect=protect)

    def _try_cleanup_intermediates(
        self, work_dir: Path, video_id: str, *, protect: Path | None
    ) -> None:
        """중간 파일을 치운다. 실패해도 예외를 흘리지 않고 로그로만 남긴다."""
        try:
            failed = _cleanup_intermediates(work_dir, video_id, protect=protect)
        except OSError as exc:
            self._emit(
                LogLine(
                    video_id=video_id,
                    text=f"[yt-rec] 중간 파일을 치우지 못했다: {exc}",
                )
            )
            return
        if not failed:
            return
        names = ", ".join(path.name for path in failed)
        self._emit(
            LogLine(
                video_id=video_id,
                text=f"[yt-rec] 중간 파일을 치우지 못했다: {names}",
            )
        )

    # -- 상태 파일 -------------------------------------------------------------

    @staticmethod
    def _save_state(work_dir: Path, result: RecordingResult) -> Path:
        """상태를 원자적으로 저장한다. 임시 파일에 쓴 뒤 교체한다.

        같은 파일에 곧바로 쓰면 그 도중에 죽었을 때 잘린 JSON 이 남는다. 교체 방식이면
        읽는 쪽은 항상 이전 상태 아니면 새 상태만 본다.
        """
        work_dir.mkdir(parents=True, exist_ok=True)
        target = work_dir / STATE_FILENAME
        tmp = work_dir / f"{STATE_FILENAME}.tmp"
        tmp.write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, target)
        return target

    @staticmethod
    def _load_state(work_dir: Path) -> dict | None:
        """보관된 상태를 읽는다. 없거나 잘렸으면 ``None``.

        ``None`` 은 "끝나지 않은 녹화"로 취급되어 복구 대상이 된다. 부분 기록된 파일
        때문에 복구를 건너뛰는 일이 없어야 한다.
        """
        try:
            state = json.loads((work_dir / STATE_FILENAME).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return state if isinstance(state, dict) else None


class _DownloadOutcome:
    """다운로드 단계에서 모은 사실."""

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.stalled = False
        self.skipped_fragments: list[int] = []
        self.downloaded_bytes: int | None = None
        self.denial: DenialCategory | None = None
        #: 이번 시도에 yt-dlp 가 진행률로 알려준 포맷 id.
        self.format_ids: tuple[str, ...] = ()
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


def _write_lock(work_dir: Path) -> None:
    """이 work 디렉터리를 쓰고 있다고 표시한다. 실패해도 녹화를 막지 않는다."""
    try:
        (work_dir / LOCK_FILENAME).write_text(
            json.dumps({"pid": os.getpid(), "at": time.time()}), encoding="utf-8"
        )
    except OSError:
        pass


def _release_lock(work_dir: Path) -> None:
    try:
        (work_dir / LOCK_FILENAME).unlink(missing_ok=True)
    except OSError:
        pass


def _process_alive(pid: int) -> bool:
    """``pid`` 가 살아 있는가. **판단할 수 없으면 살아 있는 것으로 본다.**

    모를 때 "살아 있다"로 기울이는 것은 의도한 것이다. 살아 있는 녹화의 중간 파일을
    복구가 병합해 없애는 쪽이, 복구를 한 번 미루는 쪽보다 훨씬 나쁘다. pid 가 재사용된
    경우에도 같은 방향으로 틀린다(복구를 미룬다).
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        # Windows 의 os.kill 은 signal 0 을 받지 않고 TerminateProcess 를 부른다.
        # 살아 있는지 보려고 쓰면 그 프로세스를 죽인다. 절대 쓰지 않는다.
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return code.value == 259  # STILL_ACTIVE
                return True
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001 - 판단 실패는 "살아 있다"로 본다
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # 권한이 없다는 것은 그 pid 가 살아 있다는 뜻이다
    return True


def _lock_owner(work_dir: Path) -> int | None:
    """work 디렉터리를 붙잡고 있는 살아 있는 프로세스의 pid. 없으면 ``None``."""
    try:
        data = json.loads((work_dir / LOCK_FILENAME).read_text(encoding="utf-8"))
        pid = int(data["pid"])
    except (OSError, ValueError, TypeError, KeyError):
        return None
    if pid == os.getpid():
        # 우리 프로세스가 녹화 중이다(같은 앱에서 복구를 부른 경우).
        return pid
    return pid if _process_alive(pid) else None


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


#: work 디렉터리에 남겨 두는 엔진 소관 파일. 중간 산출물이 아니다.
_PROTECTED_WORK_NAMES = frozenset(
    {STATE_FILENAME, LOG_FILENAME, "metadata.json", LOCK_FILENAME}
)


def _is_leftover_name(name: str, video_id: str) -> bool:
    """검증 성공 후 지워도 되는 중간 파일 이름인가.

    ``{id}.f*`` 미디어와 같은 줄기의 ``.part`` / ``.ytdl``, 이 녹화의 ``-Frag*``
    만 해당한다. 병합된 ``{id}.mp4`` 는 중간 파일이 아니다.
    """
    if name in _PROTECTED_WORK_NAMES:
        return False
    if name.startswith(f"{video_id}."):
        rest = name[len(video_id) + 1 :]
        if rest.startswith("f"):
            return True
        return name.endswith(".part") or name.endswith(".ytdl") or "-Frag" in name
    return name.startswith(f"{video_id}-") and "-Frag" in name


def _needs_finalize(work_dir: Path, video_id: str) -> bool:
    """끝나지 않은 녹화에 마무리할 파일이 있는가. 파일을 바꾸지 않는다."""
    if _completed_output(work_dir, video_id) is not None:
        return True
    if not work_dir.is_dir():
        return False
    for path in work_dir.iterdir():
        try:
            info = path.stat()
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
            continue
        if _is_leftover_name(path.name, video_id):
            return True
    return False


def _same_path(left: Path, right: Path) -> bool:
    """두 경로가 같은 파일을 가리키는가. 판단할 수 없으면 아니라고 본다."""
    try:
        return left.resolve() == right.resolve()
    except OSError:
        try:
            return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(
                os.path.abspath(str(right))
            )
        except OSError:
            return False


def _cleanup_intermediates(
    work_dir: Path, video_id: str, *, protect: Path | None = None
) -> list[Path]:
    """검증에 성공한 뒤에만 부른다. 메타데이터·상태·로그·최종 결과는 남긴다.

    지우지 못한 경로를 돌려준다. 한 파일의 ``OSError`` 로 나머지를 포기하지
    않고, 빈 목록이 아니면 성공으로 위장하지도 않는다.
    """
    if not work_dir.is_dir():
        return []

    failed: list[Path] = []
    for path in list(work_dir.iterdir()):
        name = path.name
        if not _is_leftover_name(name, video_id):
            continue
        try:
            info = path.stat()
        except OSError:
            failed.append(path)
            continue
        if not stat.S_ISREG(info.st_mode):
            continue
        if protect is not None and _same_path(path, protect):
            continue
        try:
            path.unlink()
        except OSError:
            failed.append(path)
    return failed


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
        # 프로세스 **그룹째** 끊는다. 직접 자식에게만 신호를 보내면 후처리용 ffmpeg
        # 손자가 살아남아 work 디렉터리 파일을 붙잡고, 병합·정리가 실패한다.
        # 그룹이 만들어져 있어야 하므로 Popen 에 start_new_session=True 를 준다.
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGINT)
        except (OSError, AttributeError, ValueError):
            try:
                process.send_signal(signal.SIGINT)
            except (OSError, ValueError):
                pass
        try:
            process.wait(timeout=15)
            return
        except subprocess.TimeoutExpired:
            pass
        # SIGINT 로 안 끝났다. 그룹째 SIGTERM.
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except (OSError, AttributeError, ValueError):
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
