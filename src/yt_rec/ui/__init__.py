"""Qt Widgets 기반 화면.

QML과 QtWebEngine은 쓰지 않는다(README `제약` 참고). 이 패키지의 위젯은
:mod:`yt_rec.state` 의 시그널만 구독하며, 백엔드나 파일시스템을 직접 조회하지
않는다.
"""

from .dashboard import Dashboard
from .main_window import MainWindow
from .settings_store import WindowSettings, create_settings
from .widgets import Badge, CollapsibleSection, ElidedLabel

__all__ = [
    "Badge",
    "CollapsibleSection",
    "Dashboard",
    "ElidedLabel",
    "MainWindow",
    "WindowSettings",
    "create_settings",
]
