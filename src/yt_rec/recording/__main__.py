"""녹화 엔진을 손으로 돌려 보기 위한 최소 명령줄.

GUI 가 붙기 전까지 실제 라이브로 수동 검증할 때 쓴다. 배포 대상이 아니다.

    python -m yt_rec.recording record <video_id> -o <출력 디렉터리> [--max-height 1080]
    python -m yt_rec.recording verify <파일>
    python -m yt_rec.recording recover -o <출력 디렉터리>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .binaries import resolve_toolchain
from .engine import RecordingEngine
from .events import ProgressReported, RecordingEvent, RecordingFinished
from .merge import verify_media
from .options import RecordingOptions


def _print_event(event: RecordingEvent) -> None:
    if isinstance(event, ProgressReported):
        snapshot = event.snapshot
        percent = f"{snapshot.percent:.1f}%" if snapshot.percent is not None else "?"
        sys.stderr.write(
            f"\r[{snapshot.format_id or '-'}] {percent} "
            f"{(snapshot.downloaded_bytes or 0) / 1_048_576:.1f}MiB "
            f"frag={snapshot.fragment_index}   "
        )
        sys.stderr.flush()
    elif isinstance(event, RecordingFinished):
        sys.stderr.write("\n")
    else:
        sys.stderr.write(f"\n{type(event).__name__}: {event}\n")


def _report(verification) -> None:
    print(f"파일:            {verification.path}")
    print(f"판정:            {verification.status_text}")
    print(f"길이:            {verification.duration}")
    print(f"데먹싱 오류:     {len(verification.demux_errors)}건")
    print(f"프레임 수:       {verification.video_frames} (예상 {verification.expected_frames})")
    print(f"역행 타임스탬프: {verification.backward_timestamps}건")
    print(f"최대 프레임 간격: {verification.max_frame_gap} (1프레임 {verification.frame_interval})")
    print(f"영상/음성 차이:  {verification.av_duration_delta}")
    for issue in verification.issues:
        print(f"  - {issue}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m yt_rec.recording")
    sub = parser.add_subparsers(dest="command", required=True)

    record = sub.add_parser("record", help="video id 하나를 녹화한다")
    record.add_argument("video_id")
    record.add_argument("-o", "--output-dir", required=True, type=Path)
    record.add_argument("--max-height", type=int, default=1080)
    record.add_argument("--fragment-retries", type=int, default=20)
    record.add_argument("--stall-timeout", type=float, default=900.0)

    verify = sub.add_parser("verify", help="결과 파일을 검증한다")
    verify.add_argument("path", type=Path)

    recover = sub.add_parser("recover", help="마무리되지 않은 녹화를 복구한다")
    recover.add_argument("-o", "--output-dir", required=True, type=Path)

    args = parser.parse_args(argv)
    toolchain = resolve_toolchain()

    if args.command == "verify":
        _report(verify_media(args.path, toolchain))
        return 0

    options = RecordingOptions(
        output_dir=args.output_dir,
        max_height=getattr(args, "max_height", None),
        fragment_retries=getattr(args, "fragment_retries", 20),
        stall_timeout_seconds=getattr(args, "stall_timeout", 900.0),
    )
    engine = RecordingEngine(options, toolchain=toolchain, on_event=_print_event)

    if args.command == "recover":
        results = engine.recover_pending()
        for result in results:
            print(f"{result.video_id}: {result.status} -> {result.output_path}")
        if not results:
            print("복구할 녹화가 없다")
        return 0

    result = engine.record(args.video_id)
    print(f"상태:   {result.status}")
    print(f"메시지: {result.message}")
    print(f"결과:   {result.output_path}")
    if result.verification:
        _report(result.verification)
    return 0 if result.succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
