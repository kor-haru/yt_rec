"""pytest 공통 설정.

Qt 위젯 테스트를 헤드리스로 돌리기 위해 PySide6를 import 하기 **전에**
``QT_QPA_PLATFORM=offscreen`` 을 세운다. 이미 값이 있으면 존중한다.

다크 테마
---------
``offscreen`` 플랫폼은 ``styleHints().colorScheme()`` 이 항상 ``Unknown`` 이고
팔레트가 라이트로 고정된다. ``setColorScheme(Dark)`` 를 불러도 팔레트는 바뀌지
않는다. 즉 **다크 테마 회귀 테스트가 라이트 팔레트만 측정하게 된다** — 이
저장소가 겪은 `다크 테마에서 안내 문구가 배경과 같은 색이 되어 통째로 보이지
않던` 사고를 막으려고 붙인 검사가, 정작 그 사고 조건에서는 돌지 않는다.

그래서 :func:`dark_palette` 로 어두운 팔레트를 직접 만들어
``QApplication.setPalette`` 로 밀어 넣는다. 실제 다크 모드로 기동하는 것과
같은 순서(**창을 만들기 전에** 팔레트를 세운다)를 지키는 것이 중요하다.
:func:`~yt_rec.ui.widgets.set_muted` 처럼 생성 시점의 팔레트에서 색을 계산해
위젯에 박아 두는 코드가 있으므로, 창을 만든 뒤에 팔레트를 바꾸면 실제 다크
모드와 다른 상태를 측정하게 된다.

녹화 엔진
---------
실제 미디어가 필요한 테스트는 ffmpeg 로 짧은 합성 클립을 그때그때 만든다.
큰 파일을 저장소에 넣지 않기 위해서다. ffmpeg/yt-dlp 가 없으면 해당 테스트는
건너뛴다.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtGui import QColor, QPalette  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from yt_rec.recording.binaries import BinaryNotFoundError, Toolchain, resolve_toolchain  # noqa: E402
from yt_rec.state.store import AppState  # noqa: E402
from yt_rec.state.stub import StubEventSource  # noqa: E402
from yt_rec.ui.settings_store import WindowSettings  # noqa: E402


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def dark_palette() -> QPalette:
    """다크 테마 팔레트. Fusion 다크와 같은 계열의 값이다.

    실제 Windows 다크 모드에서 Qt 가 만드는 팔레트에 맞춘 어두운 배경과 밝은
    본문 색이다. 정확한 색조가 아니라 `배경이 어둡고 본문이 밝다` 는 관계가
    검사에 필요한 조건이다.
    """
    palette = QPalette()
    window = QColor(0x20, 0x20, 0x20)
    base = QColor(0x2A, 0x2A, 0x2A)
    text = QColor(0xF0, 0xF0, 0xF0)
    disabled = QColor(0x7F, 0x7F, 0x7F)

    for group in (
        QPalette.ColorGroup.Active,
        QPalette.ColorGroup.Inactive,
        QPalette.ColorGroup.Disabled,
    ):
        palette.setColor(group, QPalette.ColorRole.Window, window)
        palette.setColor(group, QPalette.ColorRole.Base, base)
        palette.setColor(group, QPalette.ColorRole.AlternateBase, window)
        palette.setColor(group, QPalette.ColorRole.Button, window)
        palette.setColor(group, QPalette.ColorRole.ToolTipBase, base)
        palette.setColor(group, QPalette.ColorRole.Dark, QColor(0x14, 0x14, 0x14))
        palette.setColor(group, QPalette.ColorRole.Mid, QColor(0x3C, 0x3C, 0x3C))
        palette.setColor(group, QPalette.ColorRole.Midlight, QColor(0x50, 0x50, 0x50))
        palette.setColor(group, QPalette.ColorRole.Light, QColor(0x5A, 0x5A, 0x5A))
        palette.setColor(group, QPalette.ColorRole.Shadow, QColor(0x00, 0x00, 0x00))
        palette.setColor(group, QPalette.ColorRole.Highlight, QColor(0x2A, 0x6F, 0xB8))
        palette.setColor(group, QPalette.ColorRole.HighlightedText, text)
        for role in (
            QPalette.ColorRole.WindowText,
            QPalette.ColorRole.Text,
            QPalette.ColorRole.ButtonText,
            QPalette.ColorRole.ToolTipText,
            QPalette.ColorRole.BrightText,
        ):
            palette.setColor(
                group,
                role,
                disabled if group is QPalette.ColorGroup.Disabled else text,
            )
    return palette


@pytest.fixture(params=["light", "dark"])
def theme(request: pytest.FixtureRequest, qapp: QApplication) -> str:
    """라이트/다크 두 팔레트로 같은 검사를 돌린다.

    이 픽스처를 받는 테스트는 **픽스처를 받은 뒤에** 창을 만들어야 한다.
    pytest 픽스처 순서 때문에 자연히 그렇게 되지만, 창을 미리 만들어 둔 뒤
    적용하면 생성 시점 팔레트에서 색을 계산하는 코드가 라이트 값을 물고 있게
    된다.
    """
    original = QApplication.palette()
    if request.param == "dark":
        QApplication.setPalette(dark_palette())
    try:
        yield request.param
    finally:
        QApplication.setPalette(original)


@pytest.fixture
def state(qapp: QApplication) -> AppState:
    """갱신 묶기를 끈 저장소. 테스트는 방출 시점을 직접 통제한다."""
    store = AppState(emit_interval_ms=0)
    yield store
    store.deleteLater()


@pytest.fixture
def stub(state: AppState) -> StubEventSource:
    source = StubEventSource()
    state.attach(source)
    yield source
    source.stop()
    state.detach(source)


@pytest.fixture
def window_settings(tmp_path) -> WindowSettings:
    """사용자 설정을 건드리지 않는 임시 INI 기반 설정."""
    path = tmp_path / "yt-rec-test.ini"
    return WindowSettings(QSettings(str(path), QSettings.Format.IniFormat))


SAMPLE_SECONDS = 4
SAMPLE_FPS = 30


@pytest.fixture(scope="session")
def toolchain() -> Toolchain:
    try:
        return resolve_toolchain()
    except BinaryNotFoundError as exc:
        pytest.skip(str(exc))


@pytest.fixture(scope="session")
def sample_streams(tmp_path_factory, toolchain: Toolchain) -> tuple[Path, Path]:
    """영상만 담긴 파일과 음성만 담긴 파일. yt-dlp 중간 파일을 흉내 낸다."""
    directory = tmp_path_factory.mktemp("sample")
    video = directory / "video.mp4"
    audio = directory / "audio.m4a"

    video_cmd = [
        str(toolchain.ffmpeg), "-y", "-hide_banner", "-v", "error",
        "-f", "lavfi",
        "-i", f"testsrc2=size=320x240:rate={SAMPLE_FPS}:duration={SAMPLE_SECONDS}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-g", str(SAMPLE_FPS), str(video),
    ]
    audio_cmd = [
        str(toolchain.ffmpeg), "-y", "-hide_banner", "-v", "error",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={SAMPLE_SECONDS}",
        "-c:a", "aac", "-b:a", "64k", str(audio),
    ]
    for cmd in (video_cmd, audio_cmd):
        proc = subprocess.run(cmd, capture_output=True)
        if proc.returncode != 0:
            pytest.skip(
                "합성 클립을 만들지 못했다 (코덱 미지원?): "
                + proc.stderr.decode("utf-8", "replace")[:300]
            )
    return video, audio


@pytest.fixture()
def intermediates(tmp_path: Path, sample_streams: tuple[Path, Path]) -> tuple[Path, str]:
    """work 디렉터리에 yt-dlp 가 남긴 것과 같은 이름의 중간 파일을 놓는다."""
    video, audio = sample_streams
    work_dir = tmp_path / "work" / "VIDEOID0001"
    work_dir.mkdir(parents=True)
    (work_dir / "VIDEOID0001.f137.mp4").write_bytes(video.read_bytes())
    (work_dir / "VIDEOID0001.f140.m4a").write_bytes(audio.read_bytes())
    return work_dir, "VIDEOID0001"


@pytest.fixture(scope="session")
def python_executable() -> str:
    return sys.executable
