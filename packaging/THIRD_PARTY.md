# Bundled components and source locations

The application invokes yt-dlp, FFmpeg/ffprobe and Deno as separate executables.
It uses Qt Widgets through PySide6 Essentials, without QtWebEngine or Chromium.
The `licenses` directory preserves third-party notices and the manifest records
the exact package versions and binary SHA256 hashes. Do not remove these files.
Standalone child executables are copied after PyInstaller builds the application;
their final hashes must match the originals. In particular, the universal macOS
yt-dlp must not be thinned to one CPU slice, which would discard its Python payload.
macOS tools live in `Contents/Helpers`; only locally built FFmpeg binaries and the
outer application receive ad-hoc signatures. Downloaded vendor tools retain theirs.

- yt-dlp 2026.08.19: https://github.com/yt-dlp/yt-dlp/tree/2026.08.19
  Standalone binaries contain their own third-party components; see the bundled
  `yt-dlp-THIRD_PARTY_LICENSES.txt` in addition to `yt-dlp-LICENSE`.
- Deno v2.9.6: https://github.com/denoland/deno/tree/v2.9.6
  MIT and component notices: `deno-LICENSE.md`.
- Windows/Linux FFmpeg LGPL builds, dated 2026-09-07:
  https://github.com/BtbN/FFmpeg-Builds/releases/tag/autobuild-2026-09-07-15-39
  Build scripts, patches and source acquisition instructions:
  https://github.com/BtbN/FFmpeg-Builds
  Keep the included FFmpeg build information and licenses with the executables.
  Upstream may remove dated artifacts; retain the SHA256-verified download cache
  for rebuilds. Never silently switch a missing pinned artifact to `latest`.
- macOS FFmpeg 8.0.1: https://ffmpeg.org/releases/ffmpeg-8.0.1.tar.xz
  Built without external libraries, GPL or nonfree flags; corresponding source
  archive and COPYING files are included. Build command is in `packaging/build.py`.
- Qt/PySide6: https://code.qt.io/ and https://download.qt.io/official_releases/
  Versions are in `build-manifest.json`. LGPL/GPL and bundled library license
  texts are retained. Qt libraries remain separate files, replaceable by users.
- Python: https://www.python.org/downloads/source/
  Runtime and other Python package notices are included where supplied by wheels.

These are internal/test artifacts, not signed production releases. Before a
public release, decide the application's license, review all redistribution
requirements (including corresponding source), and sign/notarize as appropriate.
