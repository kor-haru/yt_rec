from __future__ import annotations

from PySide6.QtWidgets import QApplication

from yt_rec.state import commands as cmd
from yt_rec.state import events as ev
from yt_rec.state.models import ConnectionState, StopReason, Subscription, WatchState
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
