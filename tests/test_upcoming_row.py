"""감시 중 채널 줄의 예약 라이브 한 줄 (#108). 시계를 주입해 단계를 고정한다.

시각은 모두 로컬 벽시계로 만든다. 화면이 로컬로 옮겨 그리므로 어느 시간대의
기계에서 돌려도 `19:00` 이 그대로 나온다.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from yt_rec.state import events as ev
from yt_rec.state.models import UpcomingLive, WatchedChannel
from yt_rec.ui.dashboard import ChannelRow, Dashboard

START = datetime(2026, 9, 27, 19, 0).astimezone()
#: 백엔드가 정한 창 길이를 흉내 낸다. 화면은 이 값에서 `60분` 을 잰다.
WINDOW = timedelta(hours=1)


def upcoming(video_id="upcoming001", *, at=START, title="오늘 밤 정규 방송", channel_id="UC1", window=WINDOW):
    return UpcomingLive(video_id, channel_id, title, at, checks_from=at - window)


def row_at(moment, *items):
    row = ChannelRow(clock=lambda: moment)
    row.update_from(WatchedChannel("UC1", "채널"))
    row.set_upcoming(items)
    return row


@pytest.mark.parametrize("moment, phase", [
    (START - timedelta(hours=2), "60분 전부터 확인"),
    (START - WINDOW, "확인 중"),
    (START - timedelta(minutes=30), "확인 중"),
    (START, "시작 대기"),
    (START + timedelta(minutes=5), "시작 대기"),
])
def test_phase_follows_the_local_clock(qapp, moment, phase):
    row = row_at(moment, upcoming())
    assert row.upcoming_label.text() == f"19:00 예정 · {phase}  ·  오늘 밤 정규 방송"
    assert not row.upcoming_label.isHidden()
    assert row.upcoming_more_label.isHidden()


def test_window_length_comes_from_the_data_not_the_screen(qapp):
    row = row_at(START - timedelta(hours=3), upcoming(window=timedelta(minutes=90)))
    assert "19:00 예정 · 90분 전부터 확인" in row.upcoming_label.text()


def test_another_day_shows_the_date(qapp):
    tomorrow = datetime(2026, 9, 28, 19, 0).astimezone()
    row = row_at(START + timedelta(hours=2), upcoming(at=tomorrow))
    assert row.upcoming_label.text().startswith("09-28 19:00 예정 · 60분 전부터 확인")


def test_only_the_earliest_is_drawn_and_the_rest_are_counted(qapp):
    row = row_at(
        START - timedelta(minutes=30),
        upcoming(),
        upcoming("upcoming002", at=START + timedelta(hours=3), title="나중 방송"),
        upcoming("upcoming003", at=START + timedelta(days=1), title="내일 방송"),
    )
    assert row.upcoming_label.text() == "19:00 예정 · 확인 중  ·  오늘 밤 정규 방송"
    assert row.upcoming_more_label.text() == "외 2건"
    assert not row.upcoming_more_label.isHidden()


def test_phase_is_redrawn_by_the_countdown_repaint(qapp):
    clock = [START - timedelta(hours=2)]
    row = ChannelRow(clock=lambda: clock[0])
    row.update_from(WatchedChannel("UC1", "채널"))
    row.set_upcoming((upcoming(),))
    assert "60분 전부터 확인" in row.upcoming_label.text()
    clock[0] = START - timedelta(minutes=10)
    row.refresh_countdown()  # 대시보드의 1 초 재렌더링이 부르는 곳
    assert "확인 중" in row.upcoming_label.text()


def test_row_without_schedule_keeps_its_shape(qapp):
    row = row_at(START)
    assert row.upcoming_label.isHidden() and row.upcoming_more_label.isHidden()
    plain = row.sizeHint().height()
    row.set_upcoming((upcoming(), upcoming("upcoming002")))
    assert row.sizeHint().height() > plain
    row.set_upcoming(())
    assert row.upcoming_label.isHidden() and row.upcoming_more_label.isHidden()
    assert row.sizeHint().height() == plain


def test_dashboard_attaches_schedules_to_their_channel_rows(state):
    dashboard = Dashboard(state)
    later = upcoming("upcoming002", at=START + timedelta(hours=3), title="나중 방송")
    unknown = upcoming("upcoming009", channel_id="", title="채널 모름")
    # 예약이 채널 목록보다 먼저 와도, 순서가 뒤섞여 와도 된다.
    state.apply(ev.SchedulesChanged((later, unknown, upcoming())))
    state.apply(ev.ChannelsChanged((WatchedChannel("UC1", "채널"), WatchedChannel("UC2", "다른 채널"))))
    assert set(state.schedules) == {"UC1"}  # 채널 ID 가 빈 예약은 어느 줄에도 붙지 않는다

    rows = dashboard.channel_rows()
    assert "오늘 밤 정규 방송" in rows["UC1"].upcoming_label.text()
    assert rows["UC1"].upcoming_more_label.text() == "외 1건"
    assert rows["UC2"].upcoming_label.isHidden()

    state.apply(ev.SchedulesChanged(()))
    assert rows["UC1"].upcoming_label.isHidden() and rows["UC1"].upcoming_more_label.isHidden()
    dashboard.deleteLater()
