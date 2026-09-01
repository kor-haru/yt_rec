from __future__ import annotations

import sys
import uuid

import pytest

from yt_rec.backend.tokens import (
    MacOSKeychainStore,
    MemoryTokenStore,
    TokenStoreError,
    WindowsCredentialStore,
    default_token_store,
)


def test_메모리_저장소는_읽고_쓰고_지운다() -> None:
    store = MemoryTokenStore()
    assert store.load() is None
    store.save('{"refresh_token":"abc"}')
    assert store.load() == '{"refresh_token":"abc"}'
    store.clear()
    assert store.load() is None


def test_기본_저장소는_os에_맞는_보안_저장소다() -> None:
    store = default_token_store()
    if sys.platform == "win32":
        assert isinstance(store, WindowsCredentialStore)
    elif sys.platform == "darwin":
        assert isinstance(store, MacOSKeychainStore)
    else:
        with pytest.raises(TokenStoreError):
            store.save("x")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Credential Manager")
def test_windows_credential_manager_왕복() -> None:
    target = f"yt-rec-test/{uuid.uuid4()}"
    store = WindowsCredentialStore(target)
    try:
        assert store.load() is None
        store.save('{"refresh_token":"secret-value"}')
        assert store.load() == '{"refresh_token":"secret-value"}'
    finally:
        store.clear()
        assert store.load() is None


class _Proc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_keychain_저장소는_security_명령을_쓴다(monkeypatch) -> None:
    monkeypatch.setattr("yt_rec.backend.tokens.sys.platform", "darwin")
    calls: list[list[str]] = []

    def fake_security(args: list[str]) -> _Proc:
        calls.append(args)
        if args[0] == "find-generic-password":
            if any(item == "-w" for item in args) and len(calls) == 1:
                return _Proc(44, stderr="could not be found")
            return _Proc(0, stdout='{"refresh_token":"k"}\n')
        return _Proc(0)

    monkeypatch.setattr("yt_rec.backend.tokens._security", fake_security)
    store = MacOSKeychainStore(service="yt-rec-test/svc", account="yt-rec")
    assert store.load() is None
    store.save('{"refresh_token":"k"}')
    assert store.load() == '{"refresh_token":"k"}'
    store.clear()
    assert [item[0] for item in calls] == [
        "find-generic-password",
        "add-generic-password",
        "find-generic-password",
        "delete-generic-password",
    ]
    assert "-U" in calls[1]
    assert calls[1][-1] == '{"refresh_token":"k"}'


def test_keychain_은_macos가_아니면_만들지_않는다() -> None:
    if sys.platform == "darwin":
        pytest.skip("이 검사는 darwin 이 아닐 때만 의미가 있다")
    with pytest.raises(TokenStoreError):
        MacOSKeychainStore()
