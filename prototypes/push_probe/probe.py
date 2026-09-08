"""Isolated notification-data feasibility probe. Never imports the recording app."""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from PySide6.QtCore import QCoreApplication, QEvent, QFile, QIODevice, QObject, QUrl, Signal, Slot, Qt
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import (
    QWebEnginePage, QWebEnginePermission, QWebEngineProfile, QWebEngineScript,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget

YOUTUBE = "https://www.youtube.com"
WORLD = QWebEngineScript.ScriptWorldId.ApplicationWorld
VIDEO = re.compile(r"[A-Za-z0-9_-]{11}\Z")
CHANNEL = re.compile(r"UC[A-Za-z0-9_-]{22}\Z")


def origin(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""
    if parsed.username or parsed.password:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def summarize_data(data: object) -> dict:
    """Candidate schema only: do not infer live state or log unknown values/URLs."""
    videos, channels = set(), set()

    def visit(value, depth=0):
        if depth > 6:
            return
        if isinstance(value, dict):
            for key, item in list(value.items())[:64]:
                if key in ("videoId", "video_id") and isinstance(item, str) and VIDEO.fullmatch(item):
                    videos.add(item)
                if key in ("channelId", "channel_id") and isinstance(item, str) and CHANNEL.fullmatch(item):
                    channels.add(item)
                if key in ("url", "endpoint") and isinstance(item, str):
                    if origin(item) == YOUTUBE:
                        parsed = urlsplit(item)
                        ids = parse_qs(parsed.query).get("v", [])
                        if parsed.path == "/watch" and len(ids) == 1 and VIDEO.fullmatch(ids[0]):
                            videos.add(ids[0])
                visit(item, depth + 1)
        elif isinstance(value, list):
            for item in value[:64]:
                visit(item, depth + 1)

    visit(data)
    return {
        "schema_keys": sorted(k for k in data if isinstance(k, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", k))[:32] if isinstance(data, dict) else [],
        "video_id": next(iter(videos)) if len(videos) == 1 else None,
        "channel_id": next(iter(channels)) if len(channels) == 1 else None,
        "mapping": "unique_candidate" if len(videos) == len(channels) == 1 else "unresolved",
        "broadcast_state": "unverified",
    }


LOOKUP = """
(async () => {
    const request = REQUEST;
    const bridge = await window.probeReady;
    const send = value => bridge.deliver(JSON.stringify({...value, nonce: request.nonce}));
    try {
        if (location.origin !== request.origin) return send({status: 'origin_changed'});
        // getRegistration resolves without waiting indefinitely for a future worker.
        const registration = await navigator.serviceWorker.getRegistration(location.href);
        if (!registration) return send({status: 'no_registration'});
        if (!request.tag) return send({status: 'empty_tag'});
        const notices = await registration.getNotifications({tag: request.tag});
        if (notices.length !== 1) return send({status: 'missing_or_ambiguous'});
        const notice = notices[0];
        if (notice.tag !== request.tag || notice.title !== request.title || notice.body !== request.body)
            return send({status: 'replaced'});
        send({status: 'matched', data: notice.data});
    } catch (_) { send({status: 'lookup_failed'}); }
})();
"""


class Bridge(QObject):
    received = Signal(str)

    @Slot(str)
    def deliver(self, value: str) -> None:
        if len(value) <= 65536:
            self.received.emit(value)


class Page(QWebEnginePage):
    def javaScriptConsoleMessage(self, level, message, line, source):  # noqa: N802
        # Website console output may contain credentials or private notification data.
        pass


class Probe(QObject):
    evidence = Signal(object)

    def __init__(self, profile_dir: Path, allowed_origin: str, *, synthetic=False):
        super().__init__()
        self.allowed_origin = allowed_origin
        self.synthetic = synthetic
        self.pending = {}
        self.notifications = []
        self.profile = QWebEngineProfile("yt-rec-push-probe", self)
        self.profile.setPersistentStoragePath(str(profile_dir / "storage"))
        self.profile.setCachePath(str(profile_dir / "cache"))
        self.profile.setDownloadPath(str(profile_dir / "downloads"))
        self.profile.setPushServiceEnabled(True)
        self.profile.downloadRequested.connect(lambda item: item.cancel())
        self.page = Page(self.profile, self)
        self.channel = QWebChannel(self.page)
        self.bridge = Bridge(self.channel)
        self.channel.registerObject("probe", self.bridge)
        self.page.setWebChannel(self.channel, WORLD)
        self.bridge.received.connect(self.receive)
        library = QFile(":/qtwebchannel/qwebchannel.js")
        if not library.open(QIODevice.OpenModeFlag.ReadOnly):
            raise RuntimeError("Qt WebChannel resource is unavailable")
        script = QWebEngineScript()
        script.setName("isolated-probe-bridge")
        script.setWorldId(WORLD)
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setRunsOnSubFrames(False)
        script.setSourceCode(bytes(library.readAll()).decode() + "\nwindow.probeReady = new Promise(resolve => new QWebChannel(qt.webChannelTransport, c => resolve(c.objects.probe)));")
        self.page.scripts().insert(script)
        self.profile.setNotificationPresenter(self.present)
        self.page.permissionRequested.connect(self.permission)
        self.page.loadStarted.connect(self.navigation_started)

    def report(self, status, **values):
        self.evidence.emit({"evidence_kind": "SYNTHETIC" if self.synthetic else "REAL_YOUTUBE_CANDIDATE", "push_delivery_verified": False, "status": status, **values})

    def navigation_started(self):
        if self.pending:
            self.pending.clear()
            self.report("navigation_cancelled_lookup")

    def permission(self, permission):
        if origin(permission.origin().toString()) != self.allowed_origin or permission.permissionType() != QWebEnginePermission.PermissionType.Notifications:
            permission.deny()
            return
        allowed = self.synthetic or QMessageBox.question(None, "시험 프로필의 알림 권한", "이 격리된 시험 프로필에서 YouTube 알림을 허용할까요?") == QMessageBox.StandardButton.Yes
        permission.grant() if allowed else permission.deny()

    def present(self, notification):
        if origin(notification.origin().toString()) != self.allowed_origin:
            self.report("rejected_origin")
            return
        if origin(self.page.url().toString()) != self.allowed_origin:
            self.report("no_same_origin_page")
            return
        tag = notification.tag()
        if not tag or len(tag) > 1024:
            self.report("empty_or_oversized_tag")
            return
        if len(notification.title()) > 4096 or len(notification.message()) > 16384:
            self.report("oversized_notification")
            return
        for nonce, previous in tuple(self.pending.items()):
            if previous.get("tag") == tag:
                self.pending.pop(nonce)
                self.report("replaced_pending")
        if len(self.pending) >= 32:
            self.report("too_many_pending")
            return
        nonce = secrets.token_hex(16)
        request = {"nonce": nonce, "origin": self.allowed_origin, "tag": tag,
                   "title": notification.title(), "body": notification.message()}
        self.pending[nonce] = request
        self.notifications.append(notification)
        notification.closed.connect(lambda: self.closed(nonce, notification))
        notification.show()
        self.report("presenter_callback")
        self.page.runJavaScript(LOOKUP.replace("REQUEST", json.dumps(request)), WORLD)

    def closed(self, nonce, notification):
        self.pending.pop(nonce, None)
        if notification in self.notifications:
            self.notifications.remove(notification)

    def receive(self, text):
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            return
        if not isinstance(value, dict):
            return
        nonce = value.get("nonce")
        if not isinstance(nonce, str):
            return
        request = self.pending.pop(nonce, None)
        if request is None or origin(self.page.url().toString()) != self.allowed_origin:
            return
        status = value.get("status")
        if status == "matched":
            self.report("stored_notification_data", **summarize_data(value.get("data")))
        elif status == "registration" and request.get("kind") == "registration":
            self.report(status, service_worker=value.get("worker") is True,
                        push_subscription=value.get("subscription") is True,
                        permission=value.get("permission") if value.get("permission") in ("default", "granted", "denied") else "unknown")
        elif status in ("origin_changed", "no_registration", "empty_tag", "missing_or_ambiguous", "replaced", "lookup_failed"):
            self.report(status)

    def inspect_registration(self):
        # User-triggered, one-shot status check. Never logs an endpoint/token.
        if origin(self.page.url().toString()) != self.allowed_origin:
            self.report("no_same_origin_page")
            return
        nonce = secrets.token_hex(16)
        self.pending[nonce] = {"kind": "registration"}
        self.page.runJavaScript("""
            (async () => {
                const bridge = await window.probeReady;
                try {
                    const reg = await navigator.serviceWorker.getRegistration(location.href);
                    const sub = reg ? await reg.pushManager.getSubscription() : null;
                    bridge.deliver(JSON.stringify({nonce: NONCE, status: 'registration',
                        worker: !!reg, subscription: !!sub, permission: Notification.permission}));
                } catch (_) { bridge.deliver(JSON.stringify({nonce: NONCE, status: 'lookup_failed'})); }
            })();
        """.replace("NONCE", json.dumps(nonce)), WORLD)

    def close(self):
        self.pending.clear()
        for notification in tuple(self.notifications):
            notification.close()
        self.page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.profile.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    app = QApplication(sys.argv[:1])
    # Fixed dedicated location: never accept another browser's profile directory.
    probe = Probe(Path(__file__).resolve().parent / ".profile-youtube", YOUTUBE)
    window = QWidget()
    window.setWindowTitle("yt-rec 푸시 가능성 시험 — 녹화하지 않음")
    layout = QVBoxLayout(window)
    layout.addWidget(QLabel("별도 시험 프로필입니다. 기존 Google OAuth·Chrome 프로필·녹화에 접근하지 않습니다."))
    view = QWebEngineView(window)
    view.setPage(probe.page)
    layout.addWidget(view)
    status = QLabel("실제 YouTube 알림 수신·영상 ID 추출은 아직 검증되지 않았습니다.")
    status.setWordWrap(True)
    layout.addWidget(status)
    check = QPushButton("서비스 워커·알림 권한·푸시 구독 확인 (한 번)")
    check.clicked.connect(probe.inspect_registration)
    layout.addWidget(check)
    probe.evidence.connect(lambda item: status.setText(json.dumps(item, ensure_ascii=False)))
    probe.evidence.connect(lambda item: print(json.dumps(item, ensure_ascii=False), flush=True))
    window.resize(1000, 800)
    window.show()
    view.setUrl(QUrl(YOUTUBE))
    app.aboutToQuit.connect(probe.close)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
