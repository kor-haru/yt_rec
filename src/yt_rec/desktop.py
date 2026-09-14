"""Per-user OS startup registration. Never requires administrator privileges."""

from __future__ import annotations

import ctypes
import os
import plistlib
import subprocess
import sys
from pathlib import Path


def set_app_id() -> None:
    """Separate Windows taskbar identity from Python; call before creating windows."""
    if sys.platform == "win32":
        setter = ctypes.WinDLL("shell32").SetCurrentProcessExplicitAppUserModelID
        setter.argtypes = [ctypes.c_wchar_p]
        setter.restype = ctypes.c_long
        if setter("io.github.kor-haru.yt-rec") < 0:
            raise OSError("작업 표시줄 앱 식별자를 설정하지 못했습니다.")


def startup_command() -> list[str]:
    executable = Path(sys.executable).absolute()
    if getattr(sys, "frozen", False):
        return [str(executable)]
    if sys.platform == "win32":
        windowed = executable.with_name("pythonw.exe")
        if windowed.is_file():
            executable = windowed
    return [str(executable), "-m", "yt_rec"]


def _desktop_argument(value: str) -> str:
    # Desktop Entry strings and Exec quoting each consume a backslash layer.
    if any(character in value for character in "\n\r\0"):
        raise ValueError("자동 시작 경로에 줄바꿈을 사용할 수 없습니다.")
    value = value.replace("%", "%%").replace("\\", "\\\\\\\\")
    for character in ('"', "`", "$"):
        value = value.replace(character, "\\\\" + character)
    return '"' + value + '"'


def set_autostart(enabled: bool) -> None:
    """Register/remove only this app's next-login startup entry."""
    command = startup_command()
    if sys.platform == "win32":
        import winreg

        with winreg.CreateKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
        ) as key:
            if enabled:
                value = subprocess.list2cmdline(command)
                if len(value) > 260:
                    raise OSError("자동 시작 경로가 너무 깁니다. 앱을 더 짧은 경로에 옮겨 주세요.")
                winreg.SetValueEx(key, "yt-rec", 0, winreg.REG_SZ, value)
            else:
                try:
                    winreg.DeleteValue(key, "yt-rec")
                except FileNotFoundError:
                    pass
        return
    if sys.platform == "darwin":
        target = Path.home() / "Library/LaunchAgents/io.github.kor-haru.yt-rec.plist"
        content = plistlib.dumps({
            "Label": "io.github.kor-haru.yt-rec",
            "ProgramArguments": command,
            "RunAtLoad": True,
            "ProcessType": "Interactive",
        })
    elif sys.platform.startswith("linux"):
        config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        target = config / "autostart/yt-rec.desktop"
        content = (
            "[Desktop Entry]\nType=Application\nName=yt-rec\n"
            "Exec=" + " ".join(map(_desktop_argument, command)) + "\nTerminal=false\n"
        ).encode("utf-8")
    else:
        raise OSError("이 운영체제에서는 자동 시작을 지원하지 않습니다.")
    if enabled:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_bytes(content)
        temporary.replace(target)
    else:
        target.unlink(missing_ok=True)
