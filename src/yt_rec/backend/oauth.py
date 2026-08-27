"""Google 공식 OAuth 2.0 (설치된 앱 + 로컬 루프백).

시스템 기본 브라우저를 연다. QtWebEngine 을 쓰지 않는다.
권한은 ``youtube.readonly`` 만 요청한다.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

__all__ = [
    "YOUTUBE_READONLY",
    "ClientConfigError",
    "AuthError",
    "GoogleAuth",
    "load_client_config",
    "ENV_CLIENT_ID",
    "ENV_CLIENT_SECRET",
    "ENV_CLIENT_SECRETS",
]

YOUTUBE_READONLY = "https://www.googleapis.com/auth/youtube.readonly"

ENV_CLIENT_ID = "YT_REC_GOOGLE_CLIENT_ID"
ENV_CLIENT_SECRET = "YT_REC_GOOGLE_CLIENT_SECRET"
ENV_CLIENT_SECRETS = "YT_REC_GOOGLE_CLIENT_SECRETS"

FlowRunner = Callable[[dict[str, Any]], Any]


class ClientConfigError(RuntimeError):
    """클라이언트 ID/시크릿을 찾지 못했다."""


class AuthError(RuntimeError):
    """로그인 또는 저장된 토큰 복원에 실패했다."""


def _default_secrets_path() -> Path:
    from yt_rec.recording.options import default_settings_path

    return default_settings_path().parent / "client_secrets.json"


def load_client_config() -> dict[str, Any]:
    """환경 변수 또는 사용자 설정 경로에서 OAuth 클라이언트 설정을 읽는다.

    저장소에 커밋된 값은 쓰지 않는다.
    """
    secrets = (os.environ.get(ENV_CLIENT_SECRETS) or "").strip()
    path = Path(secrets) if secrets else _default_secrets_path()
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ClientConfigError(f"client_secrets.json 을 읽지 못했다: {exc}") from exc
        if "installed" not in data and "web" not in data:
            raise ClientConfigError("client_secrets.json 에 installed/web 항목이 없다")
        return data

    client_id = (os.environ.get(ENV_CLIENT_ID) or "").strip()
    client_secret = (os.environ.get(ENV_CLIENT_SECRET) or "").strip()
    if not client_id or not client_secret:
        raise ClientConfigError(
            "Google OAuth 클라이언트 ID/시크릿이 없다. 환경 변수 "
            f"{ENV_CLIENT_ID}, {ENV_CLIENT_SECRET} 또는 "
            f"{ENV_CLIENT_SECRETS}(client_secrets.json 경로)를 설정하라."
        )
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }


def _run_installed_app(config: dict[str, Any]) -> Any:
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_config(config, scopes=[YOUTUBE_READONLY])
    # 시스템 기본 브라우저 + 루프백. Chrome 전용이 아니다.
    return flow.run_local_server(
        host="127.0.0.1",
        bind_addr="127.0.0.1",
        port=0,
        open_browser=True,
        access_type="offline",
        prompt="consent",
    )


class GoogleAuth:
    """브라우저 로그인과 저장된 refresh token 복원."""

    def __init__(
        self,
        *,
        client_config: dict[str, Any] | None = None,
        flow_runner: FlowRunner | None = None,
    ) -> None:
        self._client_config = client_config
        self._flow_runner = flow_runner or _run_installed_app
        self.credentials: Any = None

    def login(self) -> Any:
        config = self._client_config if self._client_config is not None else load_client_config()
        try:
            self.credentials = self._flow_runner(config)
        except ClientConfigError:
            raise
        except Exception as exc:
            raise AuthError(f"Google 로그인에 실패했다: {exc}") from exc
        scopes = list(getattr(self.credentials, "scopes", None) or [])
        if scopes and YOUTUBE_READONLY not in scopes:
            raise AuthError(f"요청하지 않은 권한이 포함돼 있다: {scopes}")
        return self.credentials

    def restore(self, blob: str) -> Any:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        try:
            info = json.loads(blob)
            creds = Credentials.from_authorized_user_info(info, scopes=[YOUTUBE_READONLY])
        except Exception as exc:
            raise AuthError(f"저장된 인증 정보를 읽지 못했다: {exc}") from exc
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as exc:
                raise AuthError(f"토큰을 갱신하지 못했다: {exc}") from exc
        if not creds.valid:
            raise AuthError("저장된 인증이 유효하지 않다")
        self.credentials = creds
        return creds

    def dump(self, credentials: Any | None = None) -> str:
        creds = credentials if credentials is not None else self.credentials
        if creds is None:
            raise AuthError("저장할 인증이 없다")
        return creds.to_json()
