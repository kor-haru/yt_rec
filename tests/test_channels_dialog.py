from __future__ import annotations

from PySide6.QtWidgets import QApplication

from yt_rec.state import commands as cmd
from yt_rec.state import events as ev
from yt_rec.state.models import ConnectionState, Subscription
from yt_rec.state.store import AppState
from yt_rec.ui.account import AccountDialog
from yt_rec.ui.channels import ChannelsDialog


def test_미연결에서_연결_버튼이_명령을_보낸다(state: AppState, stub) -> None:
    received: list[object] = []
    state.command_requested.connect(received.append)
    dialog = AccountDialog(state)
    dialog.pane.connect_button.click()
    QApplication.processEvents()
    assert received == [cmd.ConnectAccount()]
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
