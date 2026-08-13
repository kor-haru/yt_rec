"""창 기하와 섹션 접힘 상태 저장.

``QSettings`` 를 얇게 감싼다. 테스트는 임시 INI 파일을 쓰는 인스턴스를 넘겨
사용자 설정을 건드리지 않는다.

기능 설정(저장 위치, 화질 상한 등)은 이슈 #11이 담당한다. 여기서 다루는 것은
창 자체의 표시 상태뿐이다.
"""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QSettings

__all__ = ["ORGANIZATION", "APPLICATION", "WindowSettings", "create_settings"]

ORGANIZATION = "yt-rec"
APPLICATION = "yt-rec"


def create_settings() -> QSettings:
    """사용자 프로필에 저장하는 기본 설정 객체."""
    return QSettings(ORGANIZATION, APPLICATION)


class WindowSettings:
    """창 표시 상태 읽기/쓰기."""

    def __init__(self, settings: QSettings | None = None) -> None:
        self._settings = settings if settings is not None else create_settings()

    @property
    def settings(self) -> QSettings:
        return self._settings

    # -- 창 기하 -------------------------------------------------------
    def geometry(self) -> QByteArray | None:
        value = self._settings.value("window/geometry")
        return value if isinstance(value, QByteArray) and not value.isEmpty() else None

    def set_geometry(self, value: QByteArray) -> None:
        self._settings.setValue("window/geometry", value)

    def window_state(self) -> QByteArray | None:
        value = self._settings.value("window/state")
        return value if isinstance(value, QByteArray) and not value.isEmpty() else None

    def set_window_state(self, value: QByteArray) -> None:
        self._settings.setValue("window/state", value)

    # -- 섹션 접힘 -----------------------------------------------------
    def section_collapsed(self, key: str, default: bool = False) -> bool:
        value = self._settings.value(f"sections/{key}/collapsed", default)
        if isinstance(value, bool):
            return value
        return str(value).lower() in ("true", "1", "yes")

    def set_section_collapsed(self, key: str, collapsed: bool) -> None:
        self._settings.setValue(f"sections/{key}/collapsed", bool(collapsed))

    def sync(self) -> None:
        """디스크에 즉시 반영한다."""
        self._settings.sync()
