# Multi Music Archiver

A GUI that wraps [scdl](https://github.com/scdl-org/scdl) (SoundCloud) and
[bandcamp-downloader](https://github.com/easlice/bandcamp-downloader) (Bandcamp) for archiving your
library from either service, with optional daily scheduling and deleted track checking.

## ⚠️ Safety Notice
I am not a developer and almost all of the code in this program is written by an LLM. It is
designed to only modify, rename, move, or delete files that it downloaded itself, or files that
share the exact filename, folder layout, and embedded SoundCloud/Bandcamp ID of what it is currently
downloading. See [Modification Rules](#modification-rules) for exact scenarios. Since the code is LLM-written, if 
you want to be *absolutely certain* of no data loss, point the save directories to fresh, empty folders.

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

## Modification Rules

Every scenario below is something the app can do to a file inside your "Save to" folder. Paths are
shown relative to that folder; `<id>` is a real SoundCloud/Bandcamp ID.

### What must be true before any file is touched

A file is only ever eligible if **all** of these hold:

- Its name carries a bracketed ID of **7 or more digits** — the format this app writes. This
  deliberately excludes human naming conventions like `[1997] OK Computer.zip`, `[01] Intro.mp3`,
  and date-style names like `[210415] Artist - Live Set.zip`, none of which can ever be claimed.
- **Bandcamp:** it also sits in the exact `<Artist>/` layout the downloader writes, *and* its ID is
  one this app can prove it downloaded — present in your archive file, seen in this run's download
  log, or recorded in the pending-claims file from a run that was interrupted.
- **SoundCloud:** with an archive file, its ID must be listed in that archive. Without one, the file
  must not have existed when the download started (the folder is snapshotted beforehand).

**Anything failing these is left strictly alone.**

### SoundCloud

**Strips the SoundCloud ID from the filename and writes it into the file's tags**
```
[318947562] Four Tet - Angel Echoes.mp3   →   Four Tet - Angel Echoes.mp3
```
Happens when `318947562` is in the selected archive.txt (or, with no archive, only for files the
current download just created). Runs after every download, and also before one starts to catch up
on files left behind by a previous interrupted run. If the clean name is taken, becomes
`Four Tet - Angel Echoes (1).mp3` instead of overwriting.

### Bandcamp

**Extracts a zip, then deletes it**
```
Artist/[1234567] Artist - Album.zip   →   Artist/Artist - Album/…
```
Happens with "Extract" enabled, whenever the album's extracted folder doesn't already contain
audio — i.e. the first time this zip is extracted (new album, or an archived album whose extracted
files went missing and are being restored). Collisions inside the extracted folder become
`Track (1).mp3` rather than overwriting.

**Files a single track into `Singles/` and renames it**
```
Artist/[1234567] Artist - Track.mp3   →   Artist/Singles/Artist - Track.mp3
```
Happens with "Extract" enabled, for a bare-audio single (Bandcamp doesn't zip singles). Collisions
become `Artist - Track (1).mp3`.

**Deletes a leftover `.part` staging file**
```
Artist/[1234567] Artist - Album.zip.part
```
Happens on the run after a download was interrupted mid-transfer — `.part` files are never the final
output, so a stale one left over is cleaned up.

**Replaces an existing zip's content**
```
Artist/[1234567] Artist - Album.zip   (content replaced)
```
Happens when a file already sits at that exact path but its size doesn't match what Bandcamp
reports for the album — treated as a damaged/partial earlier download and re-fetched. **This is the
one case where a file's content is genuinely overwritten.** With "Extract" enabled this never
happens, since a successfully extracted zip is deleted right after extraction, leaving nothing at
that path to compare.

**Deletes a redundant re-download without opening it**
```
Artist/[1234567] Artist - Album.zip   (deleted)
```
Happens when the album is already archived **and** its extracted folder already has real audio on
disk from a previous run — Bandcamp resurfaces already-owned albums when a discography bundle is
purchased, re-triggering a download of something you already have. Deleted on sight, without
extracting or validating it. (If the extracted output is missing instead, the "extracts, then
deletes" case above runs so the album self-heals rather than being dropped.)

**Quarantines a corrupt zip by renaming it**
```
Artist/[1234567] Artist - Album.zip   →   Artist/[1234567] Artist - Album.zip.corrupt
```
Happens when a zip owned by this app fails structural validation. Renamed rather than deleted so the
bytes stay recoverable if the corruption verdict was ever wrong; the next sync fetches a fresh copy.
A zip that can't be judged at all (encrypted, unsupported compression) is left untouched rather than
assumed corrupt.
