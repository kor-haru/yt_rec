"""Offline packaged-app check. All generated media/settings stay in a temporary folder."""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QSettings

from .app import build_application
from .recording.binaries import resolve_toolchain
from .recording.merge import merge_streams, verify_media
from .ui.settings_store import WindowSettings


def run_smoke(report_path: Path) -> int:
    report = {"ok": False, "platform": sys.platform, "architecture": platform.machine(),
              "frozen": bool(getattr(sys, "frozen", False))}
    context = None
    try:
        toolchain = resolve_toolchain()
        versions = {}
        for name in ("ytdlp", "ffmpeg", "ffprobe", "deno"):
            path = shutil.which("deno") if name == "deno" else str(getattr(toolchain, name))
            if path is None:
                raise RuntimeError("Deno runtime not found")
            if report["frozen"] and not Path(path).resolve().is_relative_to(Path(sys._MEIPASS).resolve()):
                raise RuntimeError(f"{name} was resolved outside the bundle")
            version = subprocess.run([path, "--version" if name in ("ytdlp", "deno") else "-version"],
                                     capture_output=True, text=True, check=True, timeout=30)
            versions[name] = version.stdout.splitlines()[0]
        report["tools"] = versions
        with tempfile.TemporaryDirectory(prefix="yt-rec-smoke-") as temporary:
            directory = Path(temporary)
            settings = WindowSettings(QSettings(str(directory / "window.ini"), QSettings.Format.IniFormat))
            context = build_application(["--stub", "populated"], settings=settings)
            context.window.show()
            context.app.processEvents()
            report["gui"] = not context.window.grab().isNull()
            video, audio = directory / "video.mp4", directory / "audio.m4a"
            for source, codec, output in (
                ("testsrc2=size=160x120:rate=25:duration=1", "mpeg4", video),
                ("sine=frequency=440:duration=1", "aac", audio),
            ):
                subprocess.run([str(toolchain.ffmpeg), "-v", "error", "-f", "lavfi", "-i", source,
                                "-c", codec, str(output)], capture_output=True, check=True, timeout=30)
            result = merge_streams([video, audio], directory / "merged.mp4", toolchain, timeout=30)
            verification = verify_media(result, toolchain, timeout=30)
            report["media"] = {"playable": verification.playable, "complete": verification.complete,
                               "issues": verification.issues}
            report["ok"] = report["gui"] and verification.playable and verification.complete
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
