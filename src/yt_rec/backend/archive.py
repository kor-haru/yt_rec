"""저장된 녹화 결과를 읽는다. 파일 조회와 실행은 백엔드 작업 스레드에서 한다."""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PySide6.QtCore import QFile

from yt_rec.recording.options import default_settings_path
from yt_rec.state.models import CompletedRecording, CompletionStatus


def _number(value: object) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _plain_path(path: Path) -> os.stat_result:
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("링크·정션 경로는 처리하지 않습니다")
    return info


def _confirmed_missing(path: Path, output_dir: Path) -> bool:
    if not path.is_relative_to(output_dir):
        return False
    parent = path.parent
    while parent.is_relative_to(output_dir):
        try:
            _plain_path(parent)
            os.listdir(parent)
            return True
        except FileNotFoundError:
            parent = parent.parent
        except (OSError, ValueError):
            return False
    return False


def _deletion_token(state_path: Path, contents: bytes, info: os.stat_result) -> tuple[str, ...]:
    return (str(state_path.absolute()), hashlib.sha256(contents).hexdigest(),
            *(str(value) for value in (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)))


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def load_archive(
    output_dir: Path, *, work_root: Path | None = None
) -> tuple[CompletedRecording, ...]:
    """종료 상태만 읽고, 파일이 없어도 이력을 남긴다. GUI에서 호출하지 않는다.

    길이는 녹화 종료 때 ffprobe로 검증한 미디어 길이다. 다운로드에 걸린
    벽시계 시간은 미디어 길이로 쓰지 않는다. 완료 파일 크기만 직접 확인한다.
    """
    output_dir = Path(output_dir).absolute()
    root = Path(work_root) if work_root is not None else output_dir / ".yt-rec"
    items = []
    readable_parents: dict[Path, bool] = {}
    for state_path in root.glob("*/state.json"):
        try:
            contents = state_path.read_bytes()
            raw = json.loads(contents)
            if not isinstance(raw, dict):
                raise ValueError("녹화 결과 형식이 올바르지 않습니다")
            status = raw.get("status")
            if status not in {"completed", "partial", "failed", "denied"}:
                continue
            metadata = raw.get("metadata") or {}
            verification = raw.get("verification") or {}
            if not isinstance(metadata, dict) or not isinstance(verification, dict):
                raise ValueError("녹화 메타데이터 형식이 올바르지 않습니다")
            output = raw.get("output_path")
            path = Path(output) if isinstance(output, str) and output else None
            if path is not None and not path.is_absolute():
                stored_work = Path(str(raw.get("work_dir") or ""))
                actual_work = state_path.parent.absolute()
                if stored_work.parts and not stored_work.is_absolute() and actual_work.parts[-len(stored_work.parts):] == stored_work.parts:
                    base = actual_work
                    for _ in stored_work.parts:
                        base = base.parent
                    path = base / path
                else:
                    # 구형 기록이나 폴더째 옮긴 결과는 현재 출력 폴더를 쓴다.
                    path = output_dir / path.name
            completion = (
                CompletionStatus(status) if status != "denied" else CompletionStatus.FAILED
            )
            notes = [str(raw.get("message") or "")]
            issues = verification.get("issues")
            if isinstance(issues, list):
                notes.extend(str(issue) for issue in issues)
            skipped = raw.get("skipped_fragments")
            if isinstance(skipped, list) and skipped:
                notes.append("받지 못한 조각: " + ", ".join(map(str, skipped)))
                notes.append("정확한 누락 시각은 저장 기록에 없습니다.")
            elif completion is CompletionStatus.PARTIAL:
                notes.append("일부 구간이 누락되었거나 완전성을 확인하지 못했습니다. 정확한 누락 시각은 저장 기록에 없습니다.")
            size = -1
            file_missing = False
            deletion_token = ()
            if path is not None:
                try:
                    info = path.stat()
                    if not stat.S_ISREG(info.st_mode):
                        raise OSError("저장된 경로가 파일이 아닙니다")
                    size = info.st_size
                    if completion in {CompletionStatus.COMPLETED, CompletionStatus.PARTIAL}:
                        deletion_token = _deletion_token(state_path, contents, info)
                except OSError as exc:
                    if isinstance(exc, FileNotFoundError):
                        if path.parent not in readable_parents:
                            readable_parents[path.parent] = _confirmed_missing(path, output_dir)
                        file_missing = readable_parents[path.parent]
                    notes.append(
                        "저장된 위치에 파일이 없습니다." if file_missing else
                        "파일 유무를 확인하지 못했습니다. 저장 폴더·드라이브 연결·접근 권한을 확인하세요."
                    )
                    completion = CompletionStatus.MISSING
            elif completion in {CompletionStatus.COMPLETED, CompletionStatus.PARTIAL}:
                notes.append("저장된 파일 경로가 없습니다.")
                completion = CompletionStatus.MISSING
            stamp = _number(raw.get("finished_at"))
            finished = datetime.fromtimestamp(stamp, timezone.utc) if stamp else None
            items.append(CompletedRecording(
                recording_id=str(raw.get("video_id") or state_path.parent.name),
                title=str(metadata.get("title") or raw.get("video_id") or state_path.parent.name),
                channel_name=str(metadata.get("channel") or metadata.get("uploader") or ""),
                finished_at=finished,
                duration=timedelta(seconds=_number(verification.get("duration"))),
                total_bytes=size,
                status=completion,
                output_path=str(path) if path is not None else None,
                note="\n".join(dict.fromkeys(note for note in notes if note)),
                file_missing=file_missing,
                deletion_token=deletion_token,
            ))
        except (OSError, ValueError, TypeError, OverflowError) as exc:
            items.append(CompletedRecording(
                recording_id=state_path.parent.name,
                title=state_path.parent.name,
                status=CompletionStatus.FAILED,
                total_bytes=-1,
                note=f"저장된 녹화 기록을 읽을 수 없습니다: {exc}",
            ))
    return tuple(sorted(items, key=lambda item: item.finished_at.timestamp() if item.finished_at else 0, reverse=True))


def archive_key(item: CompletedRecording) -> tuple[str, str, str]:
    """같은 영상의 다른 저장 위치와 후속 녹화를 함께 숨기지 않는다."""
    path = os.path.normcase(os.path.abspath(item.output_path)) if item.output_path else ""
    stamp = item.finished_at.astimezone(timezone.utc).isoformat() if item.finished_at else ""
    return item.recording_id, path, stamp


class ArchiveStore:
    """출력 폴더와 보관함 제외목록만 저장한다. 녹화 원본은 변경하지 않는다."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_settings_path().with_name("archive-roots.json")
        try:
            roots = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            roots = []
        if not isinstance(roots, list) or any(
            not isinstance(root, dict)
            or not isinstance(root.get("output_dir"), str)
            or not isinstance(root.get("work_root"), str)
            for root in roots
        ):
            raise ValueError("저장된 보관함 폴더 목록 형식이 올바르지 않습니다")
        self._roots: list[dict[str, str]] = roots
        self.dismissed_path = self.path.with_suffix(".dismissed.json")
        self.dismiss_error = ""
        try:
            try:
                dismissed = json.loads(self.dismissed_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                dismissed = []
            if not isinstance(dismissed, list) or any(
                not isinstance(key, list) or len(key) != 3
                or not all(isinstance(part, str) for part in key)
                for key in dismissed
            ):
                raise ValueError("저장된 보관함 제외목록 형식이 올바르지 않습니다")
        except (OSError, ValueError) as exc:
            self.dismiss_error = f"보관함 제외목록을 읽지 못했습니다. 원본을 보존하며 정리를 중단합니다: {exc}"
            dismissed = []
        self._dismissed = {tuple(key) for key in dismissed}

    def remember(self, output_dir: Path, *, work_root: Path | None = None) -> None:
        output = Path(output_dir).absolute()
        root = {
            "output_dir": str(output),
            "work_root": str(Path(work_root).absolute() if work_root is not None else output / ".yt-rec"),
        }
        if root in self._roots:
            return
        roots = [*self._roots, root]
        _write_json(self.path, roots)
        self._roots = roots

    def load(self) -> tuple[CompletedRecording, ...]:
        items = {}
        for root in self._roots:
            for item in load_archive(Path(root["output_dir"]), work_root=Path(root["work_root"])):
                items[(item.recording_id, item.output_path)] = item
        return self.visible(tuple(sorted(items.values(), key=lambda item: item.finished_at.timestamp() if item.finished_at else 0, reverse=True)))

    def visible(self, items: tuple[CompletedRecording, ...]) -> tuple[CompletedRecording, ...]:
        return tuple(item for item in items if archive_key(item) not in self._dismissed)

    def dismiss(self, items: tuple[CompletedRecording, ...]) -> None:
        """제외목록 저장 성공 후에만 메모리를 갱신한다. 파일은 삭제하지 않는다."""
        if self.dismiss_error:
            raise ValueError(self.dismiss_error)
        dismissed = self._dismissed | {archive_key(item) for item in items}
        if dismissed == self._dismissed:
            return
        _write_json(self.dismissed_path, sorted(dismissed))
        self._dismissed = dismissed

    def trash(self, requested: CompletedRecording) -> None:
        """저장된 완료 결과와 파일 식별값을 재검증한 한 파일만 휴지통으로 옮긴다."""
        if self.dismiss_error:
            raise ValueError(self.dismiss_error)
        items = self.load()
        selected = next((item for item in items if archive_key(item) == archive_key(requested)), None)
        if (selected is None or not selected.deletion_token
                or selected.deletion_token != requested.deletion_token
                or selected.status not in {CompletionStatus.COMPLETED, CompletionStatus.PARTIAL}):
            raise ValueError("녹화·이력·파일 상태가 바뀌었습니다. 새로고침 후 다시 선택하세요.")
        target = Path(selected.output_path)
        state_path = Path(selected.deletion_token[0])
        if (not target.is_absolute() or ".." in target.parts or ":" in target.name
                or target.suffix.lower() not in {".mp4", ".mkv", ".webm", ".m4a", ".mka", ".m4v", ".ts", ".aac"}
                or not any(state_path.parent.parent == Path(root["work_root"]).absolute()
                           and target.parent == Path(root["output_dir"]).absolute() for root in self._roots)
                or any(target.is_relative_to(Path(root["work_root"]).absolute()) for root in self._roots)
                or sum(item.output_path == selected.output_path for item in items) != 1):
            raise ValueError("등록된 출력 폴더의 최종 미디어 파일 한 개만 삭제할 수 있습니다")
        state_info = _plain_path(state_path)
        info = _plain_path(target)
        if not stat.S_ISREG(state_info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("일반 파일만 삭제할 수 있습니다. 디렉터리·하드링크는 제외합니다")
        if _deletion_token(state_path, state_path.read_bytes(), info) != selected.deletion_token:
            raise ValueError("녹화·파일 상태가 바뀌었습니다. 새로고침 후 다시 선택하세요.")
        file = QFile(target.as_posix())
        if not file.moveToTrash():
            raise OSError(f"휴지통으로 이동하지 못했습니다. 앱은 영구 삭제로 재시도하지 않습니다: {file.errorString()}")


def open_archive_path(path: str, *, reveal: bool = False) -> None:
    """로컬 미디어를 기본 앱으로 열거나 파일 관리자로 보여 준다.

    Linux는 FileManager1 선택 요청을 먼저 보내고, 지원하지 않으면 포함 폴더를 연다.
    잘못된 기록이 실행 파일이나 URL을 열지 못하도록 미디어 파일만 받는다.
    """
    target = Path(path)
    if not target.is_absolute() or target.suffix.lower() not in {".mp4", ".mkv", ".webm", ".m4a", ".m4v", ".ts", ".aac"}:
        raise ValueError("로컬 녹화 파일 경로가 올바르지 않습니다")
    if not target.is_file():
        raise FileNotFoundError("저장된 위치에 녹화 파일이 없습니다. 보관함을 새로고침하세요.")
    if sys.platform == "win32":
        if reveal:
            windows_dir = Path(os.environ.get("SystemRoot", ""))
            if not windows_dir.is_absolute():
                raise OSError("Windows 시스템 폴더 경로를 확인할 수 없습니다")
            explorer = windows_dir / "explorer.exe"
            # Explorer parses /select, itself. A list would quote the whole
            # switch for paths with spaces; quote only the validated path.
            subprocess.Popen(
                f'"{explorer}" /select,"{target}"',
                shell=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            os.startfile(str(target))
    else:
        if sys.platform != "darwin" and reveal:
            # https://wiki.freedesktop.org/www/Specifications/file-manager-interface/
            # URI 인코딩으로 공백·쉼표·한글을 D-Bus 배열 구분자와 분리한다.
            try:
                subprocess.run(
                    [
                        "dbus-send", "--session", "--print-reply", "--reply-timeout=2000",
                        "--dest=org.freedesktop.FileManager1",
                        "/org/freedesktop/FileManager1",
                        "org.freedesktop.FileManager1.ShowItems",
                        f"array:string:{target.as_uri()}", "string:",
                    ],
                    check=True, timeout=3, capture_output=True,
                )
            except (OSError, subprocess.SubprocessError):
                pass  # D-Bus 도구·세션·지원 파일 관리자가 없으면 폴더는 열어 준다.
            else:
                return
        argv = (
            ["open", *(["-R"] if reveal else []), str(target)]
            if sys.platform == "darwin"
            else ["xdg-open", str(target.parent if reveal else target)]
        )
        try:
            subprocess.run(argv, check=True, timeout=10)
        except subprocess.SubprocessError as exc:
            raise OSError("기본 앱이나 파일 관리자를 열지 못했습니다") from exc
