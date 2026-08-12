"""메타데이터 선확보와 보관 (#14).

방송 종료 직후 영상이 멤버 전용으로 바뀌면 제목을 더는 조회할 수 없다.
그래서 시작 시점에 받아 두고, 파일명은 보관된 값으로만 정해야 한다.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from yt_rec.recording.binaries import Toolchain
from yt_rec.recording.errors import DenialCategory, MetadataUnavailableError
from yt_rec.recording.metadata import METADATA_FILENAME, LiveMetadata, fetch_metadata

KST = timezone(timedelta(hours=9))


def make_metadata(**kwargs) -> LiveMetadata:
    base = dict(
        video_id="zoYkEERlM0w",
        title="행백TV 실시간 Q&A / 내 집 마련 고민 해결_260811",
        channel="행백TV",
        channel_id="UCxxxx",
        release_timestamp=int(datetime(2026, 8, 11, 20, 0, tzinfo=KST).timestamp()),
        live_status="is_live",
        fetched_at=1.0,
    )
    base.update(kwargs)
    return LiveMetadata(**base)


# -- 보관과 복원 ---------------------------------------------------------------


def test_보관하고_다시_읽어도_같다(tmp_path):
    original = make_metadata()
    original.save(tmp_path)

    assert (tmp_path / METADATA_FILENAME).exists()
    assert LiveMetadata.load(tmp_path) == original


def test_보관_파일은_UTF8_이다(tmp_path):
    make_metadata().save(tmp_path)
    text = (tmp_path / METADATA_FILENAME).read_text(encoding="utf-8")
    assert "행백TV" in text
    assert json.loads(text)["channel"] == "행백TV"


def test_보관된_값이_없으면_None(tmp_path):
    assert LiveMetadata.load(tmp_path) is None


def test_깨진_보관_파일은_None(tmp_path):
    (tmp_path / METADATA_FILENAME).write_text("{망가짐", encoding="utf-8")
    assert LiveMetadata.load(tmp_path) is None


# -- 파일명 결정 ---------------------------------------------------------------


def test_보관된_값만으로_파일명을_만든다(tmp_path):
    """조회 경로가 완전히 막혀도 이 계산에는 네트워크가 필요 없다."""
    make_metadata().save(tmp_path)
    restored = LiveMetadata.load(tmp_path)

    assert restored.basename(tz=KST) == "2026-08-11_행백TV 실시간 Q&A ／ 내 집 마련 고민 해결_260811"


def test_심야_방송은_로컬_날짜로_이름이_정해진다():
    """2026-08-12 01:30 KST 는 UTC 로는 08-11 이다. 로컬 날짜를 써야 한다."""
    epoch = int(datetime(2026, 8, 12, 1, 30, tzinfo=KST).timestamp())
    metadata = make_metadata(release_timestamp=epoch, title="심야 방송")

    assert datetime.fromtimestamp(epoch, tz=timezone.utc).date().isoformat() == "2026-08-11"
    assert metadata.basename(tz=KST) == "2026-08-12_심야 방송"


def test_release_timestamp_가_없으면_업로드_시각을_쓴다():
    epoch = int(datetime(2026, 8, 9, 12, 0, tzinfo=KST).timestamp())
    metadata = make_metadata(release_timestamp=None, upload_timestamp=epoch)
    assert metadata.basename(tz=KST).startswith("2026-08-09_")


def test_시각이_전혀_없으면_확보_시각을_쓴다():
    epoch = datetime(2026, 8, 7, 9, 0, tzinfo=KST).timestamp()
    metadata = make_metadata(release_timestamp=None, upload_timestamp=None, fetched_at=epoch)
    assert metadata.basename(tz=KST).startswith("2026-08-07_")


def test_제목이_없으면_video_id_로_이름을_만든다():
    metadata = LiveMetadata.placeholder_for("abcdEFGH123")
    assert "abcdEFGH123" in metadata.basename(tz=KST)


def test_채널을_이름에_넣을_수_있다():
    metadata = make_metadata(title="제목", channel="채널")
    assert metadata.basename("{date}_{channel}_{title}", tz=KST) == "2026-08-11_채널_제목"


def test_아주_긴_제목은_잘린다():
    metadata = make_metadata(title="가" * 400)
    name = metadata.basename(max_title_chars=50, tz=KST)
    assert name.startswith("2026-08-11_")
    assert len(name) <= 50 + len("2026-08-11_")


# -- 조회 ----------------------------------------------------------------------


def fake_toolchain(script: Path) -> Toolchain:
    """python 스크립트를 yt-dlp 자리에 세운다."""
    return Toolchain(ytdlp=script, ffmpeg=Path("ffmpeg"), ffprobe=Path("ffprobe"))


@pytest.fixture()
def stub_ytdlp(tmp_path, monkeypatch):
    """--print-to-file 로 UTF-8 JSON 을 남기는 가짜 yt-dlp."""
    script = tmp_path / "stub_ytdlp.py"
    script.write_text(
        "import json, sys\n"
        "payload = json.loads(sys.argv[1])\n"
        "argv = sys.argv[2:]\n"
        "if payload is None:\n"
        "    sys.stdout.write('ERROR: [youtube] x: Join this channel to get access "
        "to members-only content\\n')\n"
        "    raise SystemExit(1)\n"
        "target = argv[argv.index('--print-to-file') + 2]\n"
        "with open(target, 'a', encoding='utf-8') as f:\n"
        "    f.write(json.dumps(payload) + '\\n')\n",
        encoding="utf-8",
    )

    def run(payload, work_dir, sys_executable):
        import yt_rec.recording.metadata as metadata_module

        original = subprocess.run

        def patched(argv, **kwargs):
            return original(
                [sys_executable, str(script), json.dumps(payload)] + argv[1:], **kwargs
            )

        monkeypatch.setattr(metadata_module.subprocess, "run", patched)
        return fetch_metadata("VID", "http://example/", fake_toolchain(script), work_dir)

    return run


def test_조회_결과를_LiveMetadata_로_옮긴다(stub_ytdlp, tmp_path, python_executable):
    payload = {
        "id": "zoYkEERlM0w",
        "title": "세제개편안 이후 대응 전략(메디테라)",
        "channel": "메디테라",
        "channel_id": "UCabc",
        "release_timestamp": 1786000000,
        "live_status": "is_live",
    }
    metadata = stub_ytdlp(payload, tmp_path, python_executable)

    assert metadata.video_id == "zoYkEERlM0w"
    assert metadata.title == "세제개편안 이후 대응 전략(메디테라)"
    assert metadata.channel == "메디테라"
    assert metadata.release_timestamp == 1786000000
    assert metadata.fetched_at > 0


def test_한글_제목이_깨지지_않는다(stub_ytdlp, tmp_path, python_executable):
    """yt-dlp 는 표준출력을 OEM 코드페이지로 쓴다. 그래서 파일로 받는다."""
    metadata = stub_ytdlp(
        {"id": "x", "title": "왕초보도 하루만에 끝내는 경매 기초! 🔥"}, tmp_path, python_executable
    )
    assert metadata.title == "왕초보도 하루만에 끝내는 경매 기초! 🔥"


def test_멤버_전용이면_범주와_함께_실패한다(stub_ytdlp, tmp_path, python_executable):
    with pytest.raises(MetadataUnavailableError) as excinfo:
        stub_ytdlp(None, tmp_path, python_executable)
    assert excinfo.value.category is DenialCategory.MEMBERS_ONLY
    assert not excinfo.value.transient


def test_이전_조회_결과가_남아_있어도_최신_줄을_읽는다(tmp_path, python_executable, stub_ytdlp):
    (tmp_path / "metadata.raw.json").write_text('{"id":"old","title":"옛날"}\n', encoding="utf-8")
    metadata = stub_ytdlp({"id": "new", "title": "지금"}, tmp_path, python_executable)
    assert metadata.title == "지금"
