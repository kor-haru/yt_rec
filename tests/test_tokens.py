from __future__ import annotations

import sys
import uuid

import pytest

from yt_rec.backend.tokens import MemoryTokenStore, WindowsCredentialStore, default_token_store


def test_메모리_저장소는_읽고_쓰고_지운다() -> None:
    store = MemoryTokenStore()
    assert store.load() is None
    store.save('{"refresh_token":"abc"}')
    assert store.load() == '{"refresh_token":"abc"}'
    store.clear()
    assert store.load() is None


def test_기본_저장소는_윈도우에서_credential_manager다() -> None:
    store = default_token_store()
    if sys.platform == "win32":
        assert isinstance(store, WindowsCredentialStore)
    else:
        with pytest.raises(Exception):
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
