"""중간 파일 병합과 결과 검증 (#4 복구, #14 검증 지표).

검증 지표는 이 프로젝트에서 실제로 무결성을 확인할 때 쓴 것과 같다.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from yt_rec.recording.errors import ToolFailure
from yt_rec.recording.merge import (
    find_intermediates,
    merge_streams,
    probe_streams,
    verify_media,
)

pytestmark = pytest.mark.integration


# -- 중간 파일 찾기 -------------------------------------------------------------


def test_영상과_음성_중간_파일을_찾는다(intermediates):
    work_dir, video_id = intermediates
    found = find_intermediates(work_dir, video_id)
    assert [p.name for p in found] == [
        f"{video_id}.f137.mp4",
        f"{video_id}.f140.m4a",
    ]


def test_조각_임시_파일과_재개_정보는_제외한다(intermediates):
    work_dir, video_id = intermediates
    (work_dir / f"{video_id}.f137.mp4-Frag2867").write_bytes(b"partial")
    (work_dir / f"{video_id}.f137.mp4.ytdl").write_text("{}", encoding="utf-8")
    (work_dir / "metadata.json").write_text("{}", encoding="utf-8")

    names = [p.name for p in find_intermediates(work_dir, video_id)]
    assert names == [f"{video_id}.f137.mp4", f"{video_id}.f140.m4a"]


def test_part_확장자는_되돌린다(intermediates):
    work_dir, video_id = intermediates
    source = work_dir / f"{video_id}.f137.mp4"
    source.rename(work_dir / f"{video_id}.f137.mp4.part")

    found = find_intermediates(work_dir, video_id)

    assert (work_dir / f"{video_id}.f137.mp4").exists()
    assert f"{video_id}.f137.mp4" in [p.name for p in found]


def test_빈_파일은_무시한다(intermediates):
    work_dir, video_id = intermediates
    (work_dir / f"{video_id}.f251.webm").write_bytes(b"")
    names = [p.name for p in find_intermediates(work_dir, video_id)]
    assert f"{video_id}.f251.webm" not in names


def test_다른_영상의_중간_파일은_건드리지_않는다(intermediates):
    work_dir, video_id = intermediates
    (work_dir / "OTHERVIDEO.f137.mp4").write_bytes(b"x" * 100)
    names = [p.name for p in find_intermediates(work_dir, video_id)]
    assert "OTHERVIDEO.f137.mp4" not in names


# -- 병합 ----------------------------------------------------------------------


def test_영상과_음성을_다시_인코딩하지_않고_묶는다(intermediates, toolchain, tmp_path):
    work_dir, video_id = intermediates
    dest = tmp_path / "merged.mp4"

    merge_streams(find_intermediates(work_dir, video_id), dest, toolchain)

    duration, streams = probe_streams(dest, toolchain)
    kinds = sorted(s.codec_type for s in streams)
    assert kinds == ["audio", "video"]
    assert duration == pytest.approx(4.0, abs=0.2)


def test_원본_코덱이_그대로_유지된다(intermediates, toolchain, tmp_path):
    work_dir, video_id = intermediates
    dest = tmp_path / "merged.mp4"
    merge_streams(find_intermediates(work_dir, video_id), dest, toolchain)

    _, streams = probe_streams(dest, toolchain)
    codecs = {s.codec_type: s.codec_name for s in streams}
    assert codecs["video"] == "h264"
    assert codecs["audio"] == "aac"


def test_병합할_파일이_없으면_실패한다(toolchain, tmp_path):
    with pytest.raises(ToolFailure):
        merge_streams([], tmp_path / "x.mp4", toolchain)


# -- 검증 ----------------------------------------------------------------------


def test_정상_병합은_모든_지표를_통과한다(intermediates, toolchain, tmp_path):
    work_dir, video_id = intermediates
    dest = tmp_path / "merged.mp4"
    merge_streams(find_intermediates(work_dir, video_id), dest, toolchain)

    result = verify_media(dest, toolchain)

    assert result.demux_errors == ()                      # 데먹싱 오류 0건
    assert result.backward_timestamps == 0                # 역행 타임스탬프 0건
    assert result.video_frames == result.expected_frames  # 프레임 수 = 길이 x 프레임률
    assert result.max_frame_gap <= result.frame_interval * 1.5  # 간격 1프레임 이내
    assert result.av_duration_delta < 1.0                 # 영상/음성 길이 차 1초 이내
    assert result.playable and result.complete
    assert result.issues == ()
    assert result.status_text == "정상"


def test_프레임_수는_재생_길이_곱하기_프레임률이다(intermediates, toolchain, tmp_path):
    work_dir, video_id = intermediates
    dest = tmp_path / "merged.mp4"
    merge_streams(find_intermediates(work_dir, video_id), dest, toolchain)

    result = verify_media(dest, toolchain)

    assert result.frame_interval == pytest.approx(1 / 30, abs=1e-4)
    assert result.expected_frames == 120  # 4초 x 30fps
    assert abs(result.video_frames - result.expected_frames) <= 1


def test_음성이_없으면_부분_복구로_기록한다(intermediates, toolchain, tmp_path):
    """영상 중간 파일만 살아남은 경우에도 재생 가능한 파일은 만든다."""
    work_dir, video_id = intermediates
    (work_dir / f"{video_id}.f140.m4a").unlink()
    dest = tmp_path / "video_only.mp4"
    merge_streams(find_intermediates(work_dir, video_id), dest, toolchain)

    result = verify_media(dest, toolchain)

    assert result.playable is True
    assert result.complete is False
    assert any("음성" in issue for issue in result.issues)
    assert result.status_text == "부분 복구"


def test_깨진_파일은_검증에_실패한다(toolchain, tmp_path):
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"\x00" * 4096)

    result = verify_media(broken, toolchain)

    assert result.playable is False
    assert result.complete is False
    assert result.issues


def test_없는_파일은_검증에_실패한다(toolchain, tmp_path):
    result = verify_media(tmp_path / "없음.mp4", toolchain)
    assert result.playable is False


def test_중간이_잘려나가면_프레임이_비어_부분_복구가_된다(
    intermediates, toolchain, tmp_path
):
    """조각을 건너뛴 상황을 흉내 낸다: 가운데 1초를 들어낸 영상."""
    work_dir, video_id = intermediates
    source = work_dir / f"{video_id}.f137.mp4"
    gapped = tmp_path / "gapped.mp4"
    proc = subprocess.run(
        [
            str(toolchain.ffmpeg), "-y", "-hide_banner", "-v", "error",
            "-i", str(source),
            "-vf", "select='not(between(t,1,2))',setpts=N/FRAME_RATE/TB",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            str(gapped),
        ],
        capture_output=True,
    )
    if proc.returncode != 0:
        pytest.skip("구간 삭제 클립을 만들지 못했다")

    merged = tmp_path / "merged.mp4"
    merge_streams([gapped, work_dir / f"{video_id}.f140.m4a"], merged, toolchain)

    result = verify_media(merged, toolchain)

    # 영상이 1초 짧아졌으므로 음성과의 길이 차이로 누락이 드러난다.
    assert result.playable is True
    assert result.complete is False
    assert result.av_duration_delta == pytest.approx(1.0, abs=0.2)


def test_deep_을_끄면_패킷을_훑지_않는다(intermediates, toolchain, tmp_path):
    work_dir, video_id = intermediates
    dest = tmp_path / "merged.mp4"
    merge_streams(find_intermediates(work_dir, video_id), dest, toolchain)

    result = verify_media(dest, toolchain, deep=False)

    assert result.backward_timestamps is None
    assert result.max_frame_gap is None
    assert result.playable is True
