"""Native portable bundle: uv run --frozen --with pyinstaller==6.22.2 python packaging/build.py.

Downloads are version/SHA256 pinned. Build on the destination OS/architecture;
PyInstaller is a build tool only, never an application dependency.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "build/downloads"
VENDOR = ROOT / "build/vendor"
YTDLP_VERSION = "2026.08.19"
DENO_VERSION = "v2.9.6"
FFMPEG_RELEASE = "autobuild-2026-09-07-15-39"
FFMPEG_NAME = "ffmpeg-n8.1.2-51-g7ba069f4f1"
FFMPEG_SOURCE = ("https://ffmpeg.org/releases/ffmpeg-8.0.1.tar.xz",
                 "05ee0b03119b45c0bdb4df654b96802e909e0a752f72e4fe3794f487229e5a41")
TOOLS = {
    "win32-x86_64": (
        ("yt-dlp.exe", "66674953fe251b89f4d08c5f0e35e0728679bd67ab3d7d05c0562af101dd3e7a"),
        ("x86_64-pc-windows-msvc", "15e5300b0ba3c3695a7621d90160a746ec9e710228cee639afa9d580f6e3cd11"),
        ("win64-lgpl-8.1.zip", "232464b6f9f1d55fa42c1b0e7ae1c9ca5a19272ba61229e8b32a93751055e135")),
    "linux-x86_64": (
        ("yt-dlp_linux", "58162f9bfdc27458ea47bfcb311cf47028f17d8154a8bf7d689861d46399230a"),
        ("x86_64-unknown-linux-gnu", "394f07f4da2bebe6ce6f1e7ce0fa16429b29b08c35e3fac3fe25972676dff4b2"),
        ("linux64-lgpl-8.1.tar.xz", "602b4386efc01c4fccf03792d7d6c4db13e591e3b6019ee1e05fce459ec0f44d")),
    "linux-arm64": (
        ("yt-dlp_linux_aarch64", "b16e4dab368a816cd05d477d698a605a6ae87ccee1c8ffd38fa21d7254141fcc"),
        ("aarch64-unknown-linux-gnu", "9a46afc6c392c7cd2ff71a31558935545b46408d0e87f7a86908c712721c046e"),
        ("linuxarm64-lgpl-8.1.tar.xz", "bcb045c44fc4bf7818ec8fe62cf46500491e96d1c58fb1b03320611fc17bd755")),
    "darwin-x86_64": (
        ("yt-dlp_macos", "0f192b7ec147ab6288885d6351d9ab67367640029b4377576ef46dd79cf7b202"),
        ("x86_64-apple-darwin", "7d4524b82bcc557fe020a1a5b56956ed42b992ae5b28026e8ad5d17329533f5f"), None),
    "darwin-arm64": (
        ("yt-dlp_macos", "0f192b7ec147ab6288885d6351d9ab67367640029b4377576ef46dd79cf7b202"),
        ("aarch64-apple-darwin", "213a2f304f04d3c9cb5220669afad138f60a5aab1fe80962abdeb8f35807a472"), None),
}


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download(url: str, expected: str | None = None) -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    name = (hashlib.sha256(url.encode()).hexdigest()[:10] + "-" + url.split("?", 1)[0].rsplit("/", 1)[1])
    target = CACHE / name
    if not target.exists():
        print("Downloading", url, flush=True)
        request = urllib.request.Request(url, headers={"User-Agent": "yt-rec-build"})
        token = os.environ.get("GITHUB_TOKEN")
        if token and request.type == "https" and request.host == "api.github.com":
            # The CI token must never follow redirects or reach binary/raw hosts.
            request.add_unredirected_header("Authorization", f"Bearer {token}")
        temporary = target.with_suffix(target.suffix + ".part")
        with urllib.request.urlopen(request, timeout=120) as response, temporary.open("wb") as stream:
            shutil.copyfileobj(response, stream)
        temporary.replace(target)
    if expected is not None and sha256(target) != expected:
        raise RuntimeError(f"SHA256 mismatch: {url}. Refusing to execute or bundle it.")
    return target


def extract(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as package:
            for entry in package.infolist():
                resolved = (destination / entry.filename).resolve()
                if not resolved.is_relative_to(destination.resolve()):
                    raise ValueError("Unsafe archive member")
            package.extractall(destination)
    else:
        with tarfile.open(archive) as package:
            package.extractall(destination, filter="data")


def copy_binary(source: Path, name: str) -> None:
    target = VENDOR / "bin" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    target.chmod(target.stat().st_mode | 0o755)


def collect_licenses() -> None:
    target = VENDOR / "licenses"
    target.mkdir(parents=True, exist_ok=True)
    # Preserve package notices instead of guessing their licenses.
    for distribution in importlib.metadata.distributions():
        for item in distribution.files or []:
            if item.name.lower().startswith(("license", "copying", "notice", "authors")):
                source = Path(distribution.locate_file(item))
                if source.is_file():
                    destination = target / distribution.metadata["Name"] / str(item)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
    for name, url in {
        "yt-dlp-LICENSE": f"https://raw.githubusercontent.com/yt-dlp/yt-dlp/{YTDLP_VERSION}/LICENSE",
        "yt-dlp-THIRD_PARTY_LICENSES.txt": f"https://raw.githubusercontent.com/yt-dlp/yt-dlp/{YTDLP_VERSION}/THIRD_PARTY_LICENSES.txt",
        "deno-LICENSE.md": f"https://raw.githubusercontent.com/denoland/deno/{DENO_VERSION}/LICENSE.md",
    }.items():
        shutil.copy2(download(url), target / name)
    qt_version = importlib.metadata.version("PySide6-Essentials")
    listing = download(f"https://api.github.com/repos/qt/qtbase/contents/LICENSES?ref=v{qt_version}")
    for entry in json.loads(listing.read_text(encoding="utf-8")):
        if entry["type"] == "file":
            shutil.copy2(download(entry["download_url"]), target / ("Qt-" + entry["name"]))
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.is_file():
        shutil.copy2(python_license, target / "Python-LICENSE.txt")
    shutil.copy2(ROOT / "packaging/THIRD_PARTY.md", target / "SOURCES.md")


def install_child_tools(bundle: Path) -> None:
    # Never let PyInstaller analyze/thin a standalone child executable. A universal
    # yt-dlp carries its Python payload in only one slice; thinning destroys it.
    destination = bundle / ("Contents/Helpers" if sys.platform == "darwin" else "_internal/bin")
    destination.mkdir(parents=True, exist_ok=True)
    for source in (VENDOR / "bin").iterdir():
        shutil.copy2(source, destination / source.name)
    if sys.platform == "darwin":
        # Seal the changed outer bundle, retaining vendor signatures on child tools.
        subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(bundle)], check=True)
        subprocess.run(["/usr/bin/codesign", "--verify", "--all-architectures", "--deep", "--strict", str(bundle)], check=True)
    for source in (VENDOR / "bin").iterdir():
        if sha256(source) != sha256(destination / source.name):
            raise RuntimeError(f"Bundling changed the standalone executable: {source.name}")


def smoke_environment(directory: Path) -> dict[str, str]:
    """The built executable must work without host Python/tools or product data."""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("PYTHON", "YT_REC_", "QTWEBENGINE_"))
           and key not in ("QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH", "QML2_IMPORT_PATH")}
    data_paths = {name: str(directory / name.lower()) for name in (
        "APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOME", "XDG_CONFIG_HOME",
        "XDG_DATA_HOME", "XDG_CACHE_HOME", "TMP", "TEMP", "TMPDIR",
    )}
    env.update(data_paths)
    for path in data_paths.values():
        Path(path).mkdir(parents=True, exist_ok=True)
    env["PATH"] = str(Path(os.environ["SystemRoot"]) / "System32") if sys.platform == "win32" else "/usr/bin:/bin"
    env["QT_QPA_PLATFORM"] = "offscreen"
    return env


def main() -> None:
    machine = {"AMD64": "x86_64", "aarch64": "arm64"}.get(platform.machine(), platform.machine())
    target = f"{sys.platform}-{machine}"
    if target not in TOOLS:
        raise SystemExit(f"Unsupported native build target: {target}")
    ytdlp, deno, ffmpeg = TOOLS[target]
    extension = ".exe" if sys.platform == "win32" else ""
    copy_binary(download(f"https://github.com/yt-dlp/yt-dlp/releases/download/{YTDLP_VERSION}/{ytdlp[0]}", ytdlp[1]), "yt-dlp" + extension)
    archive = download(f"https://github.com/denoland/deno/releases/download/{DENO_VERSION}/deno-{deno[0]}.zip", deno[1])
    deno_dir = ROOT / "build" / f"deno-{target}"
    extract(archive, deno_dir)
    copy_binary(deno_dir / ("deno" + extension), "deno" + extension)
    if ffmpeg is not None:
        archive = download(f"https://github.com/BtbN/FFmpeg-Builds/releases/download/{FFMPEG_RELEASE}/{FFMPEG_NAME}-{ffmpeg[0]}", ffmpeg[1])
        ffmpeg_dir = ROOT / "build" / f"ffmpeg-{target}"
        extract(archive, ffmpeg_dir)
        for name in ("ffmpeg", "ffprobe"):
            candidates = list(ffmpeg_dir.rglob(name + extension))
            if len(candidates) != 1:
                raise RuntimeError(f"Expected one {name} binary, got {candidates}")
            copy_binary(candidates[0], name + extension)
        for item in ffmpeg_dir.rglob("*"):
            if item.is_file() and "bin" not in item.parts:
                dest = VENDOR / "licenses/ffmpeg" / item.relative_to(ffmpeg_dir)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, dest)
    else:
        # Native macOS build avoids shipping an Intel-only or Homebrew-linked binary.
        archive = download(*FFMPEG_SOURCE)
        ffmpeg_dir = ROOT / "build/ffmpeg-source"
        extract(archive, ffmpeg_dir)
        source = ffmpeg_dir / "ffmpeg-8.0.1"
        subprocess.run(["./configure", "--disable-autodetect", "--disable-x86asm", "--disable-doc",
                        "--disable-ffplay", "--disable-debug", "--enable-static", "--disable-shared"], cwd=source, check=True)
        subprocess.run(["make", f"-j{os.cpu_count() or 2}", "ffmpeg", "ffprobe"], cwd=source, check=True)
        for name in ("ffmpeg", "ffprobe"):
            subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(source / name)], check=True)
            copy_binary(source / name, name)
        for item in source.glob("COPYING*"):
            dest = VENDOR / "licenses/ffmpeg" / item.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, dest)
        source_archive = VENDOR / "licenses/ffmpeg/ffmpeg-8.0.1.tar.xz"
        shutil.copy2(archive, source_archive)
    collect_licenses()
    manifest = {"target": target, "python": sys.version, "pyinstaller": importlib.metadata.version("pyinstaller"),
                "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                "source_dirty": bool(subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT, text=True).strip()),
                "source_files": {str(path.relative_to(ROOT)): sha256(path)
                                 for path in [*sorted((ROOT / "src").rglob("*.py")), ROOT / "packaging/build.py", ROOT / "uv.lock"]},
                "tools": {path.name: sha256(path) for path in (VENDOR / "bin").iterdir()},
                "packages": {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()}}
    (VENDOR / "build-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    args = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--onedir",
            "--name", "yt-rec", "--paths", str(ROOT / "src"), "--distpath", str(ROOT / "dist"),
            "--workpath", str(ROOT / "build/pyinstaller"), "--specpath", str(ROOT / "build"),
            "--add-data", f"{VENDOR / 'licenses'}{os.pathsep}licenses",
            "--add-data", f"{VENDOR / 'build-manifest.json'}{os.pathsep}."]
    args += [str(ROOT / "packaging/entry.py")]
    subprocess.run(args, cwd=ROOT, check=True)
    bundle = ROOT / "dist" / ("yt-rec.app" if sys.platform == "darwin" else "yt-rec")
    install_child_tools(bundle)
    if sys.platform == "win32":
        shutil.copy2(ROOT / "README.md", bundle / "README.md")
    executable = bundle / ("Contents/MacOS/yt-rec" if sys.platform == "darwin" else "yt-rec" + extension)
    report = ROOT / "dist" / f"smoke-{target}.json"
    with tempfile.TemporaryDirectory(prefix="yt-rec-bundle-check-") as temporary:
        subprocess.run([str(executable), "--smoke-test", str(report)], check=True, timeout=120,
                       env=smoke_environment(Path(temporary)), cwd=temporary)
    if not json.loads(report.read_text(encoding="utf-8"))["ok"]:
        raise RuntimeError("Bundle smoke check did not pass")
    archive_kind = "zip" if sys.platform == "win32" else "gztar"
    archive = shutil.make_archive(str(ROOT / "dist" / f"yt-rec-{target}"), archive_kind, bundle.parent, bundle.name)
    print(f"Built and smoke-tested: {archive}", flush=True)


if __name__ == "__main__":
    main()
