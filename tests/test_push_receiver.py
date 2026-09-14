"""Local/synthetic evidence only: no account, YouTube query or recording."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from PySide6.QtCore import (
    QCoreApplication,
    QEvent,
    QEventLoop,
    QObject,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtWebEngineCore import QWebEnginePermission, QWebEngineScript
from PySide6.QtWidgets import QApplication, QMessageBox

from yt_rec.backend import push_receiver as module

QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
VIDEO = "sxyDRUJYFYw"
OTHER_VIDEO = "AiZnFPyv-Jc"


class FakeProfile(QObject):
    downloadRequested = Signal(object)

    def __init__(self, name, parent):
        super().__init__(parent)
        self.name, self.paths = name, {}

    def setPersistentStoragePath(self, path): self.paths["storage"] = path
    def setCachePath(self, path): self.paths["cache"] = path
    def setDownloadPath(self, path): self.paths["downloads"] = path
    def setPushServiceEnabled(self, enabled): self.push_enabled = enabled
    def setNotificationPresenter(self, callback): self.presenter = callback


class FakePage(QObject):
    loadStarted = Signal()
    loadFinished = Signal(bool)
    permissionRequested = Signal(object)

    def __init__(self, profile, parent):
        super().__init__(parent)
        assert len(profile.paths) == 3 and profile.push_enabled
        self.profile = profile
        self.current_url = QUrl()
        self.calls, self.navigations, self.injected_scripts = [], [], []

    def setWebChannel(self, channel, world): self.channel, self.world = channel, world
    def scripts(self): return self
    def insert(self, script): self.injected_scripts.append(script)
    def url(self): return self.current_url

    def setUrl(self, url):
        self.current_url = url
        self.navigations.append(url.toString())
        self.loadStarted.emit()

    def runJavaScript(self, script, world):
        self.calls.append((script, world))


class Notice(QObject):
    closed = Signal()

    def __init__(self, *, tag="same-tag", origin=module.YOUTUBE, title="SYNTHETIC", body="private body"):
        super().__init__()
        self._tag, self._origin, self._title, self._body = tag, origin, title, body
        self.shown = self.close_calls = 0

    def tag(self): return self._tag
    def origin(self): return QUrl(self._origin)
    def title(self): return self._title
    def message(self): return self._body
    def show(self): self.shown += 1

    def close(self):
        self.close_calls += 1
        self.closed.emit()


@pytest.fixture
def receiver(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(module, "QWebEngineProfile", FakeProfile)
    monkeypatch.setattr(module, "_Page", FakePage)
    monkeypatch.setattr(module, "default_profile_directory", lambda: tmp_path / "receiver")
    result = module.YouTubePushReceiver()
    yield result
    result.stop()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def present(receiver, **kwargs):
    receiver.start()
    notice = Notice(**kwargs)
    receiver.profile.presenter(notice)
    nonce = next(reversed(receiver._pending))
    return notice, nonce, receiver._pending[nonce].generation


def reply(receiver, nonce, generation, **values):
    receiver._bridge.deliver(json.dumps({"nonce": nonce, "generation": generation, **values}))


def test_native_request_is_required_and_is_consumed_once(receiver, capsys):
    events, status = [], []
    receiver.notification_received.connect(events.append)
    receiver.status_changed.connect(lambda code, detail: status.append((code, detail)))
    reply(receiver, "unsolicited", 0, status="matched", data={"videoId": VIDEO})
    notice, nonce, generation = present(receiver)
    pending = receiver._pending[nonce]
    assert notice.shown == 1 and events == []
    script, world = receiver.page.calls[-1]
    assert world == module._WORLD and nonce in script
    assert '"tag": "same-tag"' in script and '"body": "private body"' in script
    reply(receiver, nonce, generation, status="matched", data={"videoId": VIDEO, "token": "private-token"})
    reply(receiver, nonce, generation, status="matched", data={"videoId": VIDEO})
    assert len(events) == 1
    assert events[0].video_id == VIDEO and events[0].synthetic is False
    assert events[0].received_at == pending.received_at
    assert notice.close_calls == 1 and not receiver._pending
    assert status[-1][0] == "received"
    captured = capsys.readouterr()
    assert "private" not in str(status) + captured.out + captured.err


@pytest.mark.parametrize("data, expected", [
    ({"videoId": VIDEO}, VIDEO),
    ({"nested": [{"video_id": VIDEO}, {"navigationUrl": module.YOUTUBE + "/watch?v=" + VIDEO}]}, VIDEO),
    ({"arbitrary": module.YOUTUBE + "/watch?feature=notification&v=" + VIDEO, "icon": "https://img.invalid/a"}, VIDEO),
    ({"videoId": VIDEO, "avatar": module.YOUTUBE + "/channel/abc"}, VIDEO),
    ({"videoId": VIDEO, "other": {"videoId": OTHER_VIDEO}}, None),
    ({"navigation": module.YOUTUBE + "/watch?v=" + VIDEO + "&v=" + OTHER_VIDEO}, None),
    ({"videoId": VIDEO, "navigation": module.YOUTUBE + "/watch?v=bad"}, None),
    ({"videoId": "../../tokens"}, None),
    ({"videoId": VIDEO + "\n"}, None),
    ({"videoId": 12345678901}, None),
    ({"title": VIDEO}, None),
    ({"url": "/watch?v=" + VIDEO}, None),
    ({"url": "https://www.youtube.com.evil.invalid/watch?v=" + VIDEO}, None),
    ({"url": "https://[invalid"}, None),
    ({"url": "http://www.youtube.com/watch?v=" + VIDEO}, None),
    ({"url": "https://user@www.youtube.com/watch?v=" + VIDEO}, None),
    ({"url": "https://www.youtube.com:444/watch?v=" + VIDEO}, None),
    ({"url": "\nhttps://www.youtube.com/watch?v=" + VIDEO}, None),
    (None, None),
    ([{"videoId": VIDEO}], VIDEO),
    (module.YOUTUBE + "/watch?v=" + VIDEO, VIDEO),
    ([module.YOUTUBE + "/watch?v=" + VIDEO, module.YOUTUBE + "/watch?v=" + OTHER_VIDEO], None),
    (VIDEO, None),
])
def test_data_parser_never_guesses_a_video(data, expected):
    assert module._video_id_from_data(data) == expected


def test_traversal_limits_reject_whole_payload_instead_of_partial_candidate():
    assert module._video_id_from_data({"videoId": VIDEO, "extra": [None] * 256}) is None
    nested = {"videoId": OTHER_VIDEO}
    for _ in range(7):
        nested = {"nested": nested}
    assert module._video_id_from_data({"videoId": VIDEO, "extra": nested}) is None


@pytest.mark.parametrize("raw", ["{", "[]", '{"nonce": []}', '{"nonce": "x", "nonce": "y"}',
                                     '{"nonce": "x", "data": NaN}', "x" * (module._MAX_RESPONSE + 1)],
                         ids=["invalid", "array", "nonce-array", "duplicate", "nan", "oversized"])
def test_invalid_bridge_json_never_emits(receiver, raw):
    events = []
    receiver.notification_received.connect(events.append)
    receiver._bridge.deliver(raw)
    assert events == [] and not receiver._pending


@pytest.mark.parametrize("data", [
    '{"videoId": "sxyDRUJYFYw", "videoId": "sxyDRUJYFYw"}',
    '{"videoId": "sxyDRUJYFYw", "extra": NaN}',
])
def test_nonstandard_json_cannot_use_a_real_pending_nonce(receiver, data):
    events = []
    receiver.notification_received.connect(events.append)
    _, nonce, generation = present(receiver)
    raw = json.dumps({"nonce": nonce, "generation": generation, "status": "matched"})
    receiver._bridge.deliver(raw[:-1] + ', "data": ' + data + '}')
    assert events == [] and nonce in receiver._pending


@pytest.mark.parametrize("generation_delta", [1, True, None])
def test_wrong_document_generation_never_emits(receiver, generation_delta):
    events = []
    receiver.notification_received.connect(events.append)
    _, nonce, generation = present(receiver)
    value = generation + generation_delta if type(generation_delta) is int else generation_delta
    reply(receiver, nonce, value, status="matched", data={"videoId": VIDEO})
    assert events == []


def test_navigation_back_to_youtube_cannot_revive_a_stale_reply(receiver):
    events = []
    receiver.notification_received.connect(events.append)
    notice, nonce, generation = present(receiver)
    receiver.page.setUrl(QUrl("https://accounts.google.com"))
    receiver.page.loadFinished.emit(True)
    assert receiver._last_status[0] == "login_required"
    receiver.page.setUrl(QUrl(module.YOUTUBE))
    reply(receiver, nonce, generation, status="matched", data={"videoId": VIDEO})
    assert events == [] and notice.close_calls == 1


def test_current_origin_is_rechecked_on_reply(receiver):
    events = []
    receiver.notification_received.connect(events.append)
    _, nonce, generation = present(receiver)
    receiver.page.current_url = QUrl("https://accounts.google.com")
    reply(receiver, nonce, generation, status="matched", data={"videoId": VIDEO})
    assert events == []


@pytest.mark.parametrize("notice_args", [
    {"origin": "https://www.youtube.com.evil.invalid"}, {"origin": "http://www.youtube.com"},
    {"origin": "https://www.youtube.com:444"}, {"tag": "x" * 1025},
    {"title": "x" * 4097}, {"body": "x" * 16385},
])
def test_foreign_or_oversized_native_notices_do_not_query(receiver, notice_args):
    receiver.start()
    notice = Notice(**notice_args)
    receiver.profile.presenter(notice)
    assert notice.close_calls == 1 and not receiver._pending and not receiver.page.calls


def test_tag_replacement_retires_old_request_but_empty_tags_are_independent(receiver):
    first, old_nonce, _ = present(receiver)
    second, new_nonce, _ = present(receiver)
    assert old_nonce not in receiver._pending and new_nonce in receiver._pending
    assert first.close_calls == 0  # Do not close the replaced chain's new notice.
    first.close()
    assert new_nonce in receiver._pending
    second.close()
    assert not receiver._pending
    _, nonce_one, _ = present(receiver, tag="")
    _, nonce_two, _ = present(receiver, tag="")
    assert nonce_one != nonce_two and len(receiver._pending) == 2


def test_closed_notification_cannot_deliver_and_timeout_only_cleans_up(receiver):
    events = []
    receiver.notification_received.connect(events.append)
    notice, nonce, generation = present(receiver)
    notice.close()
    reply(receiver, nonce, generation, status="matched", data={"videoId": VIDEO})
    second, nonce, generation = present(receiver)
    calls = len(receiver.page.calls)
    pending = receiver._pending[nonce]
    assert pending.timer.isSingleShot() and pending.timer.interval() == module._REQUEST_TIMEOUT_MS
    pending.timer.timeout.emit()
    reply(receiver, nonce, generation, status="matched", data={"videoId": VIDEO})
    assert events == [] and len(receiver.page.calls) == calls
    assert not receiver._pending and second.close_calls == 1


def test_retained_native_notifications_are_bounded(receiver):
    for index in range(module._MAX_PENDING):
        present(receiver, tag=str(index))
    extra = Notice(tag="overflow")
    receiver.profile.presenter(extra)
    assert extra.close_calls == 1 and len(receiver._pending) == module._MAX_PENDING
    assert len(receiver.page.calls) == module._MAX_PENDING


def test_registration_is_event_driven_coalesced_and_not_a_recording(receiver):
    events = []
    receiver.notification_received.connect(events.append)
    assert receiver.page.navigations == [] and receiver.page.calls == []
    receiver.start()
    receiver.start()
    assert receiver.page.navigations == [module.YOUTUBE]
    receiver.page.loadFinished.emit(True)
    receiver.inspect_registration()
    assert len(receiver.page.calls) == 1
    nonce, pending = next(iter(receiver._pending.items()))
    reply(receiver, nonce, pending.generation, status="registration", worker=True, active=True, subscription=True, permission="granted")
    assert receiver._last_status[0] == "ready" and "실제 도착 대기" in receiver._last_status[1]
    assert events == []
    receiver.open_settings()
    assert receiver.page.navigations[-1] == module.NOTIFICATION_SETTINGS_URL
    receiver.page.loadFinished.emit(True)
    nonce, pending = next(iter(receiver._pending.items()))
    reply(receiver, nonce, pending.generation, status="matched", data={"videoId": VIDEO})
    assert events == [] and receiver._last_status[0] == "error"


@pytest.mark.parametrize("permission, worker, active, subscription, status", [
    ("default", True, True, False, "permission_required"),
    ("denied", True, True, True, "permission_required"),
    ("granted", False, False, False, "worker_missing"),
    ("granted", True, False, False, "worker_inactive"),
    ("granted", True, True, False, "unsubscribed"),
    ("granted", True, True, True, "ready"),
    ("granted", True, True, 1, "error"),
    ("granted", True, None, True, "error"),
    ("invalid", True, True, True, "error"),
])
def test_registration_status_is_truthful(receiver, permission, worker, active, subscription, status):
    receiver.start()
    receiver.inspect_registration()
    nonce, pending = next(iter(receiver._pending.items()))
    reply(receiver, nonce, pending.generation, status="registration", worker=worker, active=active,
          subscription=subscription, permission=permission)
    assert receiver._last_status[0] == status
    if status == "unsubscribed":
        assert "이미 켜져 있어도" in receiver._last_status[1]


def test_failed_registration_lookup_does_not_claim_permission_or_subscription(receiver):
    receiver.start()
    receiver.inspect_registration()
    assert receiver._last_status[0] == "checking"
    nonce, pending = next(iter(receiver._pending.items()))
    reply(receiver, nonce, pending.generation, status="lookup_failed")
    assert receiver._last_status[0] == "error"
    assert "조회에 실패" in receiver._last_status[1]


def test_shutdown_blocks_new_inputs_and_deletes_page_before_profile(receiver):
    events, destroyed = [], []
    receiver.notification_received.connect(events.append)
    receiver.page.destroyed.connect(lambda: destroyed.append("page"))
    receiver.profile.destroyed.connect(lambda: destroyed.append("profile"))
    _, nonce, generation = present(receiver)
    page = receiver.page
    calls = len(page.calls)
    receiver.stop()
    receiver.stop()
    receiver.open_settings()
    receiver.inspect_registration()
    reply(receiver, nonce, generation, status="matched", data={"videoId": VIDEO})
    extra = Notice()
    receiver._present(extra)
    assert events == [] and not receiver._pending and extra.close_calls == 1
    assert len(page.calls) == calls
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert destroyed == ["page", "profile"]


def test_bridge_is_not_exposed_to_page_scripts_or_subframes(receiver):
    script = receiver.page.injected_scripts[0]
    assert receiver.page.world == QWebEngineScript.ScriptWorldId.ApplicationWorld
    assert script.worldId() == receiver.page.world
    assert not script.runsOnSubFrames()
    assert script.injectionPoint() == QWebEngineScript.InjectionPoint.DocumentCreation
    assert "qt.webChannelTransport" in script.sourceCode()
    assert module._LOOKUP.count("getNotifications(") == 1
    assert module._LOOKUP.count("getRegistrations(") == 1
    assert module._REGISTRATION.count("getSubscription(") == 1
    assert "notices.length > 128" in module._LOOKUP
    assert "matches.length !== 1" in module._LOOKUP
    for code in (module._LOOKUP, module._REGISTRATION):
        assert code.count("getRegistrations(") == 1
        assert "registrations.length > 32" in code
        assert ".scope).origin !== request.origin" in code
        assert ".ready" not in code and "setInterval" not in code and "setTimeout" not in code
        assert ".subscribe(" not in code and "console." not in code


def test_permission_prompt_rejects_navigation_during_modal_loop(receiver, monkeypatch):
    class Permission:
        granted = denied = False
        def origin(self): return QUrl(module.YOUTUBE)
        def permissionType(self): return QWebEnginePermission.PermissionType.Notifications
        def grant(self): self.granted = True
        def deny(self): self.denied = True

    receiver.start()
    permission = Permission()

    def navigate(*_):
        receiver.page.setUrl(QUrl("https://accounts.google.com"))
        receiver.page.setUrl(QUrl(module.YOUTUBE))
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", navigate)
    receiver._permission(permission)
    assert permission.denied and not permission.granted
    assert receiver.page.calls == []


@pytest.fixture
def local_site():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = (b"self.addEventListener('activate', e => e.waitUntil(clients.claim()));"
                    if self.path.endswith("/sw.js") else b"<!doctype html><title>SYNTHETIC receiver test</title>")
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript" if self.path.endswith("/sw.js") else "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_): pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.mark.parametrize("scope,duplicate", [("/", False), ("/push/", False), ("/push/", True)])
def test_real_qt_local_service_worker_empty_tag_notification_reaches_receiver(qapp, tmp_path, monkeypatch, local_site, scope, duplicate):
    """Real native Qt/JS/WebChannel wiring; SYNTHETIC, not YouTube/FCM proof."""
    monkeypatch.setattr(module, "default_profile_directory", lambda: tmp_path / "synthetic-profile")
    monkeypatch.setattr(module, "YOUTUBE", local_site)
    monkeypatch.setattr(module, "_is_youtube_url", lambda value:
                        QUrl(value).scheme() == "http" and QUrl(value).authority() == QUrl(local_site).authority())
    receiver = module.YouTubePushReceiver()
    # Local showNotification needs no push transport or external subscription.
    receiver.profile.setPushServiceEnabled(False)
    receiver.profile.queryPermission(QUrl(local_site), QWebEnginePermission.PermissionType.Notifications).grant()
    events, statuses, presenter_calls = [], [], []
    loop = QEventLoop()
    deadline = QTimer()
    deadline.setSingleShot(True)
    deadline.timeout.connect(loop.quit)

    def presenter(notice):
        presenter_calls.append(notice)
        if duplicate:
            notice.show()
        else:
            receiver._present(notice)  # Keep the real production callback timing.

    receiver.profile.setNotificationPresenter(presenter)

    def notifications_stored(value):
        if duplicate and value == "synthetic_complete":
            receiver._present(presenter_calls[0])

    receiver._bridge.received.connect(notifications_stored)

    def collect(notice):
        events.append(notice)
        loop.quit()

    receiver.notification_received.connect(collect)
    def status_changed(code, detail):
        statuses.append((code, detail))
        if code == "error" and duplicate and presenter_calls:
            loop.quit()

    receiver.status_changed.connect(status_changed)

    def loaded(ok):
        if not ok:
            loop.quit()
            return
        receiver.page.runJavaScript("""
            (async () => {
                for (const script of SCRIPT_URLS) {
                const reg = await navigator.serviceWorker.register(script);
                if (!reg.active) await new Promise(resolve => {
                    const worker = reg.installing || reg.waiting;
                    worker.addEventListener('statechange', () => {
                        if (worker.state === 'activated') resolve();
                    });
                });
                await reg.showNotification('SYNTHETIC receiver notice', {
                    tag: '', body: 'Not a real YouTube push',
                    data: {videoId: 'sxyDRUJYFYw', secret: 'must-not-be-logged'}
                });
                }
                const bridge = await window.ytRecReady;
                bridge.deliver('synthetic_complete');
            })();
        """.replace("SCRIPT_URLS", json.dumps([scope + "sw.js"] + (["/other/sw.js"] if duplicate else []))), module._WORLD)

    receiver.page.loadFinished.connect(loaded)
    try:
        receiver.start()
        deadline.start(15000)
        loop.exec()
        assert len(presenter_calls) == (2 if duplicate else 1), statuses
        assert len(events) == (0 if duplicate else 1), statuses
        if duplicate:
            assert statuses[-1][0] == "error"
        else:
            assert events[0].video_id == VIDEO and events[0].synthetic is False
        assert "must-not-be-logged" not in str(statuses)
        assert Path(receiver.profile.persistentStoragePath()) == tmp_path / "synthetic-profile" / "storage"
    finally:
        deadline.stop()
        receiver.stop()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
