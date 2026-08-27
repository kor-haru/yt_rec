from __future__ import annotations

import pytest

from yt_rec.backend.youtube import API_ROOT, YouTubeApi, YouTubeError

from backend_fakes import FakeResponse, ScriptedSession


def _sub(channel_id: str, title: str, page: str | None = None) -> dict:
    payload = {
        "items": [
            {
                "snippet": {
                    "title": title,
                    "resourceId": {"channelId": channel_id},
                }
            }
        ]
    }
    if page:
        payload["nextPageToken"] = page
    return payload


def test_구독_목록을_모든_페이지에서_모은다() -> None:
    session = ScriptedSession(
        {
            "subscriptions": [
                FakeResponse(200, _sub("UC1", "하나", "p2")),
                FakeResponse(200, _sub("UC2", "둘")),
            ]
        }
    )
    api = YouTubeApi(session)
    result = api.list_subscriptions()
    assert [item.channel_id for item in result] == ["UC1", "UC2"]
    assert [item.name for item in result] == ["하나", "둘"]
    assert len(session.calls) == 2
    assert session.calls[1][1]["pageToken"] == "p2"
    assert api.quota_used == 2


def test_현재_송출_중인_라이브만_고른다() -> None:
    session = ScriptedSession(
        {
            "channels": [
                FakeResponse(
                    200,
                    {
                        "items": [
                            {
                                "id": "UC1",
                                "contentDetails": {"relatedPlaylists": {"uploads": "UU1"}},
                            }
                        ]
                    },
                )
            ],
            "playlistItems": [
                FakeResponse(
                    200,
                    {"items": [{"contentDetails": {"videoId": "live1"}}, {"contentDetails": {"videoId": "old1"}}]},
                )
            ],
            "videos": [
                FakeResponse(
                    200,
                    {
                        "items": [
                            {
                                "id": "live1",
                                "snippet": {
                                    "title": "지금 방송",
                                    "channelId": "UC1",
                                    "channelTitle": "하나",
                                },
                                "liveStreamingDetails": {"actualStartTime": "2026-01-01T00:00:00Z"},
                            },
                            {
                                "id": "old1",
                                "snippet": {"title": "지난 방송", "channelId": "UC1"},
                                "liveStreamingDetails": {
                                    "actualStartTime": "2026-01-01T00:00:00Z",
                                    "actualEndTime": "2026-01-01T01:00:00Z",
                                },
                            },
                        ]
                    },
                )
            ],
        }
    )
    api = YouTubeApi(session)
    lives = api.find_lives(["UC1"])
    assert [item.video_id for item in lives] == ["live1"]
    assert lives[0].title == "지금 방송"
    assert "search" not in "".join(url for url, _ in session.calls)
    assert all(API_ROOT in url for url, _ in session.calls)


def test_uploads_플레이리스트를_캐시한다() -> None:
    session = ScriptedSession(
        {
            "channels": [
                FakeResponse(
                    200,
                    {
                        "items": [
                            {
                                "id": "UC1",
                                "contentDetails": {"relatedPlaylists": {"uploads": "UU1"}},
                            }
                        ]
                    },
                )
            ],
            "playlistItems": [
                FakeResponse(200, {"items": []}),
                FakeResponse(200, {"items": []}),
            ],
        }
    )
    api = YouTubeApi(session)
    api.find_lives(["UC1"])
    api.find_lives(["UC1"])
    channel_calls = [url for url, _ in session.calls if url.endswith("/channels")]
    assert len(channel_calls) == 1


def test_인증_오류를_구분한다() -> None:
    session = ScriptedSession(
        {
            "subscriptions": [
                FakeResponse(
                    401,
                    {"error": {"message": "invalid", "errors": [{"reason": "authError"}]}},
                )
            ]
        }
    )
    api = YouTubeApi(session)
    with pytest.raises(YouTubeError) as caught:
        api.list_subscriptions()
    assert caught.value.kind == "auth"


def test_쿼터_초과를_구분한다() -> None:
    session = ScriptedSession(
        {
            "subscriptions": [
                FakeResponse(
                    403,
                    {"error": {"message": "quota", "errors": [{"reason": "quotaExceeded"}]}},
                )
            ]
        }
    )
    api = YouTubeApi(session)
    with pytest.raises(YouTubeError) as caught:
        api.list_subscriptions()
    assert caught.value.kind == "quota"
