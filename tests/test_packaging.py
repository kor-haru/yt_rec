from __future__ import annotations

import importlib.util
import zipfile
from unittest.mock import MagicMock
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


@pytest.mark.parametrize("system,relative", [("win32", "_internal/bin"), ("darwin", "Contents/Helpers")])
def test_standalone_tools_survive_bundle_without_processing(monkeypatch, tmp_path, system, relative):
    builder = _builder()
    monkeypatch.setattr(builder, "VENDOR", tmp_path / "vendor")
    monkeypatch.setattr(builder.sys, "platform", system)
    signer = MagicMock()
    monkeypatch.setattr(builder.subprocess, "run", signer)
    source = builder.VENDOR / "bin/yt-dlp"
    source.parent.mkdir(parents=True)
    payload = b"universal Mach-O stub\x00Python archive payload\x00MEI\x0c\x0b\x0a\x0b\x0e"
    source.write_bytes(payload)
    source.chmod(0o755)
    bundle = tmp_path / "yt-rec"
    builder.install_child_tools(bundle)
    assert (bundle / relative / "yt-dlp").read_bytes() == payload
    if system == "darwin":
        assert signer.call_count == 2
        assert "--deep" not in signer.call_args_list[0].args[0]
        assert signer.call_args_list[0].args[0][-1] == str(bundle)
    else:
        signer.assert_not_called()
