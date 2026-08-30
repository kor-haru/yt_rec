"""백엔드 테스트 대역. Google HTTP 를 열지 않는다."""

from __future__ import annotations

from yt_rec.backend.oauth import AuthError
from yt_rec.backend.youtube import ChannelRef, LiveBroadcast, YouTubeError


class FakeAuth:
    def __init__(self, credentials: object = "creds") -> None:
        self.credentials = credentials
        self.login_calls = 0
        self.restore_calls = 0
        self.fail: Exception | None = None

    def login(self) -> object:
        self.login_calls += 1
        if self.fail is not None:
            raise self.fail
        return self.credentials

    def restore(self, blob: str) -> object:
        self.restore_calls += 1
        if self.fail is not None:
            raise self.fail
        self.credentials = blob
        return blob

    def dump(self, credentials: object | None = None) -> str:
        value = credentials if credentials is not None else self.credentials
        return str(value)


class FakeYouTube:
    def __init__(
        self,
        subs: list[ChannelRef] | None = None,
        lives: list[LiveBroadcast] | None = None,
        label: str = "내 채널",
    ) -> None:
        self.subs = list(subs or [])
        self.lives = list(lives or [])
        self.label = label
        self.quota_used = 0
        self.quota_limit = 10_000
        self.fail: YouTubeError | None = None
        self.find_calls: list[tuple[str, ...]] = []
        self.last_channel_errors: dict[str, str] = {}
        self.channel_fail: dict[str, YouTubeError] = {}

    def load_account_label(self) -> str:
        self.quota_used += 1
        self._raise()
        return self.label

    def list_subscriptions(self) -> list[ChannelRef]:
        self.quota_used += 1
        self._raise()
        return list(self.subs)

    def find_lives(self, channel_ids) -> list[LiveBroadcast]:
        self.quota_used += 1
        self._raise()
        wanted = set(channel_ids)
        self.find_calls.append(tuple(channel_ids))
        self.last_channel_errors = {}
        for channel_id, error in self.channel_fail.items():
            if channel_id in wanted:
                if error.kind in ("auth", "quota"):
                    raise error
                self.last_channel_errors[channel_id] = str(error)
        skipped = set(self.last_channel_errors)
        return [
            item
            for item in self.lives
            if item.channel_id in wanted and item.channel_id not in skipped
        ]

    def _raise(self) -> None:
        if self.fail is not None:
            raise self.fail


class FakeRecorder:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.recording: set[str] = set()
        self.option_updates: list[dict] = []
        self.recover_calls = 0
        self.join_calls = 0
        self.start_fail: Exception | None = None

    def start(self, video_id: str, **_kwargs: object) -> None:
        if self.start_fail is not None:
            raise self.start_fail
        self.started.append(video_id)
        self.recording.add(video_id)

    def recover_pending(self) -> None:
        self.recover_calls += 1

    def join_all(self, timeout: float | None = None) -> None:
        self.join_calls += 1

    def stop(self, recording_id: str) -> None:
        self.stopped.append(recording_id)
        self.recording.discard(recording_id)

    def stop_all(self) -> None:
        self.stopped.extend(sorted(self.recording))
        self.recording.clear()

    def is_recording(self, video_id: str) -> bool:
        return video_id in self.recording

    def update_options(self, values: dict) -> None:
        self.option_updates.append(dict(values))


class FakeResponse:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        return self._payload


class ScriptedSession:
    """url 부분 문자열 → 응답 목록. 호출마다 하나씩 소모한다."""

    def __init__(self, table: dict[str, list[FakeResponse]]) -> None:
        self.table = {key: list(value) for key, value in table.items()}
        self.calls: list[tuple[str, dict]] = []
        self.last_timeout = None

    def get(
        self,
        url: str,
        params: dict | None = None,
        timeout: object | None = None,
    ) -> FakeResponse:
        params = params or {}
        self.calls.append((url, dict(params)))
        self.last_timeout = timeout
        for key, queue in self.table.items():
            if key in url:
                if not queue:
                    raise AssertionError(f"응답이 더 없다: {url}")
                return queue.pop(0)
        raise AssertionError(f"등록되지 않은 URL: {url}")


# re-export for tests that mention auth errors
__all__ = [
    "AuthError",
    "FakeAuth",
    "FakeRecorder",
    "FakeResponse",
    "FakeYouTube",
    "ScriptedSession",
    "YouTubeError",
]
