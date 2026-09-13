from __future__ import annotations

import ctypes
import sys
import uuid

import pytest

from yt_rec.backend.tokens import (
    LinuxSecretServiceStore,
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
    elif sys.platform.startswith("linux"):
        assert isinstance(store, LinuxSecretServiceStore)
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


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS Keychain")
def test_macos_keychain_고유한_임시_항목을_왕복한다() -> None:
    service = f"yt-rec-test/{uuid.uuid4()}"
    account = f"test-{uuid.uuid4()}"
    store = MacOSKeychainStore(service=service, account=account)
    try:
        assert store.load() is None
        store.save("첫 값 ' \" $ ; 줄바꿈\n🍎")
        assert store.load() == "첫 값 ' \" $ ; 줄바꿈\n🍎"
        store.save("갱신 값\\경로\t끝")
        assert store.load() == "갱신 값\\경로\t끝"
    finally:
        store.clear()
        assert store.load() is None


class _Proc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeSecurityFramework:
    def __init__(self) -> None:
        self.items: dict[tuple[bytes, bytes], bytes] = {}
        self.references: dict[int, tuple[bytes, bytes]] = {}
        self.buffers: dict[int, ctypes.Array] = {}
        self.fail: dict[str, int] = {}
        self.add_calls = 0
        self.modify_calls = 0
        self.delete_calls = 0
        self.freed_data = 0
        self.released_items = 0
        self._next_reference = 1

    @staticmethod
    def _key(service_length, service, account_length, account) -> tuple[bytes, bytes]:
        return bytes(service[:service_length]), bytes(account[:account_length])

    @staticmethod
    def _reference_value(reference) -> int:
        return int(reference.value if isinstance(reference, ctypes.c_void_p) else reference)

    @staticmethod
    def _set_pointer(pointer, value: int) -> None:
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p)).contents.value = value

    def SecKeychainFindGenericPassword(
        self,
        _keychain,
        service_length,
        service,
        account_length,
        account,
        password_length,
        password_data,
        item_reference,
    ) -> int:
        if "find" in self.fail:
            return self.fail["find"]
        key = self._key(service_length, service, account_length, account)
        secret = self.items.get(key)
        if secret is None:
            return -25300
        if password_length is not None:
            ctypes.cast(
                password_length, ctypes.POINTER(ctypes.c_uint32)
            ).contents.value = len(secret)
        if password_data is not None:
            buffer = ctypes.create_string_buffer(secret, len(secret) + 1)
            address = ctypes.addressof(buffer)
            self.buffers[address] = buffer
            self._set_pointer(password_data, address)
        if item_reference is not None:
            reference = self._next_reference
            self._next_reference += 1
            self.references[reference] = key
            self._set_pointer(item_reference, reference)
        return 0

    def SecKeychainAddGenericPassword(
        self,
        _keychain,
        service_length,
        service,
        account_length,
        account,
        password_length,
        password_data,
        _item_reference,
    ) -> int:
        self.add_calls += 1
        if "add" in self.fail:
            return self.fail["add"]
        key = self._key(service_length, service, account_length, account)
        if key in self.items:
            return -25299
        self.items[key] = ctypes.string_at(password_data, password_length)
        return 0

    def SecKeychainItemModifyAttributesAndData(
        self, item_reference, _attributes, password_length, password_data
    ) -> int:
        self.modify_calls += 1
        if "modify" in self.fail:
            return self.fail["modify"]
        key = self.references[self._reference_value(item_reference)]
        self.items[key] = ctypes.string_at(password_data, password_length)
        return 0

    def SecKeychainItemDelete(self, item_reference) -> int:
        self.delete_calls += 1
        if "delete" in self.fail:
            return self.fail["delete"]
        key = self.references[self._reference_value(item_reference)]
        self.items.pop(key, None)
        return 0

    def SecKeychainItemFreeContent(self, _attributes, data) -> int:
        self.freed_data += 1
        self.buffers.pop(self._reference_value(data), None)
        return 0

    def release(self, item_reference) -> None:
        self.released_items += 1
        self.references.pop(self._reference_value(item_reference), None)


def _fake_keychain(monkeypatch) -> _FakeSecurityFramework:
    monkeypatch.setattr("yt_rec.backend.tokens.sys.platform", "darwin")
    api = _FakeSecurityFramework()
    monkeypatch.setattr("yt_rec.backend.tokens._security_framework", lambda: api)
    monkeypatch.setattr("yt_rec.backend.tokens._cf_release", api.release)
    monkeypatch.setattr(
        "yt_rec.backend.tokens.subprocess.run",
        lambda *_args, **_kwargs: pytest.fail("macOS Keychain must not start a process"),
    )
    return api


def test_keychain_비밀은_native_api로만_읽고_쓰고_갱신한다(monkeypatch) -> None:
    api = _fake_keychain(monkeypatch)
    service = "yt-rec-test/서비스🔐"
    account = "계정 이름"
    store = MacOSKeychainStore(service=service, account=account)
    first = "space ' \" $ ; newline\n한글🍎"
    updated = "두 번째 값\\경로\t끝"

    assert store.load() is None
    store.clear()
    store.save(first)
    assert store.load() == first
    store.save(updated)
    assert store.load() == updated
    store.clear()
    assert store.load() is None
    assert api.items == {}
    assert api.add_calls == 2
    assert api.modify_calls == 1
    assert api.delete_calls == 1
    assert api.freed_data == 2
    assert api.released_items == 2


@pytest.mark.parametrize(
    ("operation", "failure", "message"),
    [
        ("load", "find", "읽지 못했다"),
        ("save", "add", "쓰지 못했다"),
        ("update", "modify", "쓰지 못했다"),
        ("clear", "delete", "지우지 못했다"),
    ],
)
def test_keychain_오류는_비밀을_노출하거나_무시하지_않는다(
    monkeypatch, operation: str, failure: str, message: str
) -> None:
    api = _fake_keychain(monkeypatch)
    key = (b"yt-rec-test/svc", b"yt-rec")
    if operation in {"update", "clear"}:
        api.items[key] = b"old"
    api.fail[failure] = -34018
    store = MacOSKeychainStore(service=key[0].decode(), account=key[1].decode())

    with pytest.raises(TokenStoreError, match=message) as caught:
        if operation == "load":
            store.load()
        elif operation == "clear":
            store.clear()
        else:
            store.save("private-token")
    assert "private-token" not in str(caught.value)


def test_keychain_은_macos가_아니면_만들지_않는다() -> None:
    if sys.platform == "darwin":
        pytest.skip("이 검사는 darwin 이 아닐 때만 의미가 있다")
    with pytest.raises(TokenStoreError):
        MacOSKeychainStore()


def test_linux는_secret_tool을_쓰고_비밀은_인자에_넣지_않는다(monkeypatch) -> None:
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return _Proc(0, stdout='{"refresh_token":"private"}\n') if len(calls) == 3 else _Proc(1 if args[1] == "lookup" else 0)

    monkeypatch.setattr("yt_rec.backend.tokens.subprocess.run", run)
    monkeypatch.setattr("yt_rec.backend.tokens.sys.platform", "linux")
    store = default_token_store()
    assert store.load() is None
    store.save('{"refresh_token":"private"}')
    assert store.load() == '{"refresh_token":"private"}'
    store.clear()
    assert [call[0][1] for call in calls] == ["lookup", "store", "lookup", "clear"]
    assert calls[1][1]["input"] == '{"refresh_token":"private"}'
    assert all("private" not in " ".join(args) for args, _ in calls)
    assert all(kwargs["timeout"] == 30 for _, kwargs in calls)


@pytest.mark.parametrize("action", ["load", "save", "clear"])
def test_linux_키링_오류는_토큰을_노출하거나_조용히_무시하지_않는다(monkeypatch, action) -> None:
    monkeypatch.setattr("yt_rec.backend.tokens.subprocess.run", lambda *_a, **_k: _Proc(1, stderr="locked private-token"))
    store = LinuxSecretServiceStore()
    with pytest.raises(TokenStoreError) as caught:
        getattr(store, action)("private-token") if action == "save" else getattr(store, action)()
    assert "private-token" not in str(caught.value)


def test_secret_tool이_없으면_설치_안내를_준다(monkeypatch) -> None:
    def run(*args, **kwargs):
        raise FileNotFoundError("secret-tool")

    monkeypatch.setattr("yt_rec.backend.tokens.subprocess.run", run)
    with pytest.raises(TokenStoreError, match="libsecret-tools"):
        LinuxSecretServiceStore().load()
