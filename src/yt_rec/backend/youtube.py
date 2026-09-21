"""YouTube Data API v3 클라이언트.

HTTP 세션을 주입받아 네트워크를 테스트에서 막는다. google-api-python-client 는
쓰지 않는다.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from yt_rec.recording.ownership import validate_video_id

__all__ = [
    "API_ROOT",
    "HTTP_TIMEOUT",
    "YouTubeError",
    "ChannelRef",
    "LiveBroadcast",
    "VideoState",
    "YouTubeApi",
    "session_from_credentials",
    "recommended_poll_interval",
]

API_ROOT = "https://www.googleapis.com/youtube/v3"
#: AuthorizedSession.get 에 넘기는 (connect, read) 초.
HTTP_TIMEOUT = (10.0, 30.0)

_COSTS = {
    "subscriptions": 1,
    "channels": 1,
    "playlistItems": 1,
    "videos": 1,
    "search": 100,
}

_AUTH_REASONS = {
    "authError",
    "invalidCredentials",
    "forbidden",
    "insufficientPermissions",
    "unauthorized",
}
_QUOTA_REASONS = {
    "quotaExceeded",
    "dailyLimitExceeded",
    "rateLimitExceeded",
    "userRateLimitExceeded",
}


class YouTubeError(RuntimeError):
    """API 호출 실패. ``kind`` 는 ``auth`` / ``quota`` / ``network``."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class ChannelRef:
    channel_id: str
    name: str


@dataclass(frozen=True)
class LiveBroadcast:
    video_id: str
    channel_id: str
    title: str
    channel_name: str = ""


#: 방송 전 프리미어와 예약 라이브를 가르는 유일한 신호가 길이다. 예약 라이브는
#: 아직 찍히지 않았으니 ``P0D`` 이고, 프리미어는 이미 만들어 둔 파일이라 실제
#: 길이가 붙는다. API 에 프리미어 전용 필드는 없다 — 이 값은 추정이다.
_ZERO_DURATIONS = frozenset({"", "P0D", "PT0S", "P0DT0H0M0S"})


@dataclass(frozen=True)
class VideoState:
    """videos.list 한 번이 알려 주는 영상의 실제 상태.

    ``status`` 는 ``live`` / ``upcoming`` / ``none`` / ``ended`` / ``unknown``
    중 하나다. :meth:`YouTubeApi.get_live` 는 이 넷을 ``None`` 하나로 뭉갠다.
    같은 응답에 이미 들어 있던 값을 읽을 뿐이라 quota 추가 비용이 없다(#82).
    """

    video_id: str
    status: str
    #: 채널 정보를 알 수 있을 때의 영상 메타데이터. ``live`` 가 아니어도 채운다.
    broadcast: LiveBroadcast | None = None
    #: ``upcoming`` 일 때 YouTube 가 알려 준 예정 시작 시각(epoch 초).
    scheduled_start: float | None = None
    #: 프리미어로 보이는가. 전용 필드가 없어 길이로 추정한 값이다.
    premiere: bool = False


def _epoch(value: object) -> float | None:
    """RFC3339 시각 문자열을 epoch 초로. 해석할 수 없으면 ``None``."""
    text = str(value or "").strip()
    if not text:
        return None
    if text[-1] in "Zz":
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def _video_state_from_item(video_id: str, item: dict[str, Any]) -> VideoState:
    snippet = item.get("snippet") or {}
    details = item.get("liveStreamingDetails") or {}
    content = str(snippet.get("liveBroadcastContent") or "")
    channel_id = str(snippet.get("channelId") or "")
    broadcast = LiveBroadcast(
        video_id=video_id,
        channel_id=channel_id,
        title=str(snippet.get("title") or video_id),
        channel_name=str(snippet.get("channelTitle") or ""),
    ) if channel_id else None
    if details.get("actualEndTime"):
        return VideoState(video_id, "ended", broadcast)
    if content == "live" and details.get("actualStartTime") and broadcast is not None:
        return VideoState(video_id, "live", broadcast)
    scheduled = _epoch(details.get("scheduledStartTime"))
    if content == "upcoming" and scheduled is not None and broadcast is not None:
        duration = str((item.get("contentDetails") or {}).get("duration") or "").upper()
        return VideoState(video_id, "upcoming", broadcast, scheduled, duration not in _ZERO_DURATIONS)
    if content == "none":
        return VideoState(video_id, "none", broadcast)
    # 예정 시각 없는 upcoming, 시작 시각 없는 live, 없는 영상은 모두 판단 불가다.
    return VideoState(video_id, "unknown", broadcast)


class HttpSession(Protocol):
    def get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        timeout: float | tuple[float, float] | None = None,
    ) -> Any: ...


def recommended_poll_interval(
    channel_count: int,
    *,
    quota_limit: int = 10_000,
    floor_seconds: float = 60.0,
    budget_ratio: float = 0.9,
) -> float:
    """하루 quota 를 넘기지 않는 최소 폴링 간격(초).

    업로드 플레이리스트를 캐시한 뒤 poll 당 비용은 채널별 playlistItems(1) +
    videos.list(1) 이다. 6채널·60초면 하루 10,080 단위가 되어 기본 한도를 넘는다.
    """
    n = max(0, int(channel_count))
    if n == 0:
        return floor_seconds
    units_per_poll = n + 1
    budget = max(1.0, float(quota_limit) * budget_ratio)
    interval = 86400.0 * units_per_poll / budget
    return max(floor_seconds, interval)


def _is_auth_refresh_failure(exc: BaseException) -> bool:
    name = type(exc).__name__
    text = str(exc).lower()
    if "invalid_grant" in text or "revoked" in text:
        return True
    return name == "RefreshError" and "invalid_grant" in text


def session_from_credentials(credentials: Any) -> Any:
    from google.auth.transport.requests import AuthorizedSession

    return AuthorizedSession(credentials)


class YouTubeApi:
    """subscriptions.list 전 페이지와 선택 채널의 현재 라이브를 조회한다."""

    def __init__(self, session: HttpSession, *, quota_limit: int = 10_000) -> None:
        self._session = session
        self.quota_used = 0
        self.quota_limit = quota_limit
        self._uploads: dict[str, str] = {}
        self._account_label = ""
        self.last_channel_errors: dict[str, str] = {}

    @property
    def account_label(self) -> str:
        return self._account_label

    def list_subscriptions(self) -> list[ChannelRef]:
        items = self._paginate(
            "subscriptions",
            {"part": "snippet", "mine": "true", "maxResults": "50"},
        )
        result: list[ChannelRef] = []
        seen: set[str] = set()
        for item in items:
            snippet = item.get("snippet") or {}
            resource = snippet.get("resourceId") or {}
            channel_id = str(resource.get("channelId") or "")
            name = str(snippet.get("title") or channel_id)
            if not channel_id or channel_id in seen:
                continue
            seen.add(channel_id)
            result.append(ChannelRef(channel_id=channel_id, name=name))
        return result

    def load_account_label(self) -> str:
        data = self._get("channels", {"part": "snippet", "mine": "true", "maxResults": "1"})
        items = data.get("items") or []
        if not items:
            self._account_label = ""
            return ""
        snippet = items[0].get("snippet") or {}
        self._account_label = str(snippet.get("title") or items[0].get("id") or "")
        return self._account_label

    def get_live(self, video_id: str) -> LiveBroadcast | None:
        """Check only the notified video with one videos.list request; no scan."""
        state = self._video_state(video_id, "snippet,liveStreamingDetails")
        return state.broadcast if state.status == "live" else None

    def get_video_state(self, video_id: str) -> VideoState:
        """알림받은 영상 하나의 상태를 videos.list 한 번으로 확정한다.

        :meth:`get_live` 와 요청 수도 quota(1 단위)도 같다. ``contentDetails``
        를 함께 읽는 것은 part 추가일 뿐 비용이 붙지 않으며, 프리미어 추정에만
        쓴다. 예약 라이브면 ``scheduled_start`` 에 예정 시작 시각이 들어온다.
        """
        return self._video_state(video_id, "snippet,liveStreamingDetails,contentDetails")

    def _video_state(self, video_id: str, parts: str) -> VideoState:
        validate_video_id(video_id)
        payload = self._get("videos", {"part": parts, "id": video_id})
        for item in payload.get("items") or []:
            if item.get("id") != video_id:
                continue
            return _video_state_from_item(video_id, item)
        return VideoState(video_id, "unknown")

    def find_lives(self, channel_ids: Sequence[str]) -> list[LiveBroadcast]:
        """선택된 채널의 *현재 송출 중* 라이브만 돌려준다.

        search.list(100 단위) 대신 channels + playlistItems + videos 를 묶는다.
        uploads 플레이리스트 ID 는 채널마다 캐시한다.
        """
        ids = [cid for cid in channel_ids if cid]
        self.last_channel_errors = {}
        if not ids:
            return []
        self._resolve_uploads(ids)
        video_ids: list[str] = []
        video_channel: dict[str, str] = {}
        for channel_id in ids:
            playlist_id = self._uploads.get(channel_id)
            if not playlist_id:
                continue
            try:
                data = self._get(
                    "playlistItems",
                    {
                        "part": "contentDetails",
                        "playlistId": playlist_id,
                        "maxResults": "5",
                    },
                )
            except YouTubeError as exc:
                if exc.kind in ("auth", "quota"):
                    raise
                self.last_channel_errors[channel_id] = str(exc)
                continue
            for item in data.get("items") or []:
                details = item.get("contentDetails") or {}
                video_id = str(details.get("videoId") or "")
                if not video_id:
                    continue
                video_ids.append(video_id)
                video_channel[video_id] = channel_id
        return self._lives_among(video_ids, video_channel)

    def _resolve_uploads(self, channel_ids: Sequence[str]) -> None:
        missing = [cid for cid in channel_ids if cid not in self._uploads]
        for chunk in _chunks(missing, 50):
            data = self._get(
                "channels",
                {"part": "contentDetails", "id": ",".join(chunk), "maxResults": "50"},
            )
            found: set[str] = set()
            for item in data.get("items") or []:
                channel_id = str(item.get("id") or "")
                related = ((item.get("contentDetails") or {}).get("relatedPlaylists") or {})
                uploads = str(related.get("uploads") or "")
                if channel_id and uploads:
                    self._uploads[channel_id] = uploads
                    found.add(channel_id)
            for channel_id in chunk:
                if channel_id not in found:
                    self._uploads.setdefault(channel_id, "")

    def _lives_among(
        self, video_ids: Sequence[str], video_channel: dict[str, str]
    ) -> list[LiveBroadcast]:
        lives: list[LiveBroadcast] = []
        seen: set[str] = set()
        for chunk in _chunks(list(dict.fromkeys(video_ids)), 50):
            data = self._get(
                "videos",
                {"part": "snippet,liveStreamingDetails", "id": ",".join(chunk)},
            )
            for item in data.get("items") or []:
                video_id = str(item.get("id") or "")
                details = item.get("liveStreamingDetails") or {}
                if not video_id or video_id in seen:
                    continue
                if not details.get("actualStartTime") or details.get("actualEndTime"):
                    continue
                snippet = item.get("snippet") or {}
                channel_id = str(snippet.get("channelId") or video_channel.get(video_id) or "")
                lives.append(
                    LiveBroadcast(
                        video_id=video_id,
                        channel_id=channel_id,
                        title=str(snippet.get("title") or video_id),
                        channel_name=str(snippet.get("channelTitle") or ""),
                    )
                )
                seen.add(video_id)
        return lives

    def _paginate(self, endpoint: str, params: dict[str, str]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        token: str | None = None
        while True:
            page = dict(params)
            if token:
                page["pageToken"] = token
            data = self._get(endpoint, page)
            items.extend(data.get("items") or [])
            token = data.get("nextPageToken") or None
            if not token:
                break
        return items

    def _get(self, endpoint: str, params: dict[str, str]) -> dict[str, Any]:
        url = f"{API_ROOT}/{endpoint}"
        cleaned = {k: v for k, v in params.items() if v is not None and v != ""}
        try:
            response = self._session.get(url, params=cleaned, timeout=HTTP_TIMEOUT)
        except YouTubeError:
            raise
        except Exception as exc:
            if _is_auth_refresh_failure(exc) or type(exc).__name__ == "RefreshError":
                raise YouTubeError("auth", str(exc)) from exc
            raise YouTubeError("network", str(exc)) from exc
        self.quota_used += _COSTS.get(endpoint, 1)
        status = int(getattr(response, "status_code", 200))
        payload: Any
        try:
            payload = response.json()
        except Exception:
            payload = {}
        if status >= 400:
            _raise_http(status, payload if isinstance(payload, dict) else {})
        return payload if isinstance(payload, dict) else {}


def _raise_http(status: int, payload: dict[str, Any]) -> None:
    error = payload.get("error") if isinstance(payload, dict) else {}
    if not isinstance(error, dict):
        error = {}
    message = str(error.get("message") or payload or f"HTTP {status}")
    reasons = {
        str(item.get("reason") or "")
        for item in (error.get("errors") or [])
        if isinstance(item, dict)
    }
    if status in (401, 403) and (reasons & _AUTH_REASONS):
        raise YouTubeError("auth", message)
    if status == 403 and (reasons & _QUOTA_REASONS or "quota" in message.lower()):
        raise YouTubeError("quota", message)
    if status in (401, 403):
        raise YouTubeError("auth" if status == 401 else "quota", message)
    raise YouTubeError("network", message)


def _chunks(values: Sequence[str], size: int) -> Iterable[list[str]]:
    items = list(values)
    for index in range(0, len(items), size):
        yield items[index : index + size]
