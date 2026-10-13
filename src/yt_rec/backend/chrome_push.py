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

계정이 알림에 반응하지 않으면 YouTube 가 푸시를 끊는 것으로 보인다 (#118). 그래서
받은 알림은 표시하자마자 스크립트로 닫고 '열어봄'으로 기록하며, 녹화로 넘어간 영상은
숨은 새 창에서 로그인한 채로 잠깐 본다. 모두 알림이나 녹화가 생겼을 때만 돈다.
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
from .browser_web import BrowserRequest, record_interactions
from .notification_history import ReceivedNotification
from .push_payload import (
    NOTIFICATION_SETTINGS_URL,
    YOUTUBE,
    NoVideo,
    _VIDEO_ID,
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
#: 녹화로 넘어간 영상을 로그인한 채로 볼 때 재생할 본편 길이(초) (#118). 60~90초 사이.
_WATCH_SECONDS = 75
#: 시청 한 건의 상한. 광고와 로딩까지 덮고도 끝나지 않으면 창을 닫는다.
_WATCH_LIMIT_MS = 240_000
#: 시청 창이 본편 재생을 마쳤다고 알릴 때 쓰는 ``Runtime.addBinding`` 이름.
_WATCH_BINDING = "__ytRecWatched"


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
#:
#: 띄운 알림은 표시가 끝나자마자 스크립트로 닫는다 (#118). 그대로 두면 Windows
#: 알림 센터가 72시간 뒤 만료로 닫고, Chrome 은 이것을 사용자가 닫은 것으로
#: 보고한다. 그러면 YouTube 워커의 ``notificationclose`` 처리기가 '무시하고 닫음'을
#: 기록한다. 스크립트로 닫으면 ``notificationclose`` 가 나지 않는다. 닫기가
#: 실패해도 YouTube 가 받는 ``showNotification`` 결과는 원래 것 그대로다.
_HOOK = """
(() => {
  const scope = self;
  const send = value => { try { BINDING(JSON.stringify(value)); } catch (e) {} };
  const dismiss = (registration, title, options) => Promise.resolve().then(() => {
    const tag = options && typeof options.tag === 'string' ? options.tag : '';
    return registration.getNotifications(tag ? {tag} : {}).then(list => {
      const shown = tag ? list : list.filter(item => item.title === title);
      if (!shown.length) send({kind: 'close_failed', reason: 'missing'});
      for (const item of shown) item.close();
    });
  }).catch(error => send({kind: 'close_failed', reason: String((error && error.name) || 'error')}));
  const wrap = () => {
    if (scope.__ytRecWrapped || !scope.registration) return false;
    const proto = Object.getPrototypeOf(scope.registration);
    const original = proto.showNotification;
    proto.showNotification = function (title, options) {
      send({kind: 'notification', title: String(title), options: options || {}});
      return Promise.resolve(original.apply(this, arguments))
        .then(value => dismiss(this, String(title), options).then(() => value));
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

#: 시청 창의 새 문서마다 페이지 스크립트보다 먼저 돈다 (#118). watch 페이지에서만
#: 움직인다. 소리는 재생 전에 끄고, 플레이어가 저장된 음량으로 되돌려도 곧바로 다시
#: 끈다. 건너뛰기 단추가 보이면 누르고, 광고가 아닌 본편이 실제로 재생된 시간만 센다.
#: YouTube 에 아무것도 묻지 않는다 — 창 안의 플레이어 상태만 본다.
_WATCH = """
(() => {
  if (window.top !== window || location.pathname !== '/watch' || window.__ytRecWatching) return;
  window.__ytRecWatching = true;
  const mute = event => {
    if (event.target instanceof HTMLMediaElement && !event.target.muted) event.target.muted = true;
  };
  for (const type of ['loadstart', 'play', 'volumechange']) document.addEventListener(type, mute, true);
  let played = 0, last = 0;
  const timer = setInterval(() => {
    const now = Date.now();
    const player = document.getElementById('movie_player');
    const video = document.querySelector('#movie_player video');
    const skip = document.querySelector('.ytp-skip-ad-button, .ytp-ad-skip-button, .ytp-ad-skip-button-modern');
    if (skip) skip.click();
    const advert = !!player && player.classList.contains('ad-showing');
    if (video) {
      video.muted = true;
      if (video.paused && !video.ended) video.play().catch(() => {});
    }
    if (last && video && !advert && !video.paused && video.readyState >= 3) played += (now - last) / 1000;
    last = now;
    if (played >= SECONDS) {
      clearInterval(timer);
      try { BINDING(JSON.stringify({played: Math.round(played)})); } catch (e) {}
    }
  }, 1000);
})();
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
    browser_changed = Signal(bool)  # page usable, not authenticated or push-ready
    # 알림 닫기·열어봄 기록·짧은 시청의 결과 (#118). (성공 여부, 고정 문구). 푸시가 다시
    # 오는지 운영 중에 따져 볼 근거라 앱이 앱 로그에 남긴다. 수신 상태와는 무관하다.
    engagement = Signal(bool, str)

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
        # 브라우저를 접을 때마다 올린다. 그 전에 걸어 둔 연결 재시도·포트 응답·재시작
        # 예약은 이 값이 달라졌으면 낡은 것이므로 아무것도 하지 않는다 (#117).
        self._generation = 0
        self._restart_pending = False
        self._rechecks = 0
        self._awaiting_pong = False
        self._worker_sessions: dict[str, str] = {}  # targetId -> sessionId
        self._page_session = ""
        self._page_target = ""
        self._page_sessions: dict[str, str] = {}
        self._page_urls: dict[str, str] = {}
        # 녹화로 넘어간 영상을 잠깐 보는 창 (#118). 한 번에 하나뿐이다. ``_watching`` 은
        # 그 한 건의 표지라 늦게 온 응답이 다음 시청을 건드리지 못한다. 시청 창으로
        # 만든 targetId 는 닫은 뒤에도 기억한다 — 닫히는 중에 늦게 온 사건으로 그 창을
        # 수신용 YouTube 탭으로 붙잡으면, 곧 떨어져 나가면서 계정 연결을 흔든다.
        self._watching: object | None = None
        self._watch_target = ""
        self._watch_session = ""
        self._watch_targets: set[str] = set()
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
        self._bring_up(adopt=True)

    def _bring_up(self, visible_url: str = "", *, adopt: bool = False) -> None:
        """수신기를 세운다. ``visible_url`` 이 있으면 그 주소를 보이는 창으로 연다.

        ``adopt`` 일 때만 포트 파일이 가리키는 브라우저에 먼저 붙어 본다. 앱이 새로
        뜰 때 남아 있던 고아 브라우저를 되찾는 경로다. 우리가 띄운 브라우저가 끝난
        뒤의 재시작에서는 쓰지 않는다 — 그 포트는 아직 종료 중인 이전 Chrome 이라
        잠깐 붙었다가 또 끊긴다 (#117).
        """
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
        port = _read_port(profile / PORT_FILE) if adopt and not visible_url else 0
        if port:
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
        # 실행에 실패하면 ``start`` 안에서 곧바로 재시작이 예약되며 세대가 바뀐다.
        # 그 전에 세대를 잡아 둬야 이 연결 재시도가 낡은 것으로 걸러진다.
        generation = self._generation
        process.start()
        QTimer.singleShot(_CONNECT_RETRY_MS, lambda: self._retry_resolve(generation))

    def _retry_resolve(self, generation: int) -> None:
        # 그사이 브라우저를 접었으면 이 재시도는 이전 Chrome 의 것이다. 이어 가면
        # 이전 포트에 붙거나, 횟수를 다 썼다며 새로 띄운 Chrome 까지 접는다 (#117).
        if self._closed or self._socket is not None or generation != self._generation:
            return
        self._attempts += 1
        if self._attempts > _CONNECT_ATTEMPTS:
            self._schedule_restart("chrome_down", "수신 브라우저(Chrome)에 연결하지 못했습니다. 잠시 뒤 다시 시도합니다.")
            return
        self._resolve(on_failure=lambda: QTimer.singleShot(
            _CONNECT_RETRY_MS, lambda: self._retry_resolve(generation)))

    def _resolve(self, *, on_failure: Callable[[], None]) -> None:
        """디버깅 포트에서 브라우저 세션 WebSocket 주소를 받아 온다."""
        generation = self._generation
        request = QNetworkRequest(QUrl(f"http://127.0.0.1:{self._port}/json/version"))
        reply = self._network.get(request)

        def finished() -> None:
            body = bytes(reply.readAll())
            failed = reply.error() != QNetworkReply.NetworkError.NoError
            reply.deleteLater()
            if self._closed or self._socket is not None or generation != self._generation:
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
        self._schedule_restart("chrome_down", "수신 브라우저와의 연결이 끊어졌습니다. 다시 연결을 시도합니다.")

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
        cdp.events["Page.loadEventFired"] = self._page_loaded
        cdp.events["Page.frameNavigated"] = self._page_loading
        self._cdp = cdp
        self._worker_sessions.clear()
        self._page_session = self._page_target = ""
        self._page_sessions.clear()
        self._page_urls.clear()
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
        if target in self._watch_targets:
            return  # 잠깐 보는 시청 창이다. 수신용 YouTube 탭으로 붙잡지 않는다 (#118).
        if info.get("type") == "page":
            previous = self._page_urls.get(target)
            self._page_urls[target] = url
            if target in self._page_sessions and previous != url:
                self.browser_changed.emit(False)
            if _is_youtube_url(url) and target not in self._page_sessions and self._cdp is not None:
                # Mark pending before the asynchronous attach reply.
                self._page_sessions[target] = ""
                self._page_target = target
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
            self._page_target = target
            self._page_sessions[target] = session
            self._page_urls[target] = url
            self._cdp.call("Page.enable", session=session)
            self._cdp.call("Runtime.enable", session=session)
            self._cdp.call("Runtime.addBinding", {"name": "__ytRecAccountChanged"}, session=session)
            hook = "if (!window.__ytRecAccountHook) { window.__ytRecAccountHook=true; addEventListener('yt-navigate-finish', () => __ytRecAccountChanged('')); }"
            self._cdp.call("Page.addScriptToEvaluateOnNewDocument", {"source": hook}, session=session)
            self._cdp.call("Runtime.evaluate", {"expression": hook}, session=session)
            self._cdp.call("Runtime.evaluate", {
                "expression": "JSON.stringify(document.readyState === 'complete')", "returnByValue": True,
            }, session=session, on_reply=lambda reply: self._page_loaded({}, session) if CdpProtocol.value(reply) is True else None)
            self._cdp.call("ServiceWorker.enable", session=session)
            self.inspect_registration()

    def _page_loading(self, params: dict, session: str) -> None:
        frame = params.get("frame") or {}
        if session in self._page_sessions.values() and not frame.get("parentId"):
            self.browser_changed.emit(False)

    def _page_loaded(self, _params: dict, session: str) -> None:
        if self._closed:
            return
        target = next((target for target, value in self._page_sessions.items() if value == session), "")
        if target and _is_youtube_url(self._page_urls.get(target, "")):
            self._page_target, self._page_session = target, session
            self.browser_changed.emit(True)

    @Slot(object)
    def read_browser(self, request: BrowserRequest) -> None:
        if request.done.is_set():
            return
        cdp, session = self._cdp, self._page_session
        if self._closed or cdp is None or not session:
            request.done.set()
            return

        def finished(reply: dict) -> None:
            if not request.done.is_set() and self._cdp is cdp:
                request.value = CdpProtocol.value(reply)
                request.done.set()

        call = cdp.call("Runtime.evaluate", {
            "expression": request.expression, "awaitPromise": True,
            "returnByValue": True, "timeout": 42000,
        }, session=session, on_reply=finished)

        def expired() -> None:
            cdp._replies.pop(call, None)
            request.done.set()

        QTimer.singleShot(44000, expired)

    def _detached(self, params: dict, _session: str) -> None:
        session = params.get("sessionId")
        if not isinstance(session, str):
            return
        for target, current in tuple(self._worker_sessions.items()):
            if current == session:
                del self._worker_sessions[target]
        if session == self._page_session:
            self._page_session = self._page_target = ""
            self.browser_changed.emit(False)
        for target, value in tuple(self._page_sessions.items()):
            if value == session:
                self._page_sessions.pop(target, None)
                self._page_urls.pop(target, None)
        if not self._page_session and self._page_sessions:
            target, session = next(iter(self._page_sessions.items()))
            self._page_loaded({}, session)

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
        if self._watch_session and session == self._watch_session:
            if not self._closed and params.get("name") == _WATCH_BINDING:
                self._watch_reported(params.get("payload"))
            return
        if (not self._closed and params.get("name") == "__ytRecAccountChanged"
                and session in self._page_sessions.values()):
            self._page_loaded({}, session)
            return
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
        elif value.get("kind") == "close_failed":
            # 알림 수신은 이미 끝났다. 수신 상태를 바꾸지 않고 사유만 남긴다 (#118).
            reason = value.get("reason")
            if not (isinstance(reason, str) and reason.isascii() and reason.isalpha() and len(reason) <= 64):
                reason = "unknown"
            self._engaged(False, f"표시한 알림을 닫지 못했습니다: {reason}")
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
        self._record_opened(options.get("data"))
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

    # -- 열어봄·시청 (#118) ---------------------------------------------------
    #
    # 2026-10-06 부터 계정 알림함에는 알림이 쌓이는데 이 브라우저로는 푸시가 오지
    # 않았다. 알림을 한 번도 열지 않았고, 녹화는 비로그인 yt-dlp 라 채널 시청으로도
    # 잡히지 않는다. YouTube 가 반응 없는 계정으로 본 것으로 추정한다. 그래서 받은
    # 알림은 연 것으로 기록하고, 녹화로 넘어간 영상은 로그인한 채로 잠깐 본다. 둘 다
    # 알림이나 녹화가 생겼을 때만 돈다 — 주기 조회가 아니다. 무엇이 실패해도 로그만
    # 남기고, 수신 상태와 녹화는 이 결과를 기다리지 않는다.

    def _engaged(self, ok: bool, message: str) -> None:
        """결과 한 건을 모듈 로거와 :attr:`engagement` 로 함께 남긴다. 문구는 고정문이다."""
        (_LOG.info if ok else _LOG.warning)("%s", message)
        self.engagement.emit(ok, message)

    def _record_opened(self, data: object) -> None:
        """받은 알림을 '열어봄'으로 기록한다.

        YouTube 워커가 알림을 눌렀을 때 보내는 ``record_interactions`` 요청과 같은
        본문을 로그인된 수신 탭에서 보낸다. 인증 머리글은 페이지 안에서 만든다.
        """
        endpoint = data.get("clickEndpoint") if isinstance(data, dict) else None
        endpoint = endpoint.get("recordNotificationInteractionsEndpoint") if isinstance(endpoint, dict) else None
        request = endpoint.get("serializedInteractionsRequest") if isinstance(endpoint, dict) else None
        if request is None:
            _LOG.info("열어봄을 기록할 정보가 없는 알림입니다")
            return
        try:
            expression = record_interactions(request)
        except ValueError:
            self._engaged(False, "알림의 열어봄 기록 요청 형식을 알 수 없어 보내지 않았습니다")
            return
        cdp, session = self._cdp, self._page_session
        if cdp is None or not session:
            self._engaged(False, "수신 브라우저에 YouTube 탭이 없어 알림 열어봄을 기록하지 못했습니다")
            return

        def finished(reply: dict) -> None:
            value = CdpProtocol.value(reply)
            if isinstance(value, dict) and value.get("ok") is True:
                self._engaged(True, "알림 열어봄을 기록했습니다")
                return
            error = value.get("error") if isinstance(value, dict) else None
            kind = error if error in ("auth", "format", "network") else "unknown"
            self._engaged(False, f"알림 열어봄을 기록하지 못했습니다: {kind}")

        cdp.call("Runtime.evaluate", {"expression": expression, "awaitPromise": True, "returnByValue": True},
                 session=session, on_reply=finished)

    @Slot(str)
    def watch_briefly(self, video_id: str) -> None:
        """녹화로 넘어간 영상을 수신 브라우저에서 로그인한 채로 잠깐 본다.

        같은 창의 백그라운드 탭은 숨은 상태라 YouTube 가 재생하지 않으므로 새 창
        (``newWindow``)으로 연다. 헤드리스 브라우저의 새 창은 화면에 뜨지 않는다.
        창을 띄운 모드이거나 헤드리스인지 확인되지 않으면 하지 않는다. 음소거로
        본편 :data:`_WATCH_SECONDS` 초를 재생한 뒤 창을 닫는다. 한 번에 하나만 보고,
        보는 중에 넘어온 영상은 건너뛴다.
        """
        if self._closed or not isinstance(video_id, str) or not _VIDEO_ID.fullmatch(video_id):
            return
        if self._cdp is None or self._visible:
            _LOG.info("수신 브라우저가 숨은 상태로 연결돼 있지 않아 녹화 영상 시청을 건너뜁니다")
            return
        if self._watching is not None:
            _LOG.info("다른 영상을 보는 중이라 이번 녹화 영상 시청은 건너뜁니다")
            return
        token = self._watching = object()
        self._watch_target = self._watch_session = ""
        QTimer.singleShot(_WATCH_LIMIT_MS, lambda: self._watch_finish(token, "제한 시간 안에 끝나지 않았습니다"))
        # 이어받은 브라우저는 창을 띄운 모드일 수 있다. 헤드리스인지 먼저 확인한다.
        self._cdp.call("Browser.getVersion", on_reply=lambda reply: self._watch_open(token, video_id, reply))

    def _watch_open(self, token: object, video_id: str, reply: dict) -> None:
        if self._watching is not token or self._cdp is None:
            return
        result = reply.get("result")
        agent = result.get("userAgent") if isinstance(result, dict) else None
        if not isinstance(agent, str) or "HeadlessChrome" not in agent:
            self._watch_finish(token, "수신 브라우저가 헤드리스인지 확인하지 못했습니다")
            return
        # 처음부터 watch 주소로 만들면 ``Target.targetCreated`` 가 응답보다 먼저 와서
        # 수신용 YouTube 탭으로 붙잡힌다. 빈 창으로 만들어 targetId 를 받은 뒤 옮긴다.
        self._cdp.call("Target.createTarget", {"url": "about:blank", "newWindow": True},
                       on_reply=lambda reply: self._watch_created(token, video_id, reply))

    def _watch_created(self, token: object, video_id: str, reply: dict) -> None:
        result = reply.get("result")
        target = result.get("targetId") if isinstance(result, dict) else None
        if not isinstance(target, str) or not target:
            self._watch_finish(token, "시청 창을 만들지 못했습니다")
            return
        self._watch_targets.add(target)
        self._page_urls.pop(target, None)
        if self._watching is not token or self._cdp is None:
            # 그사이 끝났다. 늦게 생긴 창도 남기지 않는다.
            if self._cdp is not None:
                self._cdp.call("Target.closeTarget", {"targetId": target})
            return
        self._watch_target = target
        self._cdp.call("Target.attachToTarget", {"targetId": target, "flatten": True},
                       on_reply=lambda reply: self._watch_attached(token, video_id, reply))

    def _watch_attached(self, token: object, video_id: str, reply: dict) -> None:
        cdp = self._cdp
        if self._watching is not token or cdp is None:
            return
        result = reply.get("result")
        session = result.get("sessionId") if isinstance(result, dict) else None
        if not isinstance(session, str) or not session:
            self._watch_finish(token, "시청 창에 붙지 못했습니다")
            return
        self._watch_session = session
        cdp.call("Runtime.enable", session=session)
        cdp.call("Runtime.addBinding", {"name": _WATCH_BINDING}, session=session)
        cdp.call("Page.enable", session=session)
        source = _WATCH.replace("SECONDS", str(_WATCH_SECONDS)).replace("BINDING(", _WATCH_BINDING + "(")
        cdp.call("Page.addScriptToEvaluateOnNewDocument", {"source": source}, session=session)
        cdp.call("Page.navigate", {"url": f"{YOUTUBE}/watch?v={video_id}"}, session=session,
                 on_reply=lambda reply: self._watch_navigated(token, reply))

    def _watch_navigated(self, token: object, reply: dict) -> None:
        result = reply.get("result")
        if not isinstance(result, dict) or result.get("errorText"):
            self._watch_finish(token, "영상 페이지를 열지 못했습니다")

    def _watch_reported(self, payload: object) -> None:
        try:
            value = json.loads(payload) if isinstance(payload, str) and len(payload) <= 256 else None
        except (ValueError, RecursionError):
            value = None
        played = value.get("played") if isinstance(value, dict) else None
        if type(played) is not int:
            self._watch_finish(self._watching, "시청 창이 알 수 없는 값을 보냈습니다")
            return
        self._watch_finish(self._watching, played=played)

    def _watch_finish(self, token: object, problem: str = "", *, played: int = 0) -> None:
        """시청 한 건을 끝내고 창을 닫는다. 같은 건은 한 번만 끝난다."""
        if token is None or self._watching is not token:
            return
        target = self._watch_target
        self._watching, self._watch_target, self._watch_session = None, "", ""
        if target and self._cdp is not None:
            self._cdp.call("Target.closeTarget", {"targetId": target})
        if problem:
            self._engaged(False, f"녹화가 시작된 영상을 잠깐 보지 못했습니다: {problem}")
        else:
            self._engaged(True, f"녹화가 시작된 영상을 로그인한 채로 {played}초 보고 창을 닫았습니다")

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
            # 붙는 중이거나 재시작을 기다리는 중이면 그쪽이 붙은 뒤 확인까지 한다. 여기서
            # 또 세우면 같은 프로필에 Chrome 이 둘 뜬다 (#117). 아무것도 진행 중이 아닐
            # 때만 — 예컨대 Chrome 을 못 찾았던 뒤 — 다시 세운다.
            if not self._closed and self._started and not self._restart_pending and not self._port:
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
            self._schedule_restart("chrome_down", "수신 브라우저(Chrome)가 종료되었습니다. 다시 실행을 시도합니다.")
            return
        if self._cdp is None or self._socket is None or not self._socket.isValid():
            self._schedule_restart("chrome_down", "수신 브라우저와의 연결이 끊어졌습니다. 다시 연결을 시도합니다.")
            return
        if self._awaiting_pong:
            self._schedule_restart("error", "수신 브라우저가 응답하지 않습니다. 연결을 다시 만듭니다.")
            return
        self._awaiting_pong = True
        self._cdp.call("Browser.getVersion", on_reply=self._pong)

    def _pong(self, _message: dict) -> None:
        self._awaiting_pong = False

    def _schedule_restart(self, code: str, detail: str) -> None:
        """브라우저를 접고 재시작을 한 번만 예약한다.

        창을 닫으면 소켓 끊김과 프로세스 종료가 따로 들어오고, 하트비트나 연결
        재시도도 같은 사건을 뒤늦게 다시 본다. 이미 예약되어 있으면 알리지도 다시
        예약하지도 않는다 — 겹친 재시작이 같은 프로필에 Chrome 을 둘 띄워 끊김이
        되풀이됐다 (#117).

        우리가 띄운 브라우저였다면 포트 파일을 버리고 다음에는 새로 띄운다. 이어받은
        브라우저는 우리가 끝낼 수 없으니 예전처럼 그 포트에 다시 붙어 본다.
        """
        if self._closed or self._restart_pending:
            return
        self._status(code, detail)
        owned = self._owns_process
        # 접는 동안 이전 프로세스가 끝날 때까지 기다린다. 프로필 잠금이 풀린 뒤에만
        # 다음 Chrome 이 뜬다.
        self._teardown()
        if owned:
            self._forget_port()
        delay = _RESTART_DELAY_MS[min(self._restarts, len(_RESTART_DELAY_MS) - 1)]
        self._restarts += 1
        self._restart_pending = True
        generation = self._generation
        QTimer.singleShot(delay, lambda: self._restart(generation, adopt=not owned))

    def _restart(self, generation: int, *, adopt: bool) -> None:
        # 그사이 창을 열어 새로 세웠으면 이 예약은 낡은 것이다. 그대로 돌면 보이는 창이
        # 떠 있는 동안 헤드리스를 또 세운다 (#117).
        if self._closed or generation != self._generation:
            return
        self._restart_pending = False
        if self._started:
            self._bring_up(adopt=adopt)

    def _teardown(self) -> None:
        self._generation += 1
        self._restart_pending = False
        self._port = 0
        self.browser_changed.emit(False)
        self._heartbeat.stop()
        self._visible = False  # 다음 기동은 언제나 헤드리스다. 보이는 모드는 눌어붙지 않는다.
        self._cdp = None
        self._worker_sessions.clear()
        self._page_session = self._page_target = ""
        self._page_sessions.clear()
        self._page_urls.clear()
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
                    # kill 도 비동기다. 끝나기 전에 다음 Chrome 을 띄우면 그 Chrome 은
                    # 프로필을 쥔 이전 프로세스에 명령줄만 넘기고 바로 끝난다.
                    process.waitForFinished(2000)
            process.deleteLater()
        self._owns_process = False

    def _process_finished(self, _code: int, _status: object) -> None:
        self._schedule_restart("chrome_down", "수신 브라우저(Chrome)가 종료되었습니다. 다시 실행을 시도합니다.")

    def _process_error(self, _error: object) -> None:
        self._schedule_restart("chrome_down", "수신 브라우저(Chrome)를 실행하지 못했습니다. 다시 실행을 시도합니다.")

    # -- 창 -------------------------------------------------------------------

    @Slot()
    def open_browser(self) -> None:
        self._open(YOUTUBE)

    @Slot()
    def open_settings(self) -> None:
        self._open(NOTIFICATION_SETTINGS_URL)

    @Slot()
    def open_logout(self) -> None:
        self.browser_changed.emit(False)
        self._open(YOUTUBE + "/logout")

    def _open(self, url: str) -> None:
        """Chrome 창에 탭을 열고 앞으로 부른다. 앱 안에 웹뷰를 띄우지 않는다.

        사용자가 직접 로그인하거나 알림 설정을 바꾸는 경로다. 평소에는 헤드리스라
        ``Target.createTarget`` 이 보이지 않는 탭만 만들어 아무 일도 안 한 것처럼
        보인다. Chrome 은 같은 ``--user-data-dir`` 에 두 번째 프로세스를 붙이지
        않으므로, 돌고 있는 브라우저를 닫고 같은 프로필을 보이는 모드로 다시 띄운다
        — 프로필이 같아 로그인과 푸시 등록은 그대로 남는다. 사용자가 그 창을 닫으면
        기존 재시작 경로가 돌면서 다시 헤드리스로 올라온다. 그 재시작은 닫힌 창의
        포트에 다시 붙지 않고 새로 띄운다 (#117).
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
