"""Browser login contract; synthetic page/CDP data, never real credentials."""
from __future__ import annotations

import json
import threading

import pytest
from PySide6.QtCore import QObject, QThread, Signal, Slot

from test_notification_source import production_backend as production_backend, until, flush, notice, VIDEO
from yt_rec.backend import browser_web as web
from yt_rec.backend.youtube import YouTubeError
from yt_rec.state import commands as cmd, events as ev
from yt_rec.state.models import ConnectionState

CHANNEL = "UC" + "a" * 22
ACCOUNT = "a" * 64


def feed(channels=None, **values):
    return {"account": ACCOUNT, "channels": channels if channels is not None else [
        {"channel_id": CHANNEL, "name": "selected"}], "complete": True, **values}


def player(status="live"):
    return {"account": ACCOUNT, "player": {
        "videoDetails": {"videoId": VIDEO, "channelId": CHANNEL, "title": "test",
                         "isLiveContent": True, "isUpcoming": status == "upcoming"},
        "playabilityStatus": {"status": "LIVE_STREAM_OFFLINE" if status == "upcoming" else "OK"},
        "microformat": {"playerMicroformatRenderer": {"liveBroadcastDetails": {
            "isLiveNow": status == "live", "startTimestamp": "2026-09-29T00:00:00+00:00",
            **({"endTimestamp": "2026-09-29T01:00:00+00:00"} if status == "ended" else {}),
        }}},
    }}


@pytest.mark.parametrize("status", ["live", "upcoming", "ended"])
def test_video_states_keep_actual_live_upcoming_and_ended_separate(status):
    result = web.video_state(VIDEO, player(status))
    assert result.status == status and result.broadcast.channel_id == CHANNEL
    assert (result.scheduled_start is not None) is (status == "upcoming")


def test_unavailable_or_wrong_video_never_starts_and_premiere_is_retained():
    value = player()
    value["player"]["videoDetails"]["videoId"] = "other000001"
    assert web.video_state(VIDEO, value).status == "unknown"
    value = player("upcoming")
    value["player"]["videoDetails"]["isLiveContent"] = False
    assert web.video_state(VIDEO, value).premiere
    value = player()
    value["player"]["playabilityStatus"]["status"] = "LOGIN_REQUIRED"
    assert web.video_state(VIDEO, value).status != "live"


@pytest.mark.parametrize("value", [None, {}, feed(complete=False), feed(channels=[{}]), {"error":"auth"}])
def test_invalid_and_incomplete_subscriptions_are_not_empty_success(value):
    with pytest.raises(YouTubeError):
        web.subscriptions(value)
    assert web.subscriptions(feed(channels=[])) == (ACCOUNT, [])


class Reader(QObject):
    def __init__(self):
        super().__init__()
        self.requests = []
        self.value = feed()
        self.video_value = player()
        self.hold = False

    @Slot(object)
    def read_browser(self, request):
        self.requests.append((request, QThread.currentThread()))
        if not self.hold:
            request.value = self.video_value if "/watch?v=" in request.expression else self.value
            request.done.set()


def test_production_browser_path_skips_oauth_preserves_selection_and_resumes_notice(production_backend, monkeypatch, qapp):
    from yt_rec.backend import source as module
    def forbidden(*_args, **_kwargs):
        pytest.fail("browser login must not construct OAuth/token storage")
    monkeypatch.setattr(module, "GoogleAuth", forbidden)
    monkeypatch.setattr(module, "default_token_store", forbidden)
    bridge, reader = web.BrowserYouTube(), Reader()
    bridge.bind(reader)
    source, api, engines, selected, seen, events = production_backend(browser=bridge)
    selected.save([CHANNEL])
    source.start()
    flush(source, qapp)
    assert not source._controller._connected
    assert source.receive_notification(notice(), trusted=True)
    flush(source, qapp)
    assert source._notifications.pending_video_ids == (VIDEO,)
    source.browser_changed(True)
    until(qapp, lambda: VIDEO in engines)
    assert source._controller._connected and selected.load() == (CHANNEL,)
    assert not api.get_calls and not api.find_calls and source._poll_timer is None
    assert all(thread is qapp.thread() for _, thread in reader.requests)
    before = len(reader.requests)
    for _ in range(4):
        source.tick()
    flush(source, qapp)
    assert len(reader.requests) == before
    # Losing/replacing the browser session must not stop a running engine.
    source.browser_changed(False)
    flush(source, qapp)
    assert engines[VIDEO].actions.empty() and selected.load() == (CHANNEL,)
    assert not source._controller._connected
    reader.value = {"error": "auth"}
    source.browser_changed(True)
    flush(source, qapp)
    assert not source._controller._connected
    assert source._controller._subs[0].selected
    assert engines[VIDEO].actions.empty()
    reader.value = feed()
    source.browser_changed(True)
    flush(source, qapp)
    assert source._controller._connected
    assert any(isinstance(event, ev.AccountChanged) for event in events)
    bridge.close()


def test_account_change_releases_waiter_and_late_reply_cannot_connect(production_backend, qapp):
    bridge, reader = web.BrowserYouTube(), Reader()
    reader.hold = True
    bridge.bind(reader)
    source, _, _, _, _, _ = production_backend(browser=bridge)
    source.start()
    source.browser_changed(True)
    until(qapp, lambda: reader.requests)
    old = reader.requests[0][0]
    source.browser_changed(False)
    old.value = feed()
    old.done.set()
    flush(source, qapp)
    assert not bridge.available and not source._controller._connected
    bridge.close()


def test_bridge_refuses_gui_blocking_and_bounds_silent_receiver(qapp, monkeypatch):
    bridge = web.BrowserYouTube()
    with pytest.raises(RuntimeError):
        bridge.snapshot()
    monkeypatch.setattr(web, "TIMEOUT", .02)
    result = []
    def run():
        try:
            bridge.snapshot()
        except YouTubeError as error:
            result.append(error.kind)
    thread = threading.Thread(target=run)
    thread.start()
    until(qapp, lambda: not thread.is_alive())
    thread.join()
    assert result == ["network"] and bridge._pending == []
    bridge.close()


def test_refresh_and_logout_reachable_without_connection(state, stub):
    assert state.connection is ConnectionState.DISCONNECTED
    assert state.send_command(cmd.RefreshSubscriptions())
    assert state.send_command(cmd.DisconnectAccount())


def test_normal_app_auto_login_logout_and_expired_watch_restore_without_oauth(production_backend, monkeypatch, qapp, window_settings):
    from yt_rec import app as application
    from yt_rec.backend import source as module
    from yt_rec.backend.notifications import LiveNotification
    import time
    def forbidden(*_args, **_kwargs):
        pytest.fail("OAuth must not run")
    monkeypatch.setattr(module, "GoogleAuth", forbidden)
    monkeypatch.setattr(module, "default_token_store", forbidden)
    captured = []
    def factory(**kwargs):
        values = production_backend(**kwargs)
        values[3].save([CHANNEL])
        captured.append(values)
        return values[0]
    class Receiver(Reader):
        notification_received = Signal(object)
        notification_arrived = Signal(object)
        status_changed = Signal(str, str)
        browser_changed = Signal(bool)
        def __init__(self, parent=None):
            super().__init__()
            self.setParent(parent)
            self.opens = 0
        def start(self):
            self.status_changed.emit("ready", "Push readiness does not prove login")
        def open_browser(self):
            self.opens += 1
        def open_logout(self):
            self.value = {"error":"auth"}
            self.browser_changed.emit(False)
            self.browser_changed.emit(True)
        def stop(self):
            pass
    monkeypatch.setattr(application, "create_backend_source", factory)
    monkeypatch.setattr(application, "notification_receiver_class", lambda _: Receiver)
    context = application.build_application(["--emit-interval-ms", "0"], settings=window_settings)
    source, _, engines, selected, _, _ = captured[0]
    receiver = context.notifications.receiver
    try:
        flush(source, qapp)
        assert context.state.connection is ConnectionState.DISCONNECTED
        context.state.connect_account()
        assert receiver.opens == 1
        assert not receiver.requests  # Canceled login never reports success.
        receiver.browser_changed.emit(True)  # Existing signed-in startup / login completion.
        until(qapp, lambda: context.state.connection is ConnectionState.CONNECTED)
        assert context.state.subscriptions[0].selected and selected.load() == (CHANNEL,)
        context.state.connect_account()
        assert receiver.opens == 2  # Account switch is usable while connected.
        receiver.video_value = {"error":"auth"}
        receiver.value = {"error":"auth"}
        receiver.notification_received.emit(LiveNotification(VIDEO, time.time(), synthetic=False))
        until(qapp, lambda: context.state.connection is ConnectionState.DISCONNECTED)
        until(qapp, lambda: VIDEO in source._notifications.pending_video_ids)
        assert not engines
        receiver.value, receiver.video_value = feed(), player()
        receiver.browser_changed.emit(True)
        until(qapp, lambda: VIDEO in engines)
        context.state.disconnect_account()
        until(qapp, lambda: context.state.connection is ConnectionState.DISCONNECTED)
        assert engines[VIDEO].actions.empty() and selected.load() == (CHANNEL,)
    finally:
        context.notifications.stop()
        context.window.close()


def page_document(content, *, account="private-fixture-only", logged_out=False):
    data = {"responseContext":{"mainAppWebResponseContext":{"datasyncId":account,"loggedOut":logged_out}},
            "contents":{"twoColumnBrowseResultsRenderer":content}}
    # Earlier non-object bootstrap call reproduces the real page's parser trap.
    return '<script>ytcfg.set(other);function x(){}</script><script>ytcfg.set(' + json.dumps({
        "INNERTUBE_CONTEXT":{"client":{"clientName":"WEB","clientVersion":"1"}}, "SESSION_INDEX":"0"
    }) + ');</script><script>var ytInitialData = ' + json.dumps(data) + ';</script>'


def channel_node(index):
    return {"channelRenderer":{"channelId":"UC" + str(index).zfill(22), "title":{"simpleText":f"channel {index}"}}}


def continuation(token):
    return {"continuationItemRenderer":{"continuationEndpoint":{"continuationCommand":{"token":token}}}}


def continuation_page(items, account="private-fixture-only"):
    return {"responseContext":{"mainAppWebResponseContext":{"loggedOut":False,"datasyncId":account}},
            "onResponseReceivedActions":[{"appendContinuationItemsAction":{"continuationItems":items}}]}


@pytest.fixture
def run_script(qapp):
    """Run the exact async production JS in off-record Chromium with only fake fetch."""
    from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QTimer
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
    profile = QWebEngineProfile()
    class Page(QWebEnginePage):
        def javaScriptConsoleMessage(self, _level, message, _line, _source):
            if message.startswith("RESULT:"):
                self.result = json.loads(message[7:])
                self.loop.quit()
    page = Page(profile)
    def run(responses):
        page.result = None
        page.loop = QEventLoop()
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(page.loop.quit)
        script = """(() => {
            const location = {origin:'https://www.youtube.com'};
            const document = {cookie:'SAPISID=synthetic-only'};
            const crypto = {subtle:{digest:async () => new Uint8Array(32).buffer}};
            const calls = [], replies = RESPONSES;
            const fetch = async (url, options) => {
              calls.push(url); const value = replies.shift();
              if (value === undefined) throw new Error('extra fetch');
              return {ok:true,url:location.origin + url,text:async () => value,json:async () => value};
            };
            SCRIPT.then(value => console.log('RESULT:' + JSON.stringify({value:JSON.parse(value), calls})));
        })();""".replace("RESPONSES", json.dumps(responses)).replace("SCRIPT", web.expression())
        def loaded(_ok):
            page.runJavaScript(script)
        page.loadFinished.connect(loaded)
        page.setHtml("<html><body>synthetic only</body></html>")
        timer.start(5000)
        page.loop.exec()
        page.loadFinished.disconnect(loaded)
        assert page.result is not None
        return page.result
    yield run
    page.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    profile.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_script_reads_all_pages_without_exporting_account_or_continuation(run_script):
    first = page_document([channel_node(1), continuation("private-continuation")])
    second = continuation_page([channel_node(2)])
    result = run_script([first, second, first])
    assert result["value"]["complete"] and len(result["value"]["channels"]) == 2
    assert len(result["calls"]) == 3
    assert "private-" not in json.dumps(result)


@pytest.mark.parametrize("case", ["empty", "malformed", "loggedout", "changed", "repeated", "error-message", "page-account-change"])
def test_script_distinguishes_empty_auth_failure_account_switch_and_incomplete(run_script, case):
    initial = page_document([channel_node(1)])
    if case == "empty":
        empty = page_document([{"messageRenderer":{"text":{"simpleText":"No subscriptions"}}}])
        response = run_script([empty, empty])["value"]
        assert response["complete"] and response["channels"] == []
        return
    if case == "malformed":
        replies, expected = [page_document([])], "format"
    elif case == "loggedout":
        replies, expected = [page_document([], logged_out=True)], "auth"
    elif case == "changed":
        replies, expected = [initial, page_document([channel_node(1)], account="other")], "changed"
    elif case == "error-message":
        replies, expected = [page_document([{"messageRenderer":{"text":{"simpleText":"An error occurred. Please try again later."}}}])], "format"
    elif case == "page-account-change":
        initial = page_document([channel_node(1), continuation("next")])
        replies, expected = [initial, continuation_page([channel_node(2)], account="other")], "changed"
    else:
        initial = page_document([channel_node(1), continuation("same")])
        repeated = continuation_page([continuation("same")])
        replies, expected = [initial, repeated], "incomplete"
    assert run_script(replies)["value"] == {"error":expected}
