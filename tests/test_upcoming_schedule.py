"""예약 라이브(`upcoming`) 처리. 가짜 시계만 쓰고 실제로 자지 않는다 (#82).

라이브 시작 순간에는 푸시가 오지 않는다. 30 분 전 예고 푸시가 유일한 기회이므로
그 한 번으로 예정 시각을 적어 두고, 방송자가 예고보다 일찍 켜는 경우를 위해
*알림 받은 시점부터* 창 안에서, 늦게 켜는 경우를 위해 예정 시각 이후에, 각각
유한하게 다시 확인한다 (#103).
"""

from __future__ import annotations

import threading

import pytest

from yt_rec.backend.notifications import LiveNotification, NotificationRecorder
from yt_rec.backend.schedule import (
    EARLY_CHECK_WINDOW_SECONDS,
    MAX_EARLY_CHECKS,
    MAX_RECHECKS,
    MAX_SCHEDULE_AHEAD_SECONDS,
    MAX_WAIT_SECONDS,
    RECHECK_INTERVAL_SECONDS,
    FileScheduleStore,
    MemoryScheduleStore,
    ScheduledLive,
    ScheduleWaker,
)
from yt_rec.backend.selection import MemorySeenStore, MemorySelectionStore
from yt_rec.backend.youtube import LiveBroadcast, VideoState, YouTubeApi

from backend_fakes import FakeResponse, ScriptedSession

VIDEO = "notify00001"
START = 1_000_000.0
#: 예고 알림 수신 시각. 실측 21 분 40 초 전이고 창(60 분) 안이다.
NOTICE_AT = START - 1300.0
#: 알림 직후 첫 이른 확인. 알림 자체가 한 번 물었으므로 1 분 뒤다.
FIRST_EARLY = NOTICE_AT + RECHECK_INTERVAL_SECONDS


class Clock:
    def __init__(self, now: float = START - 600.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Api:
    """videos.list 한 번이 돌려주는 상태만 흉내 낸다."""

    def __init__(self, state: VideoState | None = None) -> None:
        self.calls: list[str] = []
        self.state = state if state is not None else VideoState(VIDEO, "none")

    def live(self) -> None:
        self.state = VideoState(VIDEO, "live", LiveBroadcast(VIDEO, "UC1", "방송 중"))

    def upcoming(self, scheduled_at: float, *, premiere: bool = False) -> None:
        self.state = VideoState(
            VIDEO, "upcoming", LiveBroadcast(VIDEO, "UC1", "예약 라이브"), scheduled_at, premiere
        )

    def get_video_state(self, video_id: str) -> VideoState:
        self.calls.append(video_id)
        return self.state


class Recorder:
    def __init__(self) -> None:
        self.active: set[str] = set()
        self.calls: list[str] = []

    def is_recording(self, video_id: str) -> bool:
        return video_id in self.active

    def start(self, video_id: str, **metadata: object) -> bool:
        self.active.add(video_id)
        self.calls.append(video_id)
        return True


def build(*, clock=None, store=None, premieres=False, seen=None):
    clock = clock if clock is not None else Clock()
    api, recorder = Api(), Recorder()
    updates: list = []
    wakes: list[int] = []
    handler = NotificationRecorder(
        youtube=lambda: api,
        selection=MemorySelectionStore(["UC1"]),
        recorder=recorder,
        seen=seen if seen is not None else MemorySeenStore(),
        on_update=updates.append,
        clock=clock,
        schedules=store,
        record_premieres=lambda: premieres,
        on_schedule=lambda: wakes.append(1),
    )
    return handler, api, recorder, clock, updates, wakes


def notice(at: float | None = None) -> LiveNotification:
    return LiveNotification(VIDEO, NOTICE_AT if at is None else at)


def spent_early() -> MemoryScheduleStore:
    """이른 확인 예산을 다 쓴 예약 하나. 예정 시각 이후 몫만 남아 있다."""
    return MemoryScheduleStore([ScheduledLive(VIDEO, START, NOTICE_AT, 0, False, MAX_EARLY_CHECKS)])


# ----------------------------------------------------------------------
# 2번: 한 번의 videos.list 가 가르는 세 갈래
# ----------------------------------------------------------------------
def test_live_branch_records_now():
    handler, api, recorder, _, _, _ = build()
    api.live()
    assert handler.receive(notice()).status == "handed"
    assert recorder.calls == [VIDEO]
    assert api.calls == [VIDEO]
    assert handler.scheduled_video_ids == ()


def test_none_branch_is_ignored_silently_and_never_scheduled():
    handler, api, recorder, _, updates, _ = build()
    result = handler.receive(notice())
    assert result.status == "ignored" and result.reason
    assert recorder.calls == []
    assert handler.scheduled_video_ids == ()
    assert handler.next_schedule_at() is None
    assert [update.status for update in updates] == ["ignored"]


def test_the_scheduled_reason_tells_the_truth_about_when_it_checks():
    """사유가 동작과 어긋나면 안 된다. 창이 생기기 전 문구는 "예정 시각부터" 였다.

    epoch 를 그대로 내보내면 로그에서 읽을 수 없으므로 로컬 시각으로 적는다.
    """
    from yt_rec.backend.notifications import _local_time
    from yt_rec.backend.schedule import MAX_EARLY_CHECKS, MAX_RECHECKS

    handler, api, recorder, clock, updates, wakes = build()
    api.upcoming(START)
    reason = handler.receive(notice()).reason

    assert _local_time(START) in reason
    assert str(int(START)) not in reason  # epoch 가 새어 나가면 안 된다
    assert f"{MAX_EARLY_CHECKS}회" in reason and f"{MAX_RECHECKS}회" in reason


def test_upcoming_branch_schedules_without_recording():
    handler, api, recorder, clock, updates, wakes = build()
    api.upcoming(START)
    result = handler.receive(notice())
    assert result.status == "scheduled"
    assert recorder.calls == []
    assert handler.scheduled_video_ids == (VIDEO,)
    assert handler.next_schedule_at() == FIRST_EARLY
    assert wakes == [1]  # 대기자에게 새 예약을 알렸다
    assert [update.status for update in updates] == ["scheduled"]


def test_schedule_uses_the_api_value_not_the_notification_body():
    """본문 문구("30분 후에")는 반올림·현지화된 값이라 보지 않는다."""
    store = MemoryScheduleStore()
    handler, api, _, _, _, _ = build(store=store)
    api.upcoming(START + 1304.0)  # 실측: 문구 30분, 실제 21분 44초
    handler.receive(notice(at=START))
    assert store.load()[0].scheduled_at == START + 1304.0


# ----------------------------------------------------------------------
# 3번: 창이 열리기 전에는 요청이 0 회
# ----------------------------------------------------------------------
def test_waiting_until_the_window_opens_issues_no_api_request():
    """먼 예약은 창이 열릴 때까지 잔다. 30 일 뒤 예약까지 매분 물으면 한도가 녹는다."""
    handler, api, recorder, clock, _, _ = build(clock=Clock(START - 4 * 60 * 60.0))
    api.upcoming(START)
    handler.receive(notice(at=clock.now))
    assert api.calls == [VIDEO]
    api.calls.clear()

    opens = START - EARLY_CHECK_WINDOW_SECONDS
    assert handler.next_schedule_at() == opens
    moment = clock.now
    while moment < opens:
        clock.now = moment
        assert handler.check_schedules() == ()
        moment += 30.0
    assert api.calls == []
    assert recorder.calls == []
    assert handler.scheduled_video_ids == (VIDEO,)

    clock.now = opens
    api.live()
    assert handler.check_schedules() == (VIDEO,)
    assert api.calls == [VIDEO]
    assert recorder.calls == [VIDEO]
    assert handler.scheduled_video_ids == ()


def test_notice_inside_the_window_checks_from_the_notice_not_the_start_time():
    """실측(#103): 예정 11:00 인데 10:57:44 에 켰다. 기다렸으면 2 분 16 초를 놓친다."""
    handler, api, recorder, clock, _, _ = build()
    api.upcoming(START)
    handler.receive(notice())
    api.calls.clear()
    assert handler.next_schedule_at() == FIRST_EARLY

    clock.now = FIRST_EARLY
    assert handler.check_schedules() == (VIDEO,)  # 예정 시각 전인데 확인했다
    assert handler.next_schedule_at() == FIRST_EARLY + RECHECK_INTERVAL_SECONDS
    assert handler.check_schedules() == ()  # 같은 분 안에서는 늘지 않는다
    assert api.calls == [VIDEO]
    assert recorder.calls == []


def test_early_start_is_recorded_before_the_scheduled_time():
    handler, api, recorder, clock, _, _ = build()
    api.upcoming(START)
    handler.receive(notice())
    clock.now = START - 136.0  # 실측: 예고보다 2 분 16 초 일찍 켰다
    api.live()
    assert handler.check_schedules() == (VIDEO,)
    assert recorder.calls == [VIDEO]
    assert handler.scheduled_video_ids == ()


def test_duplicate_push_while_scheduled_issues_no_request():
    handler, api, _, _, _, _ = build()
    api.upcoming(START)
    handler.receive(notice())
    api.calls.clear()
    again = handler.receive(notice(at=START - 100.0))
    assert again.status == "scheduled"
    assert api.calls == []


def test_construction_and_idle_checks_start_no_thread(monkeypatch):
    monkeypatch.setattr(
        threading.Thread, "start",
        lambda self: pytest.fail("예약 처리는 스스로 스레드를 띄우지 않는다"),
    )
    store = MemoryScheduleStore([ScheduledLive(VIDEO, START + 4 * 60 * 60.0, NOTICE_AT)])
    handler, api, _, _, _, _ = build(store=store)
    for _ in range(50):
        handler.check_schedules()
    assert api.calls == []


# ----------------------------------------------------------------------
# 4번: 1 분 간격. 예산은 이른 확인 60 회와 예정 시각 이후 30 회로 따로 센다
# ----------------------------------------------------------------------
def test_rechecks_once_a_minute_and_gives_up_at_the_cap():
    """이른 확인을 다 쓴 예약. 예정 시각 이후 몫 30 회는 그대로 남아 있다."""
    handler, api, recorder, clock, updates, _ = build(store=spent_early())
    api.upcoming(START)

    for index in range(MAX_RECHECKS):
        clock.now = START + index * RECHECK_INTERVAL_SECONDS
        assert handler.check_schedules() == (VIDEO,), index
        # 확인 직후에는 다음 만기가 정확히 1 분 뒤다.
        assert handler.next_schedule_at() == clock.now + RECHECK_INTERVAL_SECONDS
        # 같은 분 안에서 다시 불러도 요청이 늘지 않는다.
        assert handler.check_schedules() == ()
    assert len(api.calls) == MAX_RECHECKS
    assert recorder.calls == []

    clock.now = START + MAX_RECHECKS * RECHECK_INTERVAL_SECONDS
    assert handler.check_schedules() == ()
    assert len(api.calls) == MAX_RECHECKS  # 포기는 요청을 더 쓰지 않는다
    assert handler.scheduled_video_ids == ()
    assert handler.next_schedule_at() is None
    assert updates[-1].status == "expired" and updates[-1].reason


def test_late_start_within_the_cap_is_recorded():
    handler, api, recorder, clock, _, _ = build(store=spent_early())
    api.upcoming(START)
    clock.now = START
    handler.check_schedules()
    clock.now = START + RECHECK_INTERVAL_SECONDS
    api.live()
    assert handler.check_schedules() == (VIDEO,)
    assert recorder.calls == [VIDEO]
    assert handler.scheduled_video_ids == ()


def test_early_checks_spend_their_own_budget_and_leave_the_rechecks_intact():
    """창을 통째로 써도 예정 시각 이후 30 회가 남는다. 예산을 따로 센다."""
    store = MemoryScheduleStore()
    handler, api, recorder, clock, updates, _ = build(
        clock=Clock(START - 2 * 60 * 60.0), store=store
    )
    api.upcoming(START)
    handler.receive(notice(at=clock.now))
    api.calls.clear()

    opens = START - EARLY_CHECK_WINDOW_SECONDS
    for index in range(MAX_EARLY_CHECKS):
        clock.now = opens + index * RECHECK_INTERVAL_SECONDS
        assert handler.check_schedules() == (VIDEO,), index
    assert len(api.calls) == MAX_EARLY_CHECKS
    assert store.load()[0].attempts == 0  # 예정 시각 이후 몫은 손대지 않았다
    assert handler.next_schedule_at() == START  # 이른 확인은 창에서 끝난다

    for index in range(MAX_RECHECKS):
        clock.now = START + index * RECHECK_INTERVAL_SECONDS
        assert handler.check_schedules() == (VIDEO,), index
    assert recorder.calls == []
    clock.now = START + MAX_RECHECKS * RECHECK_INTERVAL_SECONDS
    assert handler.check_schedules() == ()
    assert handler.scheduled_video_ids == ()
    assert updates[-1].status == "expired"
    # 예약 1 건의 최악 비용: 알림 1 + 이른 확인 60 + 재확인 30 (하루 한도 10,000).
    assert 1 + len(api.calls) == 91


def test_moved_scheduled_time_restarts_both_budgets():
    store = MemoryScheduleStore()
    handler, api, _, clock, _, _ = build(store=store)
    api.upcoming(START)
    handler.receive(notice())
    clock.now = FIRST_EARLY
    handler.check_schedules()
    assert store.load()[0].early_attempts == 1

    api.upcoming(START + 3600.0)  # 방송자가 예정 시각을 미뤘다
    clock.now = FIRST_EARLY + RECHECK_INTERVAL_SECONDS
    handler.check_schedules()
    entry = store.load()[0]
    assert entry.scheduled_at == START + 3600.0
    assert (entry.attempts, entry.early_attempts) == (0, 0)
    assert handler.next_schedule_at() == START  # 밀린 예정 시각의 창이 열리는 때


def test_unchanged_scheduled_time_keeps_the_spent_counts():
    store = MemoryScheduleStore()
    handler, api, _, clock, _, _ = build(store=store)
    api.upcoming(START)
    handler.receive(notice())
    for index in range(3):
        clock.now = FIRST_EARLY + index * RECHECK_INTERVAL_SECONDS
        handler.check_schedules()
    assert store.load()[0].early_attempts == 3  # 같은 예정 시각이면 이어 센다


def test_schedule_is_dropped_when_the_video_turns_out_not_to_be_a_live():
    handler, api, _, clock, _, _ = build()
    api.upcoming(START)
    handler.receive(notice())
    clock.now = START
    api.state = VideoState(VIDEO, "ended", LiveBroadcast(VIDEO, "UC1", "끝난 방송"))
    handler.check_schedules()
    assert handler.scheduled_video_ids == ()


# ----------------------------------------------------------------------
# 예약 보관: 재시작과 이미 지난 예정 시각
# ----------------------------------------------------------------------
def test_schedule_survives_a_restart(tmp_path):
    path = tmp_path / "upcoming_lives.json"
    handler, api, _, _, _, _ = build(store=FileScheduleStore(path))
    api.upcoming(START)
    handler.receive(notice())
    assert path.exists()

    restarted, restarted_api, recorder, clock, _, _ = build(store=FileScheduleStore(path))
    assert restarted.scheduled_video_ids == (VIDEO,)
    assert restarted.next_schedule_at() == FIRST_EARLY
    assert restarted_api.calls == []  # 복원만으로는 요청하지 않는다

    clock.now = START
    restarted_api.live()
    assert restarted.check_schedules() == (VIDEO,)
    assert recorder.calls == [VIDEO]
    assert FileScheduleStore(path).load() == ()


def test_restored_schedule_whose_time_already_passed_rechecks_immediately(tmp_path):
    path = tmp_path / "upcoming_lives.json"
    FileScheduleStore(path).save([ScheduledLive(VIDEO, START, NOTICE_AT)])
    clock = Clock(START + 300.0)  # 예정 시각이 5 분 지난 뒤 앱이 켜졌다
    handler, api, recorder, _, _, _ = build(clock=clock, store=FileScheduleStore(path))
    assert handler.next_schedule_at() <= clock.now  # 이미 만기다
    waker = ScheduleWaker(due_at=handler.next_schedule_at, on_due=handler.check_schedules, clock=clock)
    assert waker.next_wait() == 0.0  # 기다리지 않고 바로 확인한다

    api.live()
    assert handler.check_schedules() == (VIDEO,)
    assert recorder.calls == [VIDEO]


def test_unreadable_schedule_file_does_not_break_startup(tmp_path):
    path = tmp_path / "upcoming_lives.json"
    path.write_text('{"schedules": [{"video_id": "bad"}, ' + '{"video_id": "notify00001", '
                    '"scheduled_at": 1000000.0, "received_at": 999000.0}]}', encoding="utf-8")
    assert FileScheduleStore(path).load() == (ScheduledLive(VIDEO, START, 999000.0),)


def test_old_format_schedule_file_still_loads(tmp_path):
    """이른 확인이 생기기 전에 저장된 파일. 없는 필드는 0 으로 본다."""
    path = tmp_path / "upcoming_lives.json"
    path.write_text('{"schedules": [{"video_id": "notify00001", "scheduled_at": 1000000.0, '
                    '"received_at": 999000.0, "attempts": 3, "synthetic": false}]}', encoding="utf-8")
    entry = FileScheduleStore(path).load()[0]
    assert entry.early_attempts == 0
    assert entry.attempts == 3  # 예정 시각 이후 몫은 읽은 그대로다
    assert entry.due_at() == 999000.0 + RECHECK_INTERVAL_SECONDS


def test_early_checks_missed_while_the_app_was_off_are_not_paid_for():
    """창 앞부분을 통째로 놓쳤다고 지난 1 분마다 요청을 하나씩 태우지 않는다."""
    store = MemoryScheduleStore([ScheduledLive(VIDEO, START, START - 3 * 60 * 60.0)])
    clock = Clock(START - 300.0)  # 창이 열린 지 55 분 만에 앱이 켜졌다
    handler, api, _, _, _, _ = build(clock=clock, store=store)
    api.upcoming(START)
    assert handler.check_schedules() == (VIDEO,)
    assert len(api.calls) == 1
    assert handler.next_schedule_at() == clock.now + RECHECK_INTERVAL_SECONDS
    assert handler.check_schedules() == ()


# ----------------------------------------------------------------------
# 프리미어와 먼 미래 예약
# ----------------------------------------------------------------------
def test_premiere_is_ignored_by_default_and_recorded_when_enabled():
    handler, api, _, _, updates, _ = build()
    api.upcoming(START, premiere=True)
    assert handler.receive(notice()).status == "ignored"
    assert "프리미어" in updates[-1].reason
    assert handler.scheduled_video_ids == ()

    enabled, enabled_api, _, _, _, _ = build(premieres=True)
    enabled_api.upcoming(START, premiere=True)
    assert enabled.receive(notice()).status == "scheduled"
    assert enabled.scheduled_video_ids == (VIDEO,)


def test_far_future_schedule_is_refused():
    handler, api, _, clock, updates, _ = build()
    api.upcoming(clock.now + MAX_SCHEDULE_AHEAD_SECONDS + 1.0)
    assert handler.receive(notice()).status == "ignored"
    assert handler.scheduled_video_ids == ()
    assert updates[-1].reason


def test_waker_never_holds_a_timer_longer_than_a_day_and_sleeps_when_empty():
    clock = Clock(START)
    due: list[float | None] = [None]
    fired: list[float] = []
    waits: list[float | None] = []
    waker = ScheduleWaker(
        due_at=lambda: due[0], on_due=lambda: fired.append(clock.now),
        clock=clock, wait=waits.append,
    )
    assert waker.next_wait() is None  # 예약이 없으면 무기한 잔다
    due[0] = clock.now + 90.0
    assert waker.next_wait() == 90.0
    due[0] = clock.now + 3 * 24 * 60 * 60.0  # 사흘 뒤 예약
    assert waker.next_wait() == MAX_WAIT_SECONDS
    due[0] = clock.now - 10.0
    assert waker.next_wait() == 0.0

    assert waker.run_once() is True
    assert waits == [0.0] and fired == [clock.now]
    waker.request_stop()
    assert waker.run_once() is False
    assert fired == [clock.now]  # 종료 요청 뒤에는 알리지 않는다


# ----------------------------------------------------------------------
# API 가 네 상태를 가른다 (같은 요청, quota 1 단위)
# ----------------------------------------------------------------------
def _item(**changes: object) -> dict:
    item = {
        "id": VIDEO,
        "snippet": {"channelId": "UC1", "title": "예약", "liveBroadcastContent": "upcoming"},
        "liveStreamingDetails": {"scheduledStartTime": "2026-09-21T09:06:44Z"},
        "contentDetails": {"duration": "P0D"},
    }
    item.update(changes)  # type: ignore[arg-type]
    return item


@pytest.mark.parametrize(
    "item,status,scheduled",
    [
        (_item(), "upcoming", 1_789_981_604.0),
        (_item(snippet={"channelId": "UC1", "title": "방송", "liveBroadcastContent": "live"},
               liveStreamingDetails={"actualStartTime": "2026-09-21T09:06:44Z"}), "live", None),
        (_item(snippet={"channelId": "UC1", "title": "업로드", "liveBroadcastContent": "none"},
               liveStreamingDetails={}), "none", None),
        (_item(liveStreamingDetails={"scheduledStartTime": "2026-09-21T09:06:44Z",
                                     "actualEndTime": "2026-09-21T11:00:00Z"}), "ended", None),
        (_item(liveStreamingDetails={}), "unknown", None),
    ],
)
def test_one_request_tells_the_four_states_apart(item, status, scheduled):
    session = ScriptedSession({"videos": [FakeResponse(200, {"items": [item]})]})
    api = YouTubeApi(session)
    state = api.get_video_state(VIDEO)
    assert state.status == status
    assert state.scheduled_start == scheduled
    assert len(session.calls) == 1
    assert session.calls[0][1] == {
        "part": "snippet,liveStreamingDetails,contentDetails", "id": VIDEO,
    }
    assert api.quota_used == 1  # get_live 와 같은 1 단위. part 추가는 비용이 없다.


@pytest.mark.parametrize(
    "duration,premiere", [("P0D", False), ("PT0S", False), (None, False), ("PT21M44S", True)]
)
def test_premiere_is_guessed_from_duration_only(duration, premiere):
    details = {} if duration is None else {"duration": duration}
    session = ScriptedSession({"videos": [FakeResponse(200, {"items": [_item(contentDetails=details)]})]})
    state = YouTubeApi(session).get_video_state(VIDEO)
    assert state.status == "upcoming"
    assert state.premiere is premiere


def test_missing_video_is_unknown_not_a_crash():
    session = ScriptedSession({"videos": [FakeResponse(200, {"items": []})]})
    state = YouTubeApi(session).get_video_state(VIDEO)
    assert state.status == "unknown" and state.broadcast is None
