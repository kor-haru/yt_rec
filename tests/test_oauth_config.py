from __future__ import annotations

import json

import pytest

from yt_rec.backend.oauth import (
    ENV_CLIENT_ID,
    ENV_CLIENT_SECRET,
    ENV_CLIENT_SECRETS,
    YOUTUBE_READONLY,
    ClientConfigError,
    GoogleAuth,
    load_client_config,
)


def test_환경_변수에서_installed_설정을_만든다(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv(ENV_CLIENT_SECRETS, raising=False)
    monkeypatch.setenv(ENV_CLIENT_ID, "id.apps.googleusercontent.com")
    monkeypatch.setenv(ENV_CLIENT_SECRET, "secret")
    monkeypatch.setattr(
        "yt_rec.backend.oauth._default_secrets_path", lambda: tmp_path / "missing.json"
    )
    config = load_client_config()
    installed = config["installed"]
    assert installed["client_id"] == "id.apps.googleusercontent.com"
    assert installed["client_secret"] == "secret"
    assert installed["redirect_uris"] == ["http://localhost"]


def test_secrets_파일_경로를_읽는다(monkeypatch, tmp_path) -> None:
    path = tmp_path / "client_secrets.json"
    path.write_text(
        json.dumps({"installed": {"client_id": "from-file", "client_secret": "s"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv(ENV_CLIENT_SECRETS, str(path))
    config = load_client_config()
    assert config["installed"]["client_id"] == "from-file"


def test_비밀이_없으면_오류다(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv(ENV_CLIENT_ID, raising=False)
    monkeypatch.delenv(ENV_CLIENT_SECRET, raising=False)
    monkeypatch.delenv(ENV_CLIENT_SECRETS, raising=False)
    monkeypatch.setattr(
        "yt_rec.backend.oauth._default_secrets_path", lambda: tmp_path / "missing.json"
    )
    with pytest.raises(ClientConfigError):
        load_client_config()


def test_요청_권한은_readonly_하나다() -> None:
    assert YOUTUBE_READONLY.endswith("youtube.readonly")
    assert "youtube.force-ssl" not in YOUTUBE_READONLY


def test_로그인_대기는_시간_제한이_있다(monkeypatch) -> None:
    import yt_rec.backend.oauth as oauth

    monkeypatch.setattr(oauth, "LOGIN_TIMEOUT_SECONDS", 0.05)

    class Flow:
        @classmethod
        def from_client_config(cls, *_a, **_k):
            return cls()

        def run_local_server(self, **_k):
            import time

            time.sleep(2)
            return "never"

    monkeypatch.setattr("google_auth_oauthlib.flow.InstalledAppFlow", Flow)
    with pytest.raises(oauth.AuthError, match="대기"):
        oauth._run_installed_app({"installed": {"client_id": "x", "client_secret": "y"}})
