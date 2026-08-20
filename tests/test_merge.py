"""중간 파일 병합과 결과 검증 (#4 복구, #14 검증 지표).

검증 지표는 이 프로젝트에서 실제로 무결성을 확인할 때 쓴 것과 같다.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from yt_rec.recording.errors import ToolFailure, ToolTimeout
from yt_rec.recording.merge import (
    check_demux,
    find_intermediates,
    merge_streams,
    probe_streams,
    select_merge_sources,
    verify_media,
)
from yt_rec.recording import merge as merge_module

from conftest import STUB_TIMEOUT

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
    """조각을 건너뛴 상황을 흉내 낸다: 가운데 1초를 들어낸 영상.

    ``setpts`` 로 PTS 를 다시 번호 붙이면 안 된다. 조각을 건너뛴 실제 파일은 타임라인에
    구멍이 그대로 남고, 그것이 누락을 드러내는 신호다. 번호를 다시 붙이면 구멍이 사라져
    "영상이 1초 짧다"라는 다른 신호만 남는데, 그 신호는 정상 녹화에서도 조각 하나
    만큼(실측 최대 0.99초) 나타나 구분할 수 없다.
    """
    work_dir, video_id = intermediates
    source = work_dir / f"{video_id}.f137.mp4"
    gapped = tmp_path / "gapped.mp4"
    proc = subprocess.run(
        [
            str(toolchain.ffmpeg), "-y", "-hide_banner", "-v", "error",
            "-i", str(source),
            "-vf", "select='not(between(t,1,2))'", "-fps_mode", "passthrough",
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

    # 1초짜리 구멍이 프레임 간격으로 드러난다.
    assert result.playable is True
    assert result.complete is False
    assert result.max_frame_gap == pytest.approx(1.0, abs=0.2)
    assert any("최대 프레임 간격" in issue for issue in result.issues)


def test_deep_을_끄면_패킷을_훑지_않는다(intermediates, toolchain, tmp_path):
    work_dir, video_id = intermediates
    dest = tmp_path / "merged.mp4"
    merge_streams(find_intermediates(work_dir, video_id), dest, toolchain)

    result = verify_media(dest, toolchain, deep=False)

    assert result.backward_timestamps is None
    assert result.max_frame_gap is None
    assert result.playable is True


# -- A. 낡은 중간 파일이 결과에 섞이지 않는다 -------------------------------------


def _stale_work_dir(tmp_path: Path, make_clip) -> tuple[Path, str]:
    """지난 시도의 낡은 영상(f137, 3초)과 이번 시도의 영상·음성(f299/f140, 5초).

    사용자가 화질 상한을 낮춰 다시 녹화하면 포맷 id 가 달라져 실제로 이렇게 남는다.
    """
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    video_id = "VID"
    stale = make_clip("stale.mp4", seconds=3)
    fresh = make_clip("fresh.mp4", seconds=5)
    audio = make_clip("audio.m4a", seconds=5, kind="audio")
    (work_dir / f"{video_id}.f137.mp4").write_bytes(stale.read_bytes())
    (work_dir / f"{video_id}.f299.mp4").write_bytes(fresh.read_bytes())
    (work_dir / f"{video_id}.f140.m4a").write_bytes(audio.read_bytes())
    return work_dir, video_id


def test_이번_시도가_받은_포맷만_병합_대상이_된다(tmp_path, toolchain, make_clip):
    """낡은 f137 이 트랙 0 이 되면 프레임 검사가 그 트랙을 보고 통과한다."""
    work_dir, video_id = _stale_work_dir(tmp_path, make_clip)

    selection = select_merge_sources(
        find_intermediates(work_dir, video_id), toolchain, format_ids=("299", "140")
    )

    assert [p.name for p in selection.sources] == [
        f"{video_id}.f299.mp4",
        f"{video_id}.f140.m4a",
    ]
    assert any("f137" in note for note in selection.excluded)


def test_포맷_id_를_모르면_최근_파일을_쓴다(tmp_path, toolchain, make_clip):
    """복구 경로에는 이번 시도의 포맷 정보가 없다. 이름의 사전순으로 고르면 안 된다."""
    work_dir, video_id = _stale_work_dir(tmp_path, make_clip)
    stale = work_dir / f"{video_id}.f137.mp4"
    fresh = work_dir / f"{video_id}.f299.mp4"
    os.utime(stale, (1_600_000_000, 1_600_000_000))
    os.utime(fresh, (1_700_000_000, 1_700_000_000))

    selection = select_merge_sources(find_intermediates(work_dir, video_id), toolchain)

    assert [p.name for p in selection.sources] == [
        f"{video_id}.f299.mp4",
        f"{video_id}.f140.m4a",
    ]


def test_시각이_같으면_큰_파일을_쓴다(tmp_path, toolchain, make_clip):
    """복구 직후 두 영상의 mtime 이 같은 초로 찍히면 이름 순은 낡은 f137 을 고른다."""
    work_dir, video_id = _stale_work_dir(tmp_path, make_clip)
    stamp = 1_700_000_000
    for name in (f"{video_id}.f137.mp4", f"{video_id}.f299.mp4", f"{video_id}.f140.m4a"):
        os.utime(work_dir / name, (stamp, stamp))

    selection = select_merge_sources(find_intermediates(work_dir, video_id), toolchain)

    assert [p.name for p in selection.sources] == [
        f"{video_id}.f299.mp4",
        f"{video_id}.f140.m4a",
    ]


def test_이번_포맷이_디스크에_없으면_낡은_파일을_쓰지_않는다(
    tmp_path, toolchain, make_clip
):
    """yt-dlp 가 이번 시도의 중간 파일을 병합하고 지운 뒤. 지난 시도의 f137 만 남는다.

    매칭이 없으면 후보 전체로 돌아가면 낡은 3초가 다시 고른다.
    """
    work_dir, video_id = _stale_work_dir(tmp_path, make_clip)
    (work_dir / f"{video_id}.f299.mp4").unlink()
    (work_dir / f"{video_id}.f140.m4a").unlink()

    selection = select_merge_sources(
        find_intermediates(work_dir, video_id), toolchain, format_ids=("299", "140")
    )

    assert selection.sources == ()
    assert any("f137" in note for note in selection.excluded)


def test_진행률에_음성_포맷이_빠져도_음성을_잃지_않는다(tmp_path, toolchain, make_clip):
    """걸러내기가 결과를 잃는 원인이 되어서는 안 된다."""
    work_dir, video_id = _stale_work_dir(tmp_path, make_clip)

    selection = select_merge_sources(
        find_intermediates(work_dir, video_id), toolchain, format_ids=("299",)
    )

    names = [p.name for p in selection.sources]
    assert f"{video_id}.f299.mp4" in names
    assert f"{video_id}.f140.m4a" in names, "음성 파일이 통째로 버려졌다"


def test_고른_파일만_병합하면_트랙이_하나씩만_남는다(tmp_path, toolchain, make_clip):
    work_dir, video_id = _stale_work_dir(tmp_path, make_clip)
    selection = select_merge_sources(
        find_intermediates(work_dir, video_id), toolchain, format_ids=("299", "140")
    )
    dest = tmp_path / "merged.mp4"

    merge_streams(selection.sources, dest, toolchain, maps=selection.maps)
    result = verify_media(dest, toolchain)

    assert result.video_stream_count == 1
    assert result.audio_stream_count == 1
    assert result.duration == pytest.approx(5.0, abs=0.3), "이번에 받은 5초짜리다"
    assert result.complete is True


def test_트랙이_겹치면_검증이_잡는다(tmp_path, toolchain, make_clip):
    """리뷰어 재현: 낡은 3초가 트랙 0, 이번에 받은 5초가 트랙 2 가 되어 통과했다."""
    work_dir, video_id = _stale_work_dir(tmp_path, make_clip)
    sources = find_intermediates(work_dir, video_id)
    dest = tmp_path / "overlapped.mp4"
    # 예전 방식: 입력마다 -map i:v? -map i:a? 를 붙인다.
    argv = [str(toolchain.ffmpeg), "-y", "-hide_banner", "-v", "error"]
    for source in sources:
        argv += ["-i", str(source)]
    for index in range(len(sources)):
        argv += ["-map", f"{index}:v?", "-map", f"{index}:a?"]
    argv += ["-c", "copy", str(dest)]
    if subprocess.run(argv, capture_output=True).returncode != 0:
        pytest.skip("겹친 트랙 파일을 만들지 못했다")

    result = verify_media(dest, toolchain)

    assert result.video_stream_count == 2
    assert any("영상 스트림이 2개" in issue for issue in result.issues)
    assert result.playable is False, "이 파일을 결과로 내보내면 낡은 트랙이 대표가 된다"


# -- .part 우선순위 -------------------------------------------------------------


def test_part_가_더_크면_part_를_쓴다(intermediates):
    """yt-dlp 관례에서 .part 는 진행 중(=대개 더 큰) 파일이다."""
    work_dir, video_id = intermediates
    finished = work_dir / f"{video_id}.f137.mp4"
    body = finished.read_bytes()
    finished.write_bytes(body[:100])  # 낡고 작은 파일
    (work_dir / f"{video_id}.f137.mp4.part").write_bytes(body)

    found = find_intermediates(work_dir, video_id)

    assert (work_dir / f"{video_id}.f137.mp4").read_bytes() == body
    assert f"{video_id}.f137.mp4" in [p.name for p in found]


def test_part_가_더_작으면_완결된_쪽을_쓴다(intermediates):
    work_dir, video_id = intermediates
    finished = work_dir / f"{video_id}.f137.mp4"
    body = finished.read_bytes()
    (work_dir / f"{video_id}.f137.mp4.part").write_bytes(b"\x00" * 10)

    find_intermediates(work_dir, video_id)

    assert finished.read_bytes() == body


# -- C. 재지 못한 것을 통과로 취급하지 않는다 --------------------------------------


def test_패킷_검사를_못_마치면_통과로_보지_않는다(
    intermediates, toolchain, tmp_path, monkeypatch
):
    """역행 타임스탬프를 한 번도 재지 못한 상태를 재생 가능으로 확정하면 안 된다."""
    work_dir, video_id = intermediates
    dest = tmp_path / "merged.mp4"
    merge_streams(find_intermediates(work_dir, video_id), dest, toolchain)
    monkeypatch.setattr(merge_module, "_scan_video_packets", lambda *a, **k: None)

    result = verify_media(dest, toolchain)

    assert result.backward_timestamps is None, "재지 못했다"
    assert any("패킷 검사" in issue for issue in result.issues)
    assert result.playable is False
    assert result.complete is False


# -- D. 영상/음성 길이 차 임계값 ---------------------------------------------------


def test_조각_하나만큼_음성이_길어도_정상으로_본다(toolchain, tmp_path, make_clip):
    """실측: 60fps 녹화의 av_delta 가 0.9868초(조각 하나의 98.7%)였다.

    영상은 조각 경계에서 끝나고 음성이 그만큼 더 길게 남는 구조이므로 이 차이는
    방송마다 임의값이다. 실측 조각 길이는 최대 5.0초였다.
    """
    video = make_clip("v.mp4", seconds=4)
    audio = make_clip("a.m4a", seconds=5.5, kind="audio")
    dest = tmp_path / "tail.mp4"
    merge_streams([video, audio], dest, toolchain)

    result = verify_media(dest, toolchain)

    assert result.av_duration_delta == pytest.approx(1.5, abs=0.1)
    assert not any("길이 차이" in issue for issue in result.issues)
    assert result.complete is True, "정상 녹화가 부분 복구로 기록되면 안 된다"


def test_음성이_통째로_짧으면_잡는다(toolchain, tmp_path, make_clip):
    """진짜 음성 누락은 초 단위가 아니라 분 단위로 벌어진다."""
    video = make_clip("v.mp4", seconds=10)
    audio = make_clip("a.m4a", seconds=2, kind="audio")
    dest = tmp_path / "lost.mp4"
    merge_streams([video, audio], dest, toolchain)

    result = verify_media(dest, toolchain)

    assert result.av_duration_delta == pytest.approx(8.0, abs=0.2)
    assert any("길이 차이" in issue for issue in result.issues)
    assert result.complete is False


# -- F. mkv 에서도 지표가 살아 있다 -----------------------------------------------


def test_mkv_에서도_영상_음성_길이_차이를_잰다(toolchain, tmp_path, make_clip):
    """mkv 는 스트림별 duration 을 컨테이너 필드로 내놓지 않는다.

    그대로 두면 영상·음성이 둘 다 컨테이너 길이가 되어 av_delta 가 항상 0.0 이 된다.
    ffmpeg matroska 먹서가 남기는 트랙 ``DURATION`` 태그로 되살린다.
    """
    video = make_clip("v.mp4", seconds=4)
    audio = make_clip("a.m4a", seconds=5.5, kind="audio")
    dest = tmp_path / "merged.mkv"
    merge_streams([video, audio], dest, toolchain)

    result = verify_media(dest, toolchain)

    assert result.av_duration_delta == pytest.approx(1.5, abs=0.1), "0.0 이면 지표가 죽었다"


def test_mkv_에서_프레임_수_검사가_뒤틀리지_않는다(toolchain, tmp_path, make_clip):
    """예전에는 컨테이너 길이로 예상 프레임을 계산해 '120 != 166' 이 나왔다."""
    video = make_clip("v.mp4", seconds=4)
    audio = make_clip("a.m4a", seconds=5.5, kind="audio")
    dest = tmp_path / "merged.mkv"
    merge_streams([video, audio], dest, toolchain)

    result = verify_media(dest, toolchain)

    assert result.expected_frames == pytest.approx(120, abs=1)
    assert not any("프레임 수" in issue for issue in result.issues)
    assert result.complete is True


def test_mkv_와_mp4_의_판정이_같다(toolchain, tmp_path, make_clip):
    """컨테이너를 고르는 것만으로 지표가 죽어서는 안 된다."""
    video = make_clip("v.mp4", seconds=4)
    audio = make_clip("a.m4a", seconds=5.5, kind="audio")
    results = {}
    for container in ("mp4", "mkv"):
        dest = tmp_path / f"merged.{container}"
        merge_streams([video, audio], dest, toolchain)
        results[container] = verify_media(dest, toolchain)

    assert results["mp4"].complete == results["mkv"].complete
    assert results["mp4"].av_duration_delta == pytest.approx(
        results["mkv"].av_duration_delta, abs=0.05
    )


# -- G. 가변 프레임률을 누락으로 오판하지 않는다 -----------------------------------


def test_가변_프레임률을_누락으로_오판하지_않는다(toolchain, tmp_path, make_clip):
    """방송 중 프레임률이 떨어지는 라이브. avg_frame_rate 의 역수는 기준이 못 된다.

    실측: 앞 5초 30fps + 뒤 5초 약 1.9fps 클립의 avg_frame_rate 는 16.5fps(1프레임
    0.0606초)인데 실제 간격은 절반이 0.0333초, 절반이 0.533초다.
    """
    video = make_clip(
        "vfr.mp4",
        seconds=10,
        select="if(lt(t,5),1,not(mod(n,16)))",
        fps_mode="vfr",
    )
    audio = make_clip("a.m4a", seconds=10, kind="audio")
    dest = tmp_path / "vfr_merged.mp4"
    merge_streams([video, audio], dest, toolchain)

    result = verify_media(dest, toolchain)

    assert result.max_frame_gap == pytest.approx(0.533, abs=0.05)
    assert result.frame_interval == pytest.approx(0.0333, abs=0.005), "관측된 전형 간격"
    assert not any("최대 프레임 간격" in issue for issue in result.issues)


def test_기준은_평균_프레임률이_아니라_관측된_간격이다(toolchain, tmp_path, make_clip):
    """실측 녹화 4건의 max_gap/중앙값은 1.000000~1.000030 이다. 1.0 으로는 떨어진다."""
    video = make_clip("v.mp4", seconds=4)
    audio = make_clip("a.m4a", seconds=4, kind="audio")
    dest = tmp_path / "cfr.mp4"
    merge_streams([video, audio], dest, toolchain)

    result = verify_media(dest, toolchain)

    assert result.max_frame_gap / result.frame_interval == pytest.approx(1.0, abs=0.01)
    assert result.complete is True


# -- B. 마무리 단계에 시한이 있다 --------------------------------------------------


#: 시한이 걸렸다면 이 안에 돌아와야 한다. 안 걸리면 상대가 끝날 때까지 매달린다.
_TIMEOUT_BUDGET = STUB_TIMEOUT * 4


def test_probe_streams_는_시한을_받는다(toolchain, hanging_ffprobe, make_clip):
    """예전에는 timeout 인자를 받지도 않아 무기한 매달렸다."""
    clip = make_clip("v.mp4", seconds=1)

    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        probe_streams(clip, hanging_ffprobe, timeout=STUB_TIMEOUT)

    assert time.monotonic() - started < _TIMEOUT_BUDGET


def test_병합이_시한을_넘기면_끊고_사유를_남긴다(
    tmp_path, hanging_ffmpeg, make_clip
):
    clip = make_clip("v.mp4", seconds=1)
    dest = tmp_path / "merged.mp4"

    started = time.monotonic()
    with pytest.raises(ToolTimeout) as caught:
        merge_streams(
            [clip], dest, hanging_ffmpeg, maps=("0:v:0",), timeout=STUB_TIMEOUT
        )

    assert time.monotonic() - started < _TIMEOUT_BUDGET
    assert "끝나지 않아 끊었다" in str(caught.value)
    assert not dest.exists(), "반쯤 쓰인 결과 파일은 치운다"


def test_검증이_시한을_넘기면_사유를_결과에_담는다(hanging_ffprobe, make_clip):
    clip = make_clip("v.mp4", seconds=1)

    started = time.monotonic()
    result = verify_media(clip, hanging_ffprobe, timeout=STUB_TIMEOUT)

    assert time.monotonic() - started < _TIMEOUT_BUDGET, "예외 대신 결과로 돌려준다"
    assert result.timed_out is True
    assert result.playable is False
    assert any("끊었다" in issue for issue in result.issues)


def test_데먹싱_검사도_시한을_받는다(toolchain, hanging_ffmpeg, make_clip):
    clip = make_clip("v.mp4", seconds=1)

    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        check_demux(clip, hanging_ffmpeg, timeout=STUB_TIMEOUT)

    assert time.monotonic() - started < _TIMEOUT_BUDGET


def test_패킷_훑기가_시한을_넘기면_끊는다(toolchain, hanging_ffprobe, make_clip):
    """출력을 한 줄도 내놓지 않고 물리는 경우. 루프 안에서 시각을 봐도 못 끊는다."""
    clip = make_clip("v.mp4", seconds=1)

    started = time.monotonic()
    with pytest.raises(ToolTimeout):
        merge_module._scan_video_packets(
            clip, hanging_ffprobe, timeout=STUB_TIMEOUT
        )

    assert time.monotonic() - started < _TIMEOUT_BUDGET


def test_verify_media_의_기본_시한은_유한하다():
    """기본값이 None 이면 GUI 는 시한을 따로 넘기지 않는 한 무기한 매달린다."""
    import inspect

    defaults = {
        name: p.default
        for name, p in inspect.signature(verify_media).parameters.items()
    }
    assert isinstance(defaults["timeout"], (int, float))
    assert defaults["timeout"] > 0
    merge_defaults = {
        name: p.default
        for name, p in inspect.signature(merge_streams).parameters.items()
    }
    assert isinstance(merge_defaults["timeout"], (int, float))
    probe_defaults = {
        name: p.default
        for name, p in inspect.signature(probe_streams).parameters.items()
    }
    assert isinstance(probe_defaults["timeout"], (int, float))
