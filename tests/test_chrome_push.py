"""Chrome 수신기 (#79) 검사.

실제 브라우저를 띄우지 않는다. CDP 대화만 흉내 내어 ``begin_session`` 으로
넣는다. 여기서 확인하는 것은 세 가지다: 신호에서 영상 하나를 정확히 뽑는지,
수신기가 죽었을 때 그 사실이 드러나는지, YouTube 에 주기 조회를 하지 않는지.
"""

from __future__ import annotations

import json
import logging

import pytest
from PySide6.QtCore import QObject, QTimer, Signal

from yt_rec.backend import chrome_push as module
from yt_rec.backend.notification_history import ReceivedNotification
from yt_rec.backend.notifications import LiveNotification
from yt_rec.recording.options import NOTIFICATION_RECEIVERS, RecordingOptions

VIDEO = "sxyDRUJYFYw"
OTHER_VIDEO = "AiZnFPyv-Jc"
WORKER_TARGET = "SW-TARGET"
WORKER_SESSION = "SW-SESSION"
PAGE_TARGET = "PAGE-TARGET"
PAGE_SESSION = "PAGE-SESSION"
SERVICE_WORKER_URL = "https://www.youtube.com/sw.js"
READY = {"permission": "granted", "worker": True, "active": True, "subscription": True}


class FakeProcess(QObject):
    """QProcess 대역. 실행 인자만 붙잡고 아무것도 띄우지 않는다."""

    finished = Signal(int, object)
    errorOccurred = Signal(object)
    instances: list["FakeProcess"] = []

    def __init__(self, parent=None):
        super().__init__(parent)
        self.program = ""
        self.arguments: list[str] = []
        self.started = 0
        FakeProcess.instances.append(self)

    def setProgram(self, program): self.program = program
    def setArguments(self, arguments): self.arguments = list(arguments)
    def start(self): self.started += 1
    def state(self): return module.QProcess.ProcessState.Running
    def terminate(self): pass
    def kill(self): pass
    def waitForFinished(self, _ms): return True
    def deleteLater(self): pass


@pytest.fixture
def chrome(tmp_path):
    executable = tmp_path / "chrome.exe"
    executable.write_bytes(b"")
    return executable


class _ProcessFactory:
    """``module.QProcess`` 자리에 놓을 대역. 열거형은 진짜 것을 그대로 쓴다."""

    ProcessState = module.QProcess.ProcessState

    def __call__(self, parent=None):
        return FakeProcess(parent)


@pytest.fixture
def receiver(qapp, tmp_path, chrome, monkeypatch):
    FakeProcess.instances.clear()
    monkeypatch.setattr(module, "QProcess", _ProcessFactory())
    result = module.ChromePushReceiver(
        executable_finder=lambda: chrome,
        profile_directory=lambda: tmp_path / "profile",
    )
    yield result
    result.close()


class FakeTimers:
    """``module.QTimer`` 자리에 놓을 대역. 예약을 붙잡아 두고 직접 돌린다.

    재시도를 실시간으로 기다리지 않는다. 타이머 객체 자체는 진짜 것을 쓴다.
    """

    def __init__(self):
        self.pending: list[tuple[int, object]] = []

    def __call__(self, parent=None):
        return QTimer(parent)

    def singleShot(self, delay, callback):
        self.pending.append((delay, callback))

    def fire(self) -> int:
        """예약된 것을 한 번씩 돌리고 그 개수를 준다."""
        pending, self.pending = self.pending, []
        for _delay, callback in pending:
            callback()
        return len(pending)


@pytest.fixture
def timers(monkeypatch):
    fake = FakeTimers()
    monkeypatch.setattr(module, "QTimer", fake)
    return fake


class FakeSocket(QObject):
    """QWebSocket 대역. ``_open_socket`` 과 같은 방식으로 연결해 둔다."""

    connected = Signal()
    disconnected = Signal()
    textMessageReceived = Signal(str)

    def __init__(self):
        super().__init__()
        self.sent: list[str] = []
        self.alive = True
        self.aborted = 0

    def isValid(self): return self.alive
    def sendTextMessage(self, text): self.sent.append(text)
    def abort(self): self.aborted += 1
    def flush(self): pass
    def deleteLater(self): pass


def open_session(receiver) -> list[str]:
    websocket = FakeSocket()
    websocket.disconnected.connect(receiver._socket_disconnected)
    receiver._socket = websocket
    receiver.begin_session(websocket.sendTextMessage)
    return websocket.sent


def calls(sent: list[str]) -> list[dict]:
    return [json.loads(text) for text in sent]


def methods(sent: list[str]) -> list[str]:
    return [call["method"] for call in calls(sent)]


def feed(receiver, method: str, params: dict, session: str = "") -> None:
    message: dict[str, object] = {"method": method, "params": params}
    if session:
        message["sessionId"] = session
    receiver._cdp.feed(json.dumps(message))


def attach_worker(receiver, *, session=WORKER_SESSION, target=WORKER_TARGET, waiting=True) -> None:
    feed(receiver, "Target.attachedToTarget", {
        "sessionId": session, "waitingForDebugger": waiting,
        "targetInfo": {"targetId": target, "type": "service_worker", "url": SERVICE_WORKER_URL},
    })


def attach_page(receiver) -> None:
    feed(receiver, "Target.attachedToTarget", {
        "sessionId": PAGE_SESSION, "waitingForDebugger": False,
        "targetInfo": {"targetId": PAGE_TARGET, "type": "page", "url": module.YOUTUBE + "/"},
    })


def binding(receiver, payload: object, *, session=WORKER_SESSION) -> None:
    feed(receiver, "Runtime.bindingCalled",
         {"name": module.BINDING, "payload": payload}, session)


def reply(receiver, call_id: int, value: object) -> None:
    receiver._cdp.feed(json.dumps({
        "id": call_id, "result": {"result": {"type": "string", "value": json.dumps(value)}},
    }))


def failed(receiver, call_id: int) -> None:
    """네비게이션이 커밋되면서 실행 컨텍스트와 함께 날아간 확인."""
    receiver._cdp.feed(json.dumps({"id": call_id, "result": {
        "result": {"type": "object"}, "exceptionDetails": {"text": "Execution context was destroyed."},
    }}))


def last_id(sent: list[str], method: str) -> int:
    return max(call["id"] for call in calls(sent) if call["method"] == method)


# -- 기동과 격리 --------------------------------------------------------------


def test_launch_uses_the_dedicated_profile_and_a_loopback_only_debug_port(receiver, tmp_path, chrome):
    receiver.start()
    assert len(FakeProcess.instances) == 1
    process = FakeProcess.instances[0]
    assert process.program == str(chrome) and process.started == 1
    profile = tmp_path / "profile"
    assert f"--user-data-dir={profile}" in process.arguments
    port = module._read_port(profile / module.PORT_FILE)
    assert port and f"--remote-debugging-port={port}" in process.arguments
    assert f"--remote-allow-origins=http://127.0.0.1:{port}" in process.arguments
    assert module.YOUTUBE in process.arguments


def test_the_receiver_browser_is_headless_so_the_app_never_shows_a_window(receiver):
    """수신은 백그라운드 동작이다. 앱을 켰다고 YouTube 창이 뜨면 안 된다."""
    receiver.start()
    assert "--headless=new" in FakeProcess.instances[0].arguments


@pytest.mark.parametrize("press,url", [
    ("open_browser", module.YOUTUBE),
    ("open_settings", module.NOTIFICATION_SETTINGS_URL),
])
def test_login_and_settings_show_a_real_window_on_the_same_profile(receiver, tmp_path, press, url):
    """헤드리스에서 탭만 만들면 사용자 눈에는 아무 일도 안 일어난다.

    같은 프로필을 보이는 모드로 다시 띄워야 로그인과 푸시 등록이 유지된 채로
    사용자가 직접 조작할 수 있다.
    """
    receiver.start()
    open_session(receiver)
    getattr(receiver, press)()
    assert len(FakeProcess.instances) == 2
    process = FakeProcess.instances[1]
    assert "--headless=new" not in process.arguments
    assert url in process.arguments
    assert f"--user-data-dir={tmp_path / 'profile'}" in process.arguments


def test_a_second_press_while_the_window_is_open_only_adds_a_tab(receiver):
    """창을 닫았다 다시 띄우면 입력하던 로그인이 날아간다."""
    receiver.start()
    open_session(receiver)
    receiver.open_browser()
    sent = open_session(receiver)  # 보이는 브라우저에 붙는다
    sent.clear()
    receiver.open_settings()
    assert len(FakeProcess.instances) == 2
    create = next(call for call in calls(sent) if call["method"] == "Target.createTarget")
    assert create["params"] == {"url": module.NOTIFICATION_SETTINGS_URL}


def test_a_restart_after_a_visible_window_goes_back_to_headless(receiver, monkeypatch):
    """보이는 모드는 눌어붙지 않는다. 창을 닫으면 다시 백그라운드로 돌아간다."""
    receiver.start()
    open_session(receiver)
    receiver.open_browser()
    visible = FakeProcess.instances[-1]
    assert "--headless=new" not in visible.arguments
    # 남은 포트 파일이 가리키는 브라우저는 방금 닫혔으므로 이어받기는 실패한다.
    monkeypatch.setattr(module.ChromePushReceiver, "_resolve",
                        lambda self, *, on_failure: on_failure())
    visible.finished.emit(0, None)  # 사용자가 창을 닫았다
    receiver._restart()
    assert len(FakeProcess.instances) == 3
    assert "--headless=new" in FakeProcess.instances[-1].arguments


def test_a_clean_close_forgets_the_port_so_the_next_run_cannot_adopt_a_stranger(receiver, tmp_path):
    """이어받기는 크래시로 남은 고아 브라우저 전용이다.

    정상 종료 뒤에도 포트가 남아 있으면 다음 실행이 그 포트로 먼저 붙으러 간다.
    그사이 다른 프로세스가 같은 임시 포트를 잡고 우리 오리진까지 허용해 두었다면
    전용 프로필이 아닌 브라우저를 붙잡게 된다.
    """
    receiver.start()
    port_file = tmp_path / "profile" / module.PORT_FILE
    assert module._read_port(port_file)
    receiver.close()
    assert not port_file.exists()
    assert module._read_port(port_file) == 0


def test_profile_is_never_the_personal_chrome_profile():
    path = module.chrome_profile_directory()
    assert path.name == "chrome-push" and path.parent.name == "yt-rec"
    assert "User Data" not in str(path) and "Google" not in str(path)


def test_missing_chrome_is_reported_and_nothing_is_launched(qapp, tmp_path):
    seen: list[tuple[str, str]] = []
    result = module.ChromePushReceiver(
        executable_finder=lambda: None, profile_directory=lambda: tmp_path / "profile",
    )
    result.status_changed.connect(lambda code, detail: seen.append((code, detail)))
    FakeProcess.instances.clear()
    result.start()
    assert [code for code, _ in seen] == ["chrome_missing"]
    assert "Chrome" in seen[0][1]
    assert FakeProcess.instances == []
    result.close()


def test_session_grants_permission_and_arms_service_worker_autoattach(receiver):
    sent = open_session(receiver)
    grant = next(call for call in calls(sent) if call["method"] == "Browser.grantPermissions")
    assert grant["params"] == {"origin": module.YOUTUBE, "permissions": ["notifications"]}
    attach = next(call for call in calls(sent) if call["method"] == "Target.setAutoAttach")
    assert attach["params"]["waitForDebuggerOnStart"] is True
    assert attach["params"]["filter"][0] == {"type": "service_worker", "exclude": False}
    assert "Target.setDiscoverTargets" in methods(sent)
    assert receiver.status[0] == "checking"


# -- 후킹 ---------------------------------------------------------------------


def test_paused_worker_is_hooked_before_it_is_released(receiver):
    sent = open_session(receiver)
    sent.clear()
    attach_worker(receiver, waiting=True)
    order = methods(sent)
    assert order.index("Runtime.addBinding") < order.index("Runtime.evaluate")
    # 이 순서가 핵심이다. 후킹이 끝나기 전에 워커를 깨우면 첫 푸시를 놓친다.
    assert order.index("Runtime.evaluate") < order.index("Runtime.runIfWaitingForDebugger")
    hook = next(call for call in calls(sent) if call["method"] == "Runtime.evaluate")
    assert module.BINDING + "(" in hook["params"]["expression"]
    assert "addEventListener('push'" in hook["params"]["expression"]
    # 멈춘 동안에는 registration 이 null 이라 showNotification 을 감쌀 수 없다.
    # 깨운 뒤 한 번 더 걸어야 푸시가 아닌 경로의 알림까지 잡힌다.
    assert order.count("Runtime.evaluate") == 2
    assert order[-1] == "Runtime.evaluate"


def test_a_stopped_worker_is_released_so_its_next_start_is_hooked_again(receiver):
    sent = open_session(receiver)
    attach_worker(receiver, session=WORKER_SESSION, waiting=True)
    sent.clear()
    # 실측 회귀: 붙어 있는 동안 워커를 다시 켜면 같은 타깃이 되살아날 뿐
    # attachedToTarget 이 오지 않아, 후킹이 사라진 채로 다음 푸시를 맞았다.
    feed(receiver, "Inspector.targetCrashed", {}, WORKER_SESSION)
    detach = next(call for call in calls(sent) if call["method"] == "Target.detachFromTarget")
    assert detach["params"] == {"sessionId": WORKER_SESSION}
    assert receiver._worker_sessions == {}
    live: list[object] = []
    receiver.notification_received.connect(live.append)
    binding(receiver, json.dumps({"kind": "notification", "title": "t",
                                  "options": {"data": {"videoId": VIDEO}}}))
    assert live == []
    sent.clear()
    attach_worker(receiver, session="NEXT", target="NEXT-TARGET", waiting=True)
    assert "Runtime.addBinding" in methods(sent)
    binding(receiver, json.dumps({"kind": "notification", "title": "t",
                                  "options": {"data": {"videoId": VIDEO}}}), session="NEXT")
    assert [item.video_id for item in live] == [VIDEO]


def test_already_running_worker_is_hooked_without_being_released(receiver):
    sent = open_session(receiver)
    sent.clear()
    attach_worker(receiver, waiting=False)
    assert "Runtime.runIfWaitingForDebugger" not in methods(sent)
    assert "Runtime.addBinding" in methods(sent)


def test_a_duplicate_session_is_released_and_dropped(receiver):
    sent = open_session(receiver)
    attach_worker(receiver, session=WORKER_SESSION, waiting=True)
    sent.clear()
    attach_worker(receiver, session="SECOND", waiting=True)
    # 같은 워커에 두 번 붙으면 같은 알림을 두 번 받는다. 하나만 남기되,
    # 멈춰 세운 세션을 그냥 버리면 워커가 영원히 멈추므로 먼저 깨운다.
    assert methods(sent) == ["Runtime.runIfWaitingForDebugger", "Target.detachFromTarget"]
    received: list[object] = []
    receiver.notification_received.connect(received.append)
    binding(receiver, json.dumps({"kind": "notification", "title": "t",
                                  "options": {"data": {"videoId": VIDEO}}}), session="SECOND")
    assert received == []


def test_failed_hook_installation_is_reported(receiver):
    sent = open_session(receiver)
    attach_worker(receiver)
    call_id = last_id(sent, "Runtime.evaluate")
    receiver._cdp.feed(json.dumps({"id": call_id, "result": {
        "result": {"type": "object"}, "exceptionDetails": {"text": "boom"},
    }}))
    assert receiver.status[0] == "error"


# -- 신호에서 영상 뽑기 -------------------------------------------------------


def test_notification_payload_yields_one_video_and_one_history_entry(receiver):
    open_session(receiver)
    attach_worker(receiver)
    live: list[LiveNotification] = []
    history: list[ReceivedNotification] = []
    receiver.notification_received.connect(live.append)
    receiver.notification_arrived.connect(history.append)
    binding(receiver, json.dumps({"kind": "notification", "title": "채널이 라이브 중", "options": {
        "body": "지금 방송 중", "tag": "live",
        "data": {"url": f"https://www.youtube.com/watch?v={VIDEO}"},
    }}))
    assert [item.video_id for item in live] == [VIDEO]
    assert live[0].synthetic is False
    assert [(item.title, item.body, item.synthetic) for item in history] == [
        ("채널이 라이브 중", "지금 방송 중", False)
    ]
    assert receiver.status[0] == "received"


@pytest.mark.parametrize("payload", [
    "not json at all",
    json.dumps([1, 2, 3]),
    json.dumps({"kind": "unknown", "title": "t", "options": {"data": {"videoId": VIDEO}}}),
    json.dumps({"kind": "notification", "title": 42, "options": {"data": {"videoId": VIDEO}}}),
    json.dumps({"kind": "notification", "title": "t", "options": "not an object"}),
    # 두 영상이 섞이면 무엇을 녹화할지 결정할 수 없다. 추측하지 않는다.
    json.dumps({"kind": "notification", "title": "t",
                "options": {"data": {"videoId": VIDEO, "other": {"videoId": OTHER_VIDEO}}}}),
    # 11 자가 아닌 것은 영상 ID 가 아니다.
    json.dumps({"kind": "notification", "title": "t", "options": {"data": {"videoId": "tooshort"}}}),
    # watch 주소인데 영상 ID 자리가 깨졌다. 후킹이 갈렸다는 뜻이므로 오류로 둔다.
    json.dumps({"kind": "notification", "title": "t",
                "options": {"data": {"url": module.YOUTUBE + "/watch?v=bad"}}}),
    pytest.param("x" * (module._MAX_PAYLOAD + 1), id="oversized"),
])
def test_malformed_payloads_never_start_a_recording(receiver, payload):
    open_session(receiver)
    attach_worker(receiver)
    live: list[object] = []
    receiver.notification_received.connect(live.append)
    binding(receiver, payload)
    assert live == []
    assert receiver.status[0] == "error"


def test_a_notification_without_a_video_is_ignored_instead_of_latching_an_error(receiver, caplog):
    """커뮤니티 글·멤버십 알림에는 watch 주소가 없다. 수신기 고장이 아니다 (#94).

    실측 회귀: 그런 알림 하나에 ``error`` 로 바뀐 뒤 다음 알림이 올 때까지 몇
    시간을 그대로 있었다. 그동안 브라우저도 푸시 통로도 멀쩡했다.
    """
    open_session(receiver)
    attach_worker(receiver)
    live: list[object] = []
    history: list[ReceivedNotification] = []
    receiver.notification_received.connect(live.append)
    receiver.notification_arrived.connect(history.append)
    with caplog.at_level(logging.INFO, logger=module.__name__):
        binding(receiver, json.dumps({"kind": "notification", "title": "채널이 글을 올렸습니다",
                                      "options": {"body": "멤버십 전용 안내", "data": {"tag": "post"}}}))
    assert live == []
    # 무엇이 왔는지는 이력이 남긴다. 상태는 쉬는 자리로 돌아간다.
    assert [(item.title, item.body) for item in history] == [("채널이 글을 올렸습니다", "멤버십 전용 안내")]
    assert receiver.status == ("ready", module._RESTING)
    assert "멤버십" not in caplog.text and "채널이 글을 올렸습니다" not in caplog.text
    binding(receiver, json.dumps({"kind": "notification", "title": "t",
                                  "options": {"data": {"videoId": VIDEO}}}))
    assert [item.video_id for item in live] == [VIDEO]


@pytest.mark.parametrize("data, reason", [
    ({"videoId": VIDEO, "other": {"videoId": OTHER_VIDEO}}, "ambiguous"),
    ({"url": module.YOUTUBE + "/watch?v=bad"}, "malformed"),
])
def test_an_undecidable_video_stays_an_error_and_records_which_kind(receiver, caplog, data, reason):
    """영상이 여럿이거나 주소가 깨진 것은 정상적인 알림이 아니다. 사유만 남긴다."""
    open_session(receiver)
    attach_worker(receiver)
    live: list[object] = []
    receiver.notification_received.connect(live.append)
    with caplog.at_level(logging.WARNING, logger=module.__name__):
        binding(receiver, json.dumps({"kind": "notification", "title": "제목",
                                      "options": {"body": "본문", "data": data}}))
    assert live == []
    assert receiver.status == ("error", "알림에서 영상 하나를 식별하지 못해 녹화하지 않습니다")
    assert reason in caplog.text
    assert "제목" not in caplog.text and "본문" not in caplog.text


def test_payload_from_an_unattached_session_is_ignored(receiver):
    open_session(receiver)
    attach_worker(receiver)
    live: list[object] = []
    receiver.notification_received.connect(live.append)
    binding(receiver, json.dumps({"kind": "notification", "title": "t",
                                  "options": {"data": {"videoId": VIDEO}}}), session="STRANGER")
    assert live == []


def test_raw_push_body_records_one_video_and_drops_anything_else(receiver):
    open_session(receiver)
    attach_worker(receiver)
    live: list[LiveNotification] = []
    history: list[object] = []
    receiver.notification_received.connect(live.append)
    receiver.notification_arrived.connect(history.append)
    binding(receiver, json.dumps({"kind": "push", "text": json.dumps({"videoId": VIDEO})}))
    assert [item.video_id for item in live] == [VIDEO]
    # 원본 푸시에는 표시 문구가 없다. 알림 이력에 빈 항목을 남기지 않는다.
    assert history == []
    live.clear()
    before = receiver.status
    binding(receiver, json.dumps({"kind": "push", "text": "not json"}))
    binding(receiver, json.dumps({"kind": "push", "text": None}))
    assert live == [] and receiver.status == before


# -- 상태 점검 ----------------------------------------------------------------


@pytest.mark.parametrize("value,expected", [
    ({"permission": "default", "worker": True, "active": True, "subscription": True}, "permission_required"),
    ({"permission": "denied", "worker": True, "active": True, "subscription": True}, "permission_required"),
    ({"permission": "granted", "worker": False, "active": False, "subscription": False}, "worker_missing"),
    ({"permission": "granted", "worker": True, "active": False, "subscription": False}, "worker_inactive"),
    ({"permission": "granted", "worker": True, "active": True, "subscription": False}, "unsubscribed"),
    (READY, "ready"),
    # 모양이 틀린 답은 답이 아니다. 읽지 못한 것(재확인 대상)과 달리 바로 확정한다.
    ({"permission": "granted", "worker": True, "active": True, "subscription": "yes"}, "error"),
])
def test_registration_status_is_truthful(receiver, value, expected):
    sent = open_session(receiver)
    attach_page(receiver)
    reply(receiver, last_id(sent, "Runtime.evaluate"), value)
    assert receiver.status[0] == expected


# -- 뜨는 중인 페이지 (#88) ---------------------------------------------------


def test_a_check_that_fails_while_the_page_loads_is_retried_instead_of_latched(receiver, timers):
    """갓 띄운 브라우저의 탭은 URL 만 youtube 일 뿐 문서가 아직 커밋되지 않았다.

    실측 회귀: 새로 띄운 브라우저는 2번 중 2번, 붙자마자 보낸 확인이 실패해
    ``error`` 로 눌어붙었다. 사용자가 직접 누르기 전까지 풀리지 않았다.
    """
    sent = open_session(receiver)
    attach_page(receiver)
    reply(receiver, last_id(sent, "Runtime.evaluate"), {"failed": True})
    assert receiver.status[0] == "checking"
    assert timers.fire() == 1
    reply(receiver, last_id(sent, "Runtime.evaluate"), READY)
    assert receiver.status[0] == "ready"
    # 답을 받았으면 끝이다. 다시 묻지 않는다.
    assert timers.fire() == 0


def test_a_check_that_keeps_failing_latches_the_error_and_then_stops(receiver, timers):
    """시간으로 갈린다. 계속 실패하면 정말 문제가 있는 것이다."""
    sent = open_session(receiver)
    attach_page(receiver)
    for _ in range(module._RECHECK_ATTEMPTS):
        failed(receiver, last_id(sent, "Runtime.evaluate"))
        assert receiver.status[0] == "checking"
        assert timers.fire() == 1
    failed(receiver, last_id(sent, "Runtime.evaluate"))
    assert receiver.status == ("error", "수신 브라우저의 알림 상태를 확인하지 못했습니다. 알림 상태 확인으로 다시 확인하세요")
    # 상주 폴링이 아니다. 횟수를 다 쓰면 더 묻지 않는다.
    assert timers.fire() == 0


def test_a_user_initiated_check_after_a_latched_error_gets_a_fresh_budget(receiver, timers):
    """바닥난 횟수가 사용자의 '알림 상태 확인'까지 삼키면 안 된다."""
    sent = open_session(receiver)
    attach_page(receiver)
    for _ in range(module._RECHECK_ATTEMPTS + 1):
        failed(receiver, last_id(sent, "Runtime.evaluate"))
        timers.fire()
    assert receiver.status[0] == "error"
    receiver.inspect_registration()
    failed(receiver, last_id(sent, "Runtime.evaluate"))
    assert timers.fire() == 1
    reply(receiver, last_id(sent, "Runtime.evaluate"), READY)
    assert receiver.status[0] == "ready"


@pytest.mark.parametrize("value,expected", [
    ({"permission": "denied", "worker": True, "active": True, "subscription": True}, "permission_required"),
    ({"permission": "granted", "worker": True, "active": True, "subscription": False}, "unsubscribed"),
])
def test_a_conclusive_answer_is_never_retried(receiver, timers, value, expected):
    """``ready`` 가 아니어도 답은 답이다. 사용자가 할 일이지 경쟁이 아니다."""
    sent = open_session(receiver)
    attach_page(receiver)
    reply(receiver, last_id(sent, "Runtime.evaluate"), value)
    assert receiver.status[0] == expected
    assert timers.fire() == 0


def test_an_answer_drops_a_retry_that_is_already_queued(receiver, timers):
    """늦게 온 답이 먼저다. 예약된 재확인은 낡은 것이 된다."""
    sent = open_session(receiver)
    attach_page(receiver)
    first = last_id(sent, "Runtime.evaluate")
    receiver.inspect_registration()  # 확인 두 개가 함께 떠 있다
    failed(receiver, last_id(sent, "Runtime.evaluate"))
    sent.clear()
    reply(receiver, first, READY)
    assert receiver.status[0] == "ready"
    timers.fire()
    assert "Runtime.evaluate" not in methods(sent)


def test_a_tab_that_never_attaches_asks_for_login_only_after_retrying(receiver, timers):
    """탭이 아직 안 붙은 것과 정말 탭이 없는 것은 시간으로만 갈린다."""
    open_session(receiver)
    receiver.inspect_registration()
    assert receiver.status[0] == "checking"
    for _ in range(module._RECHECK_ATTEMPTS):
        assert timers.fire() == 1
    assert receiver.status == ("login_required", "수신 브라우저에 YouTube 탭이 없습니다. 'YouTube 로그인'으로 창을 여세요")
    assert timers.fire() == 0


# -- 살아 있는지 --------------------------------------------------------------


def test_browser_exit_is_reported_and_a_restart_is_scheduled(receiver):
    receiver.start()
    seen: list[str] = []
    receiver.status_changed.connect(lambda code, _detail: seen.append(code))
    receiver._process_finished(1, None)
    assert seen == ["chrome_down"]
    assert receiver._restarts == 1
    receiver._process_error(None)
    assert seen[-1] == "chrome_down" and receiver._restarts == 2


def test_heartbeat_only_pings_the_browser_and_never_youtube(receiver):
    sent = open_session(receiver)
    sent.clear()
    receiver._pulse()
    # 살아 있는지만 묻는다. 영상·채널 상태를 주기적으로 조회하지 않는다.
    assert methods(sent) == ["Browser.getVersion"]
    receiver._cdp.feed(json.dumps({"id": calls(sent)[0]["id"], "result": {}}))
    sent.clear()
    receiver._pulse()
    assert methods(sent) == ["Browser.getVersion"]


def test_a_browser_that_stops_answering_is_not_reported_as_ready(receiver):
    open_session(receiver)
    receiver._pulse()  # 응답을 주지 않는다
    receiver._pulse()
    assert receiver.status[0] == "error"
    assert receiver._restarts == 1


def test_a_dead_socket_is_reported_instead_of_staying_silent(receiver):
    open_session(receiver)
    receiver._status("ready", "알림 수신 준비됨 · 실제 도착 대기 중")
    receiver._socket.alive = False
    receiver._pulse()
    assert receiver.status[0] == "chrome_down"


def test_close_is_final_and_idempotent(receiver):
    open_session(receiver)
    receiver.close()
    assert receiver.status[0] == "stopped"
    live: list[object] = []
    receiver.notification_received.connect(live.append)
    receiver.close()
    receiver.start()
    assert receiver.status[0] == "stopped" and live == []


# -- 설정 ---------------------------------------------------------------------


def test_receiver_option_round_trips_and_rejects_unknown_values(tmp_path):
    options = RecordingOptions(output_dir=tmp_path)
    assert options.notification_receiver == "chrome"
    assert set(NOTIFICATION_RECEIVERS) == {"chrome", "qtwebengine"}
    restored = RecordingOptions.from_dict(options.with_(notification_receiver="qtwebengine").to_dict())
    assert restored.notification_receiver == "qtwebengine"
    with pytest.raises(ValueError):
        options.with_(notification_receiver="edge")


def test_the_qtwebengine_receiver_stays_selectable():
    from yt_rec.app import notification_receiver_class

    assert notification_receiver_class("chrome") is module.ChromePushReceiver
    assert notification_receiver_class("qtwebengine").__name__ == "YouTubePushReceiver"
