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


def test_로그인할_때_개발자와_다른_계정을_선택할_수_있다(monkeypatch) -> None:
    import yt_rec.backend.oauth as oauth

    calls = {}

    class Flow:
        @classmethod
        def from_client_config(cls, config, *, scopes):
            calls["scopes"] = scopes
            return cls()

        def run_local_server(self, **kwargs):
            calls.update(kwargs)
            return "credentials"

    monkeypatch.setattr("google_auth_oauthlib.flow.InstalledAppFlow", Flow)
    assert oauth._run_installed_app({}) == "credentials"
    assert set(calls["prompt"].split()) == {"select_account", "consent"}
    assert "login_hint" not in calls
    assert calls["authorization_prompt_message"] == ""
    assert calls["timeout_seconds"] == oauth.LOGIN_TIMEOUT_SECONDS
    assert "앱에서 연결 결과" in calls["success_message"]
    assert calls["scopes"] == [YOUTUBE_READONLY]


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


@pytest.mark.parametrize("config", [
    [], {"web": {"client_id": "x", "client_secret": "y"}},
    {"installed": {"client_secret": "y"}},
    {"installed": {"client_id": "x"}},
    {"installed": {"client_id": "x", "client_secret": ""}},
    {"installed": {"client_id": "x", "client_secret": "y", "token_uri": "https://example.com/token"}},
])
def test_잘못된_JSON은_기존_설정을_덮어쓰지_않는다(monkeypatch, tmp_path, config) -> None:
    import yt_rec.backend.oauth as oauth

    monkeypatch.delenv(ENV_CLIENT_SECRETS, raising=False)
    target = tmp_path / "saved.json"
    target.write_text("existing config", encoding="utf-8")
    source = tmp_path / "download.json"
    source.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(oauth, "_default_secrets_path", lambda: target)
    with pytest.raises(ClientConfigError):
        oauth.import_client_config(source)
    assert target.read_text(encoding="utf-8") == "existing config"


def test_클라이언트_ID만_있으면_브라우저를_열기_전에_설정을_안내한다() -> None:
    called = []
    auth = GoogleAuth(client_config={"installed": {"client_id": "id"}}, flow_runner=called.append)
    with pytest.raises(ClientConfigError, match="클라이언트 ID만으로는"):
        auth.login()
    assert called == []


def test_내려받은_Desktop_JSON을_가져와서_바로_로그인에_쓴다(monkeypatch, tmp_path) -> None:
    import yt_rec.backend.oauth as oauth

    monkeypatch.delenv(ENV_CLIENT_SECRETS, raising=False)
    target = tmp_path / "config" / "client_secrets.json"
    source = tmp_path / "download.json"
    source.write_text(json.dumps({"installed": {"client_id": "id", "client_secret": "secret"}}), encoding="utf-8-sig")
    monkeypatch.setattr(oauth, "_default_secrets_path", lambda: target)
    assert oauth.import_client_config(source) == target
    assert oauth.load_client_config()["installed"]["client_id"] == "id"
    assert list(target.parent.iterdir()) == [target]


def test_토큰_갱신_철회는_재로그인이_필요한_오류다(monkeypatch) -> None:
    from google.auth.exceptions import RefreshError
    import yt_rec.backend.oauth as oauth

    class Credentials:
        expired = True
        refresh_token = "revoked"

        @classmethod
        def from_authorized_user_info(cls, info, scopes):
            return cls()

        def refresh(self, request):
            raise RefreshError("invalid_grant: Token has been expired or revoked")

    monkeypatch.setattr("google.oauth2.credentials.Credentials", Credentials)
    with pytest.raises(oauth.AuthError):
        GoogleAuth().restore("{}")
