"""표시 문자열 계약 검증 — 특히 시간대.

여기 있는 검사는 실행 환경의 로컬 시간대와 무관하게 성립해야 한다. 그래서
`UTC 로 받은 값이 어떤 문자열이 되는가` 를 직접 적지 않고, **같은 순간을 다른
시간대로 받아도 같은 문자열이 되는가** 를 본다. 그것이 계약이기 때문이다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from PySide6.QtWidgets import QApplication

from yt_rec.state import events as ev
from yt_rec.state.models import ConnectionState, WatchedChannel
from yt_rec.state.store import AppState
from yt_rec.ui.formatting import format_countdown, format_timestamp, to_local
from yt_rec.ui.main_window import MainWindow
from yt_rec.ui.settings_store import WindowSettings

KST = timezone(timedelta(hours=9))
UTC = timezone.utc

MOMENT = datetime(2026, 8, 13, 5, 47, 0, tzinfo=UTC)
"""기준 순간 하나. KST 로는 14:47 이다."""


# ----------------------------------------------------------------------
# to_local
# ----------------------------------------------------------------------
def test_to_local은_같은_순간을_유지한다() -> None:
    assert to_local(None) is None
    for value in (MOMENT, MOMENT.astimezone(KST)):
        local = to_local(value)
        assert local is not None
        assert local.tzinfo is not None
        assert local == value, "시간대만 바뀌고 가리키는 순간은 그대로여야 한다"


def test_to_local은_naive를_로컬_벽시계로_해석한다() -> None:
    """계약 위반 입력의 동작을 정해 둔다. 파이썬 ``astimezone()`` 기본 규칙과 같다."""
    naive = datetime(2026, 8, 13, 14, 47)
    local = to_local(naive)
    assert local is not None
    assert local.tzinfo is not None
    # 벽시계 숫자는 그대로 유지된다(시간대만 붙는다).
    assert (local.year, local.month, local.day, local.hour, local.minute) == (
        2026,
        8,
        13,
        14,
        47,
    )


# ----------------------------------------------------------------------
# format_timestamp
# ----------------------------------------------------------------------
def test_같은_순간은_어느_시간대로_받아도_같게_그려진다() -> None:
    """실측 회귀: ``astimezone()`` 없이 ``strftime`` 해서 UTC 값이 9시간 어긋났다.

    백엔드가 ``datetime.now(timezone.utc)`` 를 보내는지
    ``datetime.now().astimezone()`` 을 보내는지에 따라 화면 값이 달라지면
    안 된다.
    """
    assert format_timestamp(MOMENT) == format_timestamp(MOMENT.astimezone(KST))
    assert format_timestamp(MOMENT) == format_timestamp(
        MOMENT.astimezone(timezone(timedelta(hours=-5)))
    )


def test_시각이_로컬_시간대로_그려진다() -> None:
    assert format_timestamp(MOMENT) == MOMENT.astimezone().strftime("%m-%d %H:%M")


def test_시각이_없으면_대시() -> None:
    assert format_timestamp(None) == "—"


# ----------------------------------------------------------------------
# format_countdown
# ----------------------------------------------------------------------
def test_남은_시간도_어느_시간대로_받아도_같다() -> None:
    deadline = MOMENT + timedelta(minutes=5)
    assert format_countdown(deadline, reference=MOMENT) == format_countdown(
        deadline.astimezone(KST), reference=MOMENT
    )
    assert format_countdown(deadline, reference=MOMENT) == "5분 0초 후"


def test_같은_객체의_두_시각이_같은_기준으로_그려진다() -> None:
    """``last_check_at`` 과 ``next_check_at`` 이 서로 다르게 렌더링되면 안 된다.

    실측 회귀: :func:`format_countdown` 은 시간대를 올바르게 다루는데
    :func:`format_timestamp` 는 그렇지 않아, **같은 채널 객체의 두 시각이 서로
    다른 기준으로 그려졌다**.
    """
    utc_channel = WatchedChannel(
        channel_id="c",
        name="n",
        last_check_at=MOMENT - timedelta(minutes=5),
        next_check_at=MOMENT + timedelta(minutes=5),
    )
    kst_channel = WatchedChannel(
        channel_id="c",
        name="n",
        last_check_at=(MOMENT - timedelta(minutes=5)).astimezone(KST),
        next_check_at=(MOMENT + timedelta(minutes=5)).astimezone(KST),
    )
    assert format_timestamp(utc_channel.last_check_at) == format_timestamp(
        kst_channel.last_check_at
    )
    assert format_countdown(
        utc_channel.next_check_at, reference=MOMENT
    ) == format_countdown(kst_channel.next_check_at, reference=MOMENT)

    # 그리고 두 시각의 간격은 두 표현에서 같아야 한다.
    assert format_timestamp(utc_channel.last_check_at) == (
        MOMENT - timedelta(minutes=5)
    ).astimezone().strftime("%m-%d %H:%M")


def test_naive_시각도_timestamp와_countdown이_같은_규칙을_쓴다() -> None:
    """계약 위반 입력의 동작을 고정한다 — 두 함수가 갈라지지 않는 것이 핵심이다."""
    naive_deadline = datetime(2026, 8, 13, 14, 47)
    reference = datetime(2026, 8, 13, 14, 42).astimezone()

    # 로컬 벽시계로 해석하므로 5분 뒤다. `확인 중`(이미 지났다) 이 아니다.
    assert format_countdown(naive_deadline, reference=reference) == "5분 0초 후"
    # 같은 값을 시각으로 그려도 같은 해석이다.
    assert format_timestamp(naive_deadline) == "08-13 14:47"


def test_지난_시각은_확인_중() -> None:
    assert format_countdown(MOMENT - timedelta(seconds=1), reference=MOMENT) == "확인 중"
    assert format_countdown(None) == "—"


# ----------------------------------------------------------------------
# 화면까지 이어지는지
# ----------------------------------------------------------------------
def test_채널_행이_그리는_시각도_시간대에_흔들리지_않는다(
    qapp: QApplication, window_settings: WindowSettings
) -> None:
    """다섯 화면이 모두 시각을 그린다. 계약이 화면까지 이어지는지 확인한다."""
    rendered: list[tuple[str, str]] = []
    for tz in (UTC, KST, timezone(timedelta(hours=-5))):
        state = AppState(emit_interval_ms=0)
        window = MainWindow(state, settings=window_settings)
        window.show()
        state.apply(ev.ConnectionChanged(ConnectionState.CONNECTED))
        state.apply(
            ev.ChannelsChanged(
                (
                    WatchedChannel(
                        channel_id="c1",
                        name="채널",
                        last_check_at=(MOMENT - timedelta(minutes=5)).astimezone(tz),
                        next_check_at=(MOMENT + timedelta(hours=99)).astimezone(tz),
                        last_check_result="라이브 없음",
                    ),
                )
            )
        )
        QApplication.processEvents()
        row = window.dashboard.channel_rows()["c1"]
        rendered.append((row.result_label.text(), row.countdown_label.text()))
        window.close()
        state.deleteLater()

    assert len(set(rendered)) == 1, f"시간대에 따라 화면이 달라진다: {rendered}"
