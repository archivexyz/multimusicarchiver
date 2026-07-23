#!/usr/bin/env python3
"""Cross-platform PyInstaller build helper for Multi Music Archiver.

PyInstaller can't cross-compile: run this on each OS you want a build for
(macOS produces MultiMusicArchiver.app, Windows/Linux produce a
MultiMusicArchiver/ folder under dist/).

Usage:
    pip install -r requirements.txt -r packaging/requirements-build.txt
    python packaging/build.py
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC = REPO_ROOT / "packaging" / "multimusicarchiver.spec"


def main() -> int:
    return subprocess.run(
        [sys.executable, "-m", "PyInstaller", str(SPEC), "--noconfirm", "--clean"],
        cwd=REPO_ROOT,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
