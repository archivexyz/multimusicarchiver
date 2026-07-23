# Multi Music Archiver

A GUI that wraps [scdl](https://github.com/scdl-org/scdl) (SoundCloud) and
[bandcamp-downloader](https://github.com/easlice/bandcamp-downloader) (Bandcamp) for archiving your
library from either service, with optional daily scheduling and deleted track checking.

## ⚠️ Safety Notice
I am not a developer and almost all of the code in this program is written by an LLM. Accordingly, 
this program can only delete / overwrite files that itself downloaded or are identical to files it
is downloading (exact name, layout, and Bandcamp id in its filename). If you want to be *absolutely certain* of no data
loss, point the save directories to fresh folders.

## Download

Standalone builds: no Python or `pip install` required.

<table>
<tr>
<td align="center" width="33%">

### macOS

[**Download**](https://github.com/archivexyz/multimusicarchiver/releases/latest/download/MultiMusicArchiver-macos.zip)

</td>
<td align="center" width="33%">

### Windows

[**Download**](https://github.com/archivexyz/multimusicarchiver/releases/latest/download/MultiMusicArchiver-windows.zip)

</td>
<td align="center" width="33%">

### Linux

[**Download**](https://github.com/archivexyz/multimusicarchiver/releases/latest/download/MultiMusicArchiver-linux.zip)

</td>
</tr>
</table>

Builds are unsigned! MacOS will require right-click → Open the first launch (Gatekeeper), and
Windows will show a SmartScreen warning to click through.

## Requirements

```bash
pip install -r requirements.txt
```

## Running

```bash
python source/multimusicarchiver.py
```
## SoundCloud (scdl)

**Download type**
- Single track / playlist URL — `-l`: "URL can be track/playlist/user"
- All uploads (no reposts) — `-t`: "Download all uploads of a user (no reposts)"
- All + reposts — `-a`: "Download all tracks of user (including reposts)"
- Likes / favorites — `-f`: "Download all favorites (likes) of a user"
- All playlists — `-p`: "Download all playlists of a user"

**Format & quality**
- MP3 only — `--onlymp3`: "Download only mp3 files"
- FLAC (lossless only) — `--flac`: "Convert original files to .flac. Only works if the original
  file is lossless quality"
- Prefer Opus — `--opus`: "Prefer downloading opus streams over mp3 streams"
- Only Original Files — `--only-original`: "Only download songs with original file available"
- Original artwork — `--original-art`: "Download original cover art, not just 500x500 JPEG"

**Archive / sync**
- Skip existing files (-c) — `-c`: "Continue if a downloaded file already exists"
- Use archive file — `--download-archive`: "Keep track of track IDs in an archive file, and skip
  already-downloaded files"

**Auth token** — Optional OAuth token, needed for original files, likes/me, and HQ downloads with
Go+. Find it on soundcloud.com while logged in, via DevTools (F12) → Storage → Cookies →
soundcloud.com → `oauth_token` Note that tokens expire when clicking the "Sign Out" button, so it's
recommended to get this from an incognito tab before closing it.

**Archive Check** - Checks whether SoundCloud track IDs are still available online (from the archive 
file if one is set, otherwise from the SoundCloud IDs tagged on you local audio files) and reports 
which have been deleted or made private, along with any local copies you still have of them.

## Bandcamp (bandcamp-downloader)

**Format** — `--format`/`-f`: "What format to download the songs in. Default is 'mp3-320'."

**Download options**
- Include hidden — `--include-hidden`: "Download items in your collection that have been marked
  as hidden."
- Extract — valid downloaded
  zips are unzipped into an `Artist - Title` folder and single-track downloads are filed into an
  `Artist/Singles` folder.
- Summary — `--summary`: "Display a summary of the status of every item at the end."
- Dry run — `--dry-run`: "Don't actually download files, just process all the web data and report
  what would have been done."
- Verbose — `--verbose`: increases bandcamp-downloader's log verbosity (also turned on
  automatically when using an archive file, since the archive boundary is detected from its
  verbose output).

**Limits & retries**
- Parallel — `--parallel-downloads`/`-p`: "How many threads to use for parallel downloads. Set to
  '1' to disable parallelism. Default is 5. Must be between 1 and 32."
- Wait — `--wait-after-download`: "How long, in seconds, to wait after successfully completing a
  download before downloading the next file. Defaults to '1'."
- Attempts — `--max-download-attempts`: "How many times to try downloading any individual files
  before giving up on it. Defaults to '5'."
- Retry wait — `--retry-wait`: "How long, in seconds, to wait before trying to download a file
  again after a failure. Defaults to '5'."

**Purchase date range**
- Since — `--download-since`: "Only download items purchased on or after the given date.
  YYYY-MM-DD format, defaults to all items."
- Until — `--download-until`: "Only download items purchased before the given date. YYYY-MM-DD
  format, defaults to all items."

**Archive / sync**
- Use archive file — records each downloaded album/single's item ID in the archive file and skips 
re-downloading/re-processing anything already recorded.

**Cookies** — required Netscape-format `cookies.txt` export of your Bandcamp session, since
bandcamp-downloader needs it to authenticate. Export it with a browser extension such as
[Cookie-Editor](https://cookie-editor.com/) while logged in to bandcamp.com, then save the contents
as `cookie.txt`. These are only your cookies for bandcamp.com.

## Daily schedule

Available for both services. Registers an OS-level daily task (macOS: LaunchAgents, Windows: Task
Scheduler, Linux: cron) that reruns the configured profile at a fixed time, independent of the GUI
being open.


