"""Offline packaged-app check. All generated media/settings stay in a temporary folder."""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QSettings, QTimer
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView

from .app import build_application
from .recording.binaries import resolve_toolchain
from .recording.merge import merge_streams, verify_media
from .ui.settings_store import WindowSettings


def check_webengine() -> dict:
    """Exercise the bundled renderer using inline HTML and an unnamed memory profile."""
    profile = QWebEngineProfile()
    page = QWebEnginePage(profile)
    view = QWebEngineView()
    view.setPage(page)
    loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    result = {"off_the_record": profile.isOffTheRecord(), "javascript": False,
              "renderer_started": False, "rendered": False}

    def inspected(value: object) -> None:
        if not loop.isRunning():
            return  # A timed-out JavaScript callback may arrive during page deletion.
        result["javascript"] = value == "yt-rec offline 42"
        result["renderer_started"] = page.renderProcessPid() > 0
        result["rendered"] = not view.grab().isNull()
        loop.quit()

    def loaded(ok: bool) -> None:
        if ok:
            page.runJavaScript("document.body.textContent", inspected)
        else:
            loop.quit()

    page.loadFinished.connect(loaded)
    try:
        view.resize(320, 200)
        view.show()
        page.setHtml("<body><script>document.body.textContent='yt-rec offline '+(6*7)</script></body>")
        timer.start(15000)
        loop.exec()
        return result
    finally:
        timer.stop()
        page.loadFinished.disconnect(loaded)
        # Flush in order, including failed/timeout loads, without a user profile.
        view.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        profile.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def run_smoke(report_path: Path) -> int:
    report = {"ok": False, "platform": sys.platform, "architecture": platform.machine(),
              "frozen": bool(getattr(sys, "frozen", False))}
    context = None
    try:
        with tempfile.TemporaryDirectory(prefix="yt-rec-smoke-") as temporary:
            directory = Path(temporary)
            settings = WindowSettings(QSettings(str(directory / "window.ini"), QSettings.Format.IniFormat))
            context = build_application(["--stub", "populated"], settings=settings)
            context.window.show()
            context.app.processEvents()
            report["gui"] = not context.window.grab().isNull()
            report["webengine"] = check_webengine()
            toolchain = resolve_toolchain()
            report["tools"] = {}
            for name in ("ytdlp", "ffmpeg", "ffprobe", "deno"):
                report["checking_tool"] = name
                path = shutil.which("deno") if name == "deno" else str(getattr(toolchain, name))
                if path is None:
                    raise RuntimeError("Deno runtime not found")
                if report["frozen"]:
                    bundle_root = (Path(sys.executable).resolve().parent.parent if sys.platform == "darwin"
                                   else Path(sys._MEIPASS).resolve())
                    if not Path(path).resolve().is_relative_to(bundle_root):
                        raise RuntimeError(f"{name} was resolved outside the bundle")
                version = subprocess.run([path, "--version" if name in ("ytdlp", "deno") else "-version"],
                                         capture_output=True, text=True, check=True, timeout=30,
                                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                report["tools"][name] = version.stdout.splitlines()[0]
            del report["checking_tool"]
            video, audio = directory / "video.mp4", directory / "audio.m4a"
            for source, codec, output in (
                ("testsrc2=size=160x120:rate=25:duration=1", "mpeg4", video),
                ("sine=frequency=440:duration=1", "aac", audio),
            ):
                subprocess.run([str(toolchain.ffmpeg), "-v", "error", "-f", "lavfi", "-i", source,
                                "-c", codec, str(output)], capture_output=True, check=True, timeout=30,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            result = merge_streams([video, audio], directory / "merged.mp4", toolchain, timeout=30)
            verification = verify_media(result, toolchain, timeout=30)
            report["media"] = {"playable": verification.playable, "complete": verification.complete,
                               "issues": verification.issues}
            report["ok"] = (report["gui"] and all(report["webengine"].values())
                            and verification.playable and verification.complete)
            context.source.stop()
            context.window.close()
            context = None
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if context is not None:
            context.source.stop()
            context.window.close()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 1
