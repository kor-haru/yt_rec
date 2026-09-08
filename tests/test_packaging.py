from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path

import pytest

from yt_rec.recording import binaries


def _builder():
    spec = importlib.util.spec_from_file_location("bundle_builder", Path(__file__).parents[1] / "packaging/build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bundle_download_rejects_changed_cached_binary(monkeypatch, tmp_path):
    builder = _builder()
    monkeypatch.setattr(builder, "CACHE", tmp_path)
    url = "https://example.invalid/tool.exe"
    name = builder.hashlib.sha256(url.encode()).hexdigest()[:10] + "-tool.exe"
    (tmp_path / name).write_bytes(b"wrong")
    with pytest.raises(RuntimeError, match="SHA256 mismatch"):
        builder.download(url, "0" * 64)


def test_bundle_extract_rejects_archive_traversal(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("../outside.exe", "not an executable")
    with pytest.raises(ValueError, match="Unsafe archive"):
        _builder().extract(archive, tmp_path / "output")
    assert not (tmp_path / "outside.exe").exists()


def test_bundled_child_runtime_path_precedes_host_path(monkeypatch, tmp_path):
    directory = tmp_path / "bin"
    directory.mkdir()
    monkeypatch.setattr(binaries, "_bundle_dirs", lambda: [directory])
    monkeypatch.setenv("PATH", "host-tools")
    binaries.prepare_bundled_environment()
    assert binaries.os.environ["PATH"].split(binaries.os.pathsep) == [str(directory), "host-tools"]
