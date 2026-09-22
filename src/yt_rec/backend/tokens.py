"""OAuth 자격증명 저장소.

refresh token 은 평문 파일에 두지 않는다. 호출부는 :class:`TokenStore` 만 본다.
OS 분기는 :func:`default_token_store` 한 곳에만 있다.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from ctypes import wintypes
from typing import Protocol

__all__ = [
    "TokenStore",
    "TokenStoreError",
    "MemoryTokenStore",
    "WindowsCredentialStore",
    "MacOSKeychainStore",
    "LinuxSecretServiceStore",
    "default_token_store",
    "CREDENTIAL_TARGET",
    "KEYCHAIN_ACCOUNT",
]

CREDENTIAL_TARGET = "yt-rec/google-oauth"
"""OS 보안 저장소에 쓰는 대상 이름. Windows 는 Credential Manager, macOS 는 Keychain service."""

KEYCHAIN_ACCOUNT = "yt-rec"
"""macOS Keychain generic password 의 account 필드."""

_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_NOT_FOUND = 1168
_ERR_SEC_ITEM_NOT_FOUND = -25300
_ERR_SEC_DUPLICATE_ITEM = -25299
_SECURITY_FRAMEWORK = "/System/Library/Frameworks/Security.framework/Security"
_CORE_FOUNDATION = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"


class TokenStoreError(RuntimeError):
    """자격증명을 읽거나 쓰지 못했다."""


class TokenStore(Protocol):
    """자격증명 JSON 덩어리를 읽고 쓴다. 내용은 OAuth 클라이언트만 해석한다."""

    def load(self) -> str | None:
        """저장된 값이 없으면 ``None``."""

    def save(self, blob: str) -> None:
        """``blob`` 전체를 저장한다. refresh token 이 포함될 수 있다."""

    def clear(self) -> None:
        """저장된 값을 지운다. 없어도 오류로 치지 않는다."""


class MemoryTokenStore:
    """프로세스 메모리. 테스트와 주입용."""

    def __init__(self, blob: str | None = None) -> None:
        self._blob = blob

    def load(self) -> str | None:
        return self._blob

    def save(self, blob: str) -> None:
        self._blob = blob

    def clear(self) -> None:
        self._blob = None


class WindowsCredentialStore:
    """Windows Credential Manager (GENERIC credential)."""

    def __init__(self, target: str = CREDENTIAL_TARGET) -> None:
        if sys.platform != "win32":
            raise TokenStoreError("Windows Credential Manager 는 win32 에서만 쓴다")
        self.target = target

    def load(self) -> str | None:
        return _cred_read(self.target)

    def save(self, blob: str) -> None:
        _cred_write(self.target, blob)

    def clear(self) -> None:
        _cred_delete(self.target)


class MacOSKeychainStore:
    """macOS Keychain generic password. 비밀은 Security.framework 로 직접 전달한다."""

    def __init__(
        self,
        service: str = CREDENTIAL_TARGET,
        account: str = KEYCHAIN_ACCOUNT,
    ) -> None:
        if sys.platform != "darwin":
            raise TokenStoreError("Keychain 은 macOS 에서만 쓴다")
        self.service = service
        self.account = account

    def load(self) -> str | None:
        return _keychain_read(self.service, self.account)

    def save(self, blob: str) -> None:
        _keychain_write(self.service, self.account, blob)

    def clear(self) -> None:
        _keychain_delete(self.service, self.account)


class _UnsupportedTokenStore:
    def load(self) -> str | None:
        raise TokenStoreError("이 OS에서는 보안 저장소를 지원하지 않습니다.")

    def save(self, blob: str) -> None:
        raise TokenStoreError(
            "이 빌드는 Windows Credential Manager, macOS Keychain, Linux Secret Service를 지원한다. "
            "refresh token 을 평문 파일에 저장하지 않는다."
        )

    def clear(self) -> None:
        return None


class LinuxSecretServiceStore:
    """데스크톱 Secret Service. secret-tool의 stdin으로만 비밀을 전달한다."""

    def __init__(self, service: str = CREDENTIAL_TARGET, account: str = KEYCHAIN_ACCOUNT) -> None:
        self.service = service
        self.account = account

    def _run(self, action: str, blob: str | None = None) -> subprocess.CompletedProcess[str]:
        args = ["secret-tool", action]
        if action == "store":
            args.append("--label=yt-rec Google OAuth")
        args.extend(["service", self.service, "account", self.account])
        options: dict = {}
        if os.name == "nt":
            # 이 저장소는 Linux 용이지만 직접 세우면 Windows 에서도 돈다. 콘솔 없는
            # GUI 런처에서 토큰을 읽을 때마다 창이 뜨지 않게 막아 둔다(#96).
            options["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        try:
            proc = subprocess.run(
                args, input=blob, capture_output=True, text=True, timeout=30, **options
            )
        except (OSError, subprocess.TimeoutExpired) as extra:
            raise TokenStoreError(
                "Linux 보안 저장소에 연결하지 못했습니다. secret-tool(libsecret-tools)을 설치하고 "
                "로그인 키링을 잠금 해제하세요."
            ) from extra
        if proc.returncode != 0 and not (
            action in {"lookup", "clear"} and proc.returncode == 1
            and not proc.stderr.strip() and not proc.stdout.strip()
        ):
            raise TokenStoreError("Linux 보안 저장소를 사용할 수 없습니다. 로그인 키링을 확인하세요.")
        return proc

    def load(self) -> str | None:
        proc = self._run("lookup")
        return proc.stdout.rstrip("\r\n") if proc.returncode == 0 else None

    def save(self, blob: str) -> None:
        self._run("store", blob)

    def clear(self) -> None:
        self._run("clear")


def default_token_store() -> TokenStore:
    """현재 OS 에 맞는 저장소. 호출부가 OS 를 다시 보지 않게 한다."""
    if sys.platform == "win32":
        return WindowsCredentialStore()
    if sys.platform == "darwin":
        return MacOSKeychainStore()
    if sys.platform.startswith("linux"):
        return LinuxSecretServiceStore()
    return _UnsupportedTokenStore()


def _security_framework():
    try:
        dll = ctypes.CDLL(_SECURITY_FRAMEWORK)
    except (OSError, AttributeError) as extra:
        raise TokenStoreError("macOS Keychain에 연결하지 못했습니다.") from extra
    dll.SecKeychainAddGenericPassword.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    dll.SecKeychainAddGenericPassword.restype = ctypes.c_int32
    dll.SecKeychainFindGenericPassword.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    dll.SecKeychainFindGenericPassword.restype = ctypes.c_int32
    dll.SecKeychainItemModifyAttributesAndData.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    dll.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
    dll.SecKeychainItemDelete.argtypes = [ctypes.c_void_p]
    dll.SecKeychainItemDelete.restype = ctypes.c_int32
    dll.SecKeychainItemFreeContent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    dll.SecKeychainItemFreeContent.restype = ctypes.c_int32
    return dll


def _cf_release(value: ctypes.c_void_p) -> None:
    try:
        dll = ctypes.CDLL(_CORE_FOUNDATION)
    except (OSError, AttributeError) as extra:
        raise TokenStoreError("macOS Keychain에 연결하지 못했습니다.") from extra
    dll.CFRelease.argtypes = [ctypes.c_void_p]
    dll.CFRelease.restype = None
    dll.CFRelease(value)


def _keychain_names(service: str, account: str) -> tuple[bytes, bytes]:
    return service.encode("utf-8"), account.encode("utf-8")


def _find_keychain_item(dll, service: bytes, account: bytes) -> tuple[int, ctypes.c_void_p]:
    item = ctypes.c_void_p()
    status = dll.SecKeychainFindGenericPassword(
        None,
        len(service),
        service,
        len(account),
        account,
        None,
        None,
        ctypes.byref(item),
    )
    return status, item


def _keychain_read(service: str, account: str) -> str | None:
    dll = _security_framework()
    service_bytes, account_bytes = _keychain_names(service, account)
    length = ctypes.c_uint32()
    data = ctypes.c_void_p()
    status = dll.SecKeychainFindGenericPassword(
        None,
        len(service_bytes),
        service_bytes,
        len(account_bytes),
        account_bytes,
        ctypes.byref(length),
        ctypes.byref(data),
        None,
    )
    if status == _ERR_SEC_ITEM_NOT_FOUND:
        return None
    if status != 0:
        raise TokenStoreError(f"Keychain 을 읽지 못했다: {status}")
    try:
        raw = ctypes.string_at(data, length.value) if length.value else b""
        return raw.decode("utf-8")
    except UnicodeDecodeError as extra:
        raise TokenStoreError("Keychain 값을 UTF-8로 읽지 못했다") from extra
    finally:
        if data.value:
            dll.SecKeychainItemFreeContent(None, data)


def _keychain_write(service: str, account: str, blob: str) -> None:
    dll = _security_framework()
    service_bytes, account_bytes = _keychain_names(service, account)
    secret = blob.encode("utf-8")
    buffer = ctypes.create_string_buffer(secret, len(secret) + 1)
    data = ctypes.cast(buffer, ctypes.c_void_p)
    status = dll.SecKeychainAddGenericPassword(
        None,
        len(service_bytes),
        service_bytes,
        len(account_bytes),
        account_bytes,
        len(secret),
        data,
        None,
    )
    if status == _ERR_SEC_DUPLICATE_ITEM:
        status, item = _find_keychain_item(dll, service_bytes, account_bytes)
        if status == 0:
            try:
                status = dll.SecKeychainItemModifyAttributesAndData(
                    item, None, len(secret), data
                )
            finally:
                if item.value:
                    _cf_release(item)
    if status != 0:
        raise TokenStoreError(f"Keychain 에 쓰지 못했다: {status}")


def _keychain_delete(service: str, account: str) -> None:
    dll = _security_framework()
    service_bytes, account_bytes = _keychain_names(service, account)
    status, item = _find_keychain_item(dll, service_bytes, account_bytes)
    if status == _ERR_SEC_ITEM_NOT_FOUND:
        return
    if status != 0:
        raise TokenStoreError(f"Keychain 에서 지우지 못했다: {status}")
    try:
        status = dll.SecKeychainItemDelete(item)
    finally:
        if item.value:
            _cf_release(item)
    if status not in (0, _ERR_SEC_ITEM_NOT_FOUND):
        raise TokenStoreError(f"Keychain 에서 지우지 못했다: {status}")


class _CREDENTIAL(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def _advapi32():
    dll = ctypes.WinDLL("advapi32", use_last_error=True)
    dll.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIAL), wintypes.DWORD]
    dll.CredWriteW.restype = wintypes.BOOL
    dll.CredReadW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(_CREDENTIAL)),
    ]
    dll.CredReadW.restype = wintypes.BOOL
    dll.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    dll.CredDeleteW.restype = wintypes.BOOL
    dll.CredFree.argtypes = [ctypes.c_void_p]
    return dll


def _cred_write(target: str, blob: str) -> None:
    data = blob.encode("utf-8")
    buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    cred = _CREDENTIAL()
    cred.Type = _CRED_TYPE_GENERIC
    cred.TargetName = target
    cred.CredentialBlobSize = len(data)
    cred.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
    cred.Persist = _CRED_PERSIST_LOCAL_MACHINE
    cred.UserName = "yt-rec"
    if not _advapi32().CredWriteW(ctypes.byref(cred), 0):
        raise TokenStoreError(f"Credential Manager 에 쓰지 못했다: {ctypes.get_last_error()}")
    _ = buf


def _cred_read(target: str) -> str | None:
    dll = _advapi32()
    ptr = ctypes.POINTER(_CREDENTIAL)()
    if not dll.CredReadW(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(ptr)):
        err = ctypes.get_last_error()
        if err == _ERROR_NOT_FOUND:
            return None
        raise TokenStoreError(f"Credential Manager 를 읽지 못했다: {err}")
    try:
        cred = ptr.contents
        size = int(cred.CredentialBlobSize)
        if size <= 0 or not cred.CredentialBlob:
            return ""
        raw = ctypes.cast(cred.CredentialBlob, ctypes.POINTER(ctypes.c_ubyte * size)).contents
        return bytes(raw).decode("utf-8")
    finally:
        dll.CredFree(ptr)


def _cred_delete(target: str) -> None:
    dll = _advapi32()
    if dll.CredDeleteW(target, _CRED_TYPE_GENERIC, 0):
        return
    err = ctypes.get_last_error()
    if err == _ERROR_NOT_FOUND:
        return
    raise TokenStoreError(f"Credential Manager 에서 지우지 못했다: {err}")
