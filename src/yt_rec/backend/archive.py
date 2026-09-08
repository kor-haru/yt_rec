"""저장된 녹화 결과를 읽는다. 파일 조회와 실행은 백엔드 작업 스레드에서 한다."""

from __future__ import annotations

import json
import math
import os
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from yt_rec.recording.options import default_settings_path
from yt_rec.state.models import CompletedRecording, CompletionStatus


def _number(value: object) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0


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
    for state_path in root.glob("*/state.json"):
        try:
            raw = json.loads(state_path.read_text(encoding="utf-8"))
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
            if path is not None:
                try:
                    info = path.stat()
                    if not stat.S_ISREG(info.st_mode):
                        raise FileNotFoundError(str(path))
                    size = info.st_size
                except OSError:
                    notes.append("저장된 위치에 파일이 없거나 접근할 수 없습니다.")
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


class ArchiveStore:
    """사용했던 출력 폴더만 기억한다. 녹화별 state.json이 이력 원본이다."""

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

    def remember(self, output_dir: Path, *, work_root: Path | None = None) -> None:
        output = Path(output_dir).absolute()
        root = {
            "output_dir": str(output),
            "work_root": str(Path(work_root).absolute() if work_root is not None else output / ".yt-rec"),
        }
        if root in self._roots:
            return
        roots = [*self._roots, root]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(roots, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)
        self._roots = roots

    def load(self) -> tuple[CompletedRecording, ...]:
        items = {}
        for root in self._roots:
            for item in load_archive(Path(root["output_dir"]), work_root=Path(root["work_root"])):
                items[(item.recording_id, item.output_path)] = item
        return tuple(sorted(items.values(), key=lambda item: item.finished_at.timestamp() if item.finished_at else 0, reverse=True))


def open_archive_path(path: str, *, reveal: bool = False) -> None:
    """로컬 미디어를 기본 앱으로 열거나 파일 관리자로 보여 준다.

    Linux 파일 관리자 공통 선택 규약이 없으므로 포함 폴더를 연다.
    잘못된 기록이 실행 파일이나 URL을 열지 못하도록 미디어 파일만 받는다.
    """
    target = Path(path)
    if not target.is_absolute() or target.suffix.lower() not in {".mp4", ".mkv", ".webm", ".m4a", ".m4v", ".ts", ".aac"}:
        raise ValueError("로컬 녹화 파일 경로가 올바르지 않습니다")
    if not target.is_file():
        raise FileNotFoundError("저장된 위치에 녹화 파일이 없습니다. 보관함을 새로고침하세요.")
    if sys.platform == "win32":
        if reveal:
            subprocess.Popen(["explorer.exe", f"/select,{target}"], creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            os.startfile(str(target))
    else:
        argv = (
            ["open", *(["-R"] if reveal else []), str(target)]
            if sys.platform == "darwin"
            else ["xdg-open", str(target.parent if reveal else target)]
        )
        try:
            subprocess.run(argv, check=True, timeout=10)
        except subprocess.SubprocessError as exc:
            raise OSError("기본 앱이나 파일 관리자를 열지 못했습니다") from exc
