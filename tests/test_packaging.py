from __future__ import annotations

import importlib.util
import io
import json
import zipfile
from types import SimpleNamespace
from unittest.mock import MagicMock
from pathlib import Path

import pytest

from yt_rec.recording import binaries


def _builder():
    spec = importlib.util.spec_from_file_location("bundle_builder", Path(__file__).parents[1] / "packaging/build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("system,machine", [
    ("darwin", "arm64"), ("darwin", "x86_64"),
    ("win32", "AMD64"), ("linux", "x86_64"),
])
def test_mac_webengine_core_keeps_canonical_framework_destination(monkeypatch, tmp_path, system, machine):
    builder = _builder()
    monkeypatch.setattr(builder, "ROOT", tmp_path)
    monkeypatch.setattr(builder, "VENDOR", tmp_path / "vendor")
    (builder.VENDOR / "bin").mkdir(parents=True)
    (builder.VENDOR / "licenses/ffmpeg").mkdir(parents=True)
    monkeypatch.setattr(builder.sys, "platform", system)
    monkeypatch.setattr(builder.platform, "machine", lambda: machine)
    download = tmp_path / "download"
    download.write_bytes(b"test-only archive")
    monkeypatch.setattr(builder, "download", lambda *args: download)
    monkeypatch.setattr(builder, "copy_binary", lambda *args: None)
    monkeypatch.setattr(builder, "collect_licenses", lambda: None)
    monkeypatch.setattr(builder, "sha256", lambda path: "test-sha256")
    monkeypatch.setattr(builder.subprocess, "check_output", lambda *args, **kwargs: "test-source")

    def extract(_archive, destination):
        destination.mkdir(parents=True, exist_ok=True)
        for name in ("ffmpeg", "ffprobe"):
            (destination / (name + (".exe" if system == "win32" else ""))).write_bytes(b"tool")

    monkeypatch.setattr(builder, "extract", extract)
    monkeypatch.setattr(builder.importlib.metadata, "version", lambda name: "test-version")
    monkeypatch.setattr(builder.importlib.metadata, "distributions", lambda: [])
    distribution = MagicMock()
    distribution.locate_file.side_effect = lambda relative: tmp_path / "site-packages" / relative
    locate_distribution = MagicMock(return_value=distribution)
    monkeypatch.setattr(builder.importlib.metadata, "distribution", locate_distribution)
    commands = []

    class CommandCaptured(Exception):
        pass

    def run(command, **kwargs):
        if command[1:3] == ["-m", "PyInstaller"]:
            commands.append(command)
            raise CommandCaptured

    monkeypatch.setattr(builder.subprocess, "run", run)
    with pytest.raises(CommandCaptured):
        builder.main()
    command, = commands
    if system == "darwin":
        relative = Path("PySide6/Qt/lib/QtWebEngineCore.framework/Versions/A")
        locate_distribution.assert_called_once_with("PySide6-Addons")
        distribution.locate_file.assert_called_once_with(relative / "QtWebEngineCore")
        source = tmp_path / "site-packages" / relative / "QtWebEngineCore"
        assert command[command.index("--add-binary") + 1] == f"{source}{builder.os.pathsep}{relative}"
    else:
        locate_distribution.assert_not_called()
        assert "--add-binary" not in command
    assert command[-1] == str(tmp_path / "packaging/entry.py")
    assert "--windowed" in command and "--onedir" in command


def test_bundle_download_rejects_changed_cached_binary(monkeypatch, tmp_path):
    builder = _builder()
    monkeypatch.setattr(builder, "CACHE", tmp_path)
    url = "https://example.invalid/tool.exe"
    name = builder.hashlib.sha256(url.encode()).hexdigest()[:10] + "-tool.exe"
    (tmp_path / name).write_bytes(b"wrong")
    with pytest.raises(RuntimeError, match="SHA256 mismatch"):
        builder.download(url, "0" * 64)


@pytest.mark.parametrize("url,authenticated", [
    ("https://api.github.com/repos/qt/qtbase/contents/LICENSES?ref=v6.11.1", True),
    ("http://api.github.com/LICENSES", False),
    ("https://api.github.com.example.invalid/LICENSES", False),
    ("https://raw.githubusercontent.com/qt/qtbase/v6.11.1/LICENSES", False),
    ("https://github.com/qt/qtbase/releases/download/LICENSES", False),
])
def test_build_token_is_api_only_and_never_redirected(monkeypatch, tmp_path, url, authenticated):
    builder = _builder()
    monkeypatch.setattr(builder, "CACHE", tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "test-only-build-token")

    def open_request(request, timeout):
        assert request.get_header("Authorization") == (
            "Bearer test-only-build-token" if authenticated else None
        )
        redirected = builder.urllib.request.HTTPRedirectHandler().redirect_request(
            request, None, 302, "Found", {}, "https://example.invalid/LICENSES",
        )
        assert not redirected.has_header("Authorization")
        return io.BytesIO(b"license notice")

    monkeypatch.setattr(builder.urllib.request, "urlopen", open_request)
    assert builder.download(url).read_bytes() == b"license notice"


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


def test_bundle_smoke_isolates_user_data_and_removes_host_overrides(monkeypatch, tmp_path):
    builder = _builder()
    monkeypatch.setenv("PATH", "host-python-and-tools")
    monkeypatch.setenv("PYTHONPATH", "host-python")
    monkeypatch.setenv("YT_REC_FFMPEG", "host-ffmpeg")
    monkeypatch.setenv("QTWEBENGINE_CHROMIUM_FLAGS", "host-flags")
    monkeypatch.setenv("QT_PLUGIN_PATH", "host-qt")
    env = builder.smoke_environment(tmp_path)
    for name in ("APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOME", "XDG_CONFIG_HOME",
                 "XDG_DATA_HOME", "XDG_CACHE_HOME", "TMP", "TEMP", "TMPDIR"):
        assert Path(env[name]).is_relative_to(tmp_path) and Path(env[name]).is_dir()
    assert env["PATH"] != "host-python-and-tools"
    assert env["QT_QPA_PLATFORM"] == "offscreen"
    assert not {"PYTHONPATH", "YT_REC_FFMPEG", "QTWEBENGINE_CHROMIUM_FLAGS", "QT_PLUGIN_PATH"} & env.keys()
    assert builder.os.environ["PATH"] == "host-python-and-tools"  # Parent is untouched.


def test_offline_webengine_smoke_runs_real_renderer(qapp):
    from yt_rec.smoke import check_webengine

    assert check_webengine() == {
        "off_the_record": True, "javascript": True, "renderer_started": True, "rendered": True,
    }


def test_offline_webengine_smoke_does_not_accept_broken_javascript(qapp, monkeypatch):
    from yt_rec import smoke

    monkeypatch.setattr(smoke.QWebEnginePage, "runJavaScript", lambda _page, _script, callback: callback("wrong"))
    assert smoke.check_webengine()["javascript"] is False


def test_webengine_late_callback_does_not_access_deleted_page(qapp, monkeypatch):
    from yt_rec import smoke

    loop = smoke.QEventLoop()
    errors = []
    monkeypatch.setattr(smoke, "QEventLoop", lambda: loop)

    def delayed(page, _script, callback):
        def destroyed_callback():
            try:
                callback("")
            except RuntimeError as exc:
                errors.append(exc)
        page.destroyed.connect(destroyed_callback)
        loop.quit()

    monkeypatch.setattr(smoke.QWebEnginePage, "runJavaScript", delayed)
    assert smoke.check_webengine()["javascript"] is False
    assert errors == []


def test_smoke_failure_preserves_exact_tool_checkpoint(qapp, tmp_path, monkeypatch):
    from yt_rec import smoke

    monkeypatch.setattr(smoke, "check_webengine", lambda: {"javascript": True})
    monkeypatch.setattr(smoke, "resolve_toolchain", lambda: SimpleNamespace(ytdlp=tmp_path / "blocked.exe"))

    def blocked(*args, **kwargs):
        raise OSError("application control blocked this file")

    monkeypatch.setattr(smoke.subprocess, "run", blocked)
    path = tmp_path / "report.json"
    assert smoke.run_smoke(path) == 1
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["checking_tool"] == "ytdlp" and report["tools"] == {}
    assert report["gui"] and not report["ok"]
    assert "application control" in report["error"]
