"""
Multi Music Archiver — CustomTkinter GUI wrapping scdl (https://github.com/scdl-org/scdl)
and bandcamp-downloader (https://github.com/easlice/bandcamp-downloader).
Requirements: pip install customtkinter scdl
"""

import argparse
import base64
import hashlib
import html
import sys
import sysconfig

import customtkinter as ctk
import subprocess
import threading
import os
import json
import platform
import re
import shutil
import shlex
import textwrap
import webbrowser
import math
import zipfile
import urllib.request
from tkinter import filedialog, messagebox, simpledialog, Menu, Text
from datetime import datetime

if platform.system() == "Windows":
    import winreg

# ── Theme ────────────────────────────────────────────────────────────────────
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

LEGACY_CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "scdl_gui")
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".config", "multimusicarchiver", "settings.json")
CONFIG_DIR = os.path.dirname(CONFIG_PATH)
LOG_DIR = os.path.join(CONFIG_DIR, "logs")
SCHEDULE_DIR = os.path.join(CONFIG_DIR, "schedules")


def migrate_legacy_config_dir():
    """One-time move of settings/schedules/logs from the old ~/.config/scdl_gui
    directory (pre-rename) into the new ~/.config/multimusicarchiver location.
    Registered OS-level daily schedules (Task Scheduler/launchd/cron) still
    point at the old script path and need to be re-registered separately --
    this only carries over saved settings and schedule profile files."""
    if os.path.isdir(LEGACY_CONFIG_DIR) and not os.path.exists(CONFIG_DIR):
        try:
            shutil.move(LEGACY_CONFIG_DIR, CONFIG_DIR)
        except OSError:
            pass


migrate_legacy_config_dir()
NAME_FORMAT = "[%(id)s] %(uploader)s - %(title)s.%(ext)s"
PLAYLIST_NAME_FORMAT = "%(playlist_index)s. [%(id)s] %(uploader)s - %(title)s.%(ext)s"
BANDCAMP_FORMATS = ("mp3-320", "flac", "wav", "aiff-lossless", "alac", "aac-hi", "mp3-v0", "vorbis")

BANDCAMP_COOKIES_HELP_PREFIX = (
    "File with exported Bandcamp cookies in Netscape format. Can be retrieved with a browser extension like "
)
BANDCAMP_COOKIES_HELP_LINK_TEXT = "Cookie-Editor"
BANDCAMP_COOKIES_HELP_LINK_URL = "https://cookie-editor.com/"
BANDCAMP_COOKIES_HELP_SUFFIX = (
    ". Go to Bandcamp while logged in -> export as Netscape in the extension -> paste the contents into a cookie.txt file."
    " These are only your cookies for bandcamp.com."
)


def is_frozen_app() -> bool:
    return bool(getattr(sys, "frozen", False))


def child_process_env() -> dict:
    """Env for subprocesses whose text output we capture. Without this,
    Windows defaults a piped (non-console) Python child's stdout to the
    legacy ANSI codepage (e.g. cp1252), and it crashes with a
    UnicodeEncodeError the moment an artist/track name has a character
    that codepage can't represent. Forcing UTF-8 avoids that entirely.

    PYTHONUNBUFFERED matters just as much: a piped (non-console) stdout is
    fully block-buffered by default, so a child's print()/tqdm.write() calls
    can sit unflushed for a while. If we then forcefully kill that child
    (e.g. taskkill /T when stopping or hitting an archive boundary), any
    buffered-but-unflushed output is lost -- including the exact lines our
    Bandcamp archive detection depends on seeing in real time."""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def reconfigure_embedded_std_streams():
    """The frozen app's bootloader (PyInstaller >= 6) starts Python with an
    isolated config that ignores PYTHONUNBUFFERED/PYTHONIOENCODING, so the
    env vars from child_process_env() do nothing for a frozen child process:
    its stdout into the GUI's pipe stays block-buffered (tqdm's bar on stderr
    shows up live, but 'Album being saved to' lines sit in the buffer --
    breaking both the live log and the archive-boundary detection that needs
    them in real time) and on Windows it falls back to the legacy codepage.
    Reconfigure the streams in-process instead. Harmless when running from
    source, and guarded because a windowed frozen app launched without pipes
    may have stub streams that don't support reconfigure()."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except Exception:
            pass


def user_scripts_dir() -> str:
    if os.name == "nt":
        scheme = "nt_user"
    elif sys.platform == "darwin":
        scheme = "osx_framework_user" if sysconfig.get_config_var("PYTHONFRAMEWORK") else "posix_user"
    else:
        scheme = "posix_user"
    return os.path.expanduser(sysconfig.get_path("scripts", scheme=scheme))


def scdl_executable_names() -> tuple[str, ...]:
    return ("scdl.exe", "scdl.cmd", "scdl.bat", "scdl") if os.name == "nt" else ("scdl",)


def candidate_scdl_paths() -> list[str]:
    dirs = [
        user_scripts_dir(),
        os.path.join(sys.prefix, "Scripts"),
        os.path.join(sys.base_prefix, "Scripts"),
        os.path.dirname(sys.executable),
    ]
    if os.name == "nt":
        home = os.path.expanduser("~")
        appdata = os.environ.get("APPDATA", "")
        localappdata = os.environ.get("LOCALAPPDATA", "")
        if appdata:
            dirs.append(os.path.join(appdata, "Python", "Scripts"))
            python_root = os.path.join(appdata, "Python")
            if os.path.isdir(python_root):
                dirs.extend(os.path.join(python_root, name, "Scripts") for name in os.listdir(python_root))
        if localappdata:
            programs_python = os.path.join(localappdata, "Programs", "Python")
            if os.path.isdir(programs_python):
                dirs.extend(os.path.join(programs_python, name, "Scripts") for name in os.listdir(programs_python))
            packages = os.path.join(localappdata, "Packages")
            if os.path.isdir(packages):
                for name in os.listdir(packages):
                    if name.startswith("PythonSoftwareFoundation.Python."):
                        scripts_root = os.path.join(packages, name, "LocalCache", "local-packages")
                        if os.path.isdir(scripts_root):
                            dirs.extend(
                                os.path.join(scripts_root, child, "Scripts")
                                for child in os.listdir(scripts_root)
                                if child.startswith("Python")
                            )
        dirs.append(os.path.join(home, ".local", "bin"))
    else:
        dirs.extend(("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin"))

    candidates = []
    seen = set()
    for directory in dirs:
        directory = os.path.expanduser(directory)
        for executable in scdl_executable_names():
            path = os.path.join(directory, executable)
            normalized = os.path.normcase(os.path.abspath(path))
            if normalized not in seen:
                seen.add(normalized)
                candidates.append(path)
    return candidates


def scdl_config_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".config", "scdl", "scdl.cfg")


def ensure_scdl_config_file():
    path = scdl_config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as config:
            config.write("")


def dry_run_scdl_setup(scdl: str | None = None):
    ensure_scdl_config_file()
    scdl = scdl or resolve_scdl_path()
    if not scdl:
        return
    try:
        subprocess.run([scdl, "--version"], capture_output=True, text=True, timeout=30)
    except Exception:
        pass


def append_to_current_path(directory: str):
    current = os.environ.get("PATH", "")
    paths = [os.path.normcase(os.path.abspath(p)) for p in current.split(os.pathsep) if p]
    normalized = os.path.normcase(os.path.abspath(directory))
    if normalized not in paths:
        os.environ["PATH"] = directory + os.pathsep + current if current else directory


def add_directory_to_user_path(directory: str):
    directory = os.path.abspath(os.path.expanduser(directory))
    append_to_current_path(directory)

    if platform.system() == "Windows":
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
            try:
                existing, value_type = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                existing, value_type = "", winreg.REG_EXPAND_SZ
            parts = [p for p in str(existing).split(os.pathsep) if p]
            normalized = os.path.normcase(os.path.abspath(directory))
            if normalized not in {os.path.normcase(os.path.abspath(os.path.expandvars(p))) for p in parts}:
                new_value = os.pathsep.join(parts + [directory]) if parts else directory
                winreg.SetValueEx(key, "Path", 0, value_type, new_value)
        return

    profile_path = os.path.expanduser("~/.profile")
    line = f'\n# Added by scdl GUI\nexport PATH="$PATH:{directory}"\n'
    try:
        existing = ""
        if os.path.exists(profile_path):
            with open(profile_path, encoding="utf-8") as f:
                existing = f.read()
        if directory not in existing:
            with open(profile_path, "a", encoding="utf-8") as f:
                f.write(line)
    except OSError:
        pass


# ── Helpers ──────────────────────────────────────────────────────────────────
def save_config(data: dict):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(data, f, indent=2)


def load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {}


def resolve_scdl_path(profile: dict | None = None) -> str | None:
    if is_frozen_app():
        return sys.executable

    if profile:
        configured = os.path.expanduser(str(profile.get("scdl_path", "")).strip())
        if configured and os.path.exists(configured):
            return configured
    else:
        configured = os.path.expanduser(str(load_config().get("scdl_path", "")).strip())
        if configured and os.path.exists(configured):
            return configured

    for executable in scdl_executable_names():
        found = shutil.which(executable)
        if found:
            return found

    for candidate in candidate_scdl_paths():
        path = os.path.expanduser(candidate)
        if os.path.isfile(path):
            return path
    return None


def scdl_available() -> bool:
    return is_frozen_app() or resolve_scdl_path() is not None


def save_scdl_path(path: str):
    config = load_config()
    config["scdl_path"] = path
    save_config(config)


def scdl_installed_version() -> str | None:
    try:
        from importlib import metadata
        return metadata.version("scdl")
    except Exception:
        return None


def scdl_latest_version(timeout: float = 6) -> str | None:
    try:
        request = urllib.request.Request(
            "https://pypi.org/pypi/scdl/json",
            headers={"User-Agent": "multimusicarchiver"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        return data.get("info", {}).get("version")
    except Exception:
        return None


def check_scdl_update() -> str | None:
    """Returns the latest scdl version if an update is available, else None."""
    installed = scdl_installed_version()
    if not installed:
        return None
    latest = scdl_latest_version()
    if latest and latest != installed:
        return latest
    return None


def bandcamp_cookies_help_message(link_url: str = BANDCAMP_COOKIES_HELP_LINK_URL) -> str:
    return f"{BANDCAMP_COOKIES_HELP_PREFIX}{link_url}{BANDCAMP_COOKIES_HELP_SUFFIX}"


def write_bandcamp_cookies_template(path: str):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("# Netscape HTTP Cookie File\n")
        for line in textwrap.wrap(bandcamp_cookies_help_message(), width=100):
            f.write(f"# {line}\n")
        f.write("\n")


def open_text_file_in_editor(path: str):
    system = platform.system()
    if system == "Windows":
        subprocess.Popen(["notepad.exe", path])
    elif system == "Darwin":
        subprocess.Popen(["open", "-e", path])
    else:
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
        if editor and shutil.which(editor):
            subprocess.Popen([editor, path])
        else:
            subprocess.Popen(["xdg-open", path])


def yt_dlp_available() -> str | None:
    yt_dlp = shutil.which("yt-dlp")
    if yt_dlp:
        return yt_dlp

    scdl = resolve_scdl_path()
    if not scdl:
        return None

    sibling = os.path.join(os.path.dirname(scdl), "yt-dlp")
    return sibling if os.path.exists(sibling) else None


def scdl_python_available(profile: dict | None = None) -> str | None:
    if is_frozen_app():
        return sys.executable

    scdl = resolve_scdl_path(profile)
    if not scdl:
        return None

    script_candidates = [scdl]
    if os.name == "nt":
        base, _ = os.path.splitext(scdl)
        script_candidates.extend((base + "-script.py", base + ".py"))

    shebang = ""
    for script_path in script_candidates:
        try:
            with open(script_path, encoding="utf-8") as script:
                shebang = script.readline().strip()
            break
        except (OSError, UnicodeError):
            continue

    if shebang.startswith("#!"):
        python = shebang[2:].strip()
        if python.startswith('"') and '"' in python[1:]:
            python = python[1:].split('"', 1)[0]
        if python and os.path.exists(python):
            return python
    return sys.executable


def probe_soundcloud_track_ids_inline(url: str, token: str) -> list[tuple[int, str]]:
    from yt_dlp import YoutubeDL
    from yt_dlp.extractor.soundcloud import SoundcloudIE

    class QuietLogger:
        def debug(self, msg):
            pass

        def info(self, msg):
            pass

        def warning(self, msg):
            pass

        def error(self, msg):
            print(msg, file=sys.stderr)

    old_extract_info_dict = SoundcloudIE._extract_info_dict

    def flat_extract_info_dict(self, info, full_title=None, secret_token=None, extract_flat=False):
        return old_extract_info_dict(self, info, full_title, secret_token, True)

    SoundcloudIE._extract_info_dict = flat_extract_info_dict

    params = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "logger": QuietLogger(),
    }
    if token:
        params["username"] = "oauth"
        params["password"] = token

    def collect_tracks(node, tracks):
        if isinstance(node, list):
            for entry in node:
                collect_tracks(entry, tracks)
            return
        if not isinstance(node, dict):
            return

        entries = node.get("entries")
        if entries is not None:
            collect_tracks(entries, tracks)
            return

        track_id = node.get("id")
        if not track_id:
            for key in ("url", "webpage_url", "original_url"):
                value = str(node.get(key) or "")
                match = re.search(r"(?:tracks/|soundcloud:tracks:)(\d{6,})", value)
                if match:
                    track_id = match.group(1)
                    break
        if track_id:
            try:
                index = int(node.get("playlist_index") or len(tracks) + 1)
            except (TypeError, ValueError):
                index = len(tracks) + 1
            tracks.append((index, str(track_id)))

    try:
        with YoutubeDL(params) as ydl:
            info = ydl.extract_info(url, download=False, process=False)
    finally:
        SoundcloudIE._extract_info_dict = old_extract_info_dict

    tracks = []
    collect_tracks(info, tracks)
    return tracks


def probe_soundcloud_track_ids(
    url: str,
    dl_type: str,
    token: str,
    profile: dict | None = None,
) -> tuple[list[tuple[int, str]] | None, str | None]:
    python = scdl_python_available(profile)
    if not python:
        return None, "scdl was not found; skipped to avoid the original download API."

    if is_frozen_app():
        try:
            return probe_soundcloud_track_ids_inline(scdl_effective_url(url, dl_type), token), None
        except Exception as err:
            return None, str(err)

    helper = r'''
import json
import sys

from yt_dlp import YoutubeDL
from yt_dlp.extractor.soundcloud import SoundcloudIE


class QuietLogger:
    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        print(msg, file=sys.stderr)


old_extract_info_dict = SoundcloudIE._extract_info_dict


def flat_extract_info_dict(self, info, full_title=None, secret_token=None, extract_flat=False):
    return old_extract_info_dict(self, info, full_title, secret_token, True)


SoundcloudIE._extract_info_dict = flat_extract_info_dict

url = sys.argv[1]
token = sys.argv[2]
params = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "extract_flat": "in_playlist",
    "logger": QuietLogger(),
}
if token:
    params["username"] = "oauth"
    params["password"] = token


def collect_tracks(node, tracks):
    if isinstance(node, list):
        for entry in node:
            collect_tracks(entry, tracks)
        return
    if not isinstance(node, dict):
        return

    entries = node.get("entries")
    if entries is not None:
        collect_tracks(entries, tracks)
        return

    track_id = node.get("id")
    if not track_id:
        for key in ("url", "webpage_url", "original_url"):
            value = str(node.get(key) or "")
            match = __import__("re").search(r"(?:tracks/|soundcloud:tracks:)(\d{6,})", value)
            if match:
                track_id = match.group(1)
                break
    if track_id:
        try:
            index = int(node.get("playlist_index") or len(tracks) + 1)
        except (TypeError, ValueError):
            index = len(tracks) + 1
        tracks.append((index, str(track_id)))


with YoutubeDL(params) as ydl:
    info = ydl.extract_info(url, download=False, process=False)

tracks = []
collect_tracks(info, tracks)
print(json.dumps(tracks))
'''
    try:
        result = subprocess.run(
            [python, "-c", helper, scdl_effective_url(url, dl_type), token],
            capture_output=True,
            text=True,
            timeout=180,
            encoding="utf-8",
            errors="replace",
            env=child_process_env(),
        )
    except subprocess.TimeoutExpired:
        return None, "Archive preflight timed out; skipped to avoid the original download API."

    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "unknown error"
        return None, f"Archive preflight failed ({detail}); skipped to avoid the original download API."

    try:
        return [(int(index), str(track_id)) for index, track_id in json.loads(result.stdout)], None
    except (json.JSONDecodeError, TypeError, ValueError):
        return None, "Archive preflight found no track ids; skipped to avoid the original download API."


ARCHIVE_SCAN_HELPER = r'''
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from yt_dlp import YoutubeDL
from yt_dlp.extractor.soundcloud import SoundcloudIE


class QuietLogger:
    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        pass


old_extract_info_dict = SoundcloudIE._extract_info_dict


def flat_extract_info_dict(self, info, full_title=None, secret_token=None, extract_flat=False):
    return old_extract_info_dict(self, info, full_title, secret_token, True)


SoundcloudIE._extract_info_dict = flat_extract_info_dict

token = sys.argv[1]
workers = int(sys.argv[2])
track_ids = json.loads(sys.stdin.read())
params = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "extract_flat": "in_playlist",
    "logger": QuietLogger(),
}
if token:
    params["username"] = "oauth"
    params["password"] = token

thread_local = threading.local()


def get_ydl():
    if not hasattr(thread_local, "ydl"):
        thread_local.ydl = YoutubeDL(params)
    return thread_local.ydl


def check_track(track_id):
    try:
        info = get_ydl().extract_info(
            f"https://api.soundcloud.com/tracks/{track_id}",
            download=False,
            process=False,
        )
        found_id = str(info.get("id") or "")
        ok = found_id == str(track_id)
        return {
            "id": str(track_id),
            "ok": ok,
            "error": None if ok else "Resolved to a different or empty track response",
        }
    except Exception as err:
        return {"id": str(track_id), "ok": False, "error": str(err)}


with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
    futures = [executor.submit(check_track, track_id) for track_id in track_ids]
    for future in as_completed(futures):
        print(json.dumps(future.result()), flush=True)
'''


MP3_TAG_HELPER = r'''
import json
import os
import re
import sys
from pathlib import Path

from mutagen.id3 import ID3, ID3NoHeaderError, TXXX
from mutagen.mp4 import MP4
from mutagen.wave import WAVE

SOUNDCLOUD_ID_DESC = "SoundCloud ID"
MP4_SOUNDCLOUD_ID_KEY = "----:com.apple.iTunes:SoundCloud ID"
FILENAME_ID_RE = re.compile(r"^(?P<prefix>.*?)\[(?P<id>\d{6,})\]\s*(?P<title>.+)$")
AUDIO_EXTS = {".mp3", ".m4a", ".mp4", ".aac", ".flac", ".opus", ".ogg", ".wav"}


def audio_files_under(base):
    if not base.exists():
        return
    for root, _, files in os.walk(base, followlinks=True):
        for filename in files:
            path = Path(root) / filename
            if path.suffix.lower() in AUDIO_EXTS:
                yield path


def unique_path(path):
    if not path.exists():
        return path
    for index in range(1, 1000):
        candidate = path.with_name(f"{path.stem} ({index}){path.suffix}")
        if not candidate.exists():
            return candidate
    return path


def filename_id_match(stem):
    match = FILENAME_ID_RE.match(stem)
    if not match:
        return None
    prefix = re.sub(r"(?:\s*[-_.])+\s*$", "", match.group("prefix").strip())
    title = match.group("title").strip()
    if prefix:
        clean_stem = f"{prefix} - {title}"
    else:
        clean_stem = title
    return match.group("id"), clean_stem


def tag_id3(path, track_id):
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    tags.delall("TXXX:" + SOUNDCLOUD_ID_DESC)
    tags.add(TXXX(encoding=3, desc=SOUNDCLOUD_ID_DESC, text=[str(track_id)]))
    tags.save(path)


def read_id3(path):
    try:
        tags = ID3(path)
    except Exception:
        return None
    for frame in tags.getall("TXXX"):
        if frame.desc == SOUNDCLOUD_ID_DESC and frame.text:
            return str(frame.text[0])
    return None


def tag_wav(path, track_id):
    audio = WAVE(path)
    if audio.tags is None:
        audio.add_tags()
    audio.tags.delall("TXXX:" + SOUNDCLOUD_ID_DESC)
    audio.tags.add(TXXX(encoding=3, desc=SOUNDCLOUD_ID_DESC, text=[str(track_id)]))
    audio.save()


def read_wav(path):
    try:
        audio = WAVE(path)
    except Exception:
        return None
    if audio.tags is None:
        return None
    for frame in audio.tags.getall("TXXX"):
        if frame.desc == SOUNDCLOUD_ID_DESC and frame.text:
            return str(frame.text[0])
    return None


def tag_mp4(path, track_id):
    tags = MP4(path)
    tags[MP4_SOUNDCLOUD_ID_KEY] = [str(track_id).encode("utf-8")]
    tags.save()


def read_mp4(path):
    try:
        tags = MP4(path)
    except Exception:
        return None
    values = tags.tags.get(MP4_SOUNDCLOUD_ID_KEY) if tags.tags else None
    if not values:
        return None
    value = values[0]
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def tag_file(path, track_id):
    ext = path.suffix.lower()
    if ext == ".mp3":
        tag_id3(path, track_id)
    elif ext == ".wav":
        tag_wav(path, track_id)
    elif ext in {".m4a", ".mp4", ".aac"}:
        tag_mp4(path, track_id)
    else:
        audio = __import__("mutagen").File(path)
        if audio is None:
            raise ValueError(f"Unsupported audio file: {path}")
        audio[SOUNDCLOUD_ID_DESC] = [str(track_id)]
        audio.save()


def read_track_id(path):
    ext = path.suffix.lower()
    if ext == ".mp3":
        return read_id3(path)
    if ext == ".wav":
        return read_wav(path)
    if ext in {".m4a", ".mp4", ".aac"}:
        return read_mp4(path)
    try:
        audio = __import__("mutagen").File(path)
    except Exception:
        return None
    if not audio:
        return None
    values = audio.get(SOUNDCLOUD_ID_DESC) or audio.get(SOUNDCLOUD_ID_DESC.lower())
    return str(values[0]) if values else None


mode = sys.argv[1]
base = Path(sys.argv[2]).expanduser()

if mode == "postprocess":
    changed = []
    for path in audio_files_under(base):
        match = filename_id_match(path.stem)
        if not match:
            continue
        track_id, clean_stem = match
        tag_file(path, track_id)
        target = unique_path(path.with_name(clean_stem + path.suffix))
        old_path = path
        if target != path:
            path.rename(target)
        changed.append({"id": track_id, "old": str(old_path), "new": str(target)})
    print(json.dumps(changed))
elif mode == "scan":
    tagged = {}
    audio_count = 0
    dir_count = 0
    if base.exists():
        for _, _, _ in os.walk(base, followlinks=True):
            dir_count += 1
    for path in audio_files_under(base):
        audio_count += 1
        track_id = read_track_id(path)
        if track_id:
            tagged.setdefault(track_id, []).append(str(path))
    print(json.dumps({"tagged": tagged, "audio_count": audio_count, "dir_count": dir_count}))
else:
    raise SystemExit(f"Unknown mode: {mode}")
'''


def build_scdl_cmd(profile: dict) -> list[str]:
    url = profile.get("url", "").strip()
    if not url:
        raise ValueError("Missing SoundCloud URL")

    if is_frozen_app():
        cmd = [sys.executable, "--run-scdl", "-l", url]
    else:
        scdl = resolve_scdl_path(profile)
        if not scdl:
            raise FileNotFoundError("scdl was not found. Open the GUI normally and register the schedule again.")
        cmd = [scdl, "-l", url]
    ensure_scdl_config_file()

    dl = profile.get("dl_type", "track")
    if dl == "all":
        cmd.append("-a")
    elif dl == "uploads":
        cmd.append("-t")
    elif dl == "likes":
        cmd.append("-f")
    elif dl == "playlists":
        cmd.append("-p")

    path = profile.get("path", "").strip()
    if path:
        cmd += ["--path", os.path.expanduser(path)]

    cmd += [
        "--name-format", NAME_FORMAT,
        "--playlist-name-format", PLAYLIST_NAME_FORMAT,
    ]

    token = profile.get("token", "").strip()
    if token:
        cmd += ["--auth-token", token]

    if profile.get("only_mp3"):
        cmd.append("--onlymp3")
    if profile.get("flac"):
        cmd.append("--flac")
    if profile.get("opus"):
        cmd.append("--opus")
    if profile.get("original"):
        cmd.append("--only-original")
    if profile.get("original_art"):
        cmd.append("--original-art")
    if profile.get("skip_existing"):
        cmd.append("-c")

    if profile.get("use_archive"):
        archive = profile.get("archive_path", "").strip()
        if not archive:
            raise ValueError("Missing archive path")
        archive = os.path.expanduser(archive)
        os.makedirs(os.path.dirname(archive) or ".", exist_ok=True)
        if not os.path.exists(archive):
            with open(archive, "w", encoding="utf-8"):
                pass
        cmd += ["--download-archive", archive]

    return cmd


BANDCAMP_MAX_NAME_PART_LEN = 80


def build_bandcamp_cmd(profile: dict) -> list[str]:
    username = profile.get("bandcamp_username", "").strip()
    if not username:
        raise ValueError("Missing Bandcamp username")

    cookies = profile.get("bandcamp_cookies", "").strip()
    if not cookies:
        raise ValueError("Missing Bandcamp cookies file")
    cookies = os.path.expanduser(cookies)
    if not os.path.isfile(cookies):
        raise ValueError("Bandcamp cookies file does not exist")

    # bandcamp-downloader is vendored into this app (source/vendor_bandcamp_downloader.py)
    # rather than resolved as a separately installed tool, so it's always run by
    # re-invoking this same program (or the frozen executable) with --run-bandcamp.
    if is_frozen_app():
        cmd = [sys.executable, "--run-bandcamp"]
    else:
        cmd = [sys.executable, os.path.abspath(__file__), "--run-bandcamp"]

    path = profile.get("bandcamp_path_to", "").strip()
    if path:
        cmd += ["--directory", os.path.expanduser(path)]

    cmd += ["--filename-format", BANDCAMP_FILENAME_FORMAT]

    file_format = profile.get("bandcamp_format", "mp3-320").strip() or "mp3-320"
    cmd += ["--cookies", cookies, "--format", file_format]

    incremental_sync = bool(bandcamp_archive_ids_for_profile(profile))

    for key, flag in (
        ("bandcamp_parallel_downloads", "--parallel-downloads"),
        ("bandcamp_wait_after_download", "--wait-after-download"),
        ("bandcamp_max_download_attempts", "--max-download-attempts"),
        ("bandcamp_retry_wait", "--retry-wait"),
    ):
        value = "1" if incremental_sync and key == "bandcamp_parallel_downloads" else str(profile.get(key, "")).strip()
        if value:
            cmd += [flag, value]

    for key, flag in (
        ("bandcamp_download_since", "--download-since"),
        ("bandcamp_download_until", "--download-until"),
    ):
        value = str(profile.get(key, "")).strip()
        if value:
            cmd += [flag, value]

    if profile.get("bandcamp_include_hidden"):
        cmd.append("--include-hidden")
    if profile.get("bandcamp_summary"):
        cmd.append("--summary")
    if profile.get("bandcamp_dry_run"):
        cmd.append("--dry-run")
    if profile.get("bandcamp_verbose") or incremental_sync:
        cmd.append("--verbose")
    if incremental_sync:
        # Need VERBOSE>=2 so the tool logs 'Album being saved to [...]' for
        # every fresh download, which is how we detect the archive boundary.
        cmd.append("--verbose")

    cmd.append(username)
    return cmd


def bandcamp_base_path(profile: dict) -> str:
    path = profile.get("bandcamp_path_to", "").strip()
    return os.path.expanduser(path) if path else os.getcwd()


def is_zip_valid(path: str) -> bool:
    try:
        with zipfile.ZipFile(path) as archive:
            return archive.testzip() is None
    except Exception:
        # Besides BadZipFile/OSError, testzip() can raise RuntimeError
        # (encrypted members) or NotImplementedError (unsupported
        # compression); a validity probe must never abort the whole scan.
        return False


BANDCAMP_ITEM_NAME_RE = re.compile(r"^\[(?P<id>\d+)\]\s*(?P<label>.+)$")
BANDCAMP_SAVE_LOG_RE = re.compile(r"Album being saved to \[(?P<path>.+)\]")
BANDCAMP_DIRECT_AUDIO_EXTS = {".mp3", ".m4a", ".flac", ".aiff", ".wav", ".ogg"}
BANDCAMP_FILENAME_FORMAT = os.path.join("{artist}", "[{item_id}] {artist} - {title}")


def parse_bandcamp_item_from_stem(stem: str) -> tuple[str, str] | None:
    match = BANDCAMP_ITEM_NAME_RE.match(stem)
    if not match:
        return None
    return match.group("id"), match.group("label").strip()


def bandcamp_item_from_save_line(line: str) -> tuple[str, str] | None:
    """Returns (item_id, save_path) for an 'Album being saved to [...]' log
    line, or None if the line isn't one (or the filename lacks our [id] tag)."""
    match = BANDCAMP_SAVE_LOG_RE.search(line)
    if not match:
        return None
    save_path = match.group("path")
    stem = os.path.splitext(os.path.basename(save_path))[0]
    parsed = parse_bandcamp_item_from_stem(stem)
    return (parsed[0], save_path) if parsed else None


def bandcamp_item_output_backed(save_path: str, extract: bool) -> bool:
    """Whether the archived item being re-downloaded to save_path already has
    real output on disk backing its archive entry: an extracted album folder,
    or a sorted single. With extraction off nothing can back a re-download --
    an intact zip at save_path would have satisfied the downloader's
    existing-file check and no download would have started. Used to tell a
    redundant re-download (safe to stop the sync at) from a self-heal
    re-download of an album whose files were lost (must be left to finish)."""
    root = os.path.dirname(save_path)
    stem, ext = os.path.splitext(os.path.basename(save_path))
    ext = ext.lower()
    parsed = parse_bandcamp_item_from_stem(stem)
    if not parsed:
        return False
    _, label = parsed
    if ext == ".zip":
        return extract and bandcamp_zip_already_extracted(root, label)
    if ext in BANDCAMP_DIRECT_AUDIO_EXTS:
        return bandcamp_single_already_sorted(os.path.join(root, "Singles"), label, ext)
    return False


WINDOWS_INVALID_FILENAME_CHARS_RE = re.compile(r'[<>:"|?*\x00-\x1f]')
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_windows_filename(name: str) -> str:
    """Zip member names can contain characters a real Windows filesystem
    refuses outright -- not just <>:"|?*, but any control character (0x00-
    0x1f), which is exactly what crashed extraction on a track whose artist
    name embedded a literal backspace. Replace those, trim trailing dots/
    spaces (also illegal), and dodge the reserved DOS device names."""
    name = WINDOWS_INVALID_FILENAME_CHARS_RE.sub("_", name).rstrip(" .")
    if not name:
        name = "_"
    if name.split(".")[0].upper() in WINDOWS_RESERVED_NAMES:
        name = "_" + name
    return name


def unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    for index in range(1, 1000):
        candidate = f"{base} ({index}){ext}"
        if not os.path.exists(candidate):
            return candidate
    return path


def extract_zip_safely(zip_path: str, target_dir: str):
    """Extracts every member of zip_path into target_dir, sanitizing each
    path segment for Windows-illegal characters and dropping '..'/'.'
    segments (also closes the classic zip-slip path-traversal hole that
    ZipFile.extractall() is vulnerable to). Members whose sanitized paths
    collide (e.g. 'a?' and 'a*' both mapping to 'a_', or duplicate names)
    are uniquified so one track can't silently overwrite another."""
    os.makedirs(target_dir, exist_ok=True)
    seen_files: set[str] = set()
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            parts = [p for p in member.filename.replace("\\", "/").split("/") if p not in ("", ".", "..")]
            if not parts:
                continue
            dest = os.path.join(target_dir, *(sanitize_windows_filename(p) for p in parts))
            if member.is_dir():
                os.makedirs(dest, exist_ok=True)
                continue
            if os.path.normcase(dest) in seen_files:
                base, ext = os.path.splitext(dest)
                for index in range(1, 1000):
                    candidate = f"{base} ({index}){ext}"
                    if os.path.normcase(candidate) not in seen_files:
                        dest = candidate
                        break
            seen_files.add(os.path.normcase(dest))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with archive.open(member) as source, open(dest, "wb") as target:
                shutil.copyfileobj(source, target)


def bandcamp_zip_already_extracted(root: str, label: str) -> bool:
    target_dir = os.path.join(root, label)
    try:
        return os.path.isdir(target_dir) and any(os.scandir(target_dir))
    except OSError:
        return False


def bandcamp_single_already_sorted(singles_dir: str, label: str, ext: str) -> bool:
    expected = os.path.join(singles_dir, f"{sanitize_windows_filename(label)}{ext}")
    return os.path.isfile(expected) and os.path.getsize(expected) > 0


def process_bandcamp_downloads(
    base_path: str,
    extract: bool,
    archive_ids: set[str] | None = None,
) -> tuple[list[tuple[str, str]], int, list[str], str | None]:
    """Scan a Bandcamp download folder after a run. Only files this app
    downloaded -- recognizable by the '[item_id] ' filename prefix our
    --filename-format produces -- are ever touched; anything else in the
    folder is left strictly alone. For recognized files: drop any zip (or
    leftover .part file) that's corrupted/incomplete, and drop redundant
    re-downloads of albums already in the archive -- but only once real
    extracted/sorted output on disk backs up that archive claim, since a
    bare archive.txt entry with nothing to show for it (e.g. left by an
    older, buggy run that got interrupted before finishing extraction) must
    never cause the only copy of an album to be deleted; such items just
    get processed fresh instead, self-healing the archive. With extraction
    off, an archived zip *is* the final output, so it is never deleted.
    Optionally extracts the remaining valid zips into a clean
    'Artist - Title' album subfolder, files singles (Bandcamp downloads
    these as a bare audio file rather than a zip) into an 'Artist/Singles'
    folder with the same clean naming, and collects every newly-confirmed
    album (zipped or single) as a (item_id, 'Artist - Title') pair for
    archiving -- only once extraction/sorting has actually succeeded. A
    failure on one album (corrupted zip, unwritable file, etc.) is recorded
    and skipped rather than aborting the whole scan -- one bad album must
    never block every other album from being processed."""
    archive_ids = archive_ids or set()
    base = os.path.expanduser(base_path)
    confirmed: list[tuple[str, str]] = []
    extracted_count = 0
    removed: list[str] = []
    errors: list[str] = []
    if not os.path.isdir(base):
        return confirmed, extracted_count, removed, None
    for root, _, files in os.walk(base):
        for filename in files:
            stem, ext = os.path.splitext(filename)
            ext = ext.lower()
            path = os.path.join(root, filename)
            if ext == ".part":
                # Stale staging file from an interrupted download (the
                # vendored downloader streams to '<name>.part' and renames on
                # success). Only clean up ones carrying our [item_id] tag.
                if parse_bandcamp_item_from_stem(os.path.splitext(stem)[0]):
                    try:
                        os.remove(path)
                    except OSError as err:
                        errors.append(f"{filename}: {err}")
                        continue
                    removed.append(path)
                continue
            if ext == ".zip":
                parsed = parse_bandcamp_item_from_stem(stem)
                if not parsed:
                    # Not a zip this app downloaded -- leave it alone.
                    continue
                if parsed[0] in archive_ids:
                    if not extract:
                        # Extraction is off, so the zip itself is the final
                        # output backing the archive entry. Never delete it.
                        continue
                    if bandcamp_zip_already_extracted(root, parsed[1]):
                        # Redundant re-download of an album whose extracted
                        # output is already on disk.
                        try:
                            os.remove(path)
                        except OSError as err:
                            errors.append(f"{filename}: {err}")
                            continue
                        removed.append(path)
                        continue
                    # Archived but nothing on disk backs it up -- fall
                    # through and process it fresh (self-healing).
                if not is_zip_valid(path):
                    try:
                        os.remove(path)
                    except OSError as err:
                        errors.append(f"{filename}: {err}")
                        continue
                    removed.append(path)
                    continue
                if extract:
                    try:
                        extract_zip_safely(path, os.path.join(root, parsed[1]))
                        os.remove(path)
                    except Exception as err:
                        errors.append(f"{filename}: {err}")
                        continue
                    extracted_count += 1
                confirmed.append(parsed)
            elif ext in BANDCAMP_DIRECT_AUDIO_EXTS:
                # Bandcamp singles/tracks download as a bare audio file
                # rather than a zip, so there's nothing to extract -- instead
                # they get filed into a "Singles" subfolder under the artist
                # with the [item_id] tag stripped from the filename, the
                # same cleanup a zipped album gets via extraction.
                parsed = parse_bandcamp_item_from_stem(stem)
                if not parsed:
                    continue
                item_id, label = parsed
                singles_dir = os.path.join(root, "Singles")
                already_archived = item_id in archive_ids
                # Same trust-but-verify rule as zips: don't delete unless the
                # sorted file is actually there to prove it was archived for real.
                if already_archived and bandcamp_single_already_sorted(singles_dir, label, ext):
                    try:
                        os.remove(path)
                    except OSError as err:
                        errors.append(f"{filename}: {err}")
                        continue
                    removed.append(path)
                    continue
                try:
                    if os.path.getsize(path) <= 0:
                        continue
                except OSError:
                    continue
                try:
                    os.makedirs(singles_dir, exist_ok=True)
                    target_path = unique_path(
                        os.path.join(singles_dir, f"{sanitize_windows_filename(label)}{ext}")
                    )
                    os.replace(path, target_path)
                except OSError as err:
                    errors.append(f"{filename}: {err}")
                    continue
                confirmed.append(parsed)
    error_summary = None
    if errors:
        shown = errors[:5]
        error_summary = "; ".join(shown)
        if len(errors) > len(shown):
            error_summary += f"; and {len(errors) - len(shown)} more"
    return confirmed, extracted_count, removed, error_summary


def read_bandcamp_archive_ids(archive_path: str) -> set[str]:
    ids: set[str] = set()
    try:
        with open(archive_path, encoding="utf-8") as archive:
            for line in archive:
                entry = line.strip()
                if not entry:
                    continue
                parts = entry.split(maxsplit=2)
                if len(parts) >= 2 and parts[0].lower() == "bandcamp":
                    ids.add(parts[1])
    except FileNotFoundError:
        pass
    return ids


BANDCAMP_ARCHIVE_HEADER_RE = re.compile(r"^#\s*total\s*:?\s*(\d+)", re.IGNORECASE)
BANDCAMP_FOUND_ITEMS_RE = re.compile(r"Found \[(\d+)\] downloadable items in")


def read_bandcamp_archive_total(archive_path: str) -> int | None:
    """Reads the '# total N' header line written by append_bandcamp_archive.
    Returns None if the archive has no header (e.g. it's empty, or predates
    this feature) -- callers should treat that as 'unknown, don't validate'."""
    try:
        with open(archive_path, encoding="utf-8") as archive:
            first_line = archive.readline()
    except FileNotFoundError:
        return None
    match = BANDCAMP_ARCHIVE_HEADER_RE.match(first_line.strip())
    return int(match.group(1)) if match else None


def bandcamp_archive_ids_for_profile(profile: dict) -> set[str]:
    if not profile.get("bandcamp_use_archive"):
        return set()
    path = os.path.expanduser(profile.get("bandcamp_archive_path", "").strip())
    if not path:
        return set()
    return read_bandcamp_archive_ids(path)


def bandcamp_archive_total_for_profile(profile: dict) -> int | None:
    if not profile.get("bandcamp_use_archive"):
        return None
    path = os.path.expanduser(profile.get("bandcamp_archive_path", "").strip())
    if not path:
        return None
    return read_bandcamp_archive_total(path)


def append_bandcamp_archive(archive_path: str, confirmed: list[tuple[str, str]]) -> int:
    if not confirmed:
        return 0
    existing_ids = read_bandcamp_archive_ids(archive_path)
    seen: set[str] = set()
    new_entries: list[tuple[str, str]] = []
    for item_id, label in confirmed:
        if item_id in existing_ids or item_id in seen:
            continue
        seen.add(item_id)
        new_entries.append((item_id, label))
    if not new_entries:
        return 0

    os.makedirs(os.path.dirname(archive_path) or ".", exist_ok=True)
    try:
        with open(archive_path, encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        lines = []
    body_lines = lines[1:] if lines and BANDCAMP_ARCHIVE_HEADER_RE.match(lines[0].strip()) else lines
    if body_lines and not body_lines[-1].endswith("\n"):
        # Without this, the first new entry would be appended onto the last
        # existing line and both records would be corrupted.
        body_lines[-1] += "\n"

    new_total = len(existing_ids) + len(new_entries)
    # Write to a sibling temp file and swap it in atomically: a crash or kill
    # mid-write must never leave the archive truncated, since a wrong archive
    # is what drives deletion decisions on the next run.
    temp_path = archive_path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        f.write(f"# total {new_total}\n")
        f.writelines(body_lines)
        for item_id, label in new_entries:
            f.write(f"bandcamp {item_id} {label}\n")
    os.replace(temp_path, archive_path)
    return len(new_entries)


def terminate_process_tree(process: subprocess.Popen):
    """Kills `process` and any children it spawned. On Windows, tools
    installed via a .cmd/.bat launcher run as a child of that wrapper, and
    Popen.terminate() only kills the wrapper -- taskkill /T is needed to
    actually stop the real work underneath it."""
    if platform.system() == "Windows":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True, text=True, timeout=10,
            )
            return
        except Exception:
            pass
    try:
        process.terminate()
    except Exception:
        pass


def run_bandcamp_download(
    cmd: list[str],
    archive_ids: set[str],
    archive_total: int | None,
    extract: bool = True,
    on_line=None,
    on_start=None,
    on_archive_check=None,
    on_archive_continue=None,
    on_archive_reprocess=None,
) -> tuple[int, bool]:
    """Runs bandcamp-downloader, streaming its output to `on_line`. If
    `archive_ids` is non-empty (an incremental sync against an existing
    archive), watches for the tool starting a fresh download of an album
    that's already archived.

    Bandcamp collections are newest-first, but "newest" means most recently
    *acquired*, not most recently released -- buying a discography bundle
    that includes an album you already own re-acquires it, bumping that
    already-archived album up to a newer position without it actually being
    new. So a single already-archived hit doesn't by itself prove everything
    older is covered too. Instead, every time one comes up we check whether
    we're actually done: (new albums attempted so far this run) +
    (archive's recorded total) compared against the collection's own
    reported size. If that sum is still short, the tool is left running
    past this item -- it's very likely a re-shuffled duplicate rather than
    the sync boundary. Only once every album is accounted for (or if the
    totals aren't available to check at all) do we stop early.

    Additionally, an archived item is only ever treated as the stop boundary
    if its output actually exists on disk (extracted folder / sorted single).
    An archived item with *no* backing output is a self-heal re-download of
    lost files: killing it here would delete the partial file afterwards and
    repeat forever, so it's always left to finish instead.

    Returns (returncode, stopped_early)."""
    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        encoding="utf-8", errors="replace", env=child_process_env(),
    )
    if on_start:
        on_start(process)
    stopped_early = False
    session_new_count = 0
    found_total: int | None = None
    assert process.stdout is not None
    for line in process.stdout:
        if on_line:
            on_line(line)
        if found_total is None:
            match = BANDCAMP_FOUND_ITEMS_RE.search(line)
            if match:
                found_total = int(match.group(1))
        if archive_ids and not stopped_early:
            saved = bandcamp_item_from_save_line(line)
            if saved:
                item_id, save_path = saved
                in_archive = item_id in archive_ids
                if on_archive_check:
                    on_archive_check(item_id, in_archive)
                if not in_archive:
                    session_new_count += 1
                    continue
                if not bandcamp_item_output_backed(save_path, extract):
                    # Archived but its files are gone locally: this fresh
                    # download is the self-heal. Killing it would strand the
                    # album forever (partial file deleted, archive entry
                    # kept, same kill next run) -- let it finish.
                    if on_archive_reprocess:
                        on_archive_reprocess(item_id)
                    continue
                if archive_total is not None and found_total is not None:
                    accounted_for = session_new_count + archive_total
                    if accounted_for < found_total:
                        if on_archive_continue:
                            on_archive_continue(item_id, session_new_count, archive_total, found_total)
                        continue
                stopped_early = True
                terminate_process_tree(process)
    process.wait()
    return process.returncode, stopped_early


def redact_cmd(cmd: list[str]) -> list[str]:
    redacted = list(cmd)
    for index, part in enumerate(redacted[:-1]):
        if part == "--auth-token":
            redacted[index + 1] = "<redacted>"
        elif part == "-c" and len(redacted[index + 1]) > 200:
            redacted[index + 1] = "<inline launcher script>"
    return redacted


def audio_base_path(profile: dict) -> str:
    path = profile.get("path", "").strip()
    return os.path.expanduser(path) if path else os.getcwd()


def run_mp3_tag_helper(
    mode: str,
    base_path: str,
    profile: dict | None = None,
) -> tuple[object | None, str | None]:
    python = scdl_python_available(profile)
    if not python:
        return None, "scdl Python environment was not found."
    if is_frozen_app():
        helper_cmd = [sys.executable, "--mp3-tag-helper", mode, base_path]
    else:
        helper_cmd = [python, "-c", MP3_TAG_HELPER, mode, base_path]
    try:
        result = subprocess.run(
            helper_cmd,
            capture_output=True,
            text=True,
            timeout=300,
            encoding="utf-8",
            errors="replace",
            env=child_process_env(),
        )
    except subprocess.TimeoutExpired:
        return None, "Audio tag helper timed out."

    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "unknown error"
        return None, detail

    try:
        return json.loads(result.stdout), None
    except json.JSONDecodeError:
        return None, "Audio tag helper returned invalid JSON."


def postprocess_downloaded_mp3s(profile: dict) -> tuple[list[dict], str | None]:
    data, error = run_mp3_tag_helper("postprocess", audio_base_path(profile), profile)
    return (data or []), error


def scan_tagged_mp3s(
    base_path: str,
    profile: dict | None = None,
) -> tuple[dict[str, list[str]], str | None]:
    tagged, _, _, error = scan_audio_tags(base_path, profile)
    return tagged, error


def scan_audio_tags(
    base_path: str,
    profile: dict | None = None,
) -> tuple[dict[str, list[str]], int, int, str | None]:
    data, error = run_mp3_tag_helper("scan", base_path, profile)
    if isinstance(data, dict) and isinstance(data.get("tagged"), dict):
        tagged = {str(k): list(v) for k, v in data["tagged"].items()}
        return tagged, int(data.get("audio_count") or 0), int(data.get("dir_count") or 0), error
    if isinstance(data, dict):
        return {str(k): list(v) for k, v in data.items()}, 0, 0, error
    return {}, 0, 0, error


def scdl_effective_url(url: str, dl_type: str) -> str:
    if dl_type == "track" and url.rstrip("/").lower().endswith(("/tracks", "/likes", "/sets")):
        return url

    suffixes = {
        "uploads": "tracks",
        "likes": "likes",
        "playlists": "sets",
    }
    suffix = suffixes.get(dl_type)
    return f"{url.rstrip('/')}/{suffix}" if suffix else url


def read_archive_ids(archive_path: str) -> set[str]:
    ids: set[str] = set()
    try:
        with open(archive_path, encoding="utf-8") as archive:
            for line in archive:
                entry = line.strip()
                if not entry:
                    continue
                ids.add(entry)
                parts = entry.split(maxsplit=2)
                if len(parts) >= 2:
                    ids.add(parts[1])
                for match in re.finditer(r"\d{6,}", entry):
                    ids.add(match.group(0))
    except FileNotFoundError:
        return ids
    return ids


def archive_has_ids(archive_path: str) -> bool:
    try:
        with open(archive_path, encoding="utf-8") as archive:
            return any(line.strip() for line in archive)
    except FileNotFoundError:
        return False


def archive_line_track_id(entry: str) -> str | None:
    parts = entry.split(maxsplit=2)
    if not parts:
        return None
    if len(parts) >= 2 and parts[0].lower() == "soundcloud":
        return parts[1]
    match = re.search(r"\d{6,}", entry)
    return match.group(0) if match else parts[0]


def read_archive_track_ids(archive_path: str) -> list[str]:
    seen: set[str] = set()
    track_ids: list[str] = []
    try:
        with open(archive_path, encoding="utf-8") as archive:
            for line in archive:
                entry = line.strip()
                if not entry:
                    continue
                track_id = archive_line_track_id(entry)
                if track_id and track_id not in seen:
                    seen.add(track_id)
                    track_ids.append(track_id)
    except FileNotFoundError:
        return track_ids
    return track_ids


def archive_has_track(archive_ids: set[str], track_id: str) -> bool:
    return track_id in archive_ids or f"soundcloud {track_id}" in archive_ids


def merge_session_archive(session_path: str, archive_path: str):
    """After a download ran against a session copy of the archive (with the
    retried track ids removed so yt-dlp wouldn't skip them), fold any entries
    the downloader appended back into the real archive, then delete the
    session copy. Entries already present in the real archive are skipped."""
    try:
        with open(session_path, encoding="utf-8") as session:
            session_entries = [line.strip() for line in session if line.strip()]
    except FileNotFoundError:
        return
    existing = read_archive_ids(archive_path)
    new_lines = []
    for entry in session_entries:
        track_id = archive_line_track_id(entry)
        if entry in existing or (track_id and archive_has_track(existing, track_id)):
            continue
        new_lines.append(entry + "\n")
    if new_lines:
        needs_newline = False
        try:
            with open(archive_path, "rb") as archive:
                archive.seek(0, os.SEEK_END)
                if archive.tell():
                    archive.seek(-1, os.SEEK_END)
                    needs_newline = archive.read(1) != b"\n"
        except OSError:
            pass
        with open(archive_path, "a", encoding="utf-8") as archive:
            if needs_newline:
                archive.write("\n")
            archive.writelines(new_lines)
    try:
        os.remove(session_path)
    except OSError:
        pass


def compress_playlist_items(items: list[int]) -> str:
    ranges = []
    start = prev = items[0]
    for item in items[1:]:
        if item == prev + 1:
            prev = item
            continue
        ranges.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = item
    ranges.append(str(start) if start == prev else f"{start}-{prev}")
    return ",".join(ranges)


def with_playlist_items(cmd: list[str], items: str) -> list[str]:
    updated = list(cmd)
    argstring = f"--playlist-items {shlex.quote(items)}"
    if "--yt-dlp-args" in updated:
        index = updated.index("--yt-dlp-args") + 1
        if index < len(updated):
            updated[index] = f"{updated[index]} {argstring}"
        else:
            updated.append(argstring)
    else:
        updated += ["--yt-dlp-args", argstring]
    return updated


def filter_archived_originals(
    cmd: list[str],
    url: str,
    dl_type: str,
    archive_path: str,
    token: str,
    local_track_ids: set[str] | None = None,
    profile: dict | None = None,
) -> tuple[list[str] | None, str | None, object | None]:
    """Returns (cmd, note, finalize). `finalize`, when set, must be called
    after the download finishes: it merges the session archive copy (used so
    yt-dlp doesn't skip archived tracks being retried) back into the real
    archive."""
    archive_ids = read_archive_ids(archive_path)
    if not archive_ids:
        return cmd, "Archive is empty or missing; running full original download.", None
    local_track_ids = local_track_ids or set()

    tracks, error = probe_soundcloud_track_ids(url, dl_type, token, profile)
    if tracks is None:
        return cmd, f"Archive preflight skipped ({error}); relying on scdl's download archive.", None

    total = 0
    skipped = 0
    retry_archived = 0
    retried_ids: set[str] = set()
    unarchived_items: list[int] = []
    for index, track_id in tracks:
        if not track_id or track_id == "NA":
            continue

        total += 1
        if archive_has_track(archive_ids, track_id):
            if track_id in local_track_ids:
                skipped += 1
                continue
            retry_archived += 1
            retried_ids.add(track_id)

        unarchived_items.append(index)

    if total == 0:
        return cmd, "Archive preflight could not enumerate track ids; relying on scdl's download archive.", None
    if skipped == 0 and retry_archived == 0:
        return cmd, f"Archive preflight checked {total} track(s); none were archived.", None
    if not unarchived_items:
        return None, (
            f"All {total} track(s) are already in the archive and have tagged local audio; "
            "skipped without calling scdl."
        ), None

    filtered_cmd = with_playlist_items(cmd, compress_playlist_items(unarchived_items))
    finalize = None
    session_error = None
    if retried_ids and "--download-archive" in filtered_cmd:
        # scdl forwards --download-archive to yt-dlp, and yt-dlp skips any id
        # already in the archive even when it's explicitly selected via
        # --playlist-items -- so retried tracks would silently never download.
        # Point the run at a session copy with the retried ids removed, and
        # merge whatever the run appends back into the real archive afterwards.
        try:
            session_path = archive_path + ".session"
            session_lines = []
            with open(archive_path, encoding="utf-8") as archive_file:
                for line in archive_file:
                    entry = line.strip()
                    if entry and archive_line_track_id(entry) in retried_ids:
                        continue
                    session_lines.append(line if line.endswith("\n") else line + "\n")
            with open(session_path, "w", encoding="utf-8") as session_file:
                session_file.writelines(session_lines)
            index = filtered_cmd.index("--download-archive") + 1
            if index < len(filtered_cmd):
                filtered_cmd[index] = session_path

                def finalize(session_path=session_path, archive_path=archive_path):
                    merge_session_archive(session_path, archive_path)
        except OSError as err:
            session_error = str(err)

    note = f"Archive preflight skipped {skipped} archived track(s) with tagged local audio"
    if retry_archived:
        note += f"; retrying {retry_archived} archived track(s) missing tagged local audio"
    note += f"; downloading {len(unarchived_items)} track(s)."
    if session_error:
        note += f" Session archive could not be created ({session_error}); archived retries may be skipped."
    return filtered_cmd, note, finalize


def prepare_download_cmd(profile: dict) -> tuple[list[str] | None, str | None, object | None]:
    """Returns (cmd, note, finalize); call `finalize()` (if set) once the
    download subprocess has finished."""
    cmd = build_scdl_cmd(profile)
    if profile.get("use_archive"):
        postprocess_downloaded_mp3s(profile)
        tagged_audio, tag_error = scan_tagged_mp3s(audio_base_path(profile), profile)
        local_track_ids = set(tagged_audio.keys())
        archive_path = os.path.expanduser(profile.get("archive_path", "").strip())
        if not os.path.exists(archive_path):
            os.makedirs(os.path.dirname(archive_path) or ".", exist_ok=True)
            with open(archive_path, "w", encoding="utf-8"):
                pass
        try:
            filtered_cmd, note, finalize = filter_archived_originals(
                cmd,
                profile.get("url", "").strip(),
                profile.get("dl_type", "track"),
                archive_path,
                profile.get("token", "").strip(),
                local_track_ids,
                profile,
            )
        except Exception as err:
            return cmd, f"Archive preflight failed ({err}); running full download.", None
        if tag_error:
            note = f"Tagged audio scan failed ({tag_error}); archived tracks will be retried. {note or ''}".strip()
        return filtered_cmd, note, finalize
    return cmd, None, None


def prune_scheduled_logs(days: int = 7):
    if not os.path.isdir(LOG_DIR):
        return
    cutoff = datetime.now().timestamp() - days * 24 * 60 * 60
    for name in os.listdir(LOG_DIR):
        if not name.startswith("scheduled-") or not name.endswith(".log"):
            continue
        path = os.path.join(LOG_DIR, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            pass


def run_scheduled_download(payload: str, log_stream=None) -> int:
    out = log_stream or sys.stdout
    profile = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
    return run_scheduled_profile(profile, out, log_stream)


def run_scheduled_download_file(path: str, log_stream=None) -> int:
    out = log_stream or sys.stdout
    with open(os.path.expanduser(path), encoding="utf-8") as f:
        profile = json.load(f)
    return run_scheduled_profile(profile, out, log_stream)


def run_scheduled_profile(profile: dict, out, log_stream=None) -> int:
    service = profile.get("service", "soundcloud")
    if service == "bandcamp":
        cmd = build_bandcamp_cmd(profile)
        archive_ids = bandcamp_archive_ids_for_profile(profile)
        archive_total = bandcamp_archive_total_for_profile(profile)
        if archive_ids:
            print(
                f"Archive has {len(archive_ids)} album(s) on record; syncing sequentially and "
                "stopping once a previously archived album comes up.",
                file=out, flush=True,
            )
        print(shell_join(redact_cmd(cmd)), file=out, flush=True)

        def on_line(line):
            print(line, end="", file=out, flush=True)

        def on_archive_check(item_id, in_archive):
            status = "already archived" if in_archive else "new"
            print(f"   [archive check] {item_id}: {status}", file=out, flush=True)

        def on_archive_continue(item_id, session_new_count, archived_total, collection_total):
            print(
                f"   [archive check] {item_id} is already archived, but only "
                f"{session_new_count + archived_total}/{collection_total} album(s) are accounted "
                "for so far (likely a re-shuffled duplicate, e.g. from a discography purchase) -- "
                "continuing past it.",
                file=out, flush=True,
            )

        def on_archive_reprocess(item_id):
            print(
                f"   [archive check] {item_id} is archived but has no files on disk backing it -- "
                "letting the fresh download finish so it can self-heal.",
                file=out, flush=True,
            )

        rc, stopped_early = run_bandcamp_download(
            cmd, archive_ids, archive_total, bool(profile.get("bandcamp_extract")),
            on_line=on_line, on_archive_check=on_archive_check,
            on_archive_continue=on_archive_continue, on_archive_reprocess=on_archive_reprocess,
        )
        if stopped_early:
            print("Reached a previously archived album; stopping sync early.", file=out, flush=True)

        if profile.get("bandcamp_dry_run"):
            print("Dry run: skipped zip post-processing and archive updates.", file=out, flush=True)
            return rc

        confirmed, extracted_count, removed, error = process_bandcamp_downloads(
            bandcamp_base_path(profile), bool(profile.get("bandcamp_extract")), archive_ids
        )
        if error:
            print(f"Zip processing failed: {error}", file=out, flush=True)
        if removed:
            print(f"Removed {len(removed)} zip file(s) that were corrupted, incomplete, or already archived.", file=out, flush=True)
        if extracted_count:
            print(f"Extracted and removed {extracted_count} zip file(s).", file=out, flush=True)
        if profile.get("bandcamp_use_archive") and confirmed:
            archive_path = os.path.expanduser(profile.get("bandcamp_archive_path", "").strip())
            if archive_path:
                added = append_bandcamp_archive(archive_path, confirmed)
                if added:
                    print(f"Archived {added} newly confirmed album(s).", file=out, flush=True)
        return rc

    cmd, note, finalize = prepare_download_cmd(profile)
    if note:
        print(note, file=out, flush=True)
    if cmd is None:
        return 0
    print(shell_join(redact_cmd(cmd)), file=out, flush=True)
    if log_stream:
        rc = subprocess.run(cmd, stdout=out, stderr=subprocess.STDOUT, env=child_process_env()).returncode
    else:
        rc = subprocess.run(cmd, env=child_process_env()).returncode
    if finalize:
        try:
            finalize()
        except Exception as err:
            print(f"Archive session merge failed: {err}", file=out, flush=True)
    changed, error = postprocess_downloaded_mp3s(profile)
    if error:
        print(f"Audio tag/rename skipped: {error}", file=out, flush=True)
    elif changed:
        print(f"Tagged and renamed {len(changed)} audio file(s).", file=out, flush=True)
    return rc


def run_scheduled_download_with_log(payload: str) -> int:
    os.makedirs(LOG_DIR, exist_ok=True)
    prune_scheduled_logs()
    started = datetime.now()
    log_path = os.path.join(LOG_DIR, f"scheduled-{started.strftime('%Y-%m-%d_%H-%M-%S')}.log")
    with open(log_path, "w", encoding="utf-8") as log:
        print(f"scdl GUI scheduled run started {started.isoformat(timespec='seconds')}", file=log, flush=True)
        print(f"Log file: {log_path}\n", file=log, flush=True)
        try:
            rc = run_scheduled_download(payload, log_stream=log)
        except Exception as err:
            print(f"\nScheduled run failed before completion: {err}", file=log, flush=True)
            rc = 1
        finished = datetime.now()
        print(
            f"\nscdl GUI scheduled run finished {finished.isoformat(timespec='seconds')} "
            f"with exit code {rc}",
            file=log,
            flush=True,
        )
    return rc


def run_scheduled_download_file_with_log(path: str) -> int:
    os.makedirs(LOG_DIR, exist_ok=True)
    prune_scheduled_logs()
    started = datetime.now()
    log_path = os.path.join(LOG_DIR, f"scheduled-{started.strftime('%Y-%m-%d_%H-%M-%S')}.log")
    with open(log_path, "w", encoding="utf-8") as log:
        print(f"scdl GUI scheduled run started {started.isoformat(timespec='seconds')}", file=log, flush=True)
        print(f"Schedule file: {path}", file=log, flush=True)
        print(f"Log file: {log_path}\n", file=log, flush=True)
        try:
            rc = run_scheduled_download_file(path, log_stream=log)
        except Exception as err:
            print(f"\nScheduled run failed before completion: {err}", file=log, flush=True)
            rc = 1
        finished = datetime.now()
        print(
            f"\nscdl GUI scheduled run finished {finished.isoformat(timespec='seconds')} "
            f"with exit code {rc}",
            file=log,
            flush=True,
        )
    return rc


# ── Schedule helpers (OS-level) ───────────────────────────────────────────────
def shell_join(cmd: list[str]) -> str:
    if platform.system() == "Windows":
        return subprocess.list2cmdline(cmd)
    return " ".join(shlex.quote(part) for part in cmd)


def schedule_profile(profile: dict) -> dict:
    if profile.get("service") == "bandcamp":
        keys = (
            "service",
            "bandcamp_username",
            "bandcamp_path_to",
            "bandcamp_cookies",
            "bandcamp_format",
            "bandcamp_parallel_downloads",
            "bandcamp_wait_after_download",
            "bandcamp_max_download_attempts",
            "bandcamp_retry_wait",
            "bandcamp_download_since",
            "bandcamp_download_until",
            "bandcamp_use_archive",
            "bandcamp_archive_path",
            "bandcamp_include_hidden",
            "bandcamp_extract",
            "bandcamp_summary",
            "bandcamp_dry_run",
            "bandcamp_verbose",
        )
        return {key: profile.get(key) for key in keys if key in profile}

    keys = (
        "service",
        "url",
        "path",
        "token",
        "dl_type",
        "skip_existing",
        "only_mp3",
        "flac",
        "opus",
        "original",
        "original_art",
        "use_archive",
        "archive_path",
        "scdl_path",
    )
    return {key: profile.get(key) for key in keys if key in profile}


def build_scheduled_command(profile: dict, sid: str | None = None) -> list[str]:
    profile = schedule_profile(profile)
    if profile.get("service") != "bandcamp":
        profile["scdl_path"] = resolve_scdl_path(profile) or ""
    if sid:
        os.makedirs(SCHEDULE_DIR, exist_ok=True)
        schedule_path = os.path.join(SCHEDULE_DIR, f"{sid}.json")
        with open(schedule_path, "w", encoding="utf-8") as f:
            json.dump(profile, f, indent=2)
        return [sys.executable, os.path.abspath(__file__), "--scheduled-download-file", schedule_path]

    payload = base64.urlsafe_b64encode(json.dumps(profile).encode("utf-8")).decode("ascii")
    return [sys.executable, os.path.abspath(__file__), "--scheduled-download", payload]


def schedule_id(profile: dict, hour: int, minute: int) -> str:
    data = {
        "url": profile.get("url", ""),
        "path": os.path.expanduser(profile.get("path", "")),
        "dl_type": profile.get("dl_type", ""),
        "archive_path": os.path.expanduser(profile.get("archive_path", "")),
        "hour": hour,
        "minute": minute,
    }
    if profile.get("service") == "bandcamp":
        # The fields above are all SoundCloud's, so without these two
        # Bandcamp schedules at the same time would hash identically and
        # silently overwrite each other. SoundCloud profiles keep the legacy
        # hash so re-registering an existing schedule still replaces it.
        data.update({
            "service": "bandcamp",
            "bandcamp_username": profile.get("bandcamp_username", ""),
            "bandcamp_path_to": os.path.expanduser(profile.get("bandcamp_path_to", "")),
            "bandcamp_archive_path": os.path.expanduser(profile.get("bandcamp_archive_path", "")),
        })
    digest = hashlib.sha1(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()
    return digest[:12]


def register_schedule_macos(hour: int, minute: int, cmd: list[str], sid: str, prefix: str):
    """Write a launchd plist to ~/Library/LaunchAgents/"""
    label = f"com.{prefix}.dailysync.{sid}"
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    {''.join(f'<string>{html.escape(part)}</string>' for part in cmd)}
  </array>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>{hour}</integer>
    <key>Minute</key><integer>{minute}</integer>
  </dict>
  <key>RunAtLoad</key><false/>
</dict>
</plist>"""
    launch_agents_dir = os.path.expanduser("~/Library/LaunchAgents")
    os.makedirs(launch_agents_dir, exist_ok=True)
    plist_path = os.path.join(launch_agents_dir, f"{label}.plist")
    with open(plist_path, "w") as f:
        f.write(plist)
    subprocess.run(["launchctl", "unload", plist_path], capture_output=True, text=True)
    subprocess.run(["launchctl", "load", plist_path], check=True)
    return plist_path


def register_schedule_windows(hour: int, minute: int, cmd: list[str], sid: str, prefix: str):
    """Use schtasks to register a daily task."""
    task_name = f"{prefix}_daily_sync_{sid}"
    time_str = f"{hour:02d}:{minute:02d}"
    result = subprocess.run([
        "schtasks", "/create", "/f",
        "/tn", task_name,
        "/tr", shell_join(cmd),
        "/sc", "DAILY",
        "/st", time_str,
    ], capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(detail or f"schtasks failed with exit code {result.returncode}")
    return task_name


def register_schedule_linux(hour: int, minute: int, cmd: list[str], sid: str, prefix: str):
    """Add/replace a cron entry."""
    start_marker = f"# {prefix}_sync {sid} start"
    end_marker = f"# {prefix}_sync {sid} end"
    cron_line = f"{minute} {hour} * * * {shell_join(cmd)}\n"
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    no_crontab = result.returncode != 0 and "no crontab for" in (result.stderr or "").lower()
    if result.returncode != 0 and not no_crontab:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(detail or f"crontab -l failed with exit code {result.returncode}")

    existing = result.stdout if result.returncode == 0 else ""
    lines = []
    skipping = False
    for line in existing.splitlines():
        if line == start_marker:
            skipping = True
            continue
        if skipping and line == end_marker:
            skipping = False
            continue
        if not skipping:
            lines.append(line)
    lines.extend([start_marker, cron_line.rstrip(), end_marker])
    new_cron = "\n".join(lines).strip() + "\n"
    if not new_cron.strip() or cron_line.rstrip() not in new_cron:
        raise RuntimeError("Refusing to write an empty or invalid crontab.")

    os.makedirs(CONFIG_DIR, exist_ok=True)
    backup_path = os.path.join(CONFIG_DIR, "crontab.bak")
    with open(backup_path, "w", encoding="utf-8") as backup:
        backup.write(existing)

    proc = subprocess.run(["crontab", "-"], input=new_cron, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(detail or f"crontab write failed with exit code {proc.returncode}")
    return f"crontab entry (backup: {backup_path})"


def schedule_location() -> str:
    system = platform.system()
    if system == "Darwin":
        return os.path.expanduser("~/Library/LaunchAgents")
    if system == "Windows":
        return "taskschd.msc"
    return "crontab -e"


def open_linux_crontab_editor():
    terminal_commands = [
        ["x-terminal-emulator", "-e", "sh", "-lc", "crontab -e"],
        ["gnome-terminal", "--", "sh", "-lc", "crontab -e"],
        ["konsole", "-e", "sh", "-lc", "crontab -e"],
        ["xfce4-terminal", "-e", "sh -lc 'crontab -e'"],
        ["mate-terminal", "-e", "sh -lc 'crontab -e'"],
        ["xterm", "-e", "sh", "-lc", "crontab -e"],
    ]
    for cmd in terminal_commands:
        if shutil.which(cmd[0]):
            subprocess.Popen(cmd)
            return
    raise RuntimeError("Could not find a terminal emulator to open `crontab -e`.")


def open_schedule_location():
    system = platform.system()
    location = schedule_location()
    if system == "Darwin":
        os.makedirs(location, exist_ok=True)
        subprocess.Popen(["open", location])
    elif system == "Windows":
        os.startfile(location)
    else:
        open_linux_crontab_editor()


def open_schedule_log_location():
    os.makedirs(LOG_DIR, exist_ok=True)
    if platform.system() == "Darwin":
        subprocess.Popen(["open", LOG_DIR])
    elif platform.system() == "Windows":
        os.startfile(LOG_DIR)
    else:
        subprocess.Popen(["xdg-open", LOG_DIR])


# ── Main App ─────────────────────────────────────────────────────────────────
class TimePicker(ctk.CTkFrame):
    def __init__(self, master, values: list[str], width: int = 70, state: str = "normal"):
        super().__init__(master, fg_color="transparent", width=width, height=28)
        self.grid_propagate(False)
        self._values = values
        self._value = values[0] if values else ""
        self._state = state
        self._dropdown = None
        self._hovered_value = None
        self._rows = {}
        self._outside_click_binding = None
        self._column_count = 3 if len(values) > 12 else 1

        self.button = ctk.CTkButton(self, text=self._value, width=width, command=self._toggle_dropdown)
        self.button.grid(row=0, column=0, sticky="nsew")
        self.button.bind("<ButtonPress-1>", self._open_for_drag, add="+")
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.configure(state=state)

    def get(self) -> str:
        return self._value

    def set(self, value: str):
        self._value = value
        self.button.configure(text=value)

    def configure(self, **kwargs):
        state = kwargs.pop("state", None)
        if state is not None:
            self._state = state
            if hasattr(self, "button"):
                self.button.configure(state=state)
            if state == "disabled":
                self._close_dropdown()
        if kwargs:
            super().configure(**kwargs)

    config = configure

    def _toggle_dropdown(self):
        if self._state == "disabled":
            return
        if platform.system() == "Darwin":
            # Opening already happens on ButtonPress via _open_for_drag; tk_popup()
            # holds its own grab until dismissed, so this (fired on ButtonRelease)
            # would otherwise re-open the just-closed menu. See _open_native_menu.
            return
        if self._dropdown is not None and self._dropdown.winfo_exists():
            self._close_dropdown()
        else:
            self._open_dropdown()

    def _open_for_drag(self, _event=None):
        if self._state == "disabled":
            return
        if platform.system() == "Darwin":
            self._open_native_menu()
        else:
            self._open_dropdown()

    def _open_native_menu(self):
        """A borderless custom Toplevel (what _open_dropdown below uses) is
        unreliable to click on macOS -- Tk/Aqua doesn't always hit-test a newly
        mapped override-redirect window under a stationary cursor, so clicks on
        its rows can silently do nothing. Tk's own ttk::combobox sidesteps this
        on Aqua by posting a native menu instead of a custom popup; do the same
        here rather than fighting Tk's override-redirect click handling."""
        menu = Menu(self, tearoff=0)
        for value in self._values:
            menu.add_command(label=value, command=lambda v=value: self._select_value(v))
        x = self.winfo_rootx()
        y = self.winfo_rooty() + self.winfo_height()
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _open_dropdown(self):
        if self._dropdown is not None and self._dropdown.winfo_exists():
            return

        dropdown = ctk.CTkToplevel(self)
        dropdown.overrideredirect(True)
        dropdown.attributes("-topmost", True)
        dropdown.bind("<Escape>", lambda _event: self._close_dropdown())
        dropdown.bind("<ButtonRelease-1>", self._select_hovered)
        self._dropdown = dropdown
        self._rows = {}
        self._hovered_value = None

        row_height = 26
        column_width = max(self.winfo_width(), 56)
        for row_index, value in enumerate(self._values):
            grid_row = row_index // self._column_count
            grid_column = row_index % self._column_count
            row = ctk.CTkLabel(dropdown, text=value, width=column_width, height=row_height, anchor="center")
            row.grid(row=grid_row, column=grid_column, sticky="ew")
            row.bind("<Enter>", lambda _event, v=value: self._hover_value(v))
            row.bind("<ButtonRelease-1>", lambda _event, v=value: self._select_value(v))
            self._rows[value] = row
        for column in range(self._column_count):
            dropdown.grid_columnconfigure(column, weight=1)

        dropdown.update_idletasks()
        self._place_dropdown(row_height, column_width)
        self._outside_click_binding = self.winfo_toplevel().bind(
            "<ButtonPress-1>", self._close_on_outside_click, add="+")
        dropdown.focus_force()

    def _place_dropdown(self, row_height: int, column_width: int):
        assert self._dropdown is not None
        width = max(self.winfo_width(), column_width * self._column_count)
        row_count = max(1, math.ceil(len(self._values) / self._column_count))
        desired_height = row_height * row_count
        screen_height = self.winfo_screenheight()
        x = self.winfo_rootx()
        below_y = self.winfo_rooty() + self.winfo_height() + 3
        above_y = self.winfo_rooty() - desired_height - 3

        space_below = screen_height - below_y - 12
        space_above = self.winfo_rooty() - 12
        if desired_height <= space_below or space_below >= space_above:
            y = below_y
            height = min(desired_height, max(row_height, space_below))
        else:
            height = min(desired_height, max(row_height, space_above))
            y = max(12, self.winfo_rooty() - height - 3)

        self._dropdown.geometry(f"{width}x{height}+{x}+{y}")

    def _hover_value(self, value: str):
        self._hovered_value = value
        for row_value, row in self._rows.items():
            row.configure(fg_color=("gray75", "gray30") if row_value == value else "transparent")

    def _select_hovered(self, _event=None):
        if self._hovered_value is not None:
            self._select_value(self._hovered_value)

    def _select_value(self, value: str):
        self.set(value)
        self._close_dropdown()

    def _close_on_outside_click(self, event):
        if self._dropdown is None or not self._dropdown.winfo_exists():
            return
        clicked_widget = self.winfo_containing(event.x_root, event.y_root)
        if clicked_widget is None:
            self._close_dropdown()
            return
        parent = clicked_widget
        while parent is not None:
            if parent is self or parent is self._dropdown:
                return
            parent = getattr(parent, "master", None)
        self._close_dropdown()

    def _close_dropdown(self):
        if self._outside_click_binding is not None:
            try:
                self.winfo_toplevel().unbind("<ButtonPress-1>", self._outside_click_binding)
            except Exception:
                pass
            self._outside_click_binding = None
        if self._dropdown is not None and self._dropdown.winfo_exists():
            self._dropdown.destroy()
        self._dropdown = None
        self._hovered_value = None
        self._rows = {}


class ScdlApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Multi Music Archiver")
        self.geometry("950x820")
        self.minsize(800, 620)
        self.resizable(True, True)

        self._process: subprocess.Popen | None = None
        self._archive_scan_process: subprocess.Popen | None = None
        self._archive_scan_stopping = False
        self._archive_scan_active = False
        self._download_stopping = False
        self._active_download_profile: dict | None = None
        self._undo_stack: list[tuple[ctk.CTkEntry, str, str]] = []
        self._config = load_config()
        self._scdl_installer_running = False
        self._scdl_update_available: str | None = None
        self._scdl_controls = []
        self._bandcamp_controls = []

        self._build_ui()
        self._load_saved_values()
        if scdl_available():
            ensure_scdl_config_file()
        self._refresh_scdl_state()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        if not scdl_available():
            self._log("scdl not found. Use the Install scdl button in the header to install it.\n", "warn")

        self._check_for_updates()

    # ── UI Construction ───────────────────────────────────────────────────────
    def _build_ui(self):
        self.grid_columnconfigure(0, weight=0)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(3, weight=1)

        self.active_service = ctk.StringVar(value="soundcloud")
        sidebar = ctk.CTkFrame(self, corner_radius=0, fg_color=("gray86", "gray12"), width=150)
        sidebar.grid(row=0, column=0, rowspan=5, sticky="nsw")
        sidebar.grid_propagate(False)
        ctk.CTkLabel(sidebar, text="Archive", font=ctk.CTkFont(size=16, weight="bold")).pack(
            anchor="w", padx=16, pady=(18, 12)
        )
        self.soundcloud_nav_btn = ctk.CTkButton(
            sidebar,
            text="SoundCloud",
            anchor="w",
            command=lambda: self._switch_service("soundcloud"),
        )
        self.soundcloud_nav_btn.pack(fill="x", padx=10, pady=(0, 6))
        self.bandcamp_nav_btn = ctk.CTkButton(
            sidebar,
            text="Bandcamp",
            anchor="w",
            fg_color="transparent",
            command=lambda: self._switch_service("bandcamp"),
        )
        self.bandcamp_nav_btn.pack(fill="x", padx=10)

        # ── Header ──
        header = ctk.CTkFrame(self, corner_radius=0, fg_color=("gray90", "gray15"))
        header.grid(row=0, column=1, sticky="ew", padx=0, pady=0)
        self.header_link = ctk.CTkLabel(
            header, text="scdl  •  SoundCloud Downloader",
            font=ctk.CTkFont(size=17, weight="bold"),
            cursor="hand2",
        )
        self.header_link.pack(side="left", padx=18, pady=12)
        self.header_link.bind("<Button-1>", lambda _event: self._open_scdl_repo())
        self.install_scdl_btn = ctk.CTkButton(
            header,
            text="Install scdl",
            width=110,
            command=self._install_scdl,
        )
        self.update_scdl_btn = ctk.CTkButton(
            header,
            text="Update scdl",
            width=110,
            fg_color=("#a06a00", "#8a5a00"),
            hover_color=("#8a5a00", "#734a00"),
            command=self._install_scdl,
        )

        # ── URL + Path row ──
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=1, column=1, sticky="ew", padx=16, pady=(12, 0))
        top.grid_columnconfigure(1, weight=1)
        top.grid_columnconfigure(2, weight=0)

        ctk.CTkLabel(top, text="URL", width=112, anchor="e").grid(row=0, column=0, padx=(0, 8), pady=4)
        self.url_entry = ctk.CTkEntry(top, placeholder_text="https://soundcloud.com/…")
        self.url_entry.grid(row=0, column=1, sticky="ew", pady=4)

        ctk.CTkLabel(top, text="Save to", width=112, anchor="e").grid(row=1, column=0, padx=(0, 8), pady=4)
        self.path_entry = ctk.CTkEntry(top, placeholder_text="~/Music/SoundCloud")
        self.path_entry.grid(row=1, column=1, sticky="ew", pady=4)
        self.path_browse_btn = ctk.CTkButton(top, text="Browse…", width=80, command=self._pick_folder)
        self.path_browse_btn.grid(row=1, column=2, padx=(6, 0))

        auth_label = ctk.CTkFrame(top, fg_color="transparent", width=112)
        auth_label.grid(row=2, column=0, padx=(0, 8), pady=4, sticky="e")
        self.auth_help = ctk.CTkLabel(
            auth_label,
            text="?",
            width=22,
            height=22,
            corner_radius=11,
            fg_color=("gray80", "gray30"),
            text_color=("gray20", "gray90"),
            font=ctk.CTkFont(weight="bold"),
        )
        self.auth_help.pack(side="left", padx=(0, 5))
        self.auth_help.bind("<Enter>", self._show_auth_tooltip)
        self.auth_help.bind("<Leave>", self._hide_auth_tooltip)
        ctk.CTkLabel(auth_label, text="Auth token", width=85, anchor="e").pack(side="left")
        self.token_entry = ctk.CTkEntry(
            top,
            placeholder_text="OAuth token: needed for original files, HQ downloads with GO+",
            show="•",
        )
        self.token_entry.grid(row=2, column=1, sticky="ew", pady=4)
        self.token_show_btn = ctk.CTkButton(top, text="Show", width=80, command=self._toggle_token)
        self.token_show_btn.grid(row=2, column=2, padx=(6, 0))
        self._auth_tooltip = None
        self._auth_tooltip_hide_after = None
        self.soundcloud_top = top

        bandcamp_top = ctk.CTkFrame(self, fg_color="transparent")
        bandcamp_top.grid(row=1, column=1, sticky="ew", padx=16, pady=(12, 0))
        bandcamp_top.grid_columnconfigure(1, weight=1)
        bandcamp_top.grid_columnconfigure(2, weight=0)

        ctk.CTkLabel(bandcamp_top, text="Username", width=112, anchor="e").grid(row=0, column=0, padx=(0, 8), pady=4)
        self.bandcamp_username_entry = ctk.CTkEntry(
            bandcamp_top,
            placeholder_text="Bandcamp username from bandcamp.com/username",
        )
        self.bandcamp_username_entry.grid(row=0, column=1, columnspan=2, sticky="ew", pady=4)

        ctk.CTkLabel(bandcamp_top, text="Save to", width=112, anchor="e").grid(row=1, column=0, padx=(0, 8), pady=4)
        self.bandcamp_path_entry = ctk.CTkEntry(bandcamp_top, placeholder_text="~/Music/Bandcamp")
        self.bandcamp_path_entry.grid(row=1, column=1, sticky="ew", pady=4)
        self.bandcamp_path_browse_btn = ctk.CTkButton(
            bandcamp_top,
            text="Browse...",
            width=80,
            command=self._pick_bandcamp_folder,
        )
        self.bandcamp_path_browse_btn.grid(row=1, column=2, padx=(6, 0))

        cookies_label = ctk.CTkFrame(bandcamp_top, fg_color="transparent", width=112)
        cookies_label.grid(row=2, column=0, padx=(0, 8), pady=4, sticky="e")
        self.bandcamp_cookies_help = ctk.CTkLabel(
            cookies_label,
            text="?",
            width=22,
            height=22,
            corner_radius=11,
            fg_color=("gray80", "gray30"),
            text_color=("gray20", "gray90"),
            font=ctk.CTkFont(weight="bold"),
        )
        self.bandcamp_cookies_help.pack(side="left", padx=(0, 5))
        self.bandcamp_cookies_help.bind("<Enter>", self._show_bandcamp_cookies_tooltip)
        self.bandcamp_cookies_help.bind("<Leave>", self._hide_bandcamp_cookies_tooltip)
        ctk.CTkLabel(cookies_label, text="Cookies", width=85, anchor="e").pack(side="left")
        self._bandcamp_cookies_tooltip = None
        self._bandcamp_cookies_tooltip_hide_after = None
        self.bandcamp_cookies_entry = ctk.CTkEntry(
            bandcamp_top,
            placeholder_text="Required Netscape cookies.txt export",
        )
        self.bandcamp_cookies_entry.grid(row=2, column=1, sticky="ew", pady=4)
        self.bandcamp_cookies_browse_btn = ctk.CTkButton(
            bandcamp_top,
            text="Browse...",
            width=80,
            command=self._pick_bandcamp_cookies,
        )
        self.bandcamp_cookies_browse_btn.grid(row=2, column=2, padx=(6, 0))
        self.bandcamp_top = bandcamp_top

        # ── Options ──
        opts = ctk.CTkFrame(self)
        opts.grid(row=2, column=1, sticky="ew", padx=16, pady=10)
        opts.grid_columnconfigure((0, 1, 2, 3), weight=1)

        ctk.CTkLabel(opts, text="Download type", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, columnspan=4, sticky="w", padx=12, pady=(8, 2))

        self.dl_type = ctk.StringVar(value="track")
        dl_types = [
            ("Single track / playlist URL", "track"),
            ("All uploads (no reposts)", "uploads"),
            ("All + reposts", "all"),
            ("Likes / favorites", "likes"),
            ("All playlists", "playlists"),
        ]
        self.dl_type_buttons = []
        for i, (label, val) in enumerate(dl_types):
            btn = ctk.CTkRadioButton(opts, text=label, variable=self.dl_type, value=val)
            btn.grid(row=1 + i // 3, column=i % 3, sticky="w", padx=12, pady=2)
            self.dl_type_buttons.append(btn)

        sep = ctk.CTkFrame(opts, height=1, fg_color=("gray80", "gray30"))
        sep.grid(row=3, column=0, columnspan=4, sticky="ew", padx=8, pady=6)

        ctk.CTkLabel(opts, text="Format & quality", font=ctk.CTkFont(weight="bold")).grid(
            row=4, column=0, columnspan=4, sticky="w", padx=12, pady=(2, 2))

        self.only_mp3 = ctk.BooleanVar()
        self.flac = ctk.BooleanVar()
        self.opus = ctk.BooleanVar()
        self.original = ctk.BooleanVar()
        self.original_art = ctk.BooleanVar()
        self.format_buttons = []
        for row, column, text, var in (
            (5, 0, "MP3 only", self.only_mp3),
            (5, 1, "FLAC (lossless only)", self.flac),
            (5, 2, "Prefer Opus", self.opus),
            (6, 0, "Only Original Files", self.original),
            (6, 1, "Original artwork", self.original_art),
        ):
            btn = ctk.CTkCheckBox(opts, text=text, variable=var)
            btn.grid(row=row, column=column, sticky="w", padx=12, pady=2)
            self.format_buttons.append(btn)

        sep2 = ctk.CTkFrame(opts, height=1, fg_color=("gray80", "gray30"))
        sep2.grid(row=7, column=0, columnspan=4, sticky="ew", padx=8, pady=6)

        ctk.CTkLabel(opts, text="Archive / sync", font=ctk.CTkFont(weight="bold")).grid(
            row=8, column=0, columnspan=4, sticky="w", padx=12, pady=(2, 2))

        self.use_archive = ctk.BooleanVar(value=True)
        self.skip_existing = ctk.BooleanVar(value=True)
        self.skip_existing_btn = ctk.CTkCheckBox(opts, text="Skip existing files  (-c)", variable=self.skip_existing,
                                                 command=self._toggle_skip_existing)
        self.skip_existing_btn.grid(row=9, column=0, columnspan=4, sticky="w", padx=12, pady=2)

        self.use_archive_btn = ctk.CTkCheckBox(opts, text="Use archive file", variable=self.use_archive,
                                               command=self._toggle_archive)
        self.use_archive_btn.grid(row=10, column=0, sticky="w", padx=12, pady=2)

        self.archive_entry = ctk.CTkEntry(opts, placeholder_text="sc-archive.txt path")
        self.archive_entry.grid(row=10, column=1, columnspan=2, sticky="ew", padx=(0, 6), pady=2)
        self.archive_browse_btn = ctk.CTkButton(opts, text="Browse…", width=80,
                                                command=self._pick_archive)
        self.archive_browse_btn.grid(row=10, column=3, padx=(0, 12), pady=2)

        # ── Schedule row ──
        sep3 = ctk.CTkFrame(opts, height=1, fg_color=("gray80", "gray30"))
        sep3.grid(row=11, column=0, columnspan=4, sticky="ew", padx=8, pady=6)

        ctk.CTkLabel(opts, text="Daily schedule", font=ctk.CTkFont(weight="bold")).grid(
            row=12, column=0, columnspan=4, sticky="w", padx=12, pady=(2, 2))

        self.enable_schedule = ctk.BooleanVar()
        self.enable_schedule_btn = ctk.CTkCheckBox(opts, text="Run daily at", variable=self.enable_schedule,
                                                   command=self._toggle_schedule)
        self.enable_schedule_btn.grid(row=13, column=0, sticky="w", padx=12, pady=(2, 8))
        self.schedule_hour = TimePicker(opts, values=[f"{h:02d}" for h in range(24)], width=70, state="disabled")
        self.schedule_hour.set("08")
        self.schedule_hour.grid(row=13, column=1, sticky="w", padx=(0, 4), pady=(2, 8))
        ctk.CTkLabel(opts, text=":").grid(row=13, column=1, padx=(74, 0), sticky="w")
        self.schedule_min = TimePicker(opts, values=["00", "15", "30", "45"], width=70, state="disabled")
        self.schedule_min.set("00")
        self.schedule_min.grid(row=13, column=2, sticky="w", pady=(2, 8))
        schedule_actions = ctk.CTkFrame(opts, fg_color="transparent")
        schedule_actions.grid(row=13, column=3, sticky="e", padx=12, pady=(2, 8))
        self.schedule_btn = ctk.CTkButton(schedule_actions, text="Register schedule", width=140,
                                          command=self._register_schedule, state="disabled")
        self.schedule_btn.pack(side="left")
        self.schedule_folder_btn = ctk.CTkButton(schedule_actions, text="/", width=38,
                                                 command=self._open_schedule_location)
        self.schedule_folder_btn.pack(side="left", padx=(6, 0))
        self.schedule_logs_btn = ctk.CTkButton(schedule_actions, text="📋", width=38,
                                               command=self._open_schedule_log_location)
        self.schedule_logs_btn.pack(side="left", padx=(6, 0))
        self.soundcloud_opts = opts

        bandcamp_opts = ctk.CTkFrame(self)
        bandcamp_opts.grid(row=2, column=1, sticky="ew", padx=16, pady=10)
        bandcamp_opts.grid_columnconfigure((0, 1, 2, 3), weight=1)

        self.bandcamp_format = ctk.StringVar(value="mp3-320")
        self.bandcamp_format_menu = ctk.CTkOptionMenu(
            bandcamp_opts,
            values=list(BANDCAMP_FORMATS),
            variable=self.bandcamp_format,
            width=170,
        )

        self.bandcamp_include_hidden = ctk.BooleanVar()
        self.bandcamp_extract = ctk.BooleanVar(value=True)
        self.bandcamp_summary = ctk.BooleanVar()
        self.bandcamp_dry_run = ctk.BooleanVar()
        self.bandcamp_verbose = ctk.BooleanVar()

        ctk.CTkLabel(bandcamp_opts, text="Download options", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, columnspan=4, sticky="w", padx=12, pady=(8, 2)
        )
        self.bandcamp_format_menu.grid(row=1, column=0, sticky="w", padx=12, pady=2)
        self.bandcamp_option_buttons = []
        for row, column, text, var in (
            (1, 1, "Include hidden", self.bandcamp_include_hidden),
            (1, 2, "Extract", self.bandcamp_extract),
            (1, 3, "Summary", self.bandcamp_summary),
            (2, 1, "Dry run", self.bandcamp_dry_run),
            (2, 2, "Verbose", self.bandcamp_verbose),
        ):
            btn = ctk.CTkCheckBox(bandcamp_opts, text=text, variable=var)
            btn.grid(row=row, column=column, sticky="w", padx=12, pady=2)
            self.bandcamp_option_buttons.append(btn)

        ctk.CTkLabel(bandcamp_opts, text="Limits & retries", font=ctk.CTkFont(weight="bold")).grid(
            row=3, column=0, columnspan=4, sticky="w", padx=12, pady=(6, 2)
        )
        self.bandcamp_parallel_entry = ctk.CTkEntry(bandcamp_opts, width=82)
        self.bandcamp_parallel_entry.insert(0, "5")
        self.bandcamp_wait_entry = ctk.CTkEntry(bandcamp_opts, width=82)
        self.bandcamp_wait_entry.insert(0, "1")
        self.bandcamp_attempts_entry = ctk.CTkEntry(bandcamp_opts, width=82)
        self.bandcamp_attempts_entry.insert(0, "5")
        self.bandcamp_retry_wait_entry = ctk.CTkEntry(bandcamp_opts, width=82)
        self.bandcamp_retry_wait_entry.insert(0, "5")
        for column, label, entry in (
            (0, "Parallel", self.bandcamp_parallel_entry),
            (1, "Wait", self.bandcamp_wait_entry),
            (2, "Attempts", self.bandcamp_attempts_entry),
            (3, "Retry wait", self.bandcamp_retry_wait_entry),
        ):
            ctk.CTkLabel(bandcamp_opts, text=label, anchor="w").grid(
                row=4, column=column, sticky="w", padx=12, pady=(2, 0)
            )
            entry.grid(row=5, column=column, sticky="w", padx=12, pady=(0, 2))

        ctk.CTkLabel(bandcamp_opts, text="Purchase date range (default all purchases)", font=ctk.CTkFont(weight="bold")).grid(
            row=6, column=0, columnspan=4, sticky="w", padx=12, pady=(6, 2)
        )
        self.bandcamp_since_entry = ctk.CTkEntry(bandcamp_opts, placeholder_text="YYYY-MM-DD")
        self.bandcamp_since_entry.grid(row=7, column=0, columnspan=2, sticky="ew", padx=12, pady=(2, 4))
        self.bandcamp_until_entry = ctk.CTkEntry(bandcamp_opts, placeholder_text="YYYY-MM-DD")
        self.bandcamp_until_entry.grid(row=7, column=2, columnspan=2, sticky="ew", padx=12, pady=(2, 4))

        sep_bc_archive = ctk.CTkFrame(bandcamp_opts, height=1, fg_color=("gray80", "gray30"))
        sep_bc_archive.grid(row=8, column=0, columnspan=4, sticky="ew", padx=8, pady=6)

        ctk.CTkLabel(bandcamp_opts, text="Archive / sync", font=ctk.CTkFont(weight="bold")).grid(
            row=9, column=0, columnspan=4, sticky="w", padx=12, pady=(2, 2))

        self.bandcamp_use_archive = ctk.BooleanVar()
        self.bandcamp_use_archive_btn = ctk.CTkCheckBox(
            bandcamp_opts, text="Use archive file", variable=self.bandcamp_use_archive,
            command=self._toggle_bandcamp_archive,
        )
        self.bandcamp_use_archive_btn.grid(row=10, column=0, sticky="w", padx=12, pady=2)

        self.bandcamp_archive_entry = ctk.CTkEntry(bandcamp_opts, placeholder_text="bc-archive.txt path")
        self.bandcamp_archive_entry.grid(row=10, column=1, columnspan=2, sticky="ew", padx=(0, 6), pady=2)
        self.bandcamp_archive_browse_btn = ctk.CTkButton(bandcamp_opts, text="Browse…", width=80,
                                                         command=self._pick_bandcamp_archive)
        self.bandcamp_archive_browse_btn.grid(row=10, column=3, padx=(0, 12), pady=2)

        sep_bc = ctk.CTkFrame(bandcamp_opts, height=1, fg_color=("gray80", "gray30"))
        sep_bc.grid(row=11, column=0, columnspan=4, sticky="ew", padx=8, pady=4)

        ctk.CTkLabel(bandcamp_opts, text="Daily schedule", font=ctk.CTkFont(weight="bold")).grid(
            row=12, column=0, columnspan=4, sticky="w", padx=12, pady=(2, 2)
        )
        self.bandcamp_enable_schedule = ctk.BooleanVar()
        self.bandcamp_enable_schedule_btn = ctk.CTkCheckBox(
            bandcamp_opts,
            text="Run daily at",
            variable=self.bandcamp_enable_schedule,
            command=self._toggle_bandcamp_schedule,
        )
        self.bandcamp_enable_schedule_btn.grid(row=13, column=0, sticky="w", padx=12, pady=(2, 6))
        self.bandcamp_schedule_hour = TimePicker(bandcamp_opts, values=[f"{h:02d}" for h in range(24)], width=70, state="disabled")
        self.bandcamp_schedule_hour.set("08")
        self.bandcamp_schedule_hour.grid(row=13, column=1, sticky="w", padx=(0, 4), pady=(2, 6))
        ctk.CTkLabel(bandcamp_opts, text=":").grid(row=13, column=1, padx=(74, 0), sticky="w")
        self.bandcamp_schedule_min = TimePicker(bandcamp_opts, values=["00", "15", "30", "45"], width=70, state="disabled")
        self.bandcamp_schedule_min.set("00")
        self.bandcamp_schedule_min.grid(row=13, column=2, sticky="w", pady=(2, 6))
        bandcamp_schedule_actions = ctk.CTkFrame(bandcamp_opts, fg_color="transparent")
        bandcamp_schedule_actions.grid(row=13, column=3, sticky="e", padx=12, pady=(2, 6))
        self.bandcamp_schedule_btn = ctk.CTkButton(
            bandcamp_schedule_actions,
            text="Register schedule",
            width=140,
            command=self._register_schedule,
            state="disabled",
        )
        self.bandcamp_schedule_btn.pack(side="left")
        self.bandcamp_schedule_folder_btn = ctk.CTkButton(
            bandcamp_schedule_actions,
            text="/",
            width=38,
            command=self._open_schedule_location,
        )
        self.bandcamp_schedule_folder_btn.pack(side="left", padx=(6, 0))
        self.bandcamp_schedule_logs_btn = ctk.CTkButton(
            bandcamp_schedule_actions,
            text="📋",
            width=38,
            command=self._open_schedule_log_location,
        )
        self.bandcamp_schedule_logs_btn.pack(side="left", padx=(6, 0))
        self.bandcamp_opts = bandcamp_opts

        # ── Output views ──
        output = ctk.CTkFrame(self, fg_color="transparent")
        output.grid(row=3, column=1, sticky="nsew", padx=16, pady=(0, 8))
        output.grid_columnconfigure(0, weight=1)
        output.grid_rowconfigure(1, weight=1)

        view_bar = ctk.CTkFrame(output, fg_color="transparent")
        view_bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        view_bar.grid_columnconfigure(1, weight=1)
        self.view_bar = view_bar

        self.view_switch = ctk.CTkSegmentedButton(
            view_bar,
            values=["Log", "Archive Check"],
            command=self._switch_output_view,
        )
        self.view_switch.set("Log")
        self.view_switch.grid(row=0, column=0, sticky="w")

        self.archive_scan_btn = ctk.CTkButton(
            view_bar,
            text="Scan archive",
            width=110,
            command=self._start_archive_scan,
        )
        self.archive_scan_btn.grid(row=0, column=2, sticky="e")
        self.archive_scan_help = ctk.CTkLabel(
            view_bar,
            text="?",
            width=24,
            height=24,
            corner_radius=12,
            fg_color=("gray80", "gray30"),
            text_color=("gray20", "gray90"),
            font=ctk.CTkFont(weight="bold"),
        )
        self.archive_scan_help.grid(row=0, column=3, sticky="e", padx=(6, 0))
        self.archive_scan_help.bind("<Enter>", self._show_scan_tooltip)
        self.archive_scan_help.bind("<Leave>", self._hide_scan_tooltip)
        self._scan_tooltip = None
        self._scan_tooltip_hide_after = None

        self.output_stack = ctk.CTkFrame(output)
        self.output_stack.grid(row=1, column=0, sticky="nsew")
        self.output_stack.grid_columnconfigure(0, weight=1)
        self.output_stack.grid_rowconfigure(0, weight=1)

        self.log_box = ctk.CTkTextbox(self.output_stack, font=ctk.CTkFont(family="Courier", size=12), wrap="word")
        self.log_box.grid(row=0, column=0, sticky="nsew")
        self.log_box.configure(state="disabled")

        self.archive_box = ctk.CTkTextbox(self.output_stack, font=ctk.CTkFont(family="Courier", size=12), wrap="word")
        self.archive_box.grid(row=0, column=0, sticky="nsew")
        self.archive_box.configure(state="disabled")
        self.log_box.tkraise()

        # ── Bottom bar ──
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=4, column=1, sticky="ew", padx=16, pady=(0, 12))
        bar.grid_columnconfigure(0, weight=1)

        self.status_label = ctk.CTkLabel(bar, text="Ready", text_color=("gray50", "gray60"), anchor="w")
        self.status_label.grid(row=0, column=0, sticky="w")

        self.stop_btn = ctk.CTkButton(bar, text="Stop", width=80, fg_color="gray40",
                                      hover_color="gray30", command=self._stop, state="disabled")
        self.stop_btn.grid(row=0, column=1, padx=(6, 0))

        self.dl_btn = ctk.CTkButton(bar, text="⬇  Download", width=130, command=self._start_download)
        self.dl_btn.grid(row=0, column=2, padx=(6, 0))

        self._scdl_controls = [
            self.path_browse_btn,
            self.token_show_btn,
            *self.dl_type_buttons,
            *self.format_buttons,
            self.skip_existing_btn,
            self.use_archive_btn,
            self.archive_browse_btn,
            self.enable_schedule_btn,
            self.schedule_hour,
            self.schedule_min,
            self.schedule_btn,
            self.schedule_folder_btn,
            self.schedule_logs_btn,
            self.view_switch,
            self.archive_scan_btn,
            self.stop_btn,
            self.dl_btn,
        ]
        self._bandcamp_controls = [
            self.bandcamp_path_browse_btn,
            self.bandcamp_cookies_browse_btn,
            self.bandcamp_format_menu,
            *self.bandcamp_option_buttons,
            self.bandcamp_parallel_entry,
            self.bandcamp_wait_entry,
            self.bandcamp_attempts_entry,
            self.bandcamp_retry_wait_entry,
            self.bandcamp_since_entry,
            self.bandcamp_until_entry,
            self.bandcamp_use_archive_btn,
            self.bandcamp_archive_entry,
            self.bandcamp_archive_browse_btn,
            self.bandcamp_enable_schedule_btn,
            self.bandcamp_schedule_hour,
            self.bandcamp_schedule_min,
            self.bandcamp_schedule_btn,
            self.bandcamp_schedule_folder_btn,
            self.bandcamp_schedule_logs_btn,
            self.stop_btn,
            self.dl_btn,
        ]

        for entry in (
            self.url_entry,
            self.path_entry,
            self.token_entry,
            self.archive_entry,
            self.bandcamp_username_entry,
            self.bandcamp_path_entry,
            self.bandcamp_cookies_entry,
            self.bandcamp_parallel_entry,
            self.bandcamp_wait_entry,
            self.bandcamp_attempts_entry,
            self.bandcamp_retry_wait_entry,
            self.bandcamp_since_entry,
            self.bandcamp_until_entry,
            self.bandcamp_archive_entry,
        ):
            self._enable_entry_undo(entry)
        self.bandcamp_username_entry.bind("<FocusOut>", self._normalize_bandcamp_username)
        self.bind_all("<Command-z>", self._undo_last_entry_change)
        self.bind_all("<Control-z>", self._undo_last_entry_change)
        self._switch_service("soundcloud")

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _open_scdl_repo(self):
        if self.active_service.get() == "bandcamp":
            webbrowser.open("https://github.com/easlice/bandcamp-downloader")
        else:
            webbrowser.open("https://github.com/scdl-org/scdl")

    def _switch_service(self, service: str):
        self.active_service.set(service)
        is_bandcamp = service == "bandcamp"
        self.soundcloud_top.grid() if not is_bandcamp else self.soundcloud_top.grid_remove()
        self.soundcloud_opts.grid() if not is_bandcamp else self.soundcloud_opts.grid_remove()
        self.bandcamp_top.grid() if is_bandcamp else self.bandcamp_top.grid_remove()
        self.bandcamp_opts.grid() if is_bandcamp else self.bandcamp_opts.grid_remove()
        self.header_link.configure(
            text="Bandcamp Downloader" if is_bandcamp else "scdl  •  SoundCloud Downloader"
        )
        self.soundcloud_nav_btn.configure(fg_color=("gray75", "gray25") if not is_bandcamp else "transparent")
        self.bandcamp_nav_btn.configure(fg_color=("gray75", "gray25") if is_bandcamp else "transparent")
        if self.install_scdl_btn.winfo_ismapped():
            self.install_scdl_btn.pack_forget()
        if self.update_scdl_btn.winfo_ismapped():
            self.update_scdl_btn.pack_forget()
        if is_bandcamp:
            self.view_switch.grid_remove()
            self.archive_scan_btn.grid_remove()
            self.archive_scan_help.grid_remove()
            self.view_bar.grid_remove()
            self.log_box.tkraise()
        else:
            self.view_bar.grid()
            self.view_switch.grid()
            self.archive_scan_btn.grid()
            self.archive_scan_help.grid()
            self.archive_scan_btn.configure(state="normal" if scdl_available() else "disabled")
            self.archive_scan_help.configure(state="normal")
        self._refresh_scdl_state()

    def _refresh_scdl_state(self):
        if self.active_service.get() == "bandcamp":
            if self.update_scdl_btn.winfo_ismapped():
                self.update_scdl_btn.pack_forget()
            for widget in self._bandcamp_controls:
                widget.configure(state="normal")
            self._toggle_bandcamp_schedule()
            self._toggle_bandcamp_archive()
            if not self._process or self._process.poll() is not None:
                self.stop_btn.configure(state="disabled")
            self._set_status("Ready")
            return

        installed = scdl_available()
        if installed:
            if self.install_scdl_btn.winfo_ismapped():
                self.install_scdl_btn.pack_forget()
            for widget in self._scdl_controls:
                widget.configure(state="normal")
            self._toggle_archive()
            self._toggle_schedule()
            if not self._process or self._process.poll() is not None:
                self.stop_btn.configure(state="disabled")
            self._set_status("Ready")
            self._refresh_update_button(self.update_scdl_btn, "Update scdl", self._scdl_update_available)
            return

        if self.update_scdl_btn.winfo_ismapped():
            self.update_scdl_btn.pack_forget()
        if not self.install_scdl_btn.winfo_ismapped():
            self.install_scdl_btn.pack(side="left", padx=(0, 18), pady=8)
        self.install_scdl_btn.configure(state="disabled" if self._scdl_installer_running else "normal")
        for widget in self._scdl_controls:
            widget.configure(state="disabled")
        self.stop_btn.configure(state="disabled")
        self._set_status("scdl is not installed")

    def _refresh_update_button(self, button: ctk.CTkButton, label: str, version: str | None):
        if version:
            button.configure(text=f"{label} ({version})")
            if not button.winfo_ismapped():
                button.pack(side="left", padx=(0, 18), pady=8)
            if not self._scdl_installer_running:
                button.configure(state="normal")
        elif button.winfo_ismapped():
            button.pack_forget()

    def _check_for_updates(self):
        def run():
            scdl_update = None if is_frozen_app() else check_scdl_update()
            self.after(0, lambda: self._on_update_check_done(scdl_update))

        threading.Thread(target=run, daemon=True).start()

    def _on_update_check_done(self, scdl_update: str | None):
        self._scdl_update_available = scdl_update
        if scdl_update:
            self._log(f"An scdl update is available: v{scdl_update}\n", "warn")
        self._refresh_scdl_state()

    def _install_scdl(self):
        if is_frozen_app():
            self._log("scdl is bundled into this app; no install is needed.\n", "ok")
            self._refresh_scdl_state()
            return

        if self._scdl_installer_running:
            return

        self._scdl_installer_running = True
        self.install_scdl_btn.configure(state="disabled", text="Installing...")
        if self.update_scdl_btn.winfo_ismapped():
            self.update_scdl_btn.configure(state="disabled")
        self._refresh_scdl_state()
        self._log("\nInstalling scdl with pip...\n")

        def run():
            scripts_dir = user_scripts_dir()
            try:
                os.makedirs(scripts_dir, exist_ok=True)
                cmd = [sys.executable, "-m", "pip", "install", "--user", "--upgrade", "scdl"]
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    encoding="utf-8",
                    errors="replace",
                    env=child_process_env(),
                )
                assert proc.stdout is not None
                for line in proc.stdout:
                    self.after(0, lambda l=line: self._log(l))
                proc.wait()

                if proc.returncode != 0:
                    self.after(0, lambda rc=proc.returncode: self._on_scdl_install_done(False, f"pip exited with code {rc}"))
                    return

                add_directory_to_user_path(scripts_dir)
                found = resolve_scdl_path()
                if not found:
                    self.after(0, lambda: self._on_scdl_install_done(False, "pip finished, but scdl was not found"))
                    return

                dry_run_scdl_setup(found)
                self.after(0, lambda path=found: self._on_scdl_install_done(True, path))
            except Exception as err:
                self.after(0, lambda e=err: self._on_scdl_install_done(False, str(e)))

        threading.Thread(target=run, daemon=True).start()

    def _on_scdl_install_done(self, ok: bool, detail: str):
        self._scdl_installer_running = False
        self.install_scdl_btn.configure(state="normal", text="Install scdl")
        if ok:
            save_scdl_path(detail)
            if self.install_scdl_btn.winfo_exists():
                self.install_scdl_btn.pack_forget()
            self._scdl_update_available = None
            self._log(f"scdl installed: {detail}\n", "ok")
            self._log(f"Added to PATH for this app and your user account: {user_scripts_dir()}\n", "ok")
        else:
            self._log(f"scdl install failed: {detail}\n", "error")
            messagebox.showerror("Install scdl", f"scdl could not be installed:\n{detail}")
        self._refresh_scdl_state()

    def _toggle_token(self):
        current = self.token_entry.cget("show")
        self.token_entry.configure(show="" if current == "•" else "•")

    def _toggle_archive(self):
        self.skip_existing.set(self.use_archive.get())
        state = "normal" if self.use_archive.get() else "disabled"
        self.archive_entry.configure(state=state)
        self.archive_browse_btn.configure(state=state)

    def _toggle_skip_existing(self):
        self.use_archive.set(self.skip_existing.get())
        self._toggle_archive()

    def _toggle_schedule(self):
        state = "normal" if self.enable_schedule.get() else "disabled"
        self.schedule_hour.configure(state=state)
        self.schedule_min.configure(state=state)
        self.schedule_btn.configure(state=state)

    def _toggle_bandcamp_schedule(self):
        state = "normal" if self.bandcamp_enable_schedule.get() else "disabled"
        self.bandcamp_schedule_hour.configure(state=state)
        self.bandcamp_schedule_min.configure(state=state)
        self.bandcamp_schedule_btn.configure(state=state)

    def _toggle_bandcamp_archive(self):
        state = "normal" if self.bandcamp_use_archive.get() else "disabled"
        self.bandcamp_archive_entry.configure(state=state)
        self.bandcamp_archive_browse_btn.configure(state=state)

    def _normalize_bandcamp_username(self, _event=None):
        value = self.bandcamp_username_entry.get().strip()
        match = re.match(r"^https?://(?:www\.)?bandcamp\.com/([^/?#]+)", value, re.IGNORECASE)
        if match:
            self._set_entry_value(self.bandcamp_username_entry, match.group(1))

    def _open_schedule_location(self):
        try:
            open_schedule_location()
        except Exception as err:
            messagebox.showerror("Schedule location", str(err))

    def _open_schedule_log_location(self):
        try:
            open_schedule_log_location()
        except Exception as err:
            messagebox.showerror("Schedule logs", str(err))

    def _make_tooltip_window(self, x: int, y: int) -> ctk.CTkToplevel:
        """Bare tooltip Toplevel, styled per idlelib.tooltip's proven cross-platform
        recipe: without the MacWindowStyle call, popups like this render fully blank
        on macOS (the OS never paints an overrideredirect window it also treats as a
        normal, activatable one); without update_idletasks()+lift() afterwards, the
        content can still fail to paint or can appear behind the main window."""
        tooltip = ctk.CTkToplevel(self)
        tooltip.overrideredirect(True)
        tooltip.geometry(f"+{max(x, 0)}+{y}")
        tooltip.attributes("-topmost", True)
        try:
            tooltip.tk.call("::tk::unsupported::MacWindowStyle", "style", tooltip._w, "help", "noActivates")
        except Exception:
            pass
        return tooltip

    def _finalize_tooltip_window(self, tooltip: ctk.CTkToplevel):
        tooltip.update_idletasks()
        tooltip.lift()

    def _pointer_over_tooltip(self, tooltip: ctk.CTkToplevel | None) -> bool:
        if tooltip is None or not tooltip.winfo_exists():
            return False
        try:
            px, py = tooltip.winfo_pointerxy()
        except Exception:
            return False
        if px < 0 or py < 0:
            return False
        x1 = tooltip.winfo_rootx()
        y1 = tooltip.winfo_rooty()
        return x1 <= px < x1 + tooltip.winfo_width() and y1 <= py < y1 + tooltip.winfo_height()

    def _show_auth_tooltip(self, _event=None):
        if self._auth_tooltip_hide_after is not None:
            self.after_cancel(self._auth_tooltip_hide_after)
            self._auth_tooltip_hide_after = None
        if self._auth_tooltip is not None:
            return

        x = self.auth_help.winfo_rootx() - 30
        y = self.auth_help.winfo_rooty() + 28
        tooltip = self._make_tooltip_window(x, y)
        label = ctk.CTkLabel(
            tooltip,
            text=(
                "Find your OAuth token by visiting SoundCloud after logging in and opening developer "
                "console (press F12) and going to the Storage tab. Then under cookies > soundcloud.com "
                "you can find the entry called oauth_token."
            ),
            wraplength=340,
            justify="left",
            fg_color=("gray95", "gray20"),
            text_color=("gray10", "gray95"),
            corner_radius=6,
            font=ctk.CTkFont(size=13),
        )
        label.pack(ipadx=10, ipady=7)
        tooltip.bind("<Enter>", self._show_auth_tooltip)
        tooltip.bind("<Leave>", self._hide_auth_tooltip)
        label.bind("<Enter>", self._show_auth_tooltip)
        label.bind("<Leave>", self._hide_auth_tooltip)
        self._finalize_tooltip_window(tooltip)
        self._auth_tooltip = tooltip

    def _hide_auth_tooltip(self, _event=None):
        if self._auth_tooltip_hide_after is not None:
            self.after_cancel(self._auth_tooltip_hide_after)
        self._auth_tooltip_hide_after = self.after(180, self._destroy_auth_tooltip)

    def _destroy_auth_tooltip(self):
        tooltip = self._auth_tooltip
        if self._pointer_over_tooltip(tooltip):
            # <Enter>/<Leave> on this borderless "help"-style popup don't fire
            # reliably on macOS, so re-check the actual pointer position rather
            # than trusting them -- otherwise hovering onto the tooltip (e.g. to
            # click a hyperlink inside it) gets it destroyed out from under the
            # cursor.
            self._auth_tooltip_hide_after = self.after(180, self._destroy_auth_tooltip)
            return
        self._auth_tooltip_hide_after = None
        if tooltip is not None and tooltip.winfo_exists():
            tooltip.destroy()
        self._auth_tooltip = None

    def _show_bandcamp_cookies_tooltip(self, _event=None):
        if self._bandcamp_cookies_tooltip_hide_after is not None:
            self.after_cancel(self._bandcamp_cookies_tooltip_hide_after)
            self._bandcamp_cookies_tooltip_hide_after = None
        if self._bandcamp_cookies_tooltip is not None:
            return

        x = self.bandcamp_cookies_help.winfo_rootx() - 30
        y = self.bandcamp_cookies_help.winfo_rooty() + 28
        tooltip = self._make_tooltip_window(x, y)

        frame = ctk.CTkFrame(tooltip, fg_color=("gray95", "gray20"), corner_radius=6)
        frame.pack()
        text_widget = Text(
            frame,
            wrap="word",
            width=54,
            height=1,
            borderwidth=0,
            highlightthickness=0,
            background="gray20",
            foreground="gray95",
            font=("TkDefaultFont", -13),
            cursor="arrow",
            padx=10,
            pady=7,
        )
        text_widget.insert("1.0", BANDCAMP_COOKIES_HELP_PREFIX)
        text_widget.insert("end", BANDCAMP_COOKIES_HELP_LINK_TEXT, "hyperlink")
        text_widget.insert("end", BANDCAMP_COOKIES_HELP_SUFFIX)
        text_widget.tag_config("hyperlink", foreground="#4DA6FF", underline=True)
        text_widget.tag_bind(
            "hyperlink", "<Button-1>", lambda _e: webbrowser.open(BANDCAMP_COOKIES_HELP_LINK_URL)
        )
        text_widget.tag_bind("hyperlink", "<Enter>", lambda _e: text_widget.configure(cursor="hand2"))
        text_widget.tag_bind("hyperlink", "<Leave>", lambda _e: text_widget.configure(cursor="arrow"))
        text_widget.configure(state="disabled")
        text_widget.pack()
        text_widget.update_idletasks()
        text_widget.configure(height=text_widget.count("1.0", "end", "displaylines")[0])

        tooltip.bind("<Enter>", self._show_bandcamp_cookies_tooltip)
        tooltip.bind("<Leave>", self._hide_bandcamp_cookies_tooltip)
        text_widget.bind("<Enter>", self._show_bandcamp_cookies_tooltip)
        text_widget.bind("<Leave>", self._hide_bandcamp_cookies_tooltip)
        self._finalize_tooltip_window(tooltip)
        self._bandcamp_cookies_tooltip = tooltip

    def _hide_bandcamp_cookies_tooltip(self, _event=None):
        if self._bandcamp_cookies_tooltip_hide_after is not None:
            self.after_cancel(self._bandcamp_cookies_tooltip_hide_after)
        self._bandcamp_cookies_tooltip_hide_after = self.after(180, self._destroy_bandcamp_cookies_tooltip)

    def _destroy_bandcamp_cookies_tooltip(self):
        tooltip = self._bandcamp_cookies_tooltip
        if self._pointer_over_tooltip(tooltip):
            self._bandcamp_cookies_tooltip_hide_after = self.after(180, self._destroy_bandcamp_cookies_tooltip)
            return
        self._bandcamp_cookies_tooltip_hide_after = None
        if tooltip is not None and tooltip.winfo_exists():
            tooltip.destroy()
        self._bandcamp_cookies_tooltip = None

    def _entry_value(self, entry: ctk.CTkEntry) -> str:
        return entry.get()

    def _set_entry_value(self, entry: ctk.CTkEntry, value: str, record_undo: bool = True):
        old_value = entry.get()
        if record_undo and old_value != value:
            self._push_undo(entry, old_value, value)

        state = entry.cget("state")
        if state == "disabled":
            entry.configure(state="normal")
        entry.delete(0, "end")
        entry.insert(0, value)
        if state == "disabled":
            entry.configure(state=state)

    def _push_undo(self, entry: ctk.CTkEntry, old_value: str, new_value: str):
        if old_value == new_value:
            return
        self._undo_stack.append((entry, old_value, new_value))
        if len(self._undo_stack) > 100:
            self._undo_stack.pop(0)

    def _enable_entry_undo(self, entry: ctk.CTkEntry):
        entry.bind("<KeyPress>", lambda event, e=entry: self._remember_entry_before_key(e, event), add="+")
        entry.bind("<KeyRelease>", lambda event, e=entry: self._record_entry_after_key(e, event), add="+")

    def _remember_entry_before_key(self, entry: ctk.CTkEntry, event):
        if event.keysym.lower() == "z" and (event.state & 0x4 or event.state & 0x8):
            return
        entry._scdlgui_before_key = entry.get()

    def _record_entry_after_key(self, entry: ctk.CTkEntry, event):
        if event.keysym in {"Shift_L", "Shift_R", "Control_L", "Control_R", "Command", "Meta_L", "Meta_R", "Alt_L", "Alt_R"}:
            return
        if event.keysym.lower() == "z" and (event.state & 0x4 or event.state & 0x8):
            return
        old_value = getattr(entry, "_scdlgui_before_key", entry.get())
        new_value = entry.get()
        self._push_undo(entry, old_value, new_value)

    def _undo_last_entry_change(self, _event=None):
        while self._undo_stack:
            entry, old_value, new_value = self._undo_stack.pop()
            if entry.winfo_exists() and entry.get() == new_value:
                self._set_entry_value(entry, old_value, record_undo=False)
                entry.focus_set()
                entry.icursor("end")
                return "break"
        return "break"

    def _switch_output_view(self, view: str):
        if view == "Archive Check":
            self.archive_box.tkraise()
        else:
            self.log_box.tkraise()

    def _show_scan_tooltip(self, _event=None):
        if self._scan_tooltip_hide_after is not None:
            self.after_cancel(self._scan_tooltip_hide_after)
            self._scan_tooltip_hide_after = None
        if self._scan_tooltip is not None:
            return

        x = self.archive_scan_help.winfo_rootx() - 340
        y = self.archive_scan_help.winfo_rooty() - 48
        tooltip = self._make_tooltip_window(x, y)
        label = ctk.CTkLabel(
            tooltip,
            text=(
                "Checks whether SoundCloud track IDs are still available online "
                "and reports which have been deleted or made private, along "
                "with any local copies you still have of them. "
            ),
            wraplength=340,
            justify="left",
            fg_color=("gray95", "gray20"),
            text_color=("gray10", "gray95"),
            corner_radius=6,
            font=ctk.CTkFont(size=13),
        )
        label.pack(ipadx=10, ipady=7)
        tooltip.bind("<Enter>", self._show_scan_tooltip)
        tooltip.bind("<Leave>", self._hide_scan_tooltip)
        label.bind("<Enter>", self._show_scan_tooltip)
        label.bind("<Leave>", self._hide_scan_tooltip)
        self._finalize_tooltip_window(tooltip)
        self._scan_tooltip = tooltip

    def _hide_scan_tooltip(self, _event=None):
        if self._scan_tooltip_hide_after is not None:
            self.after_cancel(self._scan_tooltip_hide_after)
        self._scan_tooltip_hide_after = self.after(180, self._destroy_scan_tooltip)

    def _destroy_scan_tooltip(self):
        tooltip = self._scan_tooltip
        if self._pointer_over_tooltip(tooltip):
            self._scan_tooltip_hide_after = self.after(180, self._destroy_scan_tooltip)
            return
        self._scan_tooltip_hide_after = None
        if tooltip is not None and tooltip.winfo_exists():
            tooltip.destroy()
        self._scan_tooltip = None

    def _pick_folder(self):
        system = platform.system()
        if system in ("Linux", "Windows"):
            self._pick_folder_with_optional_new(is_windows=(system == "Windows"))
            return

        folder = filedialog.askdirectory(title="Select or create download folder", mustexist=False)
        if folder:
            self._set_download_folder(folder)

    def _set_download_folder(self, folder: str):
        os.makedirs(folder, exist_ok=True)
        self._set_entry_value(self.path_entry, folder)

    def _pick_bandcamp_folder(self):
        system = platform.system()
        if system in ("Linux", "Windows"):
            self._pick_bandcamp_folder_with_optional_new(is_windows=(system == "Windows"))
            return

        folder = filedialog.askdirectory(title="Select or create Bandcamp download folder", mustexist=False)
        if folder:
            os.makedirs(folder, exist_ok=True)
            self._set_entry_value(self.bandcamp_path_entry, folder)

    def _pick_bandcamp_folder_with_optional_new(self, is_windows: bool = False):
        parent = filedialog.askdirectory(title="Select parent folder", mustexist=True)
        if not parent:
            return

        create_new = messagebox.askyesno(
            "Bandcamp download folder",
            "Create a new folder inside the selected folder?",
        )
        if not create_new:
            os.makedirs(parent, exist_ok=True)
            self._set_entry_value(self.bandcamp_path_entry, parent)
            return

        name = simpledialog.askstring("New folder", "Folder name:", parent=self)
        if not name:
            return

        invalid_chars = '<>:"/\\|?*' if is_windows else "/"
        if any(char in name for char in invalid_chars):
            messagebox.showerror("New folder", f"Folder names cannot contain: {invalid_chars}")
            return

        folder = os.path.join(parent, name.strip())
        if not folder or os.path.abspath(folder) == os.path.abspath(parent):
            return
        os.makedirs(folder, exist_ok=True)
        self._set_entry_value(self.bandcamp_path_entry, folder)

    def _pick_bandcamp_cookies(self):
        path = filedialog.asksaveasfilename(
            title="Create Bandcamp cookies file",
            defaultextension=".txt",
            filetypes=[("Cookies text files", "*.txt"), ("All files", "*.*")],
            initialfile="bandcamp-cookies.txt",
        )
        if path:
            if not os.path.exists(path):
                write_bandcamp_cookies_template(path)
                open_text_file_in_editor(path)
            self._set_entry_value(self.bandcamp_cookies_entry, path)
            return

        if messagebox.askyesno("Select existing cookies file?", "Select an existing cookies.txt file instead?"):
            path = filedialog.askopenfilename(
                title="Select Netscape cookies.txt file",
                filetypes=[("Cookies text files", "*.txt"), ("All files", "*.*")],
                initialfile="bandcamp-cookies.txt",
            )
            if path:
                self._set_entry_value(self.bandcamp_cookies_entry, path)

    def _pick_folder_with_optional_new(self, is_windows: bool = False):
        parent = filedialog.askdirectory(title="Select parent folder", mustexist=True)
        if not parent:
            return

        create_new = messagebox.askyesno(
            "Download folder",
            "Create a new folder inside the selected folder?",
        )
        if not create_new:
            self._set_download_folder(parent)
            return

        name = simpledialog.askstring("New folder", "Folder name:", parent=self)
        if not name:
            return

        invalid_chars = '<>:"/\\|?*' if is_windows else "/"
        if any(char in name for char in invalid_chars):
            messagebox.showerror("New folder", f"Folder names cannot contain: {invalid_chars}")
            return

        folder = os.path.join(parent, name.strip())
        if not folder or os.path.abspath(folder) == os.path.abspath(parent):
            return
        self._set_download_folder(folder)

    def _pick_archive(self):
        path = filedialog.asksaveasfilename(
            title="Create archive file",
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            initialfile="sc-archive.txt",
        )
        if not path and messagebox.askyesno("Select existing archive file?", "Select an existing archive.txt file instead?"):
            path = filedialog.askopenfilename(
                title="Select archive file",
                filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
                initialfile="sc-archive.txt",
            )
        if path:
            if not os.path.exists(path):
                open(path, "w").close()
            self._set_entry_value(self.archive_entry, path)

    def _pick_bandcamp_archive(self):
        path = filedialog.asksaveasfilename(
            title="Create archive file",
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            initialfile="bc-archive.txt",
        )
        if not path and messagebox.askyesno("Select existing archive file?", "Select an existing archive.txt file instead?"):
            path = filedialog.askopenfilename(
                title="Select archive file",
                filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
                initialfile="bc-archive.txt",
            )
        if path:
            if not os.path.exists(path):
                open(path, "w").close()
            self._set_entry_value(self.bandcamp_archive_entry, path)

    def _log(self, text: str, kind: str = "info"):
        colors = {"warn": "orange", "error": "red", "ok": "lightgreen"}
        color = colors.get(kind)
        stick_to_bottom = self.log_box.yview()[1] >= 0.999
        self.log_box.configure(state="normal")
        if color:
            try:
                self.log_box.tag_config(kind, foreground=color)
                self.log_box.insert("end", text, (kind,))
            except Exception:
                self.log_box.insert("end", text)
        else:
            self.log_box.insert("end", text)
        if stick_to_bottom:
            self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _set_archive_text(self, text: str):
        self.archive_box.configure(state="normal")
        self.archive_box.delete("1.0", "end")
        self.archive_box.insert("end", text)
        self.archive_box.see("end")
        self.archive_box.configure(state="disabled")

    def _append_archive_text(self, text: str):
        stick_to_bottom = self.archive_box.yview()[1] >= 0.999
        self.archive_box.configure(state="normal")
        self.archive_box.insert("end", text)
        if stick_to_bottom:
            self.archive_box.see("end")
        self.archive_box.configure(state="disabled")

    def _set_status(self, text: str):
        self.status_label.configure(text=text)

    # ── Build command ─────────────────────────────────────────────────────────
    def _build_cmd(self) -> list[str] | None:
        if self.active_service.get() == "bandcamp":
            self._normalize_bandcamp_username()
            if not self.bandcamp_username_entry.get().strip():
                messagebox.showerror("Missing username", "Please enter a Bandcamp username.")
                return None
            if not self.bandcamp_cookies_entry.get().strip():
                messagebox.showerror("Missing cookies", "Please choose a Netscape cookies.txt file.")
                return None
            try:
                return build_bandcamp_cmd(self._current_profile())
            except (ValueError, FileNotFoundError) as err:
                messagebox.showerror("Bandcamp download", str(err))
                return None

        url = self.url_entry.get().strip()
        if not url:
            messagebox.showerror("Missing URL", "Please enter a SoundCloud URL.")
            return None

        try:
            return build_scdl_cmd(self._current_profile())
        except ValueError as err:
            messagebox.showerror("Missing archive path", str(err))
            return None

    def _current_profile(self) -> dict:
        return {
            "service": self.active_service.get(),
            "url": self.url_entry.get(),
            "path": self.path_entry.get(),
            "token": self.token_entry.get(),
            "dl_type": self.dl_type.get(),
            "skip_existing": self.skip_existing.get(),
            "only_mp3": self.only_mp3.get(),
            "flac": self.flac.get(),
            "opus": self.opus.get(),
            "original": self.original.get(),
            "original_art": self.original_art.get(),
            "use_archive": self.use_archive.get(),
            "archive_path": self.archive_entry.get(),
            "scdl_path": resolve_scdl_path() or "",
            "bandcamp_username": self.bandcamp_username_entry.get(),
            "bandcamp_path_to": self.bandcamp_path_entry.get(),
            "bandcamp_cookies": self.bandcamp_cookies_entry.get(),
            "bandcamp_format": self.bandcamp_format.get(),
            "bandcamp_parallel_downloads": self.bandcamp_parallel_entry.get(),
            "bandcamp_wait_after_download": self.bandcamp_wait_entry.get(),
            "bandcamp_max_download_attempts": self.bandcamp_attempts_entry.get(),
            "bandcamp_retry_wait": self.bandcamp_retry_wait_entry.get(),
            "bandcamp_download_since": self.bandcamp_since_entry.get(),
            "bandcamp_download_until": self.bandcamp_until_entry.get(),
            "bandcamp_use_archive": self.bandcamp_use_archive.get(),
            "bandcamp_archive_path": self.bandcamp_archive_entry.get(),
            "bandcamp_include_hidden": self.bandcamp_include_hidden.get(),
            "bandcamp_extract": self.bandcamp_extract.get(),
            "bandcamp_summary": self.bandcamp_summary.get(),
            "bandcamp_dry_run": self.bandcamp_dry_run.get(),
            "bandcamp_verbose": self.bandcamp_verbose.get(),
            "bandcamp_enable_schedule": self.bandcamp_enable_schedule.get(),
            "bandcamp_schedule_hour": self.bandcamp_schedule_hour.get(),
            "bandcamp_schedule_min": self.bandcamp_schedule_min.get(),
        }

    def _start_archive_scan(self):
        if not scdl_available():
            messagebox.showerror("scdl not installed", "Install scdl before scanning the archive.")
            self._refresh_scdl_state()
            return

        self._save_current_values()

        audio_dir = audio_base_path(self._current_profile())
        archive = os.path.expanduser(self.archive_entry.get().strip()) if self.use_archive.get() else ""
        using_archive = bool(archive)

        token = ""
        self.view_switch.set("Archive Check")
        self._switch_output_view("Archive Check")
        self.archive_scan_btn.configure(state="disabled")
        self.dl_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self._archive_scan_stopping = False
        self._archive_scan_active = True
        self._set_status("Scanning tagged audio…")
        self._set_archive_text(f"Scanning tagged audio files in {audio_dir}…\n")

        def run():
            # The tag scan reads every audio file under the download folder
            # and can take minutes on a large library, so it runs here rather
            # than freezing the UI thread.
            tagged_mp3s, audio_count, dir_count, tag_error = scan_audio_tags(audio_dir)
            if self._archive_scan_stopping:
                self.after(0, lambda: self._append_archive_text("\nStopped archive scan.\n"))
                self.after(0, lambda: self._finish_archive_scan_ui("Archive scan stopped"))
                return

            if using_archive:
                track_ids = read_archive_track_ids(archive)
            else:
                track_ids = list(tagged_mp3s.keys())

            if not track_ids:
                if using_archive:
                    message = "No SoundCloud track ids were found in the archive.\n"
                elif tag_error:
                    message = f"Audio tag scan failed: {tag_error}\n"
                else:
                    message = (
                        "No SoundCloud-tagged audio files were found to check. Enable an archive file, "
                        "or download tracks through this app first so they get tagged.\n"
                    )
                self.after(0, lambda m=message: self._set_archive_text(m))
                self.after(0, lambda: self._finish_archive_scan_ui("Ready"))
                return

            source_desc = "archived SoundCloud track(s)" if using_archive else "locally tagged SoundCloud track(s)"
            intro = (
                f"Scanning {len(track_ids)} {source_desc}…\n"
                f"Scanning tagged audio files in {audio_dir}\n"
                "Checking public SoundCloud availability without the auth token.\n"
                f"Checking up to {min(8, max(1, len(track_ids)))} tracks at a time.\n"
                "Deleted, private, or otherwise inaccessible tracks will appear below.\n\n"
            )
            self.after(0, lambda text=intro: self._set_archive_text(text))
            if tag_error:
                self.after(0, lambda: self._append_archive_text(f"Audio tag scan skipped: {tag_error}\n\n"))
            elif audio_count == 0:
                self.after(0, lambda: self._append_archive_text(
                    f"Error: no audio files were found in the archive directory or its playlist subfolders. "
                    f"Scanned {dir_count} folder(s).\n\n"
                ))
            elif not tagged_mp3s:
                self.after(0, lambda: self._append_archive_text(
                    "Error: no SoundCloud ID tags were found in audio files. "
                    "There are no audio files that were downloaded in this wrapper, "
                    f"or the files have not been tagged yet. Scanned {audio_count} audio file(s) "
                    f"in {dir_count} folder(s).\n\n"
                ))
            else:
                self.after(0, lambda: self._append_archive_text(
                    f"Found {audio_count} audio file(s) and {len(tagged_mp3s)} tagged SoundCloud ID(s) "
                    f"in {dir_count} folder(s).\n\n"
                ))
            self.after(0, lambda: self._set_status("Scanning archive…"))

            python = scdl_python_available()
            if not python:
                self.after(0, lambda: self._append_archive_text("scdl was not found; archive scan cannot run.\n"))
                self.after(0, lambda: self._on_archive_scan_done([], 0, stopped=False, tagged_mp3s=tagged_mp3s))
                return

            missing: list[tuple[str, str]] = []
            scanned = 0
            try:
                workers = min(8, max(1, len(track_ids)))
                helper_cmd = (
                    [sys.executable, "--archive-scan-helper", token, str(workers)]
                    if is_frozen_app()
                    else [python, "-c", ARCHIVE_SCAN_HELPER, token, str(workers)]
                )
                self._archive_scan_process = subprocess.Popen(
                    helper_cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    # stderr is never read; PIPE could deadlock the child if
                    # it filled the buffer.
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                    encoding="utf-8",
                    errors="replace",
                    env=child_process_env(),
                )
                assert self._archive_scan_process.stdin is not None
                assert self._archive_scan_process.stdout is not None

                self._archive_scan_process.stdin.write(json.dumps(track_ids))
                self._archive_scan_process.stdin.close()

                for line in self._archive_scan_process.stdout:
                    if self._archive_scan_stopping:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        result = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    scanned += 1
                    track_id = str(result.get("id") or "")
                    if not result.get("ok"):
                        error = result.get("error") or "Unavailable"
                        missing.append((track_id, error))
                        self.after(
                            0,
                            lambda tid=track_id, err=error: self._append_archive_text(f"{tid}  {err}\n"),
                        )

                    if scanned % 10 == 0 or scanned == len(track_ids):
                        self.after(0, lambda i=scanned: self._set_status(f"Scanned {i}/{len(track_ids)}"))

                if self._archive_scan_stopping and self._archive_scan_process.poll() is None:
                    self._archive_scan_process.terminate()
                self._archive_scan_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if self._archive_scan_process and self._archive_scan_process.poll() is None:
                    self._archive_scan_process.kill()
            except Exception as err:
                self.after(0, lambda e=err: self._append_archive_text(f"Archive scan error: {e}\n"))
            finally:
                stopped = self._archive_scan_stopping
                self._archive_scan_process = None
                self.after(
                    0,
                    lambda items=list(missing), done=scanned, stopped=stopped: self._on_archive_scan_done(
                        items,
                        done,
                        stopped,
                        tagged_mp3s,
                    )
                )

        threading.Thread(target=run, daemon=True).start()

    def _on_archive_scan_done(
        self,
        missing_items: list[tuple[str, str]],
        scanned_count: int,
        stopped: bool,
        tagged_mp3s: dict[str, list[str]],
    ):
        missing_ids = [track_id for track_id, _ in missing_items]
        missing_count = len(missing_ids)

        if stopped:
            self._append_archive_text(
                f"\nStopped archive scan. Found {missing_count} unavailable archived track(s) "
                f"before stopping. Scanned {scanned_count} total.\n"
            )
            self._set_status("Archive scan stopped")
        elif missing_count:
            self._append_archive_text(
                f"\nFound {missing_count} unavailable archived track(s). "
                f"Scanned {scanned_count} total.\n"
            )
            self._set_status(f"Archive scan done  ✓  {datetime.now().strftime('%H:%M:%S')}")
        else:
            self._append_archive_text(
                f"No deleted/private archived tracks found. Scanned {scanned_count} total.\n"
            )
            self._set_status(f"Archive scan done  ✓  {datetime.now().strftime('%H:%M:%S')}")

        if missing_ids:
            self._append_archive_text("\nUnavailable track IDs:\n")
            self._append_archive_text("\n".join(missing_ids) + "\n")

            self._append_archive_text("\nTagged local audio matches:\n")
            any_matches = False
            for track_id in missing_ids:
                matches = tagged_mp3s.get(track_id, [])
                if matches:
                    any_matches = True
                    self._append_archive_text(f"{track_id}\n")
                    for path in matches:
                        self._append_archive_text(f"  {path}\n")
            if not any_matches:
                self._append_archive_text("No tagged audio files matched those unavailable IDs.\n")

        self._finish_archive_scan_ui()

    def _finish_archive_scan_ui(self, status: str | None = None):
        self._archive_scan_active = False
        if scdl_available():
            self.archive_scan_btn.configure(state="normal")
            self.dl_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self._archive_scan_stopping = False
        if status is not None:
            self._set_status(status)

    # ── Download ──────────────────────────────────────────────────────────────
    def _start_download(self):
        if self.active_service.get() != "bandcamp" and not scdl_available():
            messagebox.showerror("scdl not installed", "Install scdl before downloading.")
            self._refresh_scdl_state()
            return

        cmd = self._build_cmd()
        if not cmd:
            return

        profile = self._current_profile()
        preflight_archive = profile.get("service") != "bandcamp" and profile.get("use_archive")
        self._active_download_profile = dict(profile)
        self._download_stopping = False

        self._save_current_values()
        self.dl_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self._set_status("Checking archive…" if preflight_archive else "Downloading…")

        def run():
            try:
                if profile.get("service") != "bandcamp":
                    ensure_scdl_config_file()
                    run_cmd = cmd
                    finalize = None
                    if preflight_archive:
                        self.after(0, lambda: self._log("\nChecking archive before download…\n"))
                        try:
                            run_cmd, note, finalize = prepare_download_cmd(profile)
                        except Exception as err:
                            self.after(0, lambda e=err: self._log(f"Archive preflight failed ({e}); running full download.\n", "warn"))
                            run_cmd = cmd
                            note = None
                            finalize = None
                        if note:
                            self.after(0, lambda n=note: self._log(f"{n}\n", "ok" if run_cmd is None else "info"))
                        if run_cmd is None:
                            self.after(0, lambda: self._on_done(0))
                            return

                    self.after(0, lambda: self._set_status("Downloading…"))
                    self.after(0, lambda c=run_cmd: self._log(f"\n{'─'*60}\n▶  {' '.join(redact_cmd(c))}\n{'─'*60}\n"))
                    self._process = subprocess.Popen(
                        run_cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1,
                        encoding="utf-8",
                        errors="replace",
                        env=child_process_env(),
                    )
                    assert self._process.stdout is not None
                    for line in self._process.stdout:
                        self.after(0, lambda l=line: self._log(l))
                    self._process.wait()
                    rc = -2 if self._download_stopping else self._process.returncode
                    if finalize:
                        try:
                            finalize()
                        except Exception as merge_err:
                            self.after(0, lambda e=merge_err: self._log(f"⚠  Archive session merge failed: {e}\n", "warn"))
                    changed, error = postprocess_downloaded_mp3s(profile)
                    if error:
                        self.after(0, lambda e=error: self._log(f"⚠  Audio tag/rename skipped: {e}\n", "warn"))
                    elif changed:
                        self.after(
                            0,
                            lambda count=len(changed): self._log(
                                f"Tagged SoundCloud IDs and removed filename IDs from {count} audio file(s).\n",
                                "ok",
                            ),
                        )
                    self.after(0, lambda: self._on_done(rc))
                    return

                # ── Bandcamp ──
                archive_ids = bandcamp_archive_ids_for_profile(profile)
                archive_total = bandcamp_archive_total_for_profile(profile)
                if archive_ids:
                    self.after(
                        0,
                        lambda n=len(archive_ids): self._log(
                            f"\nArchive has {n} album(s) on record; syncing sequentially and stopping "
                            "once a previously archived album comes up.\n"
                        ),
                    )
                self.after(0, lambda: self._set_status("Downloading…"))
                self.after(0, lambda c=cmd: self._log(f"\n{'─'*60}\n▶  {' '.join(redact_cmd(c))}\n{'─'*60}\n"))

                def on_line(line):
                    self.after(0, lambda l=line: self._log(l))

                def on_start(process):
                    self._process = process

                def on_archive_check(item_id, in_archive):
                    status = "already archived" if in_archive else "new"
                    self.after(
                        0,
                        lambda: self._log(f"   [archive check] {item_id}: {status}\n"),
                    )

                def on_archive_continue(item_id, session_new_count, archived_total, collection_total):
                    self.after(
                        0,
                        lambda: self._log(
                            f"   [archive check] {item_id} is already archived, but only "
                            f"{session_new_count + archived_total}/{collection_total} album(s) are "
                            "accounted for so far (likely a re-shuffled duplicate, e.g. from a "
                            "discography purchase) -- continuing past it.\n"
                        ),
                    )

                def on_archive_reprocess(item_id):
                    self.after(
                        0,
                        lambda: self._log(
                            f"   [archive check] {item_id} is archived but has no files on disk "
                            "backing it -- letting the fresh download finish so it can self-heal.\n"
                        ),
                    )

                returncode, stopped_early = run_bandcamp_download(
                    cmd, archive_ids, archive_total, bool(profile.get("bandcamp_extract")),
                    on_line=on_line, on_start=on_start,
                    on_archive_check=on_archive_check, on_archive_continue=on_archive_continue,
                    on_archive_reprocess=on_archive_reprocess,
                )
                if stopped_early:
                    self._download_stopping = True
                    self.after(
                        0,
                        lambda: self._log(
                            "\n⏹  Reached a previously archived album; stopping sync early.\n", "warn"
                        ),
                    )
                rc = -2 if self._download_stopping else returncode

                if profile.get("bandcamp_dry_run"):
                    # A dry run must not touch the filesystem: no extraction,
                    # no zip deletion, no archive updates.
                    self.after(0, lambda: self._log("Dry run: skipped zip post-processing and archive updates.\n"))
                else:
                    confirmed, extracted_count, removed, error = process_bandcamp_downloads(
                        bandcamp_base_path(profile), bool(profile.get("bandcamp_extract")), archive_ids
                    )
                    if error:
                        self.after(0, lambda e=error: self._log(f"⚠  Zip processing failed: {e}\n", "warn"))
                    if removed:
                        self.after(
                            0,
                            lambda count=len(removed): self._log(
                                f"Removed {count} zip/partial file(s) that were corrupted, incomplete, or already archived.\n",
                                "warn",
                            ),
                        )
                    if extracted_count:
                        self.after(
                            0,
                            lambda count=extracted_count: self._log(
                                f"Extracted and removed {count} zip file(s).\n", "ok"
                            ),
                        )
                    if profile.get("bandcamp_use_archive") and confirmed:
                        archive_path = os.path.expanduser(profile.get("bandcamp_archive_path", "").strip())
                        if archive_path:
                            added = append_bandcamp_archive(archive_path, confirmed)
                            if added:
                                self.after(
                                    0,
                                    lambda count=added: self._log(
                                        f"Archived {count} newly confirmed album(s).\n", "ok"
                                    ),
                                )
                self.after(0, lambda: self._on_done(rc))
            except FileNotFoundError:
                self.after(0, lambda: self._log("Downloader not found. Install the selected downloader first.\n", "error"))
                self.after(0, lambda: self._on_done(-1))
            except Exception as err:
                self.after(0, lambda e=err: self._log(f"Download failed: {e}\n", "error"))
                self.after(0, lambda: self._on_done(1))
            finally:
                self._process = None

        threading.Thread(target=run, daemon=True).start()

    def _stop(self):
        if self._archive_scan_active:
            self._archive_scan_stopping = True
            if self._archive_scan_process and self._archive_scan_process.poll() is None:
                terminate_process_tree(self._archive_scan_process)
            self._append_archive_text("\nStopping archive scan…\n")
            self._set_status("Stopping archive scan…")
            return

        if self._process and self._process.poll() is None:
            self._download_stopping = True
            terminate_process_tree(self._process)
            self._log("\n⏹  Stopping after current cleanup…\n", "warn")
            self._set_status("Stopping…")
            return
        self._on_done(-2)

    def _on_done(self, returncode: int):
        self.stop_btn.configure(state="disabled")
        if self.active_service.get() == "bandcamp" or scdl_available():
            self.dl_btn.configure(state="normal")
        if returncode == 0:
            self._set_status(f"Done  ✓  {datetime.now().strftime('%H:%M:%S')}")
            self._log("✅  Finished successfully.\n", "ok")
        elif returncode == -2:
            self._set_status("Stopped")
            self._log("Stopped. Cleanup finished.\n", "warn")
        else:
            self._set_status(f"Finished with errors (code {returncode})")
        self._download_stopping = False
        self._active_download_profile = None

    # ── Schedule registration ─────────────────────────────────────────────────
    def _register_schedule(self):
        if self.active_service.get() != "bandcamp" and not scdl_available():
            messagebox.showerror("scdl not installed", "Install scdl before registering a schedule.")
            self._refresh_scdl_state()
            return

        cmd = self._build_cmd()
        if not cmd:
            return

        try:
            if self.active_service.get() == "bandcamp":
                hour = int(self.bandcamp_schedule_hour.get())
                minute = int(self.bandcamp_schedule_min.get())
            else:
                hour = int(self.schedule_hour.get())
                minute = int(self.schedule_min.get())
            profile = self._current_profile()
            self._save_current_values(profile)
            sid = schedule_id(profile, hour, minute)
            scheduled_cmd = build_scheduled_command(profile, sid)
            prefix = "bandcampdl" if profile.get("service") == "bandcamp" else "scdl"
            system = platform.system()

            if system == "Darwin":
                loc = register_schedule_macos(hour, minute, scheduled_cmd, sid, prefix)
                msg = f"Registered launchd agent:\n{loc}"
            elif system == "Windows":
                loc = register_schedule_windows(hour, minute, scheduled_cmd, sid, prefix)
                msg = f"Registered Windows Task Scheduler task:\n{loc}"
            else:
                loc = register_schedule_linux(hour, minute, scheduled_cmd, sid, prefix)
                msg = f"Registered cron job:\n{loc}"

            messagebox.showinfo("Schedule registered", msg)
            self._log(f"\n🕐  Daily schedule set for {hour:02d}:{minute:02d}.\n", "ok")
        except Exception as e:
            messagebox.showerror("Schedule error", str(e))

    # ── Persist settings ──────────────────────────────────────────────────────
    def _save_current_values(self, profile: dict | None = None):
        save_config(profile or self._current_profile())

    def _on_close(self):
        try:
            self._save_current_values()
        finally:
            # Don't orphan a running downloader/scan: it would keep working
            # with no post-processing or archive update behind it.
            for process in (self._process, self._archive_scan_process):
                if process and process.poll() is None:
                    terminate_process_tree(process)
            self.destroy()

    def _load_saved_values(self):
        c = self._config
        if not c:
            return
        if c.get("url"):
            self._set_entry_value(self.url_entry, c["url"], record_undo=False)
        if c.get("path"):
            self._set_entry_value(self.path_entry, c["path"], record_undo=False)
        if c.get("token"):
            self._set_entry_value(self.token_entry, c["token"], record_undo=False)
        if "dl_type" in c:
            self.dl_type.set(c["dl_type"])
        if "use_archive" in c:
            self.use_archive.set(c["use_archive"])
            self.skip_existing.set(c["use_archive"])
        elif "skip_existing" in c:
            self.skip_existing.set(c["skip_existing"])
            self.use_archive.set(c["skip_existing"])
        if "only_mp3" in c:
            self.only_mp3.set(c["only_mp3"])
        if "flac" in c:
            self.flac.set(c["flac"])
        if "opus" in c:
            self.opus.set(c["opus"])
        if "original" in c:
            self.original.set(c["original"])
        if "original_art" in c:
            self.original_art.set(c["original_art"])
        self._toggle_archive()
        if c.get("use_archive") and c.get("archive_path"):
            self._set_entry_value(self.archive_entry, c.get("archive_path", ""), record_undo=False)
        if c.get("bandcamp_username"):
            self._set_entry_value(self.bandcamp_username_entry, c["bandcamp_username"], record_undo=False)
        if c.get("bandcamp_path_to"):
            self._set_entry_value(self.bandcamp_path_entry, c["bandcamp_path_to"], record_undo=False)
        if c.get("bandcamp_cookies"):
            self._set_entry_value(self.bandcamp_cookies_entry, c["bandcamp_cookies"], record_undo=False)
        if c.get("bandcamp_format") in BANDCAMP_FORMATS:
            self.bandcamp_format.set(c["bandcamp_format"])
        for key, entry, default in (
            ("bandcamp_parallel_downloads", self.bandcamp_parallel_entry, "5"),
            ("bandcamp_wait_after_download", self.bandcamp_wait_entry, "1"),
            ("bandcamp_max_download_attempts", self.bandcamp_attempts_entry, "5"),
            ("bandcamp_retry_wait", self.bandcamp_retry_wait_entry, "5"),
        ):
            self._set_entry_value(entry, str(c.get(key, default)), record_undo=False)
        if c.get("bandcamp_download_since"):
            self._set_entry_value(self.bandcamp_since_entry, c["bandcamp_download_since"], record_undo=False)
        if c.get("bandcamp_download_until"):
            self._set_entry_value(self.bandcamp_until_entry, c["bandcamp_download_until"], record_undo=False)
        if "bandcamp_use_archive" in c:
            self.bandcamp_use_archive.set(c["bandcamp_use_archive"])
        if c.get("bandcamp_archive_path"):
            self._set_entry_value(self.bandcamp_archive_entry, c["bandcamp_archive_path"], record_undo=False)
        self._toggle_bandcamp_archive()
        for key, var in (
            ("bandcamp_include_hidden", self.bandcamp_include_hidden),
            ("bandcamp_extract", self.bandcamp_extract),
            ("bandcamp_summary", self.bandcamp_summary),
            ("bandcamp_dry_run", self.bandcamp_dry_run),
            ("bandcamp_verbose", self.bandcamp_verbose),
        ):
            if key in c:
                var.set(c[key])
        if "bandcamp_enable_schedule" in c:
            self.bandcamp_enable_schedule.set(c["bandcamp_enable_schedule"])
        if c.get("bandcamp_schedule_hour"):
            self.bandcamp_schedule_hour.set(str(c["bandcamp_schedule_hour"]))
        if c.get("bandcamp_schedule_min"):
            self.bandcamp_schedule_min.set(str(c["bandcamp_schedule_min"]))
        self._toggle_bandcamp_schedule()
        self._switch_service(c.get("service", "soundcloud"))


# ── Entry point ───────────────────────────────────────────────────────────────
def run_embedded_scdl(args: list[str]) -> int:
    reconfigure_embedded_std_streams()
    import yt_dlp

    sys.modules.setdefault("yt_dlp.__init__", yt_dlp)
    setattr(yt_dlp, "__init__", yt_dlp)
    from scdl.scdl import _main

    old_argv = sys.argv[:]
    try:
        sys.argv = ["scdl", *args]
        result = _main()
        return int(result or 0)
    except SystemExit as err:
        # sys.exit() with no argument (e.g. docopt's --version/--help
        # handling in scdl) means success: code is None, not 0.
        if err.code is None:
            return 0
        return err.code if isinstance(err.code, int) else 1
    except Exception:
        # Return a clean nonzero exit instead of letting the frozen
        # bootloader report an unhandled-exception error on top of it.
        import traceback
        traceback.print_exc()
        return 1
    finally:
        sys.argv = old_argv


def run_embedded_bandcamp(args: list[str]) -> int:
    reconfigure_embedded_std_streams()
    import vendor_bandcamp_downloader as bandcamp_downloader

    unsafe_path_segments = {"", ".", ".."}
    original_sanitize_value = bandcamp_downloader.sanitize_value

    def safe_sanitize_value(value):
        value = original_sanitize_value(value)
        if isinstance(value, str):
            # An artist/title of "" (blank), "." or ".." is a legal Bandcamp
            # display name but a dangerous path segment: since it's used
            # standalone as the artist folder in our --filename-format, ".."
            # makes the tool write the file into the *parent* of the download
            # directory instead of inside it.
            if value in unsafe_path_segments:
                value = "Unsorted"
            elif len(value) > BANDCAMP_MAX_NAME_PART_LEN:
                value = value[:BANDCAMP_MAX_NAME_PART_LEN].rstrip()
        return value

    bandcamp_downloader.sanitize_value = safe_sanitize_value

    old_argv = sys.argv[:]
    try:
        sys.argv = ["bandcamp-downloader", *args]
        result = bandcamp_downloader.main()
        return int(result or 0)
    except SystemExit as err:
        # sys.exit() with no argument (e.g. docopt's --version/--help
        # handling in scdl) means success: code is None, not 0.
        if err.code is None:
            return 0
        return err.code if isinstance(err.code, int) else 1
    except Exception:
        # A hard failure (e.g. the network dying after all retries) should
        # exit cleanly with the traceback in the log, not crash the frozen
        # bootloader with an unhandled-exception error.
        import traceback
        traceback.print_exc()
        print("ERROR: bandcamp-downloader run failed; see the traceback above.", flush=True)
        return 1
    finally:
        sys.argv = old_argv


def run_inline_helper(source: str, args: list[str]) -> int:
    reconfigure_embedded_std_streams()
    old_argv = sys.argv[:]
    namespace = {"__name__": "__main__"}
    try:
        sys.argv = ["helper", *args]
        exec(source, namespace)
        return 0
    except SystemExit as err:
        # sys.exit() with no argument (e.g. docopt's --version/--help
        # handling in scdl) means success: code is None, not 0.
        if err.code is None:
            return 0
        return err.code if isinstance(err.code, int) else 1
    except Exception:
        import traceback
        traceback.print_exc()
        return 1
    finally:
        sys.argv = old_argv


if __name__ == "__main__":
    import multiprocessing

    # In the frozen app a multiprocessing worker re-executes this same
    # binary; without this guard it would fall through the argv checks below
    # and open another GUI window instead of running as a worker.
    multiprocessing.freeze_support()

    if "--run-scdl" in sys.argv:
        index = sys.argv.index("--run-scdl")
        raise SystemExit(run_embedded_scdl(sys.argv[index + 1:]))
    if "--run-bandcamp" in sys.argv:
        index = sys.argv.index("--run-bandcamp")
        raise SystemExit(run_embedded_bandcamp(sys.argv[index + 1:]))
    if "--mp3-tag-helper" in sys.argv:
        index = sys.argv.index("--mp3-tag-helper")
        raise SystemExit(run_inline_helper(MP3_TAG_HELPER, sys.argv[index + 1:]))
    if "--archive-scan-helper" in sys.argv:
        index = sys.argv.index("--archive-scan-helper")
        raise SystemExit(run_inline_helper(ARCHIVE_SCAN_HELPER, sys.argv[index + 1:]))

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--scheduled-download")
    parser.add_argument("--scheduled-download-file")
    args, _ = parser.parse_known_args()
    if args.scheduled_download:
        raise SystemExit(run_scheduled_download_with_log(args.scheduled_download))
    if args.scheduled_download_file:
        raise SystemExit(run_scheduled_download_file_with_log(args.scheduled_download_file))

    app = ScdlApp()
    app.mainloop()
