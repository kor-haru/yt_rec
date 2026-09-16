"""YouTube web notifications -> validated video IDs, without discovery polling.

Only Qt's native presenter can open a notification lookup. The isolated-world
bridge must match that request against one persisted same-origin notification.
The backend, not this receiver, verifies the channel and current live state.
Registration readiness does not prove that a real push has been delivered.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from PySide6.QtCore import (
    QFile,
    QIODevice,
    QObject,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import (
    QWebEngineNotification,
    QWebEnginePage,
    QWebEnginePermission,
    QWebEngineProfile,
    QWebEngineScript,
)
from PySide6.QtWidgets import QMessageBox

from .notifications import LiveNotification
from .notification_history import ReceivedNotification
from ..webengine_boot import profile_root

YOUTUBE = "https://www.youtube.com"
NOTIFICATION_SETTINGS_URL = YOUTUBE + "/account_notifications"
_WORLD = QWebEngineScript.ScriptWorldId.ApplicationWorld
_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
_MAX_PENDING = 32
_REQUEST_TIMEOUT_MS = 10_000
_MAX_RESPONSE = 65_536


def default_profile_directory() -> Path:
    """Product-owned profile, never a caller-supplied Chrome/cookie directory."""
    return profile_root()


def _is_youtube_url(value: str) -> bool:
    try:
        if not isinstance(value, str) or any(ord(char) <= 32 for char in value):
            return False
        parsed = urlsplit(value)
        return (parsed.scheme == "https" and parsed.hostname == "www.youtube.com"
                and parsed.port in (None, 443) and parsed.username is None and parsed.password is None)
    except (TypeError, ValueError):
        return False


def _video_id_from_data(data: object) -> str | None:
    """Fail closed on ambiguity or traversal limits; never parse titles as IDs."""
    videos: set[str] = set()
    remaining = 256

    def visit(value: object, depth: int = 0) -> None:
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 6:
            raise ValueError
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ("videoId", "video_id"):
                    if not isinstance(item, str) or not _VIDEO_ID.fullmatch(item):
                        raise ValueError
                    videos.add(item)
                visit(item, depth + 1)
        elif isinstance(value, list):
            for item in value:
                visit(item, depth + 1)
        elif isinstance(value, str) and _is_youtube_url(value):
            # YouTube's payload schema is not public. A canonical watch URL is
            # usable under any key; unrelated icon/avatar URLs are not IDs.
            parsed = urlsplit(value)
            if parsed.path == "/watch":
                ids = parse_qs(parsed.query, keep_blank_values=True).get("v", [])
                if len(ids) != 1 or not _VIDEO_ID.fullmatch(ids[0]):
                    raise ValueError
                videos.add(ids[0])

    try:
        visit(data)
    except (ValueError, RecursionError):
        return None
    return next(iter(videos)) if len(videos) == 1 else None


def _json_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError("non-JSON constant")


_LOOKUP = """
(async () => {
    const request = REQUEST;
    const bridge = await window.ytRecReady;
    const send = value => bridge.deliver(JSON.stringify({
        ...value, nonce: request.nonce, generation: request.generation
    }));
    try {
        if (location.origin !== request.origin) return send({status: 'origin_changed'});
        const registrations = await navigator.serviceWorker.getRegistrations();
        if (registrations.length > 32) return send({status: 'lookup_failed'});
        const notices = [];
        for (const registration of registrations) {
            if (new URL(registration.scope).origin !== request.origin)
                return send({status: 'origin_changed'});
            notices.push(...await registration.getNotifications({tag: request.tag}));
            // Empty tag returns all stored notices. Bound the total across scopes.
            if (notices.length > 128) return send({status: 'too_many_notices'});
        }
        const matches = notices.filter(notice => notice.tag === request.tag &&
            notice.title === request.title && notice.body === request.body);
        if (matches.length !== 1) return send({status: 'missing_or_ambiguous'});
        const notice = matches[0];
        send({status: 'matched', data: notice.data});
    } catch (_) { send({status: 'lookup_failed'}); }
})();
"""

_REGISTRATION = """
(async () => {
    const request = REQUEST;
    const bridge = await window.ytRecReady;
    const send = value => bridge.deliver(JSON.stringify({
        ...value, nonce: request.nonce, generation: request.generation
    }));
    try {
        if (location.origin !== request.origin) return send({status: 'origin_changed'});
        const registrations = await navigator.serviceWorker.getRegistrations();
        if (registrations.length > 32) return send({status: 'lookup_failed'});
        let active = false, subscription = false;
        for (const reg of registrations) {
            if (new URL(reg.scope).origin !== request.origin)
                return send({status: 'origin_changed'});
            if (reg.active?.state === 'activated') {
                active = true;
                if (await reg.pushManager.getSubscription()) subscription = true;
            }
        }
        send({status: 'registration', worker: registrations.length > 0, active, subscription,
              permission: Notification.permission});
    } catch (_) { send({status: 'lookup_failed'}); }
})();
"""


class _Bridge(QObject):
    received = Signal(str)

    @Slot(str)
    def deliver(self, value: str) -> None:
        if len(value) <= _MAX_RESPONSE:
            self.received.emit(value)


class _Page(QWebEnginePage):
    def javaScriptConsoleMessage(self, level, message, line, source):
        # Website console output can contain tokens or notification bodies.
        pass


@dataclass
class _Pending:
    kind: str
    generation: int
    timer: QTimer
    received_at: float
    notification: QWebEngineNotification | None = None
    tag: str = ""
    closed_slot: object = None


class YouTubePushReceiver(QObject):
    """GUI-thread owner. Keep alive while the browser dialog is hidden.

    stop/close is final and idempotent. The application must not reuse page
    afterwards; schedule its view for deletion before stopping the receiver.
    """

    notification_received = Signal(object)  # LiveNotification, never raw web data.
    notification_arrived = Signal(object)  # Native display text, before video identification.
    status_changed = Signal(str, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._closed = False
        self._started = False
        self._generation = 0
        self._pending: dict[str, _Pending] = {}
        self._last_status: tuple[str, str] | None = None
        directory = default_profile_directory()
        self.profile = QWebEngineProfile("yt-rec-youtube-push", self)
        self.profile.setPersistentStoragePath(str(directory / "storage"))
        self.profile.setCachePath(str(directory / "cache"))
        self.profile.setDownloadPath(str(directory / "downloads"))
        self.profile.setPushServiceEnabled(True)
        self.profile.downloadRequested.connect(lambda item: item.cancel())
        self.page = _Page(self.profile, self)
        self._channel = QWebChannel(self.page)
        self._bridge = _Bridge(self._channel)
        self._channel.registerObject("receiver", self._bridge)
        self.page.setWebChannel(self._channel, _WORLD)
        self._bridge.received.connect(self._receive)
        library = QFile(":/qtwebchannel/qwebchannel.js")
        if not library.open(QIODevice.OpenModeFlag.ReadOnly):
            self.close()
            raise RuntimeError("알림 수신용 Qt WebChannel을 불러오지 못했습니다")
        script = QWebEngineScript()
        script.setName("yt-rec-isolated-notification-bridge")
        script.setWorldId(_WORLD)
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setRunsOnSubFrames(False)
        script.setSourceCode(bytes(library.readAll()).decode() + "\nwindow.ytRecReady = new Promise(resolve => new QWebChannel(qt.webChannelTransport, c => resolve(c.objects.receiver)));")
        self.page.scripts().insert(script)
        self.profile.setNotificationPresenter(self._present)
        self.page.permissionRequested.connect(self._permission)
        self.page.loadStarted.connect(self._navigation_started)
        self.page.loadFinished.connect(self._loaded)

    def _status(self, code: str, detail: str) -> None:
        # Only fixed, caller-owned messages are published. No site text/errors.
        if (code, detail) != self._last_status:
            self._last_status = (code, detail)
            self.status_changed.emit(code, detail)

    @Slot()
    def start(self) -> None:
        if not self._started and not self._closed:
            self.open_browser()

    @Slot()
    def open_browser(self) -> None:
        self._navigate(YOUTUBE)

    @Slot()
    def open_settings(self) -> None:
        self._navigate(NOTIFICATION_SETTINGS_URL)

    def _navigate(self, url: str) -> None:
        if not self._closed:
            self._started = True
            self._navigation_started()
            self.page.setUrl(QUrl(url))

    def _navigation_started(self) -> None:
        if self._closed:
            return
        self._generation += 1
        for nonce in tuple(self._pending):
            self._finish(nonce)
        self._status("connecting", "YouTube 알림 브라우저 연결 중")

    def _loaded(self, ok: bool) -> None:
        if self._closed or not self._started:
            return
        if not ok:
            self._status("error", "알림 페이지를 열지 못했습니다. 수신 브라우저에서 다시 확인하세요")
        else:
            self.inspect_registration()

    def _permission(self, permission: QWebEnginePermission) -> None:
        if (self._closed or not _is_youtube_url(permission.origin().toString())
                or permission.permissionType() != QWebEnginePermission.PermissionType.Notifications):
            permission.deny()
            return
        generation = self._generation
        allowed = QMessageBox.question(
            None, "YouTube 알림 권한", "yt-rec의 전용 브라우저에서 YouTube 알림을 허용할까요?",
        ) == QMessageBox.StandardButton.Yes
        # A modal prompt can run a nested loop in which shutdown/navigation occurs.
        if (self._closed or generation != self._generation
                or not _is_youtube_url(self.page.url().toString())):
            permission.deny()
            return
        permission.grant() if allowed else permission.deny()
        self.inspect_registration()

    def _request(self, kind: str, notification=None, tag="") -> tuple[str, _Pending] | None:
        if len(self._pending) >= _MAX_PENDING:
            self._status("error", "처리 중인 알림이 너무 많아 새 확인을 생략했습니다")
            return None
        nonce = secrets.token_hex(16)
        timer = QTimer(self)
        timer.setSingleShot(True)
        pending = _Pending(kind, self._generation, timer, time.time(), notification, tag)
        self._pending[nonce] = pending
        timer.timeout.connect(lambda: self._expired(nonce))
        timer.start(_REQUEST_TIMEOUT_MS)  # Cleanup deadline only; never issues a query.
        return nonce, pending

    def _finish(self, nonce: str, *, close_notice: bool = True) -> _Pending | None:
        pending = self._pending.pop(nonce, None)
        if pending is None:
            return None
        pending.timer.stop()
        pending.timer.deleteLater()
        notice = pending.notification
        if notice is not None:
            notice.closed.disconnect(pending.closed_slot)
            if close_notice:
                notice.close()
        return pending

    def _expired(self, nonce: str) -> None:
        if self._finish(nonce) is not None and not self._closed:
            self._status("error", "알림 확인 시간이 지나 응답을 폐기했습니다")

    def _present(self, notification: QWebEngineNotification) -> None:
        if (self._closed or not self._started
                or not _is_youtube_url(notification.origin().toString())
                or not _is_youtube_url(self.page.url().toString())):
            notification.close()
            return
        tag, title, body = notification.tag(), notification.title(), notification.message()
        if len(tag) > 1024 or len(title) > 4096 or len(body) > 16384:
            notification.close()
            self._status("error", "식별할 수 없는 알림을 무시했습니다")
            return
        self.notification_arrived.emit(ReceivedNotification(
            datetime.now(timezone.utc), title, body, synthetic=False,
        ))
        # origin+tag identifies a replacement chain, not a unique notification.
        # Retire the old request without closing its newly replaced chain.
        for nonce, old in tuple(self._pending.items()):
            if tag and old.notification is not None and old.tag == tag:
                self._finish(nonce, close_notice=False)
        request = self._request("notification", notification, tag)
        if request is None:
            notification.close()
            return
        nonce, pending = request
        pending.closed_slot = lambda: self._finish(nonce, close_notice=False)
        notification.closed.connect(pending.closed_slot)
        notification.show()
        payload = {"nonce": nonce, "generation": pending.generation, "origin": YOUTUBE,
                   "tag": tag, "title": title, "body": body}
        self.page.runJavaScript(_LOOKUP.replace("REQUEST", json.dumps(payload)), _WORLD)

    @Slot()
    def inspect_registration(self) -> None:
        if self._closed or not self._started:
            return
        if not _is_youtube_url(self.page.url().toString()):
            self._status("login_required", "수신 브라우저에서 YouTube에 로그인하세요")
            return
        if any(item.kind == "registration" for item in self._pending.values()):
            return
        request = self._request("registration")
        if request is not None:
            nonce, pending = request
            payload = {"nonce": nonce, "generation": pending.generation, "origin": YOUTUBE}
            self._status("checking", "브라우저의 알림 권한과 수신 등록을 한 번 확인하는 중입니다")
            self.page.runJavaScript(_REGISTRATION.replace("REQUEST", json.dumps(payload)), _WORLD)

    def _receive(self, text: str) -> None:
        if self._closed or len(text) > _MAX_RESPONSE:
            return
        try:
            value = json.loads(text, object_pairs_hook=_json_object, parse_constant=_invalid_constant)
        except (ValueError, TypeError, RecursionError):
            return
        if not isinstance(value, dict) or not isinstance(value.get("nonce"), str):
            return
        pending = self._finish(value["nonce"])
        if (pending is None or pending.generation != self._generation
                or type(value.get("generation")) is not int or value["generation"] != pending.generation
                or not _is_youtube_url(self.page.url().toString())):
            return
        status = value.get("status")
        if status == "matched" and pending.kind == "notification":
            video_id = _video_id_from_data(value.get("data"))
            if video_id is None:
                self._status("error", "알림에서 영상 하나를 식별하지 못해 녹화하지 않습니다")
                return
            self._status("received", "영상 알림 수신 · 채널과 현재 방송 상태 확인 중")
            self.notification_received.emit(LiveNotification(video_id, pending.received_at, synthetic=False))
        elif status == "registration" and pending.kind == "registration":
            permission = value.get("permission")
            if (permission not in ("default", "denied", "granted")
                    or any(type(value.get(key)) is not bool for key in ("worker", "active", "subscription"))):
                self._status("error", "브라우저 알림 상태를 확인하지 못했습니다. 알림 상태 확인으로 다시 확인하세요")
            elif permission != "granted":
                self._status("permission_required", "브라우저의 YouTube 알림 권한이 허용되지 않았습니다. YouTube 알림 설정에서 권한 요청을 확인하세요")
            elif not value["worker"]:
                self._status("worker_missing", "브라우저 알림 권한은 허용됨 · YouTube 수신 프로그램(서비스 워커)이 등록되지 않았습니다. YouTube 로그인과 알림 설정을 확인하세요")
            elif not value["active"]:
                self._status("worker_inactive", "브라우저 알림 권한은 허용됨 · YouTube 수신 프로그램이 아직 활성화되지 않았습니다. 페이지가 열린 뒤 알림 상태 확인을 누르세요")
            elif not value["subscription"]:
                self._status("unsubscribed", "브라우저 알림 권한은 허용됨 · 푸시 수신 등록이 없습니다. YouTube 데스크톱 알림이 이미 켜져 있어도 등록이 없을 수 있습니다. YouTube 알림 설정을 확인한 뒤 알림 상태 확인을 누르세요")
            else:
                self._status("ready", "알림 수신 준비됨 · 실제 도착 대기 중")
        else:
            self._status("error", "브라우저 알림 상태 조회에 실패했습니다. 알림 상태 확인으로 다시 확인하세요"
                         if pending.kind == "registration" else "일치하는 알림을 확인하지 못해 응답을 폐기했습니다")

    @Slot()
    def stop(self) -> None:
        self.close()

    @Slot()
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._generation += 1
        for nonce in tuple(self._pending):
            self._finish(nonce)
        self._status("stopped", "알림 수신 종료")
        # Keep the profile alive until its final page has actually been deleted.
        self.page.destroyed.connect(self.profile.deleteLater)
        self.page.deleteLater()
