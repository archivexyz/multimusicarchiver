# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Multi Music Archiver.

Build with (from the repo root):
    pyinstaller packaging/multimusicarchiver.spec --noconfirm

Produces a onedir build (dist/MultiMusicArchiver/, or MultiMusicArchiver.app on
macOS) rather than onefile. This app frequently re-launches its own frozen
executable as a subprocess (embedded scdl/bandcamp-downloader runs, the mp3
tag helper, the archive scan helper) -- onefile would re-extract the entire
bundle to a temp directory on every one of those launches, which is both slow
and, since some of those subprocesses get killed early by design (e.g. the
archive-boundary stop-early logic), prone to leaving stale extraction dirs
behind. onedir runs the already-extracted executable directly every time.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

REPO_ROOT = Path(SPECPATH).resolve().parent
SOURCE_DIR = REPO_ROOT / "source"

datas = (
    collect_data_files("customtkinter")
    # scdl._get_config() reads scdl/scdl.cfg (a non-.py package data file, next
    # to scdl.py) as the default config template on every run. collect_submodules
    # below only pulls in scdl's Python modules, not this file, so without it
    # the frozen app crashes with FileNotFoundError the first time --run-scdl
    # executes.
    + collect_data_files("scdl")
)

hiddenimports = (
    # Only referenced from inside exec()'d helper-script string constants
    # (MP3_TAG_HELPER etc.), so static analysis can't see these imports.
    ["mutagen.id3", "mutagen.mp4", "mutagen.wave"]
    + collect_submodules("mutagen")
    + collect_submodules("scdl")
    # Vendored bandcamp-downloader (source/vendor_bandcamp_downloader.py) and
    # its dependencies.
    + ["vendor_bandcamp_downloader"]
    + collect_submodules("curl_cffi")
    + collect_submodules("browser_cookie3")
    + collect_submodules("bs4")
    + collect_submodules("tqdm")
)

a = Analysis(
    [str(SOURCE_DIR / "multimusicarchiver.py")],
    pathex=[str(SOURCE_DIR)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MultiMusicArchiver",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="MultiMusicArchiver",
)

import sys

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="MultiMusicArchiver.app",
        icon=None,
        bundle_identifier="com.archivexyz.multimusicarchiver",
        info_plist={
            "CFBundleName": "Multi Music Archiver",
            "CFBundleShortVersionString": "0.6",
            "NSHighResolutionCapable": True,
        },
    )
