"""화질 상한 적용과 설정 지속화 (#4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from yt_rec.recording.options import (
    QUALITY_PRESETS,
    RecordingOptions,
    load_settings,
    save_settings,
)


def make(**kwargs) -> RecordingOptions:
    kwargs.setdefault("output_dir", Path("out"))
    return RecordingOptions(**kwargs)


@pytest.mark.parametrize("height", [2160, 1440, 1080, 720, 480, 360])
def test_화질_상한이_포맷_선택식에_들어간다(height):
    selector = make(max_height=height).format_selector()
    assert f"bv*[height<={height}]+ba" in selector
    assert f"b[height<={height}]" in selector


def test_상한_없음이면_최고_화질을_고른다():
    assert make(max_height=None).format_selector() == "bv*+ba/b"


def test_화질_상한에_무제한_대안이_없다():
    """마지막 `/bv*+ba/b` 는 height 필터를 벗겨 720p 상한에서 1080p를 고른다."""
    selector = make(max_height=720).format_selector()
    assert "/bv*+ba/b" not in selector
    assert "height<=720" in selector


def test_최상의_영상과_최상의_오디오를_고른다():
    selector = make(max_height=1080).format_selector()
    assert selector.startswith("bv*[height<=1080]+ba")


def test_모든_프리셋이_유효한_선택식을_만든다():
    for height in QUALITY_PRESETS.values():
        selector = make(max_height=height).format_selector()
        assert selector and "+ba" in selector


# -- 지속화 -------------------------------------------------------------------


def test_설정은_저장하고_다시_읽어도_같다(tmp_path):
    path = tmp_path / "settings.json"
    original = RecordingOptions(
        output_dir=tmp_path / "녹화",
        max_height=720,
        fragment_retries=7,
        total_retries=3,
        stall_timeout_seconds=123.0,
        filename_template="{date}_{channel}_{title}",
        extra_ytdlp_args=("--cookies-from-browser", "chrome"),
    )

    save_settings(original, path)
    restored = load_settings(path)

    assert restored == original
    assert restored.max_height == 720
    assert restored.output_dir == tmp_path / "녹화"


def test_설정_파일이_없으면_기본값을_쓴다(tmp_path):
    fallback = make(max_height=480)
    assert load_settings(tmp_path / "없음.json", default=fallback) == fallback


def test_깨진_설정_파일이면_기본값을_쓴다(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{이건 JSON 이 아니다", encoding="utf-8")
    fallback = make(max_height=480)
    assert load_settings(path, default=fallback) == fallback


def test_한글_경로가_그대로_보존된다(tmp_path):
    path = tmp_path / "settings.json"
    original = make(output_dir=tmp_path / "내 녹화 폴더")
    save_settings(original, path)
    assert "내 녹화 폴더" in path.read_text(encoding="utf-8")
    assert load_settings(path).output_dir == tmp_path / "내 녹화 폴더"


# -- 방어 ---------------------------------------------------------------------


def test_재시도_상한은_음수일_수_없다():
    with pytest.raises(ValueError):
        make(fragment_retries=-1)
    with pytest.raises(ValueError):
        make(total_retries=-1)


def test_정지_판정_시간은_양수여야_한다():
    with pytest.raises(ValueError):
        make(stall_timeout_seconds=0)


def test_기본_조각_재시도_상한은_유한하다():
    """무한으로 두면 사라진 조각을 영원히 다시 요청하며 정지한다(#14)."""
    options = make()
    assert isinstance(options.fragment_retries, int)
    assert 0 < options.fragment_retries < 10_000
    assert options.fragment_retries != options.total_retries  # 구분해 설정한다


def test_work_root_기본값은_출력_디렉터리_아래다(tmp_path):
    options = make(output_dir=tmp_path)
    assert options.resolved_work_root() == tmp_path / ".yt-rec"


def test_지원하지_않는_컨테이너는_거부한다():
    with pytest.raises(ValueError):
        make(container="avi")
