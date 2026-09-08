"""Google 공식 OAuth 2.0 (설치된 앱 + 로컬 루프백).

시스템 기본 브라우저를 연다. QtWebEngine 을 쓰지 않는다.
권한은 ``youtube.readonly`` 만 요청한다.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

__all__ = [
    "YOUTUBE_READONLY",
    "ClientConfigError",
    "AuthError",
    "GoogleAuth",
    "load_client_config",
    "import_client_config",
    "ENV_CLIENT_ID",
    "ENV_CLIENT_SECRET",
    "ENV_CLIENT_SECRETS",
    "LOGIN_TIMEOUT_SECONDS",
]

YOUTUBE_READONLY = "https://www.googleapis.com/auth/youtube.readonly"
LOGIN_TIMEOUT_SECONDS = 180

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


def _read_client_config(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as extra:
        raise ClientConfigError("OAuth JSON 파일을 읽지 못했습니다. 내려받은 파일을 다시 선택하세요.") from extra
    return _validate_client_config(data)


def _validate_client_config(data: object) -> dict[str, Any]:
    installed = data.get("installed") if isinstance(data, dict) else None
    if not isinstance(installed, dict):
        raise ClientConfigError("Google Auth Platform에서 '데스크톱 앱' 유형으로 만든 OAuth JSON 파일을 선택하세요.")
    for key in ("client_id", "client_secret"):
        if not isinstance(installed.get(key), str) or not installed[key].strip():
            raise ClientConfigError(
                "OAuth JSON에 클라이언트 ID 또는 보안 비밀이 없습니다. "
                "Google Auth Platform에서 내려받은 데스크톱 앱 JSON을 가져오세요. 클라이언트 ID만으로는 연결할 수 없습니다."
            )
    endpoints = {
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    for key, default in endpoints.items():
        allowed = {default}
        if key == "auth_uri":
            allowed.add("https://accounts.google.com/o/oauth2/v2/auth")
        if installed.get(key, default) not in allowed:
            raise ClientConfigError("Google 공식 인증 주소가 아닌 OAuth 설정은 사용할 수 없습니다.")
    return {"installed": {**endpoints, **installed}}


def import_client_config(source: Path | str) -> Path:
    """검증한 Desktop JSON만 사용자 설정에 원자적으로 저장한다."""
    if (os.environ.get(ENV_CLIENT_SECRETS) or "").strip():
        raise ClientConfigError(
            f"{ENV_CLIENT_SECRETS} 환경 변수로 설정 파일이 지정되어 있습니다. "
            "해당 설정을 해제하고 앱을 다시 연 뒤 가져오세요."
        )
    config = _read_client_config(Path(source))
    target = _default_secrets_path()
    temporary: Path | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(config, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, target)
    except OSError as extra:
        raise ClientConfigError("OAuth 설정을 저장하지 못했습니다. 설정 폴더의 쓰기 권한을 확인하세요.") from extra
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target


def load_client_config() -> dict[str, Any]:
    """환경 변수 또는 사용자 설정 경로에서 OAuth 클라이언트 설정을 읽는다.

    저장소에 커밋된 값은 쓰지 않는다.
    """
    secrets = (os.environ.get(ENV_CLIENT_SECRETS) or "").strip()
    path = Path(secrets) if secrets else _default_secrets_path()
    if path.is_file():
        return _read_client_config(path)

    client_id = (os.environ.get(ENV_CLIENT_ID) or "").strip()
    client_secret = (os.environ.get(ENV_CLIENT_SECRET) or "").strip()
    if not client_id or not client_secret:
        raise ClientConfigError(
            "Google 로그인 설정이 없습니다. 계정 화면에서 'OAuth JSON 가져오기'를 눌러 "
            "Google Auth Platform에서 내려받은 데스크톱 앱 JSON 파일을 선택하세요. "
            "등록 방법은 README의 사용법에 있습니다."
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
    box: dict[str, Any] = {}
    done = threading.Event()

    def work() -> None:
        try:
            box["creds"] = flow.run_local_server(
                host="127.0.0.1",
                bind_addr="127.0.0.1",
                port=0,
                open_browser=True,
                authorization_prompt_message="",
                timeout_seconds=LOGIN_TIMEOUT_SECONDS,
                success_message="승인 요청을 받았습니다. 앱에서 연결 결과를 확인하세요. 이 창은 닫아도 됩니다.",
                access_type="offline",
                prompt="select_account consent",
            )
        except Exception as extra:  # noqa: BLE001 - 로그인 스레드에서 AuthError 로 올린다
            box["exc"] = extra
        finally:
            done.set()

    threading.Thread(target=work, name="yt-rec-oauth", daemon=True).start()
    if not done.wait(LOGIN_TIMEOUT_SECONDS):
        raise AuthError(f"로그인 대기 {LOGIN_TIMEOUT_SECONDS}초가 지났다")
    if "exc" in box:
        raise box["exc"]
    return box["creds"]


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
        config = _validate_client_config(config)
        try:
            self.credentials = self._flow_runner(config)
        except ClientConfigError:
            raise
        except Exception as extra:
            raise AuthError(f"Google 로그인에 실패했다: {extra}") from extra
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
        except Exception as extra:
            raise AuthError(f"저장된 인증 정보를 읽지 못했다: {extra}") from extra
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as extra:
                text = str(extra).lower()
                if "invalid_grant" in text or "revoked" in text:
                    raise AuthError(f"토큰을 갱신하지 못했다: {extra}") from extra
                raise
        if not creds.valid:
            raise AuthError("저장된 인증이 유효하지 않다")
        self.credentials = creds
        return creds

    def dump(self, credentials: Any | None = None) -> str:
        creds = credentials if credentials is not None else self.credentials
        if creds is None:
            raise AuthError("저장할 인증이 없다")
        return creds.to_json()
