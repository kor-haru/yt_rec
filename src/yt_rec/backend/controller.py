"""감시 루프. GUI 스레드를 막지 않도록 호출하는 쪽이 스레드를 고른다."""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone

from yt_rec.state import commands as cmd
from yt_rec.state import events as ev
from yt_rec.state.models import (
    AccountInfo,
    ConnectionState,
    LogEntry,
    QuotaStatus,
    Severity,
    StopReason,
    Subscription,
    WatchedChannel,
    WatchState,
)

from .oauth import AuthError, ClientConfigError
from .youtube import LiveBroadcast, YouTubeError

__all__ = ["WatchController", "WATCH_INTERVAL_SECONDS"]

WATCH_INTERVAL_SECONDS = 60

Emit = Callable[[ev.BackendEvent], None]


class WatchController:
    def __init__(
        self,
        *,
        emit: Emit,
        auth: object,
        tokens: object,
        selection: object,
        recorder: object,
        youtube_factory: Callable[[object], object],
        clock: Callable[[], datetime] | None = None,
        poll_interval: float = WATCH_INTERVAL_SECONDS,
    ) -> None:
        self._emit = emit
        self._auth = auth
        self._tokens = tokens
        self._selection = selection
        self._recorder = recorder
        self._youtube_factory = youtube_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.poll_interval = poll_interval
        self._lock = threading.Lock()
        self._youtube: object | None = None
        self._connected = False
        self._subs: list[Subscription] = []
        self._names: dict[str, str] = {}
        self._seen_videos: set[str] = set()

    def start(self) -> None:
        with self._lock:
            blob = self._tokens.load()
            if not blob:
                self._emit(ev.ConnectionChanged(ConnectionState.DISCONNECTED))
                return
            try:
                self._auth.restore(blob)
            except Exception as exc:
                self._tokens.clear()
                self._log(Severity.ERROR, f"저장된 인증을 쓰지 못했다: {exc}")
                self._emit(ev.ConnectionChanged(ConnectionState.DISCONNECTED))
                return
            self._finish_login_locked()

    def handle_command(self, command: cmd.GuiCommand) -> None:
        with self._lock:
            match command:
                case cmd.ConnectAccount():
                    self._connect_locked()
                case cmd.DisconnectAccount():
                    self._disconnect_locked()
                case cmd.RefreshSubscriptions():
                    if self._connected:
                        self._refresh_locked()
                case cmd.SetWatchedChannels(channel_ids=ids):
                    self._set_watched_locked(ids)
                case cmd.StopRecording(recording_id=rid):
                    self._recorder.stop(rid)
                case cmd.UpdateSettings(values=values):
                    updater = getattr(self._recorder, "update_options", None)
                    if updater is not None:
                        updater(values)

    def tick(self) -> None:
        if not self._lock.acquire(blocking=False):
            return
        try:
            if self._connected:
                self._poll_locked()
        finally:
            self._lock.release()

    def _connect_locked(self) -> None:
        if self._connected:
            return
        self._emit(ev.ConnectionChanged(ConnectionState.CONNECTING))
        try:
            creds = self._auth.login()
            self._tokens.save(self._auth.dump(creds))
        except ClientConfigError as exc:
            self._log(Severity.ERROR, str(exc))
            self._emit(ev.ConnectionChanged(ConnectionState.DISCONNECTED))
            return
        except AuthError as exc:
            self._log(Severity.ERROR, str(exc))
            self._emit(ev.ConnectionChanged(ConnectionState.DISCONNECTED))
            return
        except Exception as exc:
            self._log(Severity.ERROR, f"Google 로그인에 실패했다: {exc}")
            self._emit(ev.ConnectionChanged(ConnectionState.DISCONNECTED))
            return
        self._finish_login_locked()

    def _finish_login_locked(self) -> None:
        creds = getattr(self._auth, "credentials", None)
        self._youtube = self._youtube_factory(creds)
        self._connected = True
        self._emit(ev.ConnectionChanged(ConnectionState.CONNECTED))
        self._refresh_locked()

    def _disconnect_locked(self) -> None:
        self._connected = False
        self._youtube = None
        self._tokens.clear()
        stopper = getattr(self._recorder, "stop_all", None)
        if stopper is not None:
            stopper()
        self._subs = []
        self._emit(ev.AccountChanged(AccountInfo()))
        self._emit(ev.SubscriptionsChanged(()))
        self._emit(ev.ChannelsChanged(()))
        self._emit(ev.ConnectionChanged(ConnectionState.DISCONNECTED))

    def _refresh_locked(self) -> None:
        youtube = self._youtube
        if youtube is None:
            return
        try:
            label = youtube.load_account_label()
            fetched = youtube.list_subscriptions()
        except YouTubeError as exc:
            self._handle_api_error(exc)
            return
        self._quota_event()
        for item in fetched:
            self._names[item.channel_id] = item.name
        selected = set(self._selection.load())
        fetched_ids = {item.channel_id for item in fetched}
        subs = [
            Subscription(
                channel_id=item.channel_id,
                name=item.name,
                selected=item.channel_id in selected,
            )
            for item in fetched
        ]
        for channel_id in selected:
            if channel_id not in fetched_ids:
                subs.append(
                    Subscription(
                        channel_id=channel_id,
                        name=self._names.get(channel_id, channel_id),
                        selected=True,
                        unavailable=True,
                    )
                )
        self._subs = subs
        now = self._clock()
        self._emit(ev.AccountChanged(AccountInfo(label=label or "", last_synced_at=now)))
        self._emit(ev.SubscriptionsChanged(tuple(subs)))
        self._poll_locked()

    def _set_watched_locked(self, channel_ids: Sequence[str]) -> None:
        unique = tuple(dict.fromkeys(channel_ids))
        self._selection.save(unique)
        selected = set(unique)
        self._subs = [
            Subscription(
                channel_id=item.channel_id,
                name=item.name,
                selected=item.channel_id in selected,
                unavailable=item.unavailable,
            )
            for item in self._subs
        ]
        known = {item.channel_id for item in self._subs}
        for channel_id in unique:
            if channel_id not in known:
                self._subs.append(
                    Subscription(
                        channel_id=channel_id,
                        name=self._names.get(channel_id, channel_id),
                        selected=True,
                    )
                )
        self._emit(ev.SubscriptionsChanged(tuple(self._subs)))
        if not self._connected:
            return
        self._poll_locked()

    def _poll_locked(self) -> None:
        selected = tuple(self._selection.load())
        now = self._clock()
        next_at = now + timedelta(seconds=self.poll_interval)
        if not selected:
            self._emit(ev.ChannelsChanged(()))
            self._emit(
                ev.WatchStatusChanged(
                    state=WatchState.STOPPED,
                    channel_count=0,
                    stop_reason=StopReason.NO_CHANNELS,
                    next_check_at=None,
                )
            )
            return
        youtube = self._youtube
        if youtube is None:
            return
        try:
            lives = youtube.find_lives(selected)
        except YouTubeError as exc:
            self._handle_api_error(exc)
            return
        self._quota_event()
        live_by_channel = {item.channel_id: item for item in lives}
        channels = []
        selected_set = set(selected)
        for channel_id in selected:
            live = live_by_channel.get(channel_id)
            unavailable = any(
                item.channel_id == channel_id and item.unavailable for item in self._subs
            )
            if unavailable:
                result = "조회 불가"
            elif live:
                result = "라이브 1건 감지"
            else:
                result = "라이브 없음"
            channels.append(
                WatchedChannel(
                    channel_id=channel_id,
                    name=self._names.get(channel_id, channel_id),
                    next_check_at=next_at,
                    last_check_at=now,
                    last_check_result=result,
                    live_now=bool(live),
                )
            )
        self._emit(ev.ChannelsChanged(tuple(channels)))
        self._emit(
            ev.WatchStatusChanged(
                state=WatchState.WATCHING,
                channel_count=len(selected_set),
                next_check_at=next_at,
            )
        )
        for live in lives:
            self._maybe_record(live)

    def _maybe_record(self, live: LiveBroadcast) -> None:
        if live.video_id in self._seen_videos:
            return
        if self._recorder.is_recording(live.video_id):
            return
        self._seen_videos.add(live.video_id)
        self._recorder.start(
            live.video_id,
            channel_id=live.channel_id,
            channel_name=live.channel_name or self._names.get(live.channel_id, ""),
            title=live.title,
        )

    def _handle_api_error(self, exc: YouTubeError) -> None:
        self._log(Severity.ERROR, str(exc))
        if exc.kind == "auth":
            self._connected = False
            self._youtube = None
            self._tokens.clear()
            self._emit(
                ev.WatchStatusChanged(
                    state=WatchState.STOPPED,
                    channel_count=len(self._selection.load()),
                    stop_reason=StopReason.AUTH_EXPIRED,
                )
            )
            self._emit(ev.ConnectionChanged(ConnectionState.DISCONNECTED))
            return
        reason = StopReason.QUOTA_EXCEEDED if exc.kind == "quota" else StopReason.NETWORK_DOWN
        self._emit(
            ev.WatchStatusChanged(
                state=WatchState.STOPPED,
                channel_count=len(self._selection.load()),
                stop_reason=reason,
                next_check_at=self._clock() + timedelta(seconds=self.poll_interval),
            )
        )

    def _quota_event(self) -> None:
        youtube = self._youtube
        if youtube is None:
            return
        self._emit(
            ev.QuotaChanged(
                QuotaStatus(
                    used=int(getattr(youtube, "quota_used", 0)),
                    limit=getattr(youtube, "quota_limit", 10_000),
                )
            )
        )

    def _log(self, severity: Severity, message: str) -> None:
        self._emit(
            ev.LogAppended(
                LogEntry(at=self._clock(), severity=severity, source="backend", message=message)
            )
        )
