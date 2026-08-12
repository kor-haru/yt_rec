"""pytest 공통 설정.

Qt 위젯 테스트를 헤드리스로 돌리기 위해 PySide6를 import 하기 **전에**
``QT_QPA_PLATFORM=offscreen`` 을 세운다. 이미 값이 있으면 존중한다.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from yt_rec.state.store import AppState  # noqa: E402
from yt_rec.state.stub import StubEventSource  # noqa: E402
from yt_rec.ui.settings_store import WindowSettings  # noqa: E402


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


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
