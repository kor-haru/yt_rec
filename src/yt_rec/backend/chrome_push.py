"""실제 Chrome 을 YouTube 푸시 수신기로 쓰는 경로 (#79).

QtWebEngine 의 Chromium 은 Google 이 폐기한 구형 GCM 등록 엔드포인트를 쓰기
때문에 ``pushManager.subscribe()`` 가 영원히 비어 있다 (#62). 서버가 거절하는
것이라 앱 코드로는 우회할 수 없다. 반면 설치된 Chrome 은 현행 경로를 쓰므로
**전용 프로필**로 띄운 Chrome 을 수신기로 세운다.

수신 방식은 CDP 다. 브라우저 세션에서 ``Target.setAutoAttach`` 로 서비스 워커
타깃만 골라 붙고, 워커가 **스크립트를 돌리기 전**(``waitForDebuggerOnStart``)에
``push`` 리스너를 먼저 등록한다. 그 리스너는 YouTube 자신의 핸들러보다 먼저
돌면서 ``showNotification`` 을 감싼다. 그래서 탭이 하나도 떠 있지 않아도, 워커가
푸시 때문에 깨어나는 순간의 페이로드를 그대로 받는다.

브라우저는 헤드리스로 띄운다 (#86). 푸시 통로(MCS)와 서비스 워커는 창 없이도
그대로 살아 있으므로 수신에는 창이 필요 없다. 창은 사용자가 직접 로그인하거나
알림 설정을 바꿀 때만 뜬다.

받은 값은 기존 :class:`~.notifications.LiveNotification` /
:class:`~.notification_history.ReceivedNotification` 경로로 그대로 들어간다.
녹화 엔진은 이 모듈을 알지 못한다.

주기 조회는 하지 않는다. 하트비트는 브라우저와 소켓이 살아 있는지만 확인하며
youtube.com 에 아무것도 묻지 않는다.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QTimer, QUrl, Signal, Slot
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWebSockets import QWebSocket

from .notifications import LiveNotification
from .notification_history import ReceivedNotification
from .push_payload import (
    NOTIFICATION_SETTINGS_URL,
    YOUTUBE,
    NoVideo,
    _is_youtube_url,
    _video_from_data,
    _video_id_from_data,
)
from ..webengine_boot import profile_root

__all__ = [
    "ChromePushReceiver",
    "CdpProtocol",
    "chrome_executable",
    "chrome_profile_directory",
]

_LOG = logging.getLogger(__name__)

#: 서비스 워커가 앱으로 페이로드를 넘길 때 쓰는 ``Runtime.addBinding`` 이름.
BINDING = "__ytRecPush"
#: 재실행해도 같은 브라우저에 다시 붙기 위해 프로필에 남기는 디버깅 포트.
PORT_FILE = "yt-rec-devtools-port"

_MAX_FRAME = 1_048_576
_MAX_PAYLOAD = 65_536
_CONNECT_ATTEMPTS = 20
_CONNECT_RETRY_MS = 500
_HEARTBEAT_MS = 30_000
_ORPHAN_ATTACH_MS = 250
# 갓 띄운 Chrome 이 youtube.com 을 다 띄우는 데 걸리는 시간을 덮는다 (#88).
# 실측 (#90): ``checking`` 15:35:59.800 → ``ready`` 15:36:04.696, 4.896초. 이어받기가
# 아니라 새로 띄운 경우이고, 머신은 그동안 영상 인코딩을 물고 돌던 중이었다. 첫 확인은
# ``checking`` 에서 7ms 안에 떨어지므로 (#88 이전 로그가 바로 그 지점에서 6~7ms 만에
# ``error`` 로 확정했다) 저 4.896초는 거의 전부 재확인이 쓴 시간이다. 옛 예산 6회는
# 4.8초였으니 0.2초만 더 늦었어도 멀쩡한 브라우저가 ``error`` 로 눌어붙었다.
# 비용이 비대칭이다. 길면 진짜 오류 문구가 그만큼 늦게 뜰 뿐 그동안 화면은 ``checking``
# 이다. 짧으면 멀쩡한데 ``error`` 로 눌어붙고 사용자가 '알림 상태 확인'을 직접 누르기
# 전까지 안 풀린다. 그래서 실측의 두 배 넘게 잡는다 — 15 × 800ms = 12초.
_RECHECK_DELAY_MS = 800
_RECHECK_ATTEMPTS = 15
_RESTART_DELAY_MS = (5_000, 10_000, 20_000, 40_000, 60_000)
_SERVICE_WORKER_SCOPE = YOUTUBE + "/"
#: 쉬는 상태. 등록 확인을 마쳤을 때와, 녹화할 것이 없는 알림을 넘겼을 때 모두 여기로
#: 돌아온다. 한 곳에 둬야 둘이 갈라지지 않는다.
_RESTING = "알림 수신 준비됨 · 실제 도착 대기 중"


def chrome_executable() -> Path | None:
    """설치된 Chrome 실행 파일. 없으면 ``None``."""
    candidates: list[Path] = []
    if sys.platform == "win32":
        for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(key)
            if base:
                candidates.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
    elif sys.platform == "darwin":  # pragma: no cover - 플랫폼 의존
        candidates.append(Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))
    else:  # pragma: no cover - 플랫폼 의존
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            found = shutil.which(name)
            if found:
                candidates.append(Path(found))
    return next((path for path in candidates if path.is_file()), None)


def chrome_profile_directory() -> Path:
    """yt-rec 전용 Chrome 프로필.

    사용자의 개인 프로필(``Default``)은 어떤 경우에도 쓰지 않는다. 앱이 쿠키와
    푸시 등록을 소유해야 하고, 디버깅 포트를 연 브라우저에 개인 세션을 올릴 수는
    없기 때문이다.
    """
    return profile_root().with_name("chrome-push")


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _read_port(path: Path) -> int:
    try:
        value = int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return 0
    return value if 1 <= value <= 65535 else 0


#: 워커가 멈춰 있는 동안 돌린다. 이 시점에는 ``self.registration`` 이 아직
#: ``null`` 이고 ``ServiceWorkerRegistration`` 전역도 없어서 ``showNotification``
#: 을 바로 감쌀 수 없다. 대신 ``push`` 리스너를 **가장 먼저** 걸어 두고, 그
#: 리스너 안에서 감싼다 — 그때는 registration 이 존재하고, 리스너 등록 순서상
#: YouTube 자신의 핸들러보다 먼저 돌기 때문에 첫 알림부터 잡힌다.
_HOOK = """
(() => {
  const scope = self;
  const send = value => { try { BINDING(JSON.stringify(value)); } catch (e) {} };
  const wrap = () => {
    if (scope.__ytRecWrapped || !scope.registration) return false;
    const proto = Object.getPrototypeOf(scope.registration);
    const original = proto.showNotification;
    proto.showNotification = function (title, options) {
      send({kind: 'notification', title: String(title), options: options || {}});
      return original.apply(this, arguments);
    };
    scope.__ytRecWrapped = true;
    return true;
  };
  if (!scope.__ytRecHooked) {
    scope.__ytRecHooked = true;
    scope.addEventListener('push', event => {
      wrap();
      let text = null;
      try { text = event.data ? event.data.text() : null; } catch (e) {}
      send({kind: 'push', text: text});
    });
  }
  return JSON.stringify({wrapped: wrap()});
})()
"""

#: 권한·서비스 워커·푸시 구독을 확인한다. 사용자 조작이나 세션 시작에서 부르고,
#: 실패했을 때만 제한된 횟수로 다시 본다. 영상/채널 상태는 묻지 않는다.
_REGISTRATION = """
(async () => {
  const out = {permission: Notification.permission, worker: false, active: false, subscription: false};
  try {
    const registrations = await navigator.serviceWorker.getRegistrations();
    for (const registration of registrations) {
      if (new URL(registration.scope).origin !== 'ORIGIN') continue;
      out.worker = true;
      if (registration.active && registration.active.state === 'activated') {
        out.active = true;
        if (await registration.pushManager.getSubscription()) out.subscription = true;
      }
    }
  } catch (e) { out.failed = true; }
  return JSON.stringify(out);
})()
"""


class CdpProtocol:
    """전송 계층과 분리한 CDP 메시지 처리.

    소켓을 모르기 때문에 테스트가 브라우저 없이 이 클래스만으로 대화를 재현할 수
    있다. 호출 id 대응과 세션별 사건 분배만 한다.
    """

    def __init__(self, send_text: Callable[[str], None]) -> None:
        self._send_text = send_text
        self._next_id = 0
        self._replies: dict[int, Callable[[dict], None]] = {}
        self.events: dict[str, Callable[[dict, str], None]] = {}

    def call(
        self, method: str, params: dict | None = None, *,
        session: str = "", on_reply: Callable[[dict], None] | None = None,
    ) -> int:
        self._next_id += 1
        message: dict[str, object] = {"id": self._next_id, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        if on_reply is not None:
            self._replies[self._next_id] = on_reply
        self._send_text(json.dumps(message))
        return self._next_id

    def feed(self, text: str) -> None:
        """브라우저가 보낸 한 프레임을 처리한다. 형식이 틀리면 조용히 버린다."""
        if not isinstance(text, str) or len(text) > _MAX_FRAME:
            return
        try:
            message = json.loads(text)
        except (ValueError, RecursionError):
            return
        if not isinstance(message, dict):
            return
        if type(message.get("id")) is int:
            handler = self._replies.pop(message["id"], None)
            if handler is not None:
                handler(message)
            return
        method = message.get("method")
        handler = self.events.get(method) if isinstance(method, str) else None
        if handler is not None:
            params = message.get("params")
            session = message.get("sessionId")
            handler(params if isinstance(params, dict) else {}, session if isinstance(session, str) else "")

    @staticmethod
    def value(message: dict) -> object:
        """``Runtime.evaluate`` 응답에서 JSON 문자열 결과만 꺼낸다."""
        result = message.get("result")
        if not isinstance(result, dict) or "exceptionDetails" in result:
            return None
        inner = result.get("result")
        if not isinstance(inner, dict) or inner.get("type") != "string":
            return None
        try:
            return json.loads(inner.get("value") or "")
        except (ValueError, RecursionError):
            return None


class ChromePushReceiver(QObject):
    """전용 프로필 Chrome 을 띄워 두고 YouTube 푸시를 받아 넘긴다.

    :class:`~.push_receiver.YouTubePushReceiver` 와 같은 신호 계약을 갖는다.
    다만 브라우저가 별도 프로세스라 ``page`` 를 내놓지 않는다 — 앱은 ``page``
    유무로 내장 뷰를 띄울지 Chrome 창을 앞으로 부를지 고른다.

    ``stop``/``close`` 는 한 번만 먹고 되돌릴 수 없다.
    """

    notification_received = Signal(object)  # LiveNotification, never raw web data.
    notification_arrived = Signal(object)  # Native display text, before video identification.
    status_changed = Signal(str, str)

    def __init__(
        self, parent: QObject | None = None, *,
        executable_finder: Callable[[], Path | None] = chrome_executable,
        profile_directory: Callable[[], Path] = chrome_profile_directory,
    ) -> None:
        super().__init__(parent)
        self._find_executable = executable_finder
        self._profile_directory = profile_directory
        self._closed = False
        self._started = False
        self._last_status: tuple[str, str] | None = None
        self._process: QProcess | None = None
        self._owns_process = False
        # 우리가 창을 띄워 둔 동안만 참이다. 이어받은 브라우저는 모드를 알 수 없으니
        # 거짓으로 둔다 — 창이 필요하면 닫고 다시 띄우는 쪽이 언제나 맞다.
        self._visible = False
        self._socket: QWebSocket | None = None
        self._cdp: CdpProtocol | None = None
        self._network = QNetworkAccessManager(self)
        self._port = 0
        self._attempts = 0
        self._restarts = 0
        self._rechecks = 0
        self._awaiting_pong = False
        self._worker_sessions: dict[str, str] = {}  # targetId -> sessionId
        self._page_session = ""
        self._page_target = ""
        self._heartbeat = QTimer(self)
        self._heartbeat.setInterval(_HEARTBEAT_MS)
        self._heartbeat.timeout.connect(self._pulse)

    # -- 상태 -----------------------------------------------------------------

    def _status(self, code: str, detail: str) -> None:
        # Only fixed, caller-owned messages are published. No site text/errors.
        if (code, detail) != self._last_status:
            self._last_status = (code, detail)
            self.status_changed.emit(code, detail)

    @property
    def status(self) -> tuple[str, str]:
        return self._last_status or ("disabled", "")

    # -- 수명주기 -------------------------------------------------------------

    @Slot()
    def start(self) -> None:
        if self._closed or self._started:
            return
        self._started = True
        self._bring_up()

    def _bring_up(self, visible_url: str = "") -> None:
        """수신기를 세운다. ``visible_url`` 이 있으면 그 주소를 보이는 창으로 연다."""
        if self._closed:
            return
        executable = self._find_executable()
        if executable is None:
            self._status("chrome_missing", "Chrome을 찾지 못했습니다. Chrome을 설치한 뒤 앱을 다시 실행하세요. "
                                           "설정에서 내장 브라우저 수신기로 바꿀 수도 있지만 현재 내장 경로는 푸시 등록이 되지 않습니다.")
            return
        try:
            profile = self._profile_directory()
            profile.mkdir(parents=True, exist_ok=True)
        except (OSError, RuntimeError):
            self._status("error", "수신 브라우저 프로필 폴더를 만들지 못했습니다. 로그를 확인하세요.")
            return
        port = _read_port(profile / PORT_FILE)
        if port and not visible_url:
            # 앱만 다시 뜬 경우다. 이미 로그인된 브라우저를 두 번 띄우지 않는다.
            # 그 브라우저가 어느 모드인지는 알 수 없고, 알 필요도 없다.
            self._status("connecting", "이미 실행 중인 수신 브라우저에 연결하는 중입니다.")
            self._port = port
            self._resolve(on_failure=lambda: self._spawn(executable, profile))
            return
        self._spawn(executable, profile, visible_url)

    def _spawn(self, executable: Path, profile: Path, visible_url: str = "") -> None:
        """Chrome 을 띄운다. 기본은 헤드리스이고, ``visible_url`` 이 있을 때만 창이 뜬다."""
        if self._closed:
            return
        self._status("connecting", "수신 브라우저(Chrome)를 시작하는 중입니다.")
        self._port = _free_port()
        try:
            (profile / PORT_FILE).write_text(str(self._port), encoding="ascii")
        except OSError:
            pass  # 포트 파일은 재사용용 편의일 뿐이라 없어도 동작한다.
        arguments = [
            f"--user-data-dir={profile}",
            f"--remote-debugging-port={self._port}",
            # 디버깅 통로는 루프백에 묶이고, Origin 헤더도 이 주소만 허용한다.
            f"--remote-allow-origins=http://127.0.0.1:{self._port}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if not visible_url:
            # 수신은 백그라운드 동작이다. 헤드리스에서도 푸시 통로(MCS)와 서비스
            # 워커는 그대로 붙으므로 창을 띄울 이유가 없다 (#86).
            arguments.append("--headless=new")
        arguments.append(visible_url or YOUTUBE)
        process = QProcess(self)
        process.setProgram(str(executable))
        process.setArguments(arguments)
        process.finished.connect(self._process_finished)
        process.errorOccurred.connect(self._process_error)
        self._process = process
        self._owns_process = True
        self._visible = bool(visible_url)
        self._attempts = 0
        process.start()
        QTimer.singleShot(_CONNECT_RETRY_MS, self._retry_resolve)

    @Slot()
    def _retry_resolve(self) -> None:
        if self._closed or self._socket is not None:
            return
        self._attempts += 1
        if self._attempts > _CONNECT_ATTEMPTS:
            self._status("chrome_down", "수신 브라우저(Chrome)에 연결하지 못했습니다. 잠시 뒤 다시 시도합니다.")
            self._schedule_restart()
            return
        self._resolve(on_failure=lambda: QTimer.singleShot(_CONNECT_RETRY_MS, self._retry_resolve))

    def _resolve(self, *, on_failure: Callable[[], None]) -> None:
        """디버깅 포트에서 브라우저 세션 WebSocket 주소를 받아 온다."""
        request = QNetworkRequest(QUrl(f"http://127.0.0.1:{self._port}/json/version"))
        reply = self._network.get(request)

        def finished() -> None:
            body = bytes(reply.readAll())
            failed = reply.error() != QNetworkReply.NetworkError.NoError
            reply.deleteLater()
            if self._closed or self._socket is not None:
                return
            address = ""
            if not failed:
                try:
                    document = json.loads(body)
                    address = document.get("webSocketDebuggerUrl", "") if isinstance(document, dict) else ""
                except (ValueError, RecursionError):
                    address = ""
            if not isinstance(address, str) or not address.startswith(f"ws://127.0.0.1:{self._port}/"):
                on_failure()
                return
            self._open_socket(address)

        reply.finished.connect(finished)

    def _open_socket(self, address: str) -> None:
        websocket = QWebSocket(f"http://127.0.0.1:{self._port}")
        websocket.connected.connect(self._socket_connected)
        websocket.disconnected.connect(self._socket_disconnected)
        websocket.textMessageReceived.connect(self._socket_text)
        self._socket = websocket
        websocket.open(QUrl(address))

    @Slot()
    def _socket_connected(self) -> None:
        if self._closed or self._socket is None:
            return
        self._restarts = 0
        self.begin_session(self._socket.sendTextMessage)

    @Slot(str)
    def _socket_text(self, text: str) -> None:
        if not self._closed and self._cdp is not None:
            self._cdp.feed(text)

    @Slot()
    def _socket_disconnected(self) -> None:
        if self._closed:
            return
        self._status("chrome_down", "수신 브라우저와의 연결이 끊어졌습니다. 다시 연결을 시도합니다.")
        self._schedule_restart()

    # -- CDP 세션 -------------------------------------------------------------

    def begin_session(self, send_text: Callable[[str], None]) -> None:
        """소켓이 열린 뒤의 CDP 대화. 테스트는 소켓 없이 여기로 바로 들어온다."""
        cdp = CdpProtocol(send_text)
        cdp.events["Target.attachedToTarget"] = self._attached
        cdp.events["Target.detachedFromTarget"] = self._detached
        cdp.events["Target.targetCreated"] = self._target_seen
        cdp.events["Target.targetInfoChanged"] = self._target_seen
        cdp.events["Inspector.targetCrashed"] = self._worker_stopped
        cdp.events["Runtime.bindingCalled"] = self._binding_called
        self._cdp = cdp
        self._worker_sessions.clear()
        self._page_session = self._page_target = ""
        self._awaiting_pong = False
        # CDP 권한 부여는 이 브라우저 세션 동안만 유효하다. 연결할 때마다 다시
        # 준다. 이것이 없으면 Chrome 이 푸시를 받아도 표시를 거절한다.
        cdp.call("Browser.grantPermissions", {"origin": YOUTUBE, "permissions": ["notifications"]})
        # 서비스 워커만 자동으로 붙잡는다. 워커 스크립트가 돌기 전에 멈춰 세워야
        # 첫 푸시부터 놓치지 않는다.
        cdp.call("Target.setAutoAttach", {
            "autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True,
            "filter": [{"type": "service_worker", "exclude": False}, {"exclude": True}],
        })
        cdp.call("Target.setDiscoverTargets", {"discover": True})
        self._status("checking", "수신 브라우저의 알림 권한과 푸시 등록을 한 번 확인하는 중입니다.")
        self._heartbeat.start()

    def _target_seen(self, params: dict, _session: str) -> None:
        info = params.get("targetInfo")
        if not isinstance(info, dict):
            return
        url = info.get("url")
        target = info.get("targetId")
        if not isinstance(target, str) or not isinstance(url, str):
            return
        if info.get("type") == "page" and _is_youtube_url(url) and not self._page_target:
            self._page_target = target
            if self._cdp is not None:
                self._cdp.call("Target.attachToTarget", {"targetId": target, "flatten": True})
        elif info.get("type") == "service_worker" and _is_youtube_url(url):
            # 새로 시작하는 워커는 자동 붙잡기가 **멈춰 세운 채로** 넘겨 준다. 그
            # 편이 훨씬 안전하므로 잠깐 기다렸다가, 그래도 세션이 없을 때만 —
            # 즉 앱이 붙기 전부터 돌고 있던 워커일 때만 — 직접 붙는다.
            QTimer.singleShot(_ORPHAN_ATTACH_MS, lambda: self._attach_orphan(target))

    def _attach_orphan(self, target: str) -> None:
        if not self._closed and self._cdp is not None and target not in self._worker_sessions:
            self._cdp.call("Target.attachToTarget", {"targetId": target, "flatten": True})

    def _attached(self, params: dict, _session: str) -> None:
        info = params.get("targetInfo")
        session = params.get("sessionId")
        if not isinstance(info, dict) or not isinstance(session, str) or self._cdp is None:
            return
        url, target = info.get("url"), info.get("targetId")
        if not isinstance(url, str) or not isinstance(target, str) or not _is_youtube_url(url):
            return
        if info.get("type") == "service_worker":
            waiting = params.get("waitingForDebugger") is True
            if self._worker_sessions.get(target, session) != session:
                # 자동 붙잡기와 수동 붙잡기가 겹쳤다. 같은 알림을 두 번 받지 않도록
                # 한쪽만 남긴다. 멈춰 세운 세션이면 반드시 먼저 깨우고 버린다.
                if waiting:
                    self._cdp.call("Runtime.runIfWaitingForDebugger", session=session)
                self._cdp.call("Target.detachFromTarget", {"sessionId": session})
                return
            self._worker_sessions[target] = session
            self._install_hook(session, waiting)
        elif info.get("type") == "page":
            self._page_session = session
            self._cdp.call("ServiceWorker.enable", session=session)
            self.inspect_registration()

    def _detached(self, params: dict, _session: str) -> None:
        session = params.get("sessionId")
        if not isinstance(session, str):
            return
        for target, current in tuple(self._worker_sessions.items()):
            if current == session:
                del self._worker_sessions[target]
        if session == self._page_session:
            self._page_session = self._page_target = ""

    def _worker_stopped(self, _params: dict, session: str) -> None:
        """워커가 멈추면 세션을 놓아 준다.

        붙어 있는 동안에는 워커를 다시 켜도 **같은 타깃이 되살아날 뿐** 새
        ``attachedToTarget`` 이 오지 않는다. 그러면 후킹이 사라진 채로 다음 푸시를
        맞는다 — 실측으로 확인한 실패 모드다. 놓아 주면 다음 시작이 자동 붙잡기로
        돌아와 멈춘 상태에서 다시 후킹된다.
        """
        if self._closed or self._cdp is None or session not in self._worker_sessions.values():
            return
        for target, current in tuple(self._worker_sessions.items()):
            if current == session:
                del self._worker_sessions[target]
        self._cdp.call("Target.detachFromTarget", {"sessionId": session})

    def _install_hook(self, session: str, waiting: bool) -> None:
        cdp = self._cdp
        if cdp is None:
            return
        expression = _HOOK.replace("BINDING(", BINDING + "(")
        cdp.call("Runtime.enable", session=session)
        cdp.call("Runtime.addBinding", {"name": BINDING}, session=session)
        cdp.call("Runtime.evaluate", {"expression": expression, "returnByValue": True},
                 session=session, on_reply=self._hook_installed)
        if waiting:
            # 같은 세션의 명령은 순서대로 처리되므로 후킹이 끝난 뒤 깨어난다.
            cdp.call("Runtime.runIfWaitingForDebugger", session=session)
            # 멈춰 있는 동안에는 ``registration`` 이 아직 null 이라 showNotification
            # 을 감쌀 수 없다. 깨어난 뒤 한 번 더 걸어, 푸시가 아닌 경로로 뜨는
            # 알림까지 잡는다. 푸시 자체는 멈춘 동안 건 리스너가 이미 덮는다.
            cdp.call("Runtime.evaluate", {"expression": expression, "returnByValue": True},
                     session=session, on_reply=self._hook_installed)

    def _hook_installed(self, message: dict) -> None:
        if self._closed:
            return
        if CdpProtocol.value(message) is None:
            self._status("error", "수신 브라우저의 YouTube 수신 프로그램에 연결하지 못했습니다. 알림 상태 확인을 눌러 다시 시도하세요.")

    # -- 수신 -----------------------------------------------------------------

    def _binding_called(self, params: dict, session: str) -> None:
        if (self._closed or params.get("name") != BINDING
                or session not in self._worker_sessions.values()):
            return
        payload = params.get("payload")
        if not isinstance(payload, str) or len(payload) > _MAX_PAYLOAD:
            self._status("error", "식별할 수 없는 알림을 무시했습니다")
            return
        try:
            value = json.loads(payload)
        except (ValueError, RecursionError):
            self._status("error", "식별할 수 없는 알림을 무시했습니다")
            return
        if not isinstance(value, dict):
            self._status("error", "식별할 수 없는 알림을 무시했습니다")
            return
        if value.get("kind") == "notification":
            self._notification(value)
        elif value.get("kind") == "push":
            self._push(value)
        else:
            # 후킹은 우리가 넣은 것이다. 모르는 형태가 오면 후킹이 갈렸다는 뜻이다.
            self._status("error", "식별할 수 없는 알림을 무시했습니다")

    def _notification(self, value: dict) -> None:
        title, options = value.get("title"), value.get("options")
        if not isinstance(title, str) or not isinstance(options, dict):
            self._status("error", "식별할 수 없는 알림을 무시했습니다")
            return
        body = options.get("body")
        body = body if isinstance(body, str) else ""
        if len(title) > 4096 or len(body) > 16384:
            self._status("error", "식별할 수 없는 알림을 무시했습니다")
            return
        self.notification_arrived.emit(ReceivedNotification(
            datetime.now(timezone.utc), title, body, synthetic=False,
        ))
        video = _video_from_data(options.get("data"))
        if video is NoVideo.ABSENT:
            # 커뮤니티 글이나 멤버십 알림에는 watch 주소가 없다 (#94). 구독 채널이
            # 으레 보내는 정상적인 알림이므로 수신기 고장과 같은 칸에 넣지 않는다.
            # 오류로 두면 다음 알림이 올 때까지 몇 시간이고 그대로 눌어붙는다.
            _LOG.info("영상이 없는 알림을 넘겼습니다")
            self._status("ready", _RESTING)
            return
        if isinstance(video, NoVideo):
            # 사유만 남긴다. 알림 문구는 이력에만 있고 로그로는 나가지 않는다.
            _LOG.warning("알림에서 영상 하나를 고르지 못했습니다: %s", video.value)
            self._status("error", "알림에서 영상 하나를 식별하지 못해 녹화하지 않습니다")
            return
        self._emit(video)

    def _push(self, value: dict) -> None:
        """원본 푸시 본문. 표시 문구가 없으므로 이력에는 남기지 않는다.

        YouTube 의 본문 형식은 공개되어 있지 않다. 영상 하나를 확실히 찾아낼 때만
        쓰고, 아니면 조용히 버린다 — ``showNotification`` 후킹이 본 경로이고 그쪽이
        따로 상태를 보고한다. 중복은 NotificationRecorder 가 걸러 낸다.
        """
        text = value.get("text")
        if not isinstance(text, str) or len(text) > _MAX_PAYLOAD:
            return
        try:
            data = json.loads(text)
        except (ValueError, RecursionError):
            return
        video_id = _video_id_from_data(data)
        if video_id is not None:
            self._emit(video_id)

    def _emit(self, video_id: str) -> None:
        self._status("received", "영상 알림 수신 · 채널과 현재 방송 상태 확인 중")
        self.notification_received.emit(LiveNotification(video_id, time.time(), synthetic=False))

    # -- 점검 -----------------------------------------------------------------

    @Slot()
    def inspect_registration(self) -> None:
        """권한·워커·구독을 확인한다. 주기 타이머가 부르지 않는다.

        사용자가 직접 누른 확인이므로 앞선 실패로 바닥난 재시도 횟수를 되돌린다.
        그러지 않으면 한 번 확정된 실패 뒤의 수동 확인이 그대로 묻힌다.
        """
        self._rechecks = 0
        self._check()

    def _check(self) -> None:
        if self._closed or self._cdp is None:
            if not self._closed and self._started:
                self._bring_up()
            return
        if not self._page_session:
            # 탭이 아직 안 붙었을 뿐인 것과 정말 탭이 없는 것은 시간으로만 갈린다.
            self._check_failed("login_required", "수신 브라우저에 YouTube 탭이 없습니다. 'YouTube 로그인'으로 창을 여세요")
            return
        # 워커를 한 번 깨워 후킹까지 실제로 되는지 확인한다. 반복 타이머가 아니다.
        self._cdp.call("ServiceWorker.startWorker", {"scopeURL": _SERVICE_WORKER_SCOPE},
                       session=self._page_session)
        self._cdp.call(
            "Runtime.evaluate",
            {"expression": _REGISTRATION.replace("ORIGIN", YOUTUBE), "awaitPromise": True, "returnByValue": True},
            session=self._page_session, on_reply=self._registration,
        )

    def _check_failed(self, code: str, detail: str) -> None:
        """실패를 바로 확정하지 않고 짧은 간격으로 몇 번 더 본다 (#88).

        갓 띄운 브라우저의 탭은 URL 만 youtube 일 뿐 문서가 아직 커밋되지 않아,
        곧바로 보낸 확인이 네비게이션과 함께 날아간다. 페이지가 뜨는 중이라
        실패한 것과 정말 문제가 있는 것을 시간으로 가른다. 횟수를 다 쓰면 원래
        문구 그대로 확정한다 — 상주 폴링이 아니라 실패 뒤 제한된 횟수뿐이다.
        """
        if self._rechecks >= _RECHECK_ATTEMPTS:
            self._status(code, detail)
            return
        self._rechecks += 1
        attempt = self._rechecks
        QTimer.singleShot(_RECHECK_DELAY_MS, lambda: self._recheck(attempt))

    def _recheck(self, attempt: int) -> None:
        # 그사이 답을 받았거나 사용자가 다시 눌렀으면 이 예약은 낡은 것이다.
        if self._closed or self._cdp is None or self._rechecks != attempt:
            return
        self._check()

    def _registration(self, message: dict) -> None:
        if self._closed:
            return
        value = CdpProtocol.value(message)
        if not isinstance(value, dict) or value.get("failed") is True:
            self._check_failed("error", "수신 브라우저의 알림 상태를 확인하지 못했습니다. 알림 상태 확인으로 다시 확인하세요")
            return
        # 답을 받았다. 확정적인 결과이므로 예약된 재시도를 모두 무효로 만든다.
        self._rechecks = 0
        permission = value.get("permission")
        if (permission not in ("default", "denied", "granted")
                or any(type(value.get(key)) is not bool for key in ("worker", "active", "subscription"))):
            self._status("error", "수신 브라우저의 알림 상태를 확인하지 못했습니다. 알림 상태 확인으로 다시 확인하세요")
        elif permission != "granted":
            self._status("permission_required", "수신 브라우저의 YouTube 알림 권한이 허용되지 않았습니다. YouTube 알림 설정에서 권한 요청을 확인하세요")
        elif not value["worker"]:
            self._status("worker_missing", "알림 권한은 허용됨 · YouTube 수신 프로그램(서비스 워커)이 등록되지 않았습니다. 수신 브라우저에서 YouTube 로그인과 알림 설정을 확인하세요")
        elif not value["active"]:
            self._status("worker_inactive", "알림 권한은 허용됨 · YouTube 수신 프로그램이 아직 활성화되지 않았습니다. 잠시 뒤 알림 상태 확인을 누르세요")
        elif not value["subscription"]:
            self._status("unsubscribed", "알림 권한은 허용됨 · 푸시 수신 등록이 없습니다. 수신 브라우저에서 YouTube 알림 설정을 확인한 뒤 알림 상태 확인을 누르세요")
        else:
            self._status("ready", _RESTING)

    # -- 감시 -----------------------------------------------------------------

    @Slot()
    def _pulse(self) -> None:
        """수신기 자신이 살아 있는지만 본다. YouTube 에는 아무것도 묻지 않는다."""
        if self._closed:
            return
        if self._owns_process and self._process is not None and self._process.state() == QProcess.ProcessState.NotRunning:
            self._status("chrome_down", "수신 브라우저(Chrome)가 종료되었습니다. 다시 실행을 시도합니다.")
            self._schedule_restart()
            return
        if self._cdp is None or self._socket is None or not self._socket.isValid():
            self._status("chrome_down", "수신 브라우저와의 연결이 끊어졌습니다. 다시 연결을 시도합니다.")
            self._schedule_restart()
            return
        if self._awaiting_pong:
            self._status("error", "수신 브라우저가 응답하지 않습니다. 연결을 다시 만듭니다.")
            self._schedule_restart()
            return
        self._awaiting_pong = True
        self._cdp.call("Browser.getVersion", on_reply=self._pong)

    def _pong(self, _message: dict) -> None:
        self._awaiting_pong = False

    def _schedule_restart(self) -> None:
        if self._closed:
            return
        self._teardown()
        delay = _RESTART_DELAY_MS[min(self._restarts, len(_RESTART_DELAY_MS) - 1)]
        self._restarts += 1
        QTimer.singleShot(delay, self._restart)

    @Slot()
    def _restart(self) -> None:
        if not self._closed and self._started:
            self._bring_up()

    def _teardown(self) -> None:
        self._heartbeat.stop()
        self._visible = False  # 다음 기동은 언제나 헤드리스다. 보이는 모드는 눌어붙지 않는다.
        self._cdp = None
        self._worker_sessions.clear()
        self._page_session = self._page_target = ""
        self._awaiting_pong = False
        websocket, self._socket = self._socket, None
        if websocket is not None:
            websocket.disconnected.disconnect(self._socket_disconnected)
            websocket.abort()
            websocket.deleteLater()
        process, self._process = self._process, None
        if process is not None:
            process.finished.disconnect(self._process_finished)
            process.errorOccurred.disconnect(self._process_error)
            if self._owns_process and process.state() != QProcess.ProcessState.NotRunning:
                process.terminate()
                if not process.waitForFinished(2000):
                    process.kill()
            process.deleteLater()
        self._owns_process = False

    def _process_finished(self, _code: int, _status: object) -> None:
        if self._closed:
            return
        self._status("chrome_down", "수신 브라우저(Chrome)가 종료되었습니다. 다시 실행을 시도합니다.")
        self._schedule_restart()

    def _process_error(self, _error: object) -> None:
        if self._closed:
            return
        self._status("chrome_down", "수신 브라우저(Chrome)를 실행하지 못했습니다. 다시 실행을 시도합니다.")
        self._schedule_restart()

    # -- 창 -------------------------------------------------------------------

    @Slot()
    def open_browser(self) -> None:
        self._open(YOUTUBE)

    @Slot()
    def open_settings(self) -> None:
        self._open(NOTIFICATION_SETTINGS_URL)

    def _open(self, url: str) -> None:
        """Chrome 창에 탭을 열고 앞으로 부른다. 앱 안에 웹뷰를 띄우지 않는다.

        사용자가 직접 로그인하거나 알림 설정을 바꾸는 경로다. 평소에는 헤드리스라
        ``Target.createTarget`` 이 보이지 않는 탭만 만들어 아무 일도 안 한 것처럼
        보인다. Chrome 은 같은 ``--user-data-dir`` 에 두 번째 프로세스를 붙이지
        않으므로, 돌고 있는 브라우저를 닫고 같은 프로필을 보이는 모드로 다시 띄운다
        — 프로필이 같아 로그인과 푸시 등록은 그대로 남는다. 사용자가 그 창을 닫으면
        기존 재시작 경로가 돌면서 다시 헤드리스로 올라온다.
        """
        if self._closed:
            return
        if self._visible and self._cdp is not None:
            # 이미 창이 떠 있다. 닫았다 다시 띄우면 입력하던 로그인이 날아간다.
            self._cdp.call("Target.createTarget", {"url": url}, on_reply=self._opened)
            return
        self._started = True
        if self._cdp is not None and self._socket is not None:
            # 이어받은 브라우저도 닫는다. 창을 띄우려면 프로필이 비어 있어야 한다.
            self._cdp.call("Browser.close")
            self._socket.flush()
        self._teardown()
        self._bring_up(url)

    def _opened(self, message: dict) -> None:
        result = message.get("result")
        target = result.get("targetId") if isinstance(result, dict) else None
        if isinstance(target, str) and self._cdp is not None:
            self._cdp.call("Target.activateTarget", {"targetId": target})

    # -- 종료 -----------------------------------------------------------------

    @Slot()
    def stop(self) -> None:
        self.close()

    @Slot()
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._cdp is not None and self._socket is not None:
            # 우리가 띄우지 않고 이어받은 브라우저도 앱과 함께 닫는다.
            self._cdp.call("Browser.close")
            self._socket.flush()
        self._teardown()
        self._forget_port()
        self._status("stopped", "알림 수신 종료")

    def _forget_port(self) -> None:
        """정상 종료 뒤에는 포트 파일을 남기지 않는다.

        남겨 두면 다음 실행이 그 포트로 먼저 붙으러 간다. 방금 브라우저를 닫았으니
        보통은 연결이 실패해 새로 띄우지만, 그사이 다른 프로세스가 같은 임시 포트를
        잡고 우리 오리진까지 허용해 두었다면 우리 프로필이 아닌 브라우저를 붙잡는다.
        이어받기는 크래시로 남은 고아 브라우저를 되찾는 경로로만 남긴다.
        """
        try:
            (self._profile_directory() / PORT_FILE).unlink(missing_ok=True)
        except (OSError, RuntimeError):
            pass  # 편의용 파일이다. 못 지워도 다음 실행은 연결 실패 후 새로 띄운다.
