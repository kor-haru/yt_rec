"""#14 핵심 테스트: 조각 재시도 상한.

조각 응답을 인위적으로 404 로 만드는 스텁 서버로 실제 yt-dlp 를 돌린다.

* 유한 상한: 상한에 걸린 조각을 건너뛰고 나머지를 마저 받아 병합까지 끝낸다.
* 무한 상한: 사라진 조각을 영원히 다시 요청하며 스스로 끝내지 못한다.
  (실측으로 29만 회 재시도 / 7시간이 관측된 그 상태다.)

두 경우를 같은 조건에서 비교해, 유한 상한에서만 정상 종료되는지 확인한다.
"""

from __future__ import annotations

import subprocess
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from yt_rec.recording import (
    FragmentRetried,
    FragmentSkipped,
    RecordingEngine,
    RecordingOptions,
    RecordingStatus,
    StallDetected,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

STREAM_ID = "stream"
DEAD_SEGMENT = "seg003.ts"
SEGMENTS = 6


@pytest.fixture(scope="session")
def hls_fixture(tmp_path_factory, toolchain) -> Path:
    """6초짜리 HLS 스트림. 1초 조각 6개."""
    directory = tmp_path_factory.mktemp("hls")
    proc = subprocess.run(
        [
            str(toolchain.ffmpeg), "-y", "-hide_banner", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=30:duration={SEGMENTS}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={SEGMENTS}",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-g", "30",
            "-c:a", "aac", "-b:a", "64k",
            "-f", "hls", "-hls_time", "1", "-hls_list_size", "0",
            "-hls_playlist_type", "vod",
            "-hls_segment_filename", str(directory / "seg%03d.ts"),
            str(directory / f"{STREAM_ID}.m3u8"),
        ],
        capture_output=True,
    )
    if proc.returncode != 0:
        pytest.skip("HLS 스트림을 만들지 못했다: " + proc.stderr.decode("utf-8", "replace")[:300])
    return directory


class _DeadSegmentHandler(SimpleHTTPRequestHandler):
    """지정한 조각만 404 로 돌려준다. 방송 종료 시점에 조각이 사라진 상황이다."""

    dead_segment = DEAD_SEGMENT

    def do_GET(self):  # noqa: N802 - http.server 규약
        if self.dead_segment in self.path:
            self.send_error(404, "Gone")
            return
        super().do_GET()

    def log_message(self, *args):  # 테스트 출력을 어지럽히지 않는다
        pass


@pytest.fixture()
def stub_server(hls_fixture):
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(_DeadSegmentHandler, directory=str(hls_fixture))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def make_engine(tmp_path: Path, stub_server: str, toolchain, **overrides):
    options = RecordingOptions(
        output_dir=tmp_path / "녹화",
        url_template=stub_server + "/{video_id}.m3u8",
        live_from_start=False,  # 일반 HLS 라 해당 없음
        max_height=None,
        fragment_retry_sleep="linear=0.2:0.5",
        **overrides,
    )
    return RecordingEngine(options, toolchain=toolchain)


def test_유한_상한이면_죽은_조각을_건너뛰고_병합까지_끝낸다(
    tmp_path, stub_server, toolchain
):
    engine = make_engine(
        tmp_path,
        stub_server,
        toolchain,
        fragment_retries=2,
        total_retries=2,
        stall_timeout_seconds=120.0,
    )
    events = []
    engine.add_listener(events.append)

    started = time.monotonic()
    result = engine.record(STREAM_ID)
    elapsed = time.monotonic() - started

    assert result.stalled is False, "스스로 끝냈어야 한다"
    assert elapsed < 120
    assert result.succeeded, result.message
    assert result.output_path is not None and result.output_path.exists()

    # 상한에 걸려 건너뛴 조각이 기록된다.
    assert result.skipped_fragments == (4,)
    assert any(isinstance(e, FragmentSkipped) for e in events)

    retries = [e for e in events if isinstance(e, FragmentRetried)]
    assert [e.attempt for e in retries] == [1, 2]
    assert all(e.max_attempts == 2 for e in retries)
    assert not any(isinstance(e, StallDetected) for e in events)


def test_건너뛴_구간은_부분_복구로_기록된다(tmp_path, stub_server, toolchain):
    """조각 하나가 빠졌으므로 완전한 녹화는 아니다. 그 사실이 결과에 남아야 한다."""
    engine = make_engine(
        tmp_path, stub_server, toolchain, fragment_retries=2, stall_timeout_seconds=120.0
    )
    result = engine.record(STREAM_ID)

    assert result.status is RecordingStatus.PARTIAL
    verification = result.verification
    assert verification.playable is True
    assert verification.complete is False
    assert verification.issues
    # 1초짜리 조각이 통째로 빠졌으니 프레임 간격이 1프레임을 크게 넘는다.
    assert verification.max_frame_gap > verification.frame_interval


def test_무한_상한이면_스스로_끝내지_못한다(tmp_path, stub_server, toolchain):
    """같은 조건에서 상한만 무한으로 바꾼다. 정지 감지가 없으면 영원히 매달린다."""
    engine = make_engine(
        tmp_path,
        stub_server,
        toolchain,
        fragment_retries=2,  # 아래 추가 인자가 덮어쓴다
        stall_timeout_seconds=8.0,
        extra_ytdlp_args=("--fragment-retries", "infinite"),
    )
    events = []
    engine.add_listener(events.append)

    started = time.monotonic()
    result = engine.record(STREAM_ID)
    elapsed = time.monotonic() - started

    assert result.stalled is True, "무한 상한에서는 스스로 끝나지 않는다"
    assert result.skipped_fragments == ()  # 건너뛰지 못했다
    assert any(isinstance(e, StallDetected) for e in events)
    assert 8 <= elapsed < 120

    retries = [e for e in events if isinstance(e, FragmentRetried)]
    assert retries, "재시도를 반복했다"
    assert all(e.max_attempts is None for e in retries), "상한이 무한으로 찍힌다"
    assert retries[-1].attempt > 2, "유한 상한이었다면 2회에서 멈췄을 횟수"
