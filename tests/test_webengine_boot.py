from __future__ import annotations

from pathlib import Path

from yt_rec.webengine_boot import (
    configure_webengine_process,
    profile_root,
    reset_gcm_store_once,
)


def test_gcm_store_is_removed_once(tmp_path: Path) -> None:
    gcm = tmp_path / "storage" / "GCM Store"
    gcm.mkdir(parents=True)
    (gcm / "LOCK").write_text("stale", encoding="utf-8")
    assert reset_gcm_store_once(tmp_path) is True
    assert not gcm.exists()
    assert (tmp_path / ".gcm-os-crypt-reset-1").is_file()
    gcm.mkdir(parents=True)
    (gcm / "LOCK").write_text("new", encoding="utf-8")
    assert reset_gcm_store_once(tmp_path) is False
    assert (gcm / "LOCK").read_text(encoding="utf-8") == "new"


def test_configure_sets_user_data_dir(monkeypatch, tmp_path: Path) -> None:
    import os
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.delenv("QTWEBENGINE_CHROMIUM_FLAGS", raising=False)
    configure_webengine_process()
    flags = os.environ["QTWEBENGINE_CHROMIUM_FLAGS"]
    root = profile_root()
    assert root == tmp_path / "yt-rec" / "youtube-push"
    assert f"--user-data-dir={root}" in flags
    configure_webengine_process()
    assert os.environ["QTWEBENGINE_CHROMIUM_FLAGS"].count("--user-data-dir=") == 1
