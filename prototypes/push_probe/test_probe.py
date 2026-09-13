"""Synthetic local notifications are not evidence of actual YouTube/FCM delivery."""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QObject, QTimer, QUrl, Signal, Qt
from PySide6.QtWebEngineCore import QWebEnginePermission
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from probe import CHANNEL, LOOKUP, Probe, WORLD, create_window, summarize_data

VIDEO_ID = "sxyDRUJYFYw"
CHANNEL_ID = "UC" + "a" * 22


@pytest.fixture(scope="session")
def app():
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    return QApplication([])


@pytest.fixture
def local_site():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = (b"self.addEventListener('activate', e => e.waitUntil(clients.claim()));"
                    if self.path == "/sw.js" else b"<!doctype html><title>SYNTHETIC push probe</title>")
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript" if self.path == "/sw.js" else "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join()


def test_synthetic_service_worker_notification_reaches_native_bridge(app, local_site, tmp_path):
    probe = Probe(tmp_path / "profile", local_site, synthetic=True)
    evidence = []
    loop = QEventLoop()
    deadline = QTimer()
    deadline.setSingleShot(True)
    deadline.timeout.connect(loop.quit)  # Test deadline only, never discovery polling.
    probe.profile.queryPermission(QUrl(local_site), QWebEnginePermission.PermissionType.Notifications).grant()

    def collect(value):
        evidence.append(value)
        if value["status"] == "stored_notification_data":
            probe.inspect_registration()
        elif value["status"] == "registration":
            loop.quit()

    probe.evidence.connect(collect)

    def loaded(ok):
        assert ok
        probe.page.runJavaScript("""
            (async () => {
                const reg = await navigator.serviceWorker.register('/sw.js');
                if (!reg.active) await new Promise(resolve => {
                    const worker = reg.installing || reg.waiting;
                    worker.addEventListener('statechange', () => { if (worker.state === 'activated') resolve(); });
                });
                await reg.showNotification('SYNTHETIC scheduled live', {
                    tag: 'synthetic-unique', body: 'Not YouTube or FCM evidence',
                    data: DATA
                });
            })();
        """.replace("DATA", json.dumps({"videoId": VIDEO_ID, "channelId": CHANNEL_ID, "token": "must-not-be-logged"})))

    probe.page.loadFinished.connect(loaded)
    probe.page.setUrl(QUrl(local_site))
    deadline.start(20000)
    loop.exec()
    deadline.stop()
    try:
        assert any(item["status"] == "presenter_callback" for item in evidence), evidence
        result = next(item for item in evidence if item["status"] == "stored_notification_data")
        assert result["evidence_kind"] == "SYNTHETIC"
        assert result["video_id"] == VIDEO_ID
        assert result["channel_id"] == CHANNEL_ID
        assert result["mapping"] == "unique_candidate"
        assert result["push_delivery_verified"] is False
        registration = next(item for item in evidence if item["status"] == "registration")
        assert registration["service_worker"] is True
        assert registration["push_subscription"] is False
        assert registration["permission"] == "granted"
        assert "must-not-be-logged" not in json.dumps(evidence)
        assert probe.profile.isPushServiceEnabled()
        assert not probe.profile.isOffTheRecord()
        (tmp_path / "evidence-synthetic.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        print("Synthetic evidence:", tmp_path / "evidence-synthetic.json")
    finally:
        probe.close()


def test_data_candidates_are_strict_and_ambiguous_values_are_not_guessed():
    assert CHANNEL.fullmatch(CHANNEL_ID)
    assert summarize_data({"videoId": "../../tokens.json", "channelId": "invalid"})["mapping"] == "unresolved"
    assert summarize_data({"a": {"videoId": VIDEO_ID}, "b": {"videoId": "AiZnFPyv-Jc"}})["video_id"] is None
    assert summarize_data({"url": "https://www.youtube.com.evil.invalid/watch?v=" + VIDEO_ID})["video_id"] is None
    assert summarize_data({"url": "https://[invalid"})["video_id"] is None
    assert summarize_data({"url": "https://www.youtube.com/watch?v=" + VIDEO_ID})["video_id"] == VIDEO_ID


def test_window_reserves_space_for_the_browser_and_opens_only_normal_settings(app, tmp_path, monkeypatch):
    probe = Probe(tmp_path / "profile", "https://www.youtube.com", synthetic=True)
    navigation, checks, scripts = [], [], []
    monkeypatch.setattr(probe.page, "setUrl", lambda url: navigation.append(url.toString()))
    monkeypatch.setattr(probe.page, "runJavaScript", lambda *args: scripts.append(args))
    monkeypatch.setattr(probe, "inspect_registration", lambda: checks.append(True))
    window = create_window(probe)
    try:
        window.show()
        app.processEvents()
        view = window.findChild(QWebEngineView)
        assert view.page() is probe.page
        assert view.height() > window.height() * 0.7, (view.height(), window.height())
        assert all(label.height() < window.height() * 0.1 for label in window.findChildren(QLabel))
        assert navigation == checks == scripts == []
        settings = next(button for button in window.findChildren(QPushButton) if button.text() == "알림 설정 열기")
        settings.click()
        assert navigation == ["https://www.youtube.com/account_notifications"]
        assert checks == scripts == []
        check = next(button for button in window.findChildren(QPushButton) if "구독 확인" in button.text())
        check.click()
        assert checks == [True]
        assert scripts == []
        window.grab().save(str(tmp_path / "probe-layout.png"))
        print("Layout evidence:", tmp_path / "probe-layout.png", "browser height:", view.height())
    finally:
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        probe.close()


def test_lookup_is_callback_driven_one_shot_and_checks_identity():
    assert LOOKUP.count("getNotifications(") == 1
    assert "getRegistration(" in LOOKUP and ".ready" not in LOOKUP
    assert "if (!request.tag)" in LOOKUP
    assert "notices.length !== 1" in LOOKUP
    assert "notice.title !== request.title" in LOOKUP
    assert "notice.body !== request.body" in LOOKUP
    assert "location.origin !== request.origin" in LOOKUP
    assert "setInterval" not in LOOKUP and "setTimeout" not in LOOKUP


def test_closed_replaced_empty_and_foreign_notifications_are_not_guessed(app, tmp_path, monkeypatch):
    probe = Probe(tmp_path, "https://www.youtube.com", synthetic=True)
    calls, evidence = [], []
    probe.evidence.connect(evidence.append)
    monkeypatch.setattr(probe.page, "url", lambda: QUrl("https://www.youtube.com"))
    monkeypatch.setattr(probe.page, "runJavaScript", lambda *args: calls.append(args))

    class Notice(QObject):
        closed = Signal()

        def __init__(self, tag="same-tag", site="https://www.youtube.com"):
            super().__init__()
            self._tag, self.site = tag, site

        def tag(self): return self._tag
        def origin(self): return QUrl(self.site)
        def title(self): return "Synthetic"
        def message(self): return "Synthetic"
        def show(self): pass
        def close(self): self.closed.emit()

    try:
        probe.present(Notice(tag=""))
        probe.present(Notice(site="https://www.youtube.com.evil.invalid"))
        assert calls == []
        first, second = Notice(), Notice()
        probe.present(first)
        old_nonce = next(iter(probe.pending))
        probe.present(second)
        assert old_nonce not in probe.pending
        new_nonce = next(iter(probe.pending))
        probe.receive(json.dumps({"nonce": old_nonce, "status": "matched", "data": {"videoId": VIDEO_ID}}))
        second.close()
        probe.receive(json.dumps({"nonce": new_nonce, "status": "matched", "data": {"videoId": VIDEO_ID}}))
        probe.receive(json.dumps({"nonce": [], "status": "matched"}))
        assert not any(e["status"] == "stored_notification_data" for e in evidence)
        assert len(calls) == 2
    finally:
        probe.close()
