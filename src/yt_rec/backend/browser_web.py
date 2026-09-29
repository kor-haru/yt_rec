"""Read YouTube in the app-owned browser, without exporting credentials.

The synchronous facade is called only by backend threads. Qt/CDP work is queued
to the GUI thread; every request has a deadline and a browser-session fence.
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime

from PySide6.QtCore import QObject, QThread, Qt, Signal

from .youtube import ChannelRef, LiveBroadcast, VideoState, YouTubeError

TIMEOUT = 45.0

# No HTML, cookies, API credentials or raw account identifiers leave the page.
# This is a web-page integration, not a supported YouTube Data API contract.
_READ = r"""
(async () => {
  const deadline = new AbortController();
  const timer = setTimeout(() => deadline.abort(), 40000);
  const fail = code => { throw new Error(code); };
  const readJSON = (text, marker) => {
    let start = text.indexOf(marker);
    if (start < 0) fail('format');
    start = text.indexOf('{', start + marker.length);
    let depth = 0, quoted = false, escaped = false;
    for (let i = start; i < text.length; i++) {
      const c = text[i];
      if (quoted) { if (escaped) escaped = false; else if (c === '\\') escaped = true;
        else if (c === '"') quoted = false; }
      else if (c === '"') quoted = true;
      else if (c === '{') depth++;
      else if (c === '}' && --depth === 0) return JSON.parse(text.slice(start, i + 1));
    }
    fail('format');
  };
  const get = async path => {
    const response = await fetch(path, {credentials:'same-origin', cache:'no-store', signal:deadline.signal});
    if (!response.ok || new URL(response.url).origin !== location.origin) fail('network');
    const text = await response.text();
    if (text.length > 12000000) fail('format');
    const data = readJSON(text, 'var ytInitialData =');
    const context = data.responseContext?.mainAppWebResponseContext;
    if (context?.loggedOut === true) fail('auth');
    if (context?.loggedOut !== false || typeof context.datasyncId !== 'string' || !context.datasyncId) fail('format');
    return {text, data, account:context.datasyncId};
  };
  const hash = async text => Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',
    new TextEncoder().encode(text))), b => b.toString(16).padStart(2,'0')).join('');
  try {
    if (location.origin !== 'https://www.youtube.com') fail('auth');
    const page = await get(PATH);
    const account = await hash(page.account);
    if (EXPECTED && account !== EXPECTED) fail('changed');
    if (VIDEO) {
      const player = readJSON(page.text, 'var ytInitialPlayerResponse =');
      const d = player.videoDetails || {}, p = player.playabilityStatus || {};
      const live = player.microformat?.playerMicroformatRenderer?.liveBroadcastDetails || {};
      const slate = p.liveStreamability?.liveStreamabilityRenderer?.offlineSlate?.liveStreamOfflineSlateRenderer || {};
      return JSON.stringify({account, player:{
        videoDetails:{videoId:d.videoId,channelId:d.channelId,title:d.title,author:d.author,
          isLiveContent:d.isLiveContent,isUpcoming:d.isUpcoming},
        playabilityStatus:{status:p.status,liveStreamability:{liveStreamabilityRenderer:{offlineSlate:{
          liveStreamOfflineSlateRenderer:{scheduledStartTime:slate.scheduledStartTime}}}}},
        microformat:{playerMicroformatRenderer:{liveBroadcastDetails:{isLiveNow:live.isLiveNow,
          startTimestamp:live.startTimestamp,endTimestamp:live.endTimestamp}}}}});
    }
    let data = page.data, pages = 0;
    const channels = new Map(), tokens = new Set();
    while (true) {
      if (++pages > 100) fail('incomplete');
      const nodes = [], walk = node => {
        if (!node || typeof node !== 'object') return;
        if (node.channelRenderer) nodes.push(node.channelRenderer);
        for (const value of Object.values(node)) if (value && typeof value === 'object') walk(value);
      };
      // Only the feed content, never sidebar/recommended channels.
      let content = pages === 1 ? data.contents?.twoColumnBrowseResultsRenderer :
        data.onResponseReceivedActions || data.onResponseReceivedEndpoints;
      if (!content) fail('format');
      walk(content);
      for (const node of nodes) {
        const id = node.channelId;
        const name = node.title?.simpleText || node.title?.runs?.map(run => run.text).join('');
        if (!/^UC[A-Za-z0-9_-]{22}$/.test(id || '') || typeof name !== 'string' || !name) fail('format');
        channels.set(id, {channel_id:id, name});
        if (channels.size > 10000) fail('incomplete');
      }
      const continuations = [];
      let empty = false;
      const follow = node => {
        if (!node || typeof node !== 'object') return;
        if (node.errorRenderer || node.alertRenderer) fail('format');
        if (node.messageRenderer) {
          const text = node.messageRenderer.text;
          const message = text?.simpleText || text?.runs?.map(run => run.text).join('');
          if (["You haven't subscribed to any channels yet.", "You haven't subscribed to any channels.",
               'No subscriptions', '구독한 채널이 없습니다.', '구독한 채널이 없습니다'].includes(message)) empty = true;
          else fail('format');
        }
        if (node.continuationItemRenderer) {
          const token = node.continuationItemRenderer.continuationEndpoint?.continuationCommand?.token;
          if (typeof token !== 'string' || !token) fail('format');
          continuations.push(token);
        }
        for (const value of Object.values(node)) if (value && typeof value === 'object') follow(value);
      };
      follow(content);
      if (!continuations.length) {
        if (!channels.size && !empty) fail('format');
        // Confirm the account once more after all pages, before publishing any list.
        const final = await get('/feed/channels');
        if (final.account !== page.account) fail('changed');
        return JSON.stringify({account, channels:[...channels.values()], complete:true});
      }
      if (continuations.length !== 1 || tokens.has(continuations[0])) fail('incomplete');
      tokens.add(continuations[0]);
      const config = {};
      for (const match of page.text.matchAll(/ytcfg\.set\(\s*(?=\{)/g))
        Object.assign(config, readJSON(page.text.slice(match.index), 'ytcfg.set('));
      if (!config.INNERTUBE_CONTEXT?.client) fail('format');
      const cookies = Object.fromEntries(document.cookie.split(';').map(part => {
        const split = part.indexOf('='); return [part.slice(0,split).trim(), part.slice(split+1)];
      }));
      const sid = cookies.SAPISID || cookies['__Secure-3PAPISID'];
      if (!sid) fail('auth');
      const stamp = Math.floor(Date.now()/1000);
      const digest = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-1',
        new TextEncoder().encode(`${stamp} ${sid} ${location.origin}`))), b => b.toString(16).padStart(2,'0')).join('');
      const response = await fetch('/youtubei/v1/browse?prettyPrint=false', {
        method:'POST', credentials:'same-origin', signal:deadline.signal,
        headers:{'Content-Type':'application/json', 'X-Goog-AuthUser':String(config.SESSION_INDEX || '0'),
          'Authorization':`SAPISIDHASH ${stamp}_${digest}`, 'X-Origin':location.origin,
          'X-Youtube-Client-Name':String(config.INNERTUBE_CONTEXT_CLIENT_NAME || 1),
          'X-Youtube-Client-Version':String(config.INNERTUBE_CONTEXT.client.clientVersion)},
        body:JSON.stringify({context:config.INNERTUBE_CONTEXT, continuation:continuations[0]})});
      if (!response.ok) fail('network');
      data = await response.json();
      const context = data.responseContext?.mainAppWebResponseContext;
      if (context?.loggedOut === true) fail('auth');
      if (context?.loggedOut !== false || typeof context.datasyncId !== 'string') fail('format');
      if (context.datasyncId !== page.account) fail('changed');
    }
  } catch (error) {
    return JSON.stringify({error:['auth','changed','format','incomplete'].includes(error.message) ? error.message : 'network'});
  } finally { clearTimeout(timer); }
})()
"""


def expression(video_id: str = "", account: str = "") -> str:
    if video_id and not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise ValueError("잘못된 영상 ID")
    path = f"/watch?v={video_id}" if video_id else "/feed/channels"
    return _READ.replace("PATH", json.dumps(path)).replace("EXPECTED", json.dumps(account)).replace("VIDEO", json.dumps(video_id))


def _validated(value: object) -> dict:
    if not isinstance(value, dict):
        raise YouTubeError("network", "YouTube 페이지를 읽지 못했습니다. 다시 불러오기를 눌러 주세요.")
    error = value.get("error")
    if error:
        if error in ("auth", "changed"):
            raise YouTubeError("auth", "YouTube 로그인이 필요하거나 계정이 변경되었습니다. 브라우저에서 확인해 주세요.")
        raise YouTubeError("network", "구독 또는 영상 페이지를 완전히 읽지 못했습니다. 기존 목록을 보존했습니다. 다시 불러오기를 눌러 주세요.")
    if not re.fullmatch(r"[a-f0-9]{64}", str(value.get("account", ""))):
        raise YouTubeError("network", "YouTube 로그인 계정을 확인하지 못했습니다.")
    return value


def subscriptions(value: object) -> tuple[str, list[ChannelRef]]:
    data = _validated(value)
    if data.get("complete") is not True or not isinstance(data.get("channels"), list):
        raise YouTubeError("network", "구독 목록을 끝까지 읽지 못했습니다. 기존 목록을 보존했습니다.")
    channels = {}
    for item in data["channels"]:
        if (not isinstance(item, dict) or not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", str(item.get("channel_id", "")))
                or not isinstance(item.get("name"), str) or not item["name"].strip()):
            raise YouTubeError("network", "구독 목록의 형식이 바뀌었습니다. 기존 목록을 보존했습니다.")
        channels[item["channel_id"]] = ChannelRef(item["channel_id"], item["name"])
    return data["account"], list(channels.values())


def video_state(video_id: str, value: object) -> VideoState:
    data = _validated(value)
    player = data.get("player")
    if not isinstance(player, dict):
        raise YouTubeError("network", "영상 상태를 확인하지 못했습니다.")
    details = player.get("videoDetails") or {}
    if details.get("videoId") != video_id:
        return VideoState(video_id, "unknown")
    channel = details.get("channelId", "")
    if not isinstance(channel, str) or not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", channel):
        return VideoState(video_id, "unknown")
    live = LiveBroadcast(video_id, channel, str(details.get("title") or video_id), str(details.get("author") or ""))
    broadcast = ((player.get("microformat") or {}).get("playerMicroformatRenderer") or {}).get("liveBroadcastDetails") or {}
    playability = player.get("playabilityStatus") or {}
    if broadcast.get("endTimestamp"):
        return VideoState(video_id, "ended", live)
    premiere = details.get("isLiveContent") is not True
    if broadcast.get("isLiveNow") is True and playability.get("status") == "OK":
        return VideoState(video_id, "live", live, premiere=premiere)
    offline = (((playability.get("liveStreamability") or {}).get("liveStreamabilityRenderer") or {}).get("offlineSlate") or {}).get("liveStreamOfflineSlateRenderer") or {}
    stamp = offline.get("scheduledStartTime") or broadcast.get("startTimestamp")
    if details.get("isUpcoming") is True or playability.get("status") == "LIVE_STREAM_OFFLINE":
        try:
            epoch = float(stamp) if str(stamp).isdigit() else datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError, OverflowError):
            return VideoState(video_id, "unknown", live)
        return VideoState(video_id, "upcoming", live, epoch, premiere)
    return VideoState(video_id, "ended" if details.get("isLiveContent") else "none", live)


@dataclass
class BrowserRequest:
    expression: str
    generation: int
    done: threading.Event = field(default_factory=threading.Event)
    value: object = None


class BrowserYouTube(QObject):
    requested = Signal(object)
    session_invalidated = Signal()

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.generation = 0
        self.account = ""
        self.available = False
        self.closed = False
        self._pending: list[BrowserRequest] = []
        self._lock = threading.RLock()

    def invalidate(self) -> None:
        with self._lock:
            self.generation += 1
            self.available = False
            for request in self._pending:
                request.done.set()

    def close(self) -> None:
        self.closed = True
        self.invalidate()

    def bind(self, receiver: QObject) -> None:
        self.requested.connect(receiver.read_browser, Qt.ConnectionType.QueuedConnection)

    def _read(self, script: str) -> object:
        if QThread.currentThread() is self.thread():
            raise RuntimeError("브라우저 조회는 백엔드 스레드에서 실행해야 합니다")
        with self._lock:
            if self.closed:
                raise YouTubeError("auth", "브라우저 연결이 종료되었습니다")
            request = BrowserRequest(script, self.generation)
            self._pending.append(request)
        try:
            self.requested.emit(request)
            if not request.done.wait(TIMEOUT):
                raise YouTubeError("network", "브라우저 응답 시간이 초과되었습니다. 다시 불러오기를 눌러 주세요.")
            with self._lock:
                if request.generation != self.generation or self.closed:
                    raise YouTubeError("auth", "브라우저 계정이 변경되었습니다. 목록을 다시 확인합니다.")
                return request.value
        finally:
            request.done.set()
            with self._lock:
                self._pending.remove(request)

    def snapshot(self) -> tuple[str, list[ChannelRef]]:
        generation = self.generation
        account, channels = subscriptions(self._read(expression()))
        with self._lock:
            if generation != self.generation:
                raise YouTubeError("auth", "브라우저 계정이 변경되었습니다.")
            self.account, self.available = account, True
        return account, channels

    def get_video_state(self, video_id: str) -> VideoState:
        if not self.available:
            raise YouTubeError("auth", "YouTube 연결 복구를 기다립니다")
        try:
            return video_state(video_id, self._read(expression(video_id, self.account)))
        except YouTubeError as exc:
            if exc.kind == "auth":
                self.invalidate()
                self.session_invalidated.emit()
            raise
