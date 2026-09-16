"""한 사용자 세션에 yt-rec GUI는 하나만 둔다.

두 번째 실행은 수신기·WebEngine 프로필을 다시 열지 않고, 이미 떠 있는 창만 연다.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QLockFile, QObject, QStandardPaths, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

SOCKET_NAME = "yt-rec-kor-haru-instance"
_RAISE = b"raise\n"


def default_lock_path() -> Path:
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericDataLocation)
    if not base:
        raise RuntimeError("인스턴스 잠금 위치를 찾을 수 없습니다")
    path = Path(base) / "yt-rec" / "instance.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


class InstanceLock(QObject):
    """프로세스 하나만이 잠금과 로컬 소켓을 갖는다."""

    activate_requested = Signal()

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        lock_path: Path | None = None,
        socket_name: str = SOCKET_NAME,
    ) -> None:
        super().__init__(parent)
        self._lock_path = Path(lock_path) if lock_path is not None else default_lock_path()
        self._socket_name = socket_name
        self._lock = QLockFile(str(self._lock_path))
        self._server: QLocalServer | None = None

    def acquire(self) -> bool:
        """이 프로세스가 주 인스턴스이면 True. 아니면 기존 창을 열고 False."""
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        if not self._lock.tryLock(100):
            if self._lock.error() != QLockFile.LockError.LockFailedError:
                raise RuntimeError(
                    "인스턴스 잠금 파일을 만들지 못했습니다. 앱 데이터 폴더 권한과 디스크 여유 공간을 확인하세요."
                )
            self._ask_primary_to_raise()
            return False
        self._listen()
        return True

    def close(self) -> None:
        if self._server is not None:
            self._server.close()
            QLocalServer.removeServer(self._socket_name)
            self._server = None
        if self._lock.isLocked():
            self._lock.unlock()

    def _listen(self) -> None:
        QLocalServer.removeServer(self._socket_name)
        server = QLocalServer(self)
        server.newConnection.connect(self._on_connection)
        if not server.listen(self._socket_name):
            self._lock.unlock()
            raise RuntimeError("실행 중 인스턴스 소켓을 열지 못했습니다")
        self._server = server

    def _on_connection(self) -> None:
        if self._server is None:
            return
        sock = self._server.nextPendingConnection()
        if sock is None:
            return
        if sock.bytesAvailable() == 0:
            sock.waitForReadyRead(500)
        self._read(sock)

    def _read(self, sock: QLocalSocket) -> None:
        payload = bytes(sock.readAll())
        sock.disconnectFromServer()
        if _RAISE in payload or payload.strip() == b"raise":
            self.activate_requested.emit()

    def _ask_primary_to_raise(self) -> None:
        self._allow_primary_foreground()
        sock = QLocalSocket(self)
        sock.connectToServer(self._socket_name)
        if not sock.waitForConnected(500):
            return
        sock.write(_RAISE)
        sock.waitForBytesWritten(500)
        sock.disconnectFromServer()
        if sock.state() != QLocalSocket.LocalSocketState.UnconnectedState:
            sock.waitForDisconnected(500)

    def _allow_primary_foreground(self) -> None:
        if sys.platform != "win32":
            return
        pid = 0
        try:
            info = self._lock.getLockInfo()
        except Exception:
            return
        if isinstance(info, tuple) and info:
            try:
                pid = int(info[0])
            except (TypeError, ValueError):
                pid = 0
        if pid <= 0:
            return
        try:
            import ctypes
            ctypes.windll.user32.AllowSetForegroundWindow(pid)
        except OSError:
            return
