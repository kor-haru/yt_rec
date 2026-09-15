from __future__ import annotations

import time
import uuid

from PySide6.QtCore import QCoreApplication

from yt_rec.app import main
from yt_rec.instance import InstanceLock


def _name() -> str:
    return f"yt-rec-test-{uuid.uuid4().hex}"


def test_두_번째_실행은_기존_창_활성화를_요청하고_잠그지_않는다(qapp, tmp_path) -> None:
    name = _name()
    lock_path = tmp_path / "instance.lock"
    primary = InstanceLock(lock_path=lock_path, socket_name=name)
    raised: list[bool] = []
    primary.activate_requested.connect(lambda: raised.append(True))
    try:
        assert primary.acquire() is True
        secondary = InstanceLock(lock_path=lock_path, socket_name=name)
        assert secondary.acquire() is False
        deadline = time.monotonic() + 2
        while not raised and time.monotonic() < deadline:
            QCoreApplication.processEvents()
        assert raised == [True]
    finally:
        primary.close()


def test_주_인스턴스가_끝나면_다음_실행이_잠금을_갖는다(qapp, tmp_path) -> None:
    name = _name()
    lock_path = tmp_path / "instance.lock"
    first = InstanceLock(lock_path=lock_path, socket_name=name)
    assert first.acquire() is True
    first.close()
    second = InstanceLock(lock_path=lock_path, socket_name=name)
    try:
        assert second.acquire() is True
    finally:
        second.close()


def test_이미_실행_중이면_앱을_다시_구성하지_않는다(qapp, monkeypatch) -> None:
    monkeypatch.setattr("yt_rec.recording.binaries.prepare_bundled_environment", lambda: None)
    monkeypatch.setattr("yt_rec.app.InstanceLock.acquire", lambda self: False)

    def boom(*_args, **_kwargs):
        raise AssertionError("두 번째 프로세스는 앱을 구성하면 안 된다")

    monkeypatch.setattr("yt_rec.app.build_application", boom)
    assert main([]) == 0
