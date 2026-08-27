"""메인 창 검증 (이슈 #6), 그리고 이벤트 → 상태 → 화면 경로 (이슈 #7)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QMessageBox

from yt_rec.state import commands as cmd
from yt_rec.state import events as ev
from yt_rec.state.models import (
    CompletedRecording,
    CompletionStatus,
    ConnectionState,
    LogEntry,
    QuotaStatus,
    Recording,
    RecordingState,
    Severity,
    WatchedChannel,
    WatchState,
)
from yt_rec.state.store import AppState
from yt_rec.state.stub import EMOJI_TITLE, LONG_TITLE, StubEventSource
from yt_rec.ui.dashboard import (
    EMPTY_CHANNELS,
    EMPTY_CHANNELS_CONNECTING,
    EMPTY_CHANNELS_DISCONNECTED,
    EMPTY_COMPLETED,
    EMPTY_RECORDING,
    SECTION_CHANNELS,
    SECTION_COMPLETED,
    SECTION_RECORDING,
)
from yt_rec.ui.formatting import now
from yt_rec.ui.main_window import (
    MIN_WINDOW_WIDTH,
    STATUS_ERRORS_SAMPLE,
    STATUS_NEXT_CHECK_SAMPLE,
    MainWindow,
)
from yt_rec.ui.settings_store import WindowSettings
from yt_rec.ui.widgets import ELLIPSIS, can_elide, drawn_text


def make_window(state: AppState, settings: WindowSettings) -> MainWindow:
    window = MainWindow(state, settings=settings)
    window.show()
    QApplication.processEvents()
    return window


# ----------------------------------------------------------------------
# 백엔드 없이 기동
# ----------------------------------------------------------------------
def test_백엔드_없이도_창이_뜨고_연결_안_됨을_표시한다(
    state: AppState, window_settings: WindowSettings
) -> None:
    """이벤트 소스를 붙이지 않은 채로도 크래시 없이 떠야 한다."""
    window = make_window(state, window_settings)
    assert window.isVisible()
    assert window.watch_badge.text() == "연결 안 됨"
    assert window.watch_badge.kind() == "error"
    assert window.next_check_label.text() == "다음 확인 —"
    assert window.error_label.text() == "오류 0건"
    window.close()


def test_빈_상태_안내_문구가_섹션마다_표시된다(
    state: AppState, window_settings: WindowSettings
) -> None:
    window = make_window(state, window_settings)
    dash = window.dashboard
    assert dash.recording_empty.isVisible()
    assert dash.completed_empty.isVisible()
    assert dash.recording_empty.text() == EMPTY_RECORDING
    assert dash.completed_empty.text() == EMPTY_COMPLETED
    # 소스를 붙이지 않았으니 이 창은 `연결 안 됨` 이다. 채널 섹션도 상단 배지와
    # 같은 원인을 말해야 한다. `채널 관리에서 선택하세요` 는 이 상태에서 틀린
    # 조치다 — 채널을 골라도 백엔드가 없으면 아무 일도 일어나지 않는다.
    assert dash.channels_empty.isVisible()
    assert dash.channels_empty.text() == EMPTY_CHANNELS_DISCONNECTED
    window.close()


def test_감시가_중지되면_사유가_안내에_붙는다(
    state: AppState, stub: StubEventSource, window_settings: WindowSettings
) -> None:
    window = make_window(state, window_settings)
    stub.load_preset("empty")
    QApplication.processEvents()
    assert window.watch_badge.text() == "중지됨"
    empty_text = window.dashboard.channels_empty.text()
    assert "감시할 채널이 선택되지 않았습니다" in empty_text
    # 연결된 상태에서는 채널을 고르는 것이 실제로 조치가 된다.
    assert "채널 관리" in empty_text
    window.close()


def test_비연결_안내가_채널_안내로_덮이지_않는다(
    state: AppState, window_settings: WindowSettings
) -> None:
    """연결이 끊기면 채널 섹션도 그 원인을 말해야 한다.

    실측 회귀: 연결이 끊기면 저장소가 감시 요약을 ``UNKNOWN`` 으로 되돌리고
    ``connection`` 과 ``watch`` 를 연달아 방출한다. 나중에 도착하는 감시 통지가
    비연결 안내를 `채널 관리에서 채널을 선택하세요` 로 덮어써서, 백엔드가
    없는데 채널을 고르라는 엉뚱한 조치가 표시됐다.
    """
    window = make_window(state, window_settings)
    state.apply(ev.ConnectionChanged(ConnectionState.CONNECTED))
    state.apply(ev.WatchStatusChanged(WatchState.WATCHING, channel_count=0))
    QApplication.processEvents()
    assert window.dashboard.channels_empty.text() == EMPTY_CHANNELS

    state.apply(ev.ConnectionChanged(ConnectionState.DISCONNECTED))
    QApplication.processEvents()
    assert window.watch_badge.text() == "연결 안 됨"
    assert window.dashboard.channels_empty.text() == EMPTY_CHANNELS_DISCONNECTED

    # 감시 통지가 한 번 더 늦게 도착해도 원인이 지워지지 않아야 한다.
    state.apply(ev.WatchStatusChanged(WatchState.UNKNOWN))
    QApplication.processEvents()
    assert window.dashboard.channels_empty.text() == EMPTY_CHANNELS_DISCONNECTED
    window.close()


def test_다시_연결되면_비연결_안내가_사라진다(
    state: AppState, window_settings: WindowSettings
) -> None:
    """원인이 해소됐는데 안내가 남아 있으면 그것도 틀린 표시다."""
    window = make_window(state, window_settings)
    assert window.dashboard.channels_empty.text() == EMPTY_CHANNELS_DISCONNECTED

    state.apply(ev.ConnectionChanged(ConnectionState.CONNECTED))
    QApplication.processEvents()
    assert window.dashboard.channels_empty.text() == EMPTY_CHANNELS
    window.close()


# ----------------------------------------------------------------------
# 스텁 주입 → 상태 → 화면
# ----------------------------------------------------------------------
def test_스텁_주입이_화면에_그대로_반영된다(
    state: AppState, stub: StubEventSource, window_settings: WindowSettings
) -> None:
    window = make_window(state, window_settings)
    stub.load_preset("populated")
    QApplication.processEvents()

    dash = window.dashboard
    assert len(dash.recording_rows()) == 3
    assert len(dash.channel_rows()) == 3
    assert len(dash.completed_rows()) == 4
    assert not dash.recording_empty.isVisible()
    assert not dash.channels_empty.isVisible()
    assert not dash.completed_empty.isVisible()

    assert window.watch_badge.text() == "감시 중 3채널"
    assert window.watch_badge.kind() == "ok"
    assert window.quota_label.text() == "quota 3,120 / 10,000"
    assert window.error_label.text() == "오류 1건 (새 1건)"
    assert window.next_check_label.text().startswith("다음 확인 ")
    window.close()


def test_진행_이벤트가_카드_문구를_갱신한다(
    state: AppState, stub: StubEventSource, window_settings: WindowSettings
) -> None:
    window = make_window(state, window_settings)
    stub.emit_event(ev.ConnectionChanged(ConnectionState.CONNECTED))
    stub.emit_event(
        ev.RecordingStarted(
            Recording(
                recording_id="r1",
                title="테스트 라이브",
                channel_name="테스트 채널",
                quality="1080p60",
                state=RecordingState.RECORDING,
                started_at=now(),
            )
        )
    )
    QApplication.processEvents()
    row = window.dashboard.recording_rows()["r1"]
    assert row.title_label.text() == "테스트 라이브"
    assert row.badge.text() == "녹화 중"

    stub.emit_event(
        ev.RecordingProgress(
            recording_id="r1",
            reported_bytes=1_239_500_000,
            reported_elapsed=timedelta(hours=1, minutes=22, seconds=3),
        )
    )
    QApplication.processEvents()
    meta = row.meta_label.text()
    # 표시 값은 녹화 프로세스가 보고한 값에서 나온다.
    assert "1.2 GB" in meta
    assert "1:22:03" in meta
    assert "1080p60" in meta

    stub.emit_event(
        ev.RecordingProgress(
            recording_id="r1",
            reported_bytes=1_300_000_000,
            reported_elapsed=timedelta(hours=1, minutes=25),
            state=RecordingState.STALLED,
            detail="3분째 진행 보고 없음",
        )
    )
    QApplication.processEvents()
    assert row.badge.text() == "응답 없음"
    assert row.badge.kind() == "error"
    window.close()


def test_중지_확인_후_해당_녹화만_멈추라고_요청한다(
    state: AppState,
    stub: StubEventSource,
    window_settings: WindowSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        QMessageBox, "question", lambda *args, **kwargs: QMessageBox.StandardButton.Yes
    )
    window = make_window(state, window_settings)
    stub.emit_event(ev.ConnectionChanged(ConnectionState.CONNECTED))
    stub.emit_event(
        ev.RecordingStarted(
            Recording(
                recording_id="r1",
                title="테스트 라이브",
                state=RecordingState.RECORDING,
                started_at=now(),
            )
        )
    )
    QApplication.processEvents()
    received: list[object] = []
    state.command_requested.connect(received.append)
    window.dashboard.recording_rows()["r1"].stop_button.click()
    QApplication.processEvents()
    assert received == [cmd.StopRecording("r1", reason="사용자가 중지했습니다")]
    # 명령은 요청일 뿐. 카드는 아직 남아 있다.
    assert "r1" in window.dashboard.recording_rows()
    window.close()


def test_완료_이벤트가_카드를_최근_완료로_옮긴다(
    state: AppState, stub: StubEventSource, window_settings: WindowSettings
) -> None:
    window = make_window(state, window_settings)
    stub.emit_event(ev.RecordingStarted(Recording(recording_id="r1", title="t")))
    QApplication.processEvents()
    assert "r1" in window.dashboard.recording_rows()

    stub.emit_event(
        ev.RecordingFinished(
            CompletedRecording(
                recording_id="r1",
                title="t",
                status=CompletionStatus.PARTIAL,
                note="마지막 8초 누락",
                finished_at=now(),
                duration=timedelta(minutes=3),
                total_bytes=120_000_000,
            )
        )
    )
    QApplication.processEvents()
    assert window.dashboard.recording_rows() == {}
    done_row = window.dashboard.completed_rows()["r1"]
    assert done_row.badge.text() == "부분 복구"
    assert "마지막 8초 누락" in done_row.meta_label.text()
    window.close()


def test_오류_배지는_로그를_열면_해제된다(
    state: AppState, stub: StubEventSource, window_settings: WindowSettings
) -> None:
    window = make_window(state, window_settings)
    stub.emit_event(
        ev.LogAppended(LogEntry(at=now(), severity=Severity.ERROR, message="실패"))
    )
    QApplication.processEvents()
    assert window.error_label.text() == "오류 1건 (새 1건)"

    dialog = window.open_logs()
    QApplication.processEvents()
    assert dialog.isVisible()
    assert window.error_label.text() == "오류 1건"
    dialog.close()
    window.close()


# ----------------------------------------------------------------------
# 섹션 접힘·창 기하 복원
# ----------------------------------------------------------------------
def test_섹션을_접고_펼_수_있다(state: AppState, window_settings: WindowSettings) -> None:
    window = make_window(state, window_settings)
    section = window.dashboard.recording_section
    assert not section.is_collapsed()
    section.toggle()
    QApplication.processEvents()
    assert section.is_collapsed()
    assert not section._content.isVisible()
    section.toggle()
    QApplication.processEvents()
    assert not section.is_collapsed()
    window.close()


def test_접힘_상태가_재실행_후_복원된다(
    state: AppState, window_settings: WindowSettings
) -> None:
    window = make_window(state, window_settings)
    window.dashboard.recording_section.set_collapsed(True)
    window.dashboard.completed_section.set_collapsed(True)
    window.close()  # closeEvent가 저장한다

    assert window_settings.section_collapsed(SECTION_RECORDING) is True
    assert window_settings.section_collapsed(SECTION_CHANNELS) is False
    assert window_settings.section_collapsed(SECTION_COMPLETED) is True

    second = make_window(AppState(emit_interval_ms=0), window_settings)
    assert second.dashboard.recording_section.is_collapsed()
    assert not second.dashboard.channels_section.is_collapsed()
    assert second.dashboard.completed_section.is_collapsed()
    second.close()


def test_창_크기와_위치가_저장되고_복원된다(
    state: AppState, window_settings: WindowSettings
) -> None:
    window = make_window(state, window_settings)
    window.resize(640, 480)
    window.move(120, 90)
    QApplication.processEvents()
    window.close()

    assert window_settings.geometry() is not None

    second = make_window(AppState(emit_interval_ms=0), window_settings)
    assert second.size().width() == 640
    assert second.size().height() == 480
    second.close()


# ----------------------------------------------------------------------
# 진입점
# ----------------------------------------------------------------------
def test_모든_진입점이_열린다(state: AppState, window_settings: WindowSettings) -> None:
    window = make_window(state, window_settings)
    openers = (
        window.open_channels,
        window.open_archive,
        window.open_settings,
        window.open_logs,
        window.open_account,
    )
    for opener in openers:
        dialog = opener()
        QApplication.processEvents()
        assert dialog.isVisible(), f"{opener.__name__} 이 열리지 않았다"
        assert dialog.windowTitle()
        dialog.close()
    assert len(window.child_windows) == 0
    window.close()


def test_대시보드_버튼이_해당_화면을_연다(
    state: AppState, window_settings: WindowSettings
) -> None:
    window = make_window(state, window_settings)
    window.dashboard.manage_channels_button.click()
    QApplication.processEvents()
    assert "ChannelsDialog" in window.child_windows

    window.dashboard.open_archive_button.click()
    QApplication.processEvents()
    assert "ArchiveDialog" in window.child_windows

    for dialog in list(window.child_windows.values()):
        dialog.close()
    window.close()


# ----------------------------------------------------------------------
# 좁은 폭 / 긴 문자열 / 이모지
# ----------------------------------------------------------------------
def test_좁은_폭에서_가로_스크롤이_생기지_않는다(
    state: AppState, stub: StubEventSource, window_settings: WindowSettings
) -> None:
    window = make_window(state, window_settings)
    stub.load_preset("populated")
    QApplication.processEvents()

    assert (
        window.scroll_area.horizontalScrollBarPolicy()
        is Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )
    window.resize(window.minimumWidth(), 480)
    QApplication.processEvents()
    assert not window.scroll_area.horizontalScrollBar().isVisible()
    # 본문이 뷰포트보다 넓어지지 않아야 한다.
    assert window.dashboard.minimumSizeHint().width() <= window.scroll_area.viewport().width()
    window.close()


def test_최소_너비에서_어떤_상태_문구도_잘리지_않는다(
    state: AppState, stub: StubEventSource, window_settings: WindowSettings
) -> None:
    """배지는 짧은 문구라 말줄임이 안 된다. 찌그러지면 글자가 잘린다.

    실측 회귀: 창 최소 너비를 상단 바 요구치보다 작게 강제했더니
    `감시 중 3채널` 배지가 `시 중 3채` 로 잘렸다.
    """
    window = make_window(state, window_settings)
    stub.load_preset("populated")
    QApplication.processEvents()

    window.resize(window.minimumWidth(), 640)
    QApplication.processEvents()

    badges = [window.watch_badge]
    badges += [row.badge for row in window.dashboard.recording_rows().values()]
    badges += [row.badge for row in window.dashboard.completed_rows().values()]
    for badge in badges:
        assert badge.width() >= badge.sizeHint().width(), (
            f"배지 {badge.text()!r} 가 {badge.sizeHint().width()}px 중 "
            f"{badge.width()}px 만 받아 잘린다"
        )
    assert not window.scroll_area.horizontalScrollBar().isVisible()
    window.close()


def test_창_최소_너비가_상단_바_요구치를_밑돌지_않는다(
    state: AppState, window_settings: WindowSettings
) -> None:
    window = make_window(state, window_settings)
    assert window.minimumWidth() >= window.top_bar.minimumSizeHint().width()
    assert window.minimumWidth() >= MIN_WINDOW_WIDTH
    window.close()


def test_긴_제목은_말줄임_처리되고_원문은_보존된다(
    state: AppState, stub: StubEventSource, window_settings: WindowSettings
) -> None:
    window = make_window(state, window_settings)
    stub.load_preset("populated")
    QApplication.processEvents()

    row = window.dashboard.recording_rows()["rec-1"]
    assert row.title_label.text() == LONG_TITLE
    assert row.title_label.toolTip() == LONG_TITLE

    elided = row.title_label.elided_text(width=180)
    assert elided != LONG_TITLE
    assert elided.endswith("…")
    assert len(elided) < len(LONG_TITLE)
    window.close()


def test_이모지가_포함된_제목이_깨지지_않는다(
    state: AppState, stub: StubEventSource, window_settings: WindowSettings
) -> None:
    window = make_window(state, window_settings)
    stub.load_preset("populated")
    QApplication.processEvents()

    row = window.dashboard.recording_rows()["rec-2"]
    assert row.title_label.text() == EMOJI_TITLE
    assert "🔴" in row.title_label.text()
    assert "🥳" in row.title_label.text()

    channel_row = window.dashboard.channel_rows()["UC0000000000000000000002"]
    assert channel_row.name_label.text().startswith("🎧")
    window.close()


# ----------------------------------------------------------------------
# 남은 시간 재렌더링
# ----------------------------------------------------------------------
def test_남은_시간_표시는_받아_둔_시각으로만_다시_그린다(
    state: AppState, stub: StubEventSource, window_settings: WindowSettings
) -> None:
    window = make_window(state, window_settings)
    deadline = now() + timedelta(minutes=2, seconds=30)
    stub.emit_event(ev.ConnectionChanged(ConnectionState.CONNECTED))
    stub.emit_event(
        ev.WatchStatusChanged(
            state=WatchState.WATCHING, channel_count=1, next_check_at=deadline
        )
    )
    stub.emit_event(
        ev.ChannelsChanged(
            (WatchedChannel(channel_id="c1", name="채널", next_check_at=deadline),)
        )
    )
    QApplication.processEvents()

    assert "후" in window.next_check_label.text()
    row = window.dashboard.channel_rows()["c1"]
    assert "후" in row.countdown_label.text()

    # 재렌더링을 직접 불러도 상태 조회 없이 문자열만 다시 만들어진다.
    before = row.countdown_label.text()
    window._repaint_countdowns()
    assert row.countdown_label.text().endswith("후")
    assert before.endswith("후")
    window.close()


def test_창을_닫고_다시_열면_카운트다운이_되살아난다(
    state: AppState, window_settings: WindowSettings
) -> None:
    """``closeEvent`` 가 타이머를 멈추므로 짝이 되는 ``showEvent`` 가 필요하다.

    실측 회귀: 되살리는 곳이 없어 창을 닫고 다시 열면 카운트다운이 그 값에
    얼어붙었다.
    """
    window = make_window(state, window_settings)
    assert window._countdown_repaint_timer.isActive()

    window.close()
    QApplication.processEvents()
    assert not window._countdown_repaint_timer.isActive()

    window.show()
    QApplication.processEvents()
    assert window._countdown_repaint_timer.isActive(), (
        "다시 열었는데 카운트다운 재렌더링이 멈춰 있다"
    )
    window.close()


# ----------------------------------------------------------------------
# 좁은 폭에서 화면에 그려지는 문자열
# ----------------------------------------------------------------------
def _labels_with_text(window: MainWindow) -> list[QLabel]:
    return [
        label
        for label in window.findChildren(QLabel)
        if label.text() and label.isVisible()
    ]


def _silent_cuts(window: MainWindow) -> list[str]:
    """원문과 다르게 그려지면서 잘렸다는 표시조차 없는 라벨을 모은다.

    위젯 종류로 대상을 고르지 않는다. 창 안의 모든 :class:`QLabel` 을 훑어
    **실제로 화면에 나타나는 문자열**을 원문과 비교한다. 자유 서식 제목이
    ``…`` 로 줄어드는 것은 #6 이 인정한 동작이지만, 표시 없이 글자가 사라지는
    것은 사용자가 잘렸다는 사실조차 모른 채 값을 읽게 되므로 다른 문제다.
    """
    problems: list[str] = []
    for label in _labels_with_text(window):
        text = label.text()
        drawn = drawn_text(label)
        if drawn == text:
            continue
        if can_elide(label) and drawn.endswith(ELLIPSIS):
            continue
        problems.append(
            f"{type(label).__name__}#{label.objectName()}: "
            f"{text!r} 가 {drawn!r} 로 그려진다"
        )
    return problems


def test_창을_좁혀도_어떤_문구도_조용히_잘리지_않는다(
    state: AppState, stub: StubEventSource, window_settings: WindowSettings
) -> None:
    """실측 회귀: quota 백만대·오류 네 자리에서 상태 표시줄이 창보다 넓어졌다.

    ``오류 1234건 (새 1234건)`` 이 ``오류 123`` 으로 잘렸고(말줄임이 아니라 숫자
    절단이라 **틀린 값**이 표시됐다), offscreen 환경에서는 오류 라벨이 0px 만
    보였다. 최소 너비를 상단 바와 대시보드만 보고 계산한 뒤 ``setMinimumSize``
    로 덮어써서 실제 레이아웃 요구를 무시한 결과였다.

    이 검사는 배지 같은 특정 위젯 종류를 순회하지 않는다. 창 안의 라벨 전체를
    훑어 `그려지는 문자열이 원문과 다른데 그 사실이 화면에 드러나지 않는` 경우를
    잡는다.
    """
    window = make_window(state, window_settings)
    stub.load_preset("populated")
    # 장시간 구동에서 실제로 도달하는 크기의 값을 넣는다.
    stub.emit_event(ev.QuotaChanged(QuotaStatus(used=1_234_567, limit=10_000_000)))
    # 누적 오류를 1234건까지 올린다. 로그 1234줄을 넣는 것과 표시 결과가 같으므로
    # 표시 경로만 직접 부른다.
    window._on_errors(1234, 1234)
    QApplication.processEvents()
    assert window.error_label.text() == "오류 1234건 (새 1234건)"

    for width in (window.minimumWidth(), window.minimumWidth() + 60, 640, 760):
        window.resize(width, 640)
        QApplication.processEvents()
        problems = _silent_cuts(window)
        assert not problems, f"창 {window.width()}px 에서 조용히 잘린 문구: {problems}"
        assert not window.scroll_area.horizontalScrollBar().isVisible()
    window.close()


def test_최소_너비가_상태_표시줄을_담는다(
    state: AppState, window_settings: WindowSettings
) -> None:
    """상태 표시줄이 창보다 넓으면 라벨이 창 밖으로 밀려 사라진다.

    실측: 최장 문구에서 상태 표시줄이 674px 를 요구하는데 창 최소 너비가 376px
    이라 오류 라벨이 0px 만 보였다.
    """
    window = make_window(state, window_settings)
    # 최소 너비를 결정하는 것은 상태 표시줄이므로 최장 문구에서 확인한다.
    window.next_check_label.setText(STATUS_NEXT_CHECK_SAMPLE)
    window.quota_label.setText("quota 1,234,567 / 10,000,000")
    window.error_label.setText(STATUS_ERRORS_SAMPLE)
    window.resize(window.minimumWidth(), 640)
    QApplication.processEvents()

    status = window.statusBar()
    assert status.width() <= window.width(), (
        f"상태 표시줄이 {status.width()}px 로 창 {window.width()}px 를 넘는다"
    )
    assert window.minimumWidth() >= status.minimumSizeHint().width()
    window.close()


def test_최소_너비에서_다음_확인과_오류_건수는_말줄임되지_않는다(
    state: AppState, window_settings: WindowSettings
) -> None:
    """수치가 담긴 두 칸은 최장 문구까지 자리를 확보해 둔다.

    quota 칸까지 최장 문구를 확보하면 창 최소 너비가 674px 로 올라가 사용자가
    창을 그보다 좁게 쓸 수 없다. 그래서 quota 만 말줄임을 허용하고, 대신
    말줄임됐다는 사실이 화면에 드러난다(위의 조용한 잘림 검사).
    """
    window = make_window(state, window_settings)
    window.next_check_label.setText(STATUS_NEXT_CHECK_SAMPLE)
    window.error_label.setText(STATUS_ERRORS_SAMPLE)
    window.resize(window.minimumWidth(), 640)
    QApplication.processEvents()

    for label in (window.next_check_label, window.error_label):
        assert drawn_text(label) == label.text(), (
            f"{label.objectName()} 가 {drawn_text(label)!r} 로 잘렸다"
        )
    window.close()


def test_감시_배지_문구가_길어져도_창_크기가_흔들리지_않는다(
    state: AppState, window_settings: WindowSettings
) -> None:
    """실측 회귀: 백엔드가 죽은 상태(376px)로 저장하고 재실행 후 연결되면 398px 로
    튀어 **복원된 창 크기가 무효화됐다**(#6 수용 기준 위반).

    최소 너비가 표시 내용에 따라 커지면 Qt 가 창을 그만큼 넓힌다. 여기서는
    최소 너비에 붙여 둔 창이 배지 문구가 길어져도 그 자리에 있는지 본다.
    `언제 다시 계산하는가` 자체는
    ``test_constraints.py::test_최소_크기를_다시_잡는_곳이_생성과_표시_두_곳뿐이다``
    가 지킨다.
    """
    window = make_window(state, window_settings)
    baseline = window.minimumWidth()
    window.resize(baseline, 500)
    QApplication.processEvents()
    assert window.width() == baseline

    state.apply(ev.ConnectionChanged(ConnectionState.CONNECTED))
    state.apply(ev.WatchStatusChanged(WatchState.WATCHING, channel_count=999))
    QApplication.processEvents()
    assert window.watch_badge.text() == "감시 중 999채널"
    assert window.minimumWidth() == baseline, (
        f"배지 문구가 길어지면서 최소 너비가 {baseline} → {window.minimumWidth()} 로 커졌다"
    )
    assert window.width() == baseline, "창이 스스로 넓어졌다"
    # 그리고 그 최소 너비는 최장 배지 문구를 이미 담고 있다.
    assert drawn_text(window.watch_badge) == window.watch_badge.text()

    state.apply(ev.ConnectionChanged(ConnectionState.DISCONNECTED))
    QApplication.processEvents()
    assert window.minimumWidth() == baseline
    assert window.width() == baseline
    window.close()


def test_복원된_창_크기가_최소_너비에_밀리지_않는다(
    state: AppState, window_settings: WindowSettings
) -> None:
    """저장할 때와 복원할 때의 최소 너비가 같아야 크기가 그대로 살아난다."""
    window = make_window(state, window_settings)
    saved_width = window.minimumWidth() + 40
    window.resize(saved_width, 500)
    QApplication.processEvents()
    window.close()

    second = make_window(AppState(emit_interval_ms=0), window_settings)
    assert second.minimumWidth() == window.minimumWidth()
    assert second.width() == saved_width
    second.close()


# ----------------------------------------------------------------------
# 연결 중 표시
# ----------------------------------------------------------------------
def test_연결_중을_오류로_단정하지_않는다(
    state: AppState, window_settings: WindowSettings
) -> None:
    """``CONNECTING`` 이 ``is not CONNECTED`` 분기에 묶여 있었다.

    배지 문구는 `연결 중` 인데 색은 빨간 error 였고 상세 라벨은 `백엔드가
    기동되지 않았습니다` 라고 단정했다. 아직 진행 중인 일을 실패로 말하는 셈이다.
    """
    window = make_window(state, window_settings)
    state.apply(ev.ConnectionChanged(ConnectionState.CONNECTING))
    QApplication.processEvents()

    assert window.watch_badge.text() == "연결 중"
    assert window.watch_badge.kind() != "error", "`연결 중` 인데 오류 색이다"
    assert "기동되지 않았습니다" not in window.watch_detail_label.text()
    assert window.dashboard.channels_empty.text() == EMPTY_CHANNELS_CONNECTING

    # 끊긴 것은 여전히 오류로 표시한다.
    state.apply(ev.ConnectionChanged(ConnectionState.DISCONNECTED))
    QApplication.processEvents()
    assert window.watch_badge.kind() == "error"
    assert window.dashboard.channels_empty.text() == EMPTY_CHANNELS_DISCONNECTED
    window.close()


# ----------------------------------------------------------------------
# 하위 화면 수명
# ----------------------------------------------------------------------
def test_다이얼로그를_여닫아도_쌓이지_않는다(
    state: AppState, window_settings: WindowSettings
) -> None:
    """실측 회귀: 5회 개폐 후 고아 ``QDialog`` 자식 5개가 남았다.

    ``child_windows`` 딕셔너리만 보는 검사로는 드러나지 않는다 — 딕셔너리에서는
    빠지지만 위젯은 메인 창의 자식으로 그대로 살아 있었다.
    """
    window = make_window(state, window_settings)
    for _ in range(5):
        dialog = window.open_settings()
        QApplication.processEvents()
        assert dialog.isVisible()
        dialog.close()
        QApplication.processEvents()

    assert window.child_windows == {}
    leaked = window.findChildren(QDialog)
    assert leaked == [], f"고아 다이얼로그 {len(leaked)} 개가 남았다"
    window.close()


def test_같은_화면을_다시_열_수_있다(
    state: AppState, window_settings: WindowSettings
) -> None:
    """닫을 때 위젯을 없애므로, 다시 열 때 새로 만들어져야 한다."""
    window = make_window(state, window_settings)
    first = window.open_channels()
    QApplication.processEvents()
    first.close()
    QApplication.processEvents()

    second = window.open_channels()
    QApplication.processEvents()
    assert second.isVisible()
    assert "ChannelsDialog" in window.child_windows
    second.close()
    QApplication.processEvents()
    window.close()


# ----------------------------------------------------------------------
# 행 구분선
# ----------------------------------------------------------------------
def test_행_구분선이_실제로_그려진다(
    state: AppState, stub: StubEventSource, window_settings: WindowSettings
) -> None:
    """스타일시트에 규칙만 써 두면 평범한 ``QWidget`` 에서는 조용히 무시된다.

    실측: ``QWidget#row`` 의 아래 테두리가 한 픽셀도 그려지지 않았다.
    ``WA_StyledBackground`` 가 없으면 스타일시트의 배경·테두리를 칠하지 않는다.
    """
    window = make_window(state, window_settings)
    stub.load_preset("populated")
    QApplication.processEvents()

    rows = list(window.dashboard.channel_rows().values())
    assert rows
    row = rows[0]
    assert row.testAttribute(Qt.WidgetAttribute.WA_StyledBackground)

    image = row.grab().toImage()
    assert image.width() > 4 and image.height() > 4
    x = image.width() // 2
    middle = image.pixelColor(x, image.height() // 2)
    bottom = image.pixelColor(x, image.height() - 1)
    assert bottom != middle, (
        f"행 아래 테두리가 배경과 같은 색이다 ({bottom.name()} == {middle.name()})"
    )
    window.close()
