from __future__ import annotations

from dataclasses import replace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton, QStyle, QStyleOptionViewItem

from yt_rec.state import commands as cmd
from yt_rec.state import events as ev
from yt_rec.state.models import ConnectionState, StopReason, Subscription, WatchState
from yt_rec.state.store import AppState
from yt_rec.ui.account import AccountDialog
from yt_rec.ui.channels import ChannelsDialog, SubscriptionListModel


@pytest.mark.parametrize("value, selected", [
    (Qt.CheckState.Checked, True), (Qt.CheckState.Checked.value, True), (True, True),
    (Qt.CheckState.Unchecked, False), (Qt.CheckState.Unchecked.value, False), (False, False),
])
def test_check_state_accepts_qt_enum_integer_and_boolean_without_optimistic_updates(qapp, value, selected):
    model = SubscriptionListModel()
    original = Subscription("UC1", "channel", selected=not selected)
    model.set_subscriptions((original,))
    toggled = []
    model.toggled.connect(lambda channel_id, checked: toggled.append((channel_id, checked)))
    assert model.setData(model.index(0), value, Qt.ItemDataRole.CheckStateRole) is False
    assert toggled == [("UC1", selected)]
    assert model.at(0) == original  # The backend, not the delegate, confirms state.


def _toggle_with_delegate(dialog, row, method="mouse"):
    view = dialog.list_view
    index = dialog.proxy.index(row, 0)
    view.scrollTo(index)
    view.setCurrentIndex(index)
    view.setFocus()
    QApplication.processEvents()
    if method == "keyboard":
        QTest.keyClick(view, Qt.Key.Key_Space)
    else:
        option = QStyleOptionViewItem()
        option.initFrom(view)
        option.rect = view.visualRect(index)
        view.itemDelegate().initStyleOption(option, index)
        indicator = view.style().subElementRect(QStyle.SubElement.SE_ItemViewItemCheckIndicator, option, view)
        assert indicator.isValid()
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=indicator.center())


@pytest.mark.parametrize("method", ["mouse", "keyboard"])
def test_real_delegate_checks_and_unchecks_new_channel_preserving_eight_selections(state, method):
    state.apply(ev.ConnectionChanged(ConnectionState.CONNECTED))
    existing = tuple(Subscription(f"UC{i}", f"Selected {i}", selected=True) for i in range(8))
    subscriptions = existing + (Subscription("UCtalk", "SurplusTalk"), Subscription("UChealth", "SurplusHealth"))
    state.apply(ev.SubscriptionsChanged(subscriptions))
    received = []
    state.command_requested.connect(received.append)
    dialog = ChannelsDialog(state)
    dialog.show()
    try:
        expected = tuple(item.channel_id for item in existing)
        _toggle_with_delegate(dialog, 8, method)
        assert received == [cmd.SetWatchedChannels(expected + ("UCtalk",))]
        assert state.subscriptions == subscriptions
        confirmed = tuple(replace(item, selected=item.channel_id != "UChealth") for item in subscriptions)
        state.apply(ev.SubscriptionsChanged(confirmed))
        assert dialog.model.data(dialog.model.index(8), Qt.ItemDataRole.CheckStateRole) is Qt.CheckState.Checked
        _toggle_with_delegate(dialog, 8, method)
        assert received[-1] == cmd.SetWatchedChannels(expected)
        state.apply(ev.SubscriptionsChanged(subscriptions))
        assert dialog.model.data(dialog.model.index(8), Qt.ItemDataRole.CheckStateRole) is Qt.CheckState.Unchecked
        assert "8개 선택 / 구독 10개" in dialog.summary_label.text()
    finally:
        dialog.close()


def test_real_delegate_keeps_rapid_selections_across_search_and_selected_filters(state):
    state.apply(ev.ConnectionChanged(ConnectionState.CONNECTED))
    subscriptions = (Subscription("UCkeep", "Existing", selected=True),
                     Subscription("UCtalk", "SurplusTalk"), Subscription("UChealth", "SurplusHealth"))
    state.apply(ev.SubscriptionsChanged(subscriptions))
    received = []
    state.command_requested.connect(received.append)
    dialog = ChannelsDialog(state)
    dialog.show()
    try:
        dialog.search_edit.setText("Surplus")
        dialog.filter_unselected.click()
        assert dialog.proxy.rowCount() == 2
        _toggle_with_delegate(dialog, 0)
        _toggle_with_delegate(dialog, 1)  # No backend acknowledgement between clicks.
        assert received == [cmd.SetWatchedChannels(("UCkeep", "UCtalk")),
                            cmd.SetWatchedChannels(("UCkeep", "UCtalk", "UChealth"))]
        state.apply(ev.SubscriptionsChanged(tuple(replace(item, selected=True) for item in subscriptions)))
        assert dialog.proxy.rowCount() == 0
        dialog.filter_selected.click()
        assert dialog.proxy.rowCount() == 2
        _toggle_with_delegate(dialog, 0, "keyboard")
        assert received[-1] == cmd.SetWatchedChannels(("UCkeep", "UChealth"))
    finally:
        dialog.close()


def test_미연결에서_연결_버튼이_명령을_보낸다(state: AppState, stub) -> None:
    received: list[object] = []
    state.command_requested.connect(received.append)
    dialog = AccountDialog(state)
    dialog.pane.connect_button.click()
    QApplication.processEvents()
    assert received == [cmd.ConnectAccount()]
    dialog.close()


def test_세션_로그인은_사용자_선택을_명령에_전달한다(state: AppState, stub) -> None:
    received = []
    state.command_requested.connect(received.append)
    dialog = AccountDialog(state)
    assert not dialog.pane.session_only.isChecked()
    dialog.pane.session_only.setChecked(True)
    dialog.pane.connect_button.click()
    assert received == [cmd.ConnectAccount(session_only=True)]
    dialog.close()


def test_계정_화면에서_Desktop_JSON_가져오기_후_준비_상태가_된다(state: AppState, monkeypatch, tmp_path) -> None:
    import json
    import yt_rec.backend.oauth as oauth

    monkeypatch.delenv(oauth.ENV_CLIENT_SECRETS, raising=False)
    monkeypatch.delenv(oauth.ENV_CLIENT_ID, raising=False)
    monkeypatch.delenv(oauth.ENV_CLIENT_SECRET, raising=False)
    monkeypatch.setattr(oauth, "_default_secrets_path", lambda: tmp_path / "config" / "client_secrets.json")
    source = tmp_path / "download.json"
    source.write_text(json.dumps({"installed": {"client_id": "id", "client_secret": "s"}}), encoding="utf-8")
    monkeypatch.setattr("yt_rec.ui.account.QFileDialog.getOpenFileName", lambda *_a: (str(source), ""))
    dialog = AccountDialog(state)
    assert "설정이 없습니다" in dialog.config_status.text()
    dialog.import_button.click()
    assert "준비되었습니다" in dialog.config_status.text()
    assert oauth.load_client_config()["installed"]["client_id"] == "id"
    dialog.close()


def test_계정_화면에_인증_만료_원인을_보여_준다(state: AppState, stub) -> None:
    dialog = AccountDialog(state)
    state.apply(
        ev.WatchStatusChanged(
            state=WatchState.STOPPED, channel_count=1, stop_reason=StopReason.AUTH_EXPIRED
        )
    )
    state.apply(ev.ConnectionChanged(ConnectionState.DISCONNECTED))
    QApplication.processEvents()
    assert "만료" in dialog.pane.status_label.text()
    dialog.close()


def test_구독_목록을_체크하면_전체_교체_명령을_보낸다(state: AppState, stub) -> None:
    state.apply(ev.ConnectionChanged(ConnectionState.CONNECTED))
    state.apply(
        ev.SubscriptionsChanged(
            (
                Subscription(channel_id="UC1", name="하나"),
                Subscription(channel_id="UC2", name="둘"),
            )
        )
    )
    received: list[object] = []
    state.command_requested.connect(received.append)
    dialog = ChannelsDialog(state)
    dialog.model.toggled.emit("UC2", True)
    QApplication.processEvents()
    assert received == [cmd.SetWatchedChannels(("UC2",))]
    dialog.close()


def test_빠른_토글은_앞선_선택을_잃지_않는다(state: AppState, stub) -> None:
    state.apply(ev.ConnectionChanged(ConnectionState.CONNECTED))
    state.apply(
        ev.SubscriptionsChanged(
            (
                Subscription(channel_id="UC1", name="하나"),
                Subscription(channel_id="UC2", name="둘"),
                Subscription(channel_id="UC3", name="셋"),
            )
        )
    )
    received: list[object] = []
    state.command_requested.connect(received.append)
    dialog = ChannelsDialog(state)
    dialog.model.toggled.emit("UC1", True)
    dialog.model.toggled.emit("UC2", True)
    QApplication.processEvents()
    assert received[-1] == cmd.SetWatchedChannels(("UC1", "UC2"))
    dialog.close()


def test_검색과_선택_필터가_동작한다(state: AppState) -> None:
    state.apply(ev.ConnectionChanged(ConnectionState.CONNECTED))
    state.apply(
        ev.SubscriptionsChanged(
            tuple(
                Subscription(
                    channel_id=f"UC{i:024d}",
                    name=f"채널 {i}",
                    selected=i == 3,
                    unavailable=i == 9,
                )
                for i in range(500)
            )
        )
    )
    dialog = ChannelsDialog(state)
    assert dialog.model.rowCount() == 500
    dialog.search_edit.setText("채널 499")
    QApplication.processEvents()
    assert dialog.proxy.rowCount() == 1
    dialog.search_edit.clear()
    dialog.filter_selected.click()
    QApplication.processEvents()
    assert dialog.proxy.rowCount() == 1
    display = dialog.model.data(dialog.model.index(9, 0))
    assert "조회 불가" in display
    assert "500" in dialog.summary_label.text() or "구독 500" in dialog.summary_label.text()
    dialog.close()


def test_선택_0개는_감시를_못_한다고_알린다(state: AppState) -> None:
    state.apply(ev.ConnectionChanged(ConnectionState.CONNECTED))
    state.apply(ev.SubscriptionsChanged((Subscription(channel_id="UC1", name="하나"),)))
    dialog = ChannelsDialog(state)
    assert "감시" in dialog.summary_label.text()
    dialog.close()


@pytest.mark.parametrize("filter_mode", ["all", "search", "selected", "unselected"])
def test_전체_선택은_보기_필터와_관계없이_한_명령을_보내고_결과를_기다린다(state: AppState, filter_mode) -> None:
    state.apply(ev.ConnectionChanged(ConnectionState.CONNECTED))
    subscriptions = (
        Subscription(channel_id="UC1", name="하나"),
        Subscription(channel_id="UC2", name="둘", selected=True),
        Subscription(channel_id="UC3", name="셋"),
    )
    state.apply(ev.SubscriptionsChanged(subscriptions))
    received = []
    state.command_requested.connect(received.append)
    dialog = ChannelsDialog(state)
    button = dialog.findChild(QPushButton, "selectAllSubscriptions")
    assert button is not None
    assert button.text() == "구독 채널 전체 선택"
    assert dialog.filter_all.text() == "전체 보기"
    if filter_mode == "search":
        dialog.search_edit.setText("하나")
    elif filter_mode != "all":
        getattr(dialog, f"filter_{filter_mode}").click()
    button.click()
    assert received == [cmd.SetWatchedChannels(("UC1", "UC2", "UC3"))]
    assert state.subscriptions == subscriptions
    assert "1개 선택" in dialog.summary_label.text()
    state.apply(ev.SubscriptionsChanged(tuple(replace(item, selected=True) for item in subscriptions)))
    assert "3개 선택" in dialog.summary_label.text()
    dialog.model.toggled.emit("UC1", False)
    assert received[-1] == cmd.SetWatchedChannels(("UC2", "UC3"))
    dialog.close()


def test_전체_선택_전후의_빠른_추가선택과_해제를_잃지_않는다(state: AppState) -> None:
    state.apply(ev.ConnectionChanged(ConnectionState.CONNECTED))
    state.apply(ev.SubscriptionsChanged((
        Subscription(channel_id="UC1", name="하나"),
        Subscription(channel_id="UC2", name="둘"),
    )))
    received = []
    state.command_requested.connect(received.append)
    dialog = ChannelsDialog(state)
    dialog.model.toggled.emit("UC-extra", True)
    dialog.select_all_button.click()
    assert received[-1] == cmd.SetWatchedChannels(("UC1", "UC2", "UC-extra"))
    dialog.model.toggled.emit("UC2", False)
    assert received[-1] == cmd.SetWatchedChannels(("UC1", "UC-extra"))
    dialog.close()


def test_미연결이나_빈_구독에는_전체_선택을_보내지_않는다(state: AppState) -> None:
    received = []
    state.command_requested.connect(received.append)
    dialog = ChannelsDialog(state)
    state.apply(ev.SubscriptionsChanged((Subscription(channel_id="UC1", name="하나"),)))
    assert not dialog.select_all_button.isEnabled()
    dialog._select_all()
    state.apply(ev.ConnectionChanged(ConnectionState.CONNECTED))
    assert dialog.select_all_button.isEnabled()
    state.apply(ev.SubscriptionsChanged(()))
    assert not dialog.select_all_button.isEnabled()
    dialog._select_all()
    assert received == []
    dialog.close()


def test_전체_선택이_실제_파일에_저장되어_재열기와_재시작에_복원된다(state: AppState, tmp_path) -> None:
    from yt_rec.backend.controller import WatchController
    from yt_rec.backend.selection import FileSelectionStore
    from yt_rec.backend.tokens import MemoryTokenStore
    from yt_rec.backend.youtube import ChannelRef
    from backend_fakes import FakeAuth, FakeRecorder, FakeYouTube

    path = tmp_path / "watched_channels.json"
    FileSelectionStore(path).save(("UC-preserved",))

    def connect(store: AppState) -> WatchController:
        controller = WatchController(
            emit=store.apply, auth=FakeAuth(), tokens=MemoryTokenStore("credentials"),
            selection=FileSelectionStore(path), recorder=FakeRecorder(),
            youtube_factory=lambda _creds: FakeYouTube(subs=[ChannelRef("UC1", "하나"), ChannelRef("UC2", "둘")]),
        )
        store.command_requested.connect(controller.handle_command)
        controller.start()
        return controller

    controller = connect(state)
    dialog = ChannelsDialog(state)
    dialog.select_all_button.click()
    expected = ("UC1", "UC2", "UC-preserved")
    assert FileSelectionStore(path).load() == expected
    dialog.close()
    reopened = ChannelsDialog(state)
    assert "3개 선택" in reopened.summary_label.text()
    reopened.close()

    restarted = AppState(emit_interval_ms=0)
    restarted_controller = connect(restarted)
    assert tuple(item.channel_id for item in restarted.subscriptions if item.selected) == expected
    assert restarted.watch.channel_count == 3
    state.command_requested.disconnect(controller.handle_command)
    restarted.command_requested.disconnect(restarted_controller.handle_command)
    restarted.deleteLater()
