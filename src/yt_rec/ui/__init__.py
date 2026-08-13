"""Qt Widgets 기반 화면.

QML과 QtWebEngine은 쓰지 않는다(README `제약` 참고). 이 패키지의 위젯은
:mod:`yt_rec.state` 의 시그널만 구독하며, 백엔드나 파일시스템을 직접 조회하지
않는다.

시각을 그릴 때는 :func:`~yt_rec.ui.formatting.to_local` 을 거친다. 상태 계층이
주는 ``datetime`` 은 어느 시간대일지 모르므로, 표시 직전에 로컬로 옮겨야 화면
곳곳의 시각이 같은 기준으로 그려진다.
"""

from .dashboard import Dashboard
from .formatting import to_local
from .main_window import MainWindow
from .settings_store import WindowSettings, create_settings
from .widgets import Badge, CollapsibleSection, ElidedLabel, drawn_text

__all__ = [
    "Badge",
    "CollapsibleSection",
    "Dashboard",
    "ElidedLabel",
    "MainWindow",
    "WindowSettings",
    "create_settings",
    "drawn_text",
    "to_local",
]
