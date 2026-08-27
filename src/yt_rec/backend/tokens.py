"""OAuth 자격증명 저장소.

refresh token 은 평문 파일에 두지 않는다. 호출부는 :class:`TokenStore` 만 본다.
OS 분기는 :func:`default_token_store` 한 곳에만 있다.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import Protocol

__all__ = [
    "TokenStore",
    "TokenStoreError",
    "MemoryTokenStore",
    "WindowsCredentialStore",
    "default_token_store",
    "CREDENTIAL_TARGET",
]

CREDENTIAL_TARGET = "yt-rec/google-oauth"
"""Windows Credential Manager 에 쓰는 대상 이름."""

_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_NOT_FOUND = 1168


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


class _UnsupportedTokenStore:
    def load(self) -> str | None:
        return None

    def save(self, blob: str) -> None:
        raise TokenStoreError(
            "이 빌드는 Windows Credential Manager 만 지원한다. "
            "refresh token 을 평문 파일에 저장하지 않는다."
        )

    def clear(self) -> None:
        return None


def default_token_store() -> TokenStore:
    """현재 OS 에 맞는 저장소. 호출부가 OS 를 다시 보지 않게 한다."""
    if sys.platform == "win32":
        return WindowsCredentialStore()
    return _UnsupportedTokenStore()


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
