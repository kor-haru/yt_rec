from __future__ import annotations

import time
import uuid
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QCoreApplication, QLockFile, QObject

from yt_rec import app as application
from yt_rec.app import main
from yt_rec.instance import InstanceLock


def _name() -> str:
    return f"yt-rec-test-{uuid.uuid4().hex}"


@pytest.fixture(autouse=True)
def isolated_default_lock(monkeypatch, tmp_path):
    monkeypatch.setattr("yt_rec.instance.default_lock_path", lambda: tmp_path / "default.lock")


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


@pytest.fixture
def main_runtime(qapp, monkeypatch, tmp_path):
    primary = InstanceLock(qapp, lock_path=tmp_path / "main.lock", socket_name=_name())
    context = SimpleNamespace(
        app=SimpleNamespace(exec=Mock(return_value=0)),
        notifications=SimpleNamespace(stop=Mock()), source=SimpleNamespace(stop=Mock()),
    )
    desktop = SimpleNamespace(stopped=False, show_window=Mock(), show_initial=Mock())
    monkeypatch.setattr(application, "InstanceLock", lambda parent: primary)
    monkeypatch.setattr(application, "build_application", Mock(return_value=context))
    monkeypatch.setattr(application, "DesktopSession", Mock(return_value=desktop))
    monkeypatch.setattr(application, "set_app_id", lambda: None)
    monkeypatch.setattr("yt_rec.recording.binaries.prepare_bundled_environment", lambda: None)
    try:
        yield primary, context, desktop
    finally:
        primary.close()


@pytest.mark.parametrize("notification_stop_fails", [False, True])
def test_lock_survives_profile_deletion_and_backend_cleanup(main_runtime, notification_stop_fails):
    primary, context, _desktop = main_runtime
    observed = []

    def check_lock(stage):
        contender = QLockFile(str(primary._lock_path))
        acquired = contender.tryLock(0)
        observed.append((stage, primary._lock.isLocked(), acquired))
        contender.unlock()

    # Match the receiver's deferred page -> profile destruction without WebEngine.
    page, profile = QObject(), QObject()
    page.destroyed.connect(lambda: check_lock("page"))
    page.destroyed.connect(profile.deleteLater)
    profile.destroyed.connect(lambda: check_lock("profile"))

    def stop_notifications():
        check_lock("receiver")
        page.deleteLater()
        if notification_stop_fails:
            raise RuntimeError("SYNTHETIC notification stop failure")

    context.notifications.stop = stop_notifications
    context.source.stop = lambda: check_lock("backend")
    if notification_stop_fails:
        with pytest.raises(RuntimeError, match="SYNTHETIC notification stop"):
            main([])
    else:
        assert main([]) == 0
    assert observed == [(stage, True, False) for stage in ("receiver", "page", "profile", "backend")]
    assert not primary._lock.isLocked()
    contender = QLockFile(str(primary._lock_path))
    assert contender.tryLock(0)
    contender.unlock()


@pytest.mark.parametrize("stage", ["build", "desktop", "show"])
def test_startup_error_releases_lock_and_cleans_returned_context(main_runtime, stage):
    primary, context, desktop = main_runtime
    target = {"build": application.build_application, "desktop": application.DesktopSession,
              "show": desktop.show_initial}[stage]
    target.side_effect = RuntimeError("SYNTHETIC startup failure")
    with pytest.raises(RuntimeError, match="SYNTHETIC startup"):
        main([])
    assert not primary._lock.isLocked()
    if stage != "build":
        context.notifications.stop.assert_called_once()
        context.source.stop.assert_called_once()


@pytest.mark.parametrize("stage", ["notifications", "source"])
def test_cleanup_error_still_releases_instance_lock(main_runtime, stage):
    primary, context, _desktop = main_runtime
    getattr(context, stage).stop.side_effect = RuntimeError("SYNTHETIC cleanup failure")
    with pytest.raises(RuntimeError, match="SYNTHETIC cleanup"):
        main([])
    context.source.stop.assert_called_once()
    assert not primary._lock.isLocked()


@pytest.mark.parametrize("error", [QLockFile.LockError.PermissionError, QLockFile.LockError.UnknownError])
def test_lock_io_failure_is_not_reported_as_an_existing_instance(qapp, tmp_path, error):
    lock = InstanceLock(lock_path=tmp_path / "failure.lock", socket_name=_name())
    lock._lock = SimpleNamespace(tryLock=Mock(return_value=False), error=lambda: error)
    lock._ask_primary_to_raise = Mock()
    with pytest.raises(RuntimeError, match="인스턴스 잠금"):
        lock.acquire()
    lock._ask_primary_to_raise.assert_not_called()


def test_dead_lock_holder_becomes_primary(qapp, tmp_path) -> None:
    lock = InstanceLock(lock_path=tmp_path / "stale.lock", socket_name=_name())
    lock._lock = SimpleNamespace(
        tryLock=Mock(side_effect=[False, True]),
        error=lambda: QLockFile.LockError.LockFailedError,
        removeStaleLockFile=Mock(),
        isLocked=lambda: True,
        unlock=Mock(),
    )
    lock._ask_primary_to_raise = Mock(return_value=False)
    lock._holder_alive = Mock(return_value=False)
    lock._listen = Mock()
    assert lock.acquire() is True
    lock._lock.removeStaleLockFile.assert_called_once()
    lock._listen.assert_called_once()


def test_live_holder_without_socket_does_not_start_second_gui(qapp, tmp_path) -> None:
    lock = InstanceLock(lock_path=tmp_path / "live.lock", socket_name=_name())
    lock._lock = SimpleNamespace(
        tryLock=Mock(return_value=False),
        error=lambda: QLockFile.LockError.LockFailedError,
        removeStaleLockFile=Mock(),
    )
    lock._ask_primary_to_raise = Mock(return_value=False)
    lock._holder_alive = Mock(return_value=True)
    lock._listen = Mock()
    assert lock.acquire() is False
    lock._lock.removeStaleLockFile.assert_not_called()
    lock._listen.assert_not_called()


def test_smoke_bypasses_instance_lock(monkeypatch, tmp_path):
    from yt_rec import smoke as smoke_module

    acquire = Mock(side_effect=AssertionError("smoke must not acquire a lock"))
    monkeypatch.setattr(application, "InstanceLock", acquire)
    monkeypatch.setattr(application, "build_application", Mock())
    monkeypatch.setattr("yt_rec.recording.binaries.prepare_bundled_environment", lambda: None)
    smoke = Mock(return_value=7)
    monkeypatch.setattr(smoke_module, "run_smoke", smoke)
    assert main(["--smoke-test", str(tmp_path / "report.json")]) == 7
    acquire.assert_not_called()
    application.build_application.assert_not_called()
