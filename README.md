# playlist-pirater

Turns a playlist CSV into a local MP3 library using YouTube Music.

1. **Read a CSV** of tracks (`example.csv` shows the expected format:
   `position, artist, title, album, track_uri`).
2. **For each track**, search YouTube Music and download the highest quality
   audio available, transcoded to MP3.
3. **Store it in a configurable `music` directory** as `Artist - Title.mp3`, so
   that a later run can tell that a track was already fetched and skip it.
4. **Write an M3U playlist** named `local_<inputfilename>.m3u` that
   any media player can read. The playlist is written *before*
   the download starts — containing every audio file that is already in the
   library — and is then updated after every finished track, so it grows
   while the download is still running.
5. **Tag the MP3** with the ID3 metadata from the CSV (artist, title, album,
   album artist, track and disc number) using ffmpeg — the audio is copied,
   never re-encoded. See [ID3 tags](#id3-tags).

Matching is done on **artist + title only** — live versions, remasters,
instrumentals and covers are deliberately ignored, as the plan requires.

## Requirements

* Python 3.9+
* [`yt-dlp`](https://github.com/yt-dlp/yt-dlp) (`pip install yt-dlp`)
* [`ffmpeg`](https://ffmpeg.org/) on `PATH` (required for the MP3 conversion)

## Usage

```bash
# Download the whole playlist into ./music
python3 playlist_pirater.py example.csv --music-dir music

# See what would be downloaded, without downloading anything
python3 playlist_pirater.py example.csv --music-dir music --dry-run

# Just the first 10 tracks, with a CSV report of the run
python3 playlist_pirater.py example.csv --music-dir music --limit 10 --report report.csv

# Tag files that are already in ./music, without downloading anything
python3 playlist_pirater.py example.csv --music-dir music --tag-only

# Preview that tag-only pass first
python3 playlist_pirater.py example.csv --music-dir music --tag-only --dry-run
```

Re-running the same command is safe: tracks that are already present in the
music directory are reported as `SKIP` and are not fetched again.

### Terminal output

While the script runs you get two things at once:

* a **permanent log** on stdout, one line per track, that scrolls away:

  ```
  [   1/120] OK   Dance Gavin Dance - Ember -- Dance Gavin Dance - Ember
  [   2/120] OK   Circa Survive - Get Out -- Circa Survive - Get Out
  ```

* a **live status line** on stderr at the bottom of the terminal, showing what
  the script is doing right now:

  ```
  [2/120] searching YouTube Music for 'Circa Survive - Get Out'
  [2/120] downloading 56% (2.0 MiB/3.6 MiB) at 41.7 MiB/s ETA 00:00
  ```

The status line is rewritten in place, so it never leaves a trail. yt-dlp's own
progress bar is disabled so the two do not fight over the bottom line.

When the output is *not* a terminal (redirected to a file, or run in CI) the
in-place rewriting would be unreadable, so the status line is printed as
ordinary lines instead — one per milestone (a search starting, every 10% of a
download, a failure). The permanent log is unaffected either way.

Use `--quiet` to suppress both.

### Options

| Flag | Default | Description |
| --- | --- | --- |
| `--music-dir` | `./music` | Destination directory for the MP3s. Also settable via the `PLAYLIST_PIRATER_MUSIC_DIR` environment variable. |
| `--m3u-playlist-dir` | `./playlists` | Where the generated M3U playlist is written. Also settable via the `PLAYLIST_PIRATER_M3U_PLAYLIST_DIR` environment variable. |
| `--no-m3u-playlist` | off | Do not write an M3U playlist. |
| `--absolute-paths` | off | Write absolute paths into the playlist instead of paths relative to the music directory. Needed by players that resolve relative entries against the playlist file itself. |
| `--quality` | `320` | MP3 bitrate in kbps (`320`, `256`, `192`, `128`, `96`). |
| `--search-limit` | `10` | Max YouTube Music search results considered per track. |
| `--metadata-limit` | `8` | Max candidates that get their full metadata fetched. |
| `--report` | – | Write a CSV report of the run (status, file, YouTube video id). |
| `--dry-run` | off | Only search and report matches; download nothing. |
| `--overwrite` | off | Re-download tracks even if they already exist. |
| `--retries` | `2` | Retries per failed download. |
| `--ffmpeg-location` | PATH | Explicit path to the ffmpeg binary. |
| `--limit` | – | Only process the first N tracks. |
| `--no-tags` | off | Do not write ID3 tags onto the downloaded MP3s. |
| `--tag-only` | off | Only write ID3 tags for files already in `--music-dir`; nothing is searched or downloaded, so it works offline. |
| `--quiet` | off | Suppress the per-track log and the live status line. |

Exit code is `0` when no errors occurred, `1` when one or more tracks failed
(with `--tag-only`: when any file was missing or could not be tagged), and
`2` for usage problems (missing/invalid CSV).

## M3U playlist

Every run also writes a playlist in the widely supported M3U format, so that
any media player can read it. The playlist is named
`local_<inputfilename>.m3u` — for `example.csv` that is
`local_example.csv.m3u`.

The file is plain UTF-8 text, one song per line, with the paths relative to
the music directory. Lines starting with `#` are comments and are ignored by
the players.

```
# local_example.csv
Dance Gavin Dance - Ember.mp3
Circa Survive - Get Out.mp3
```

Only tracks that actually have a file on disk are listed: freshly downloaded
ones and ones that were already present. Tracks that could not be found are
left out, so the playlist never references a missing file.

The playlist contains **only the tracks of the input CSV** and keeps their
CSV order. Audio files that happen to be in the music directory but are not
part of the CSV are never added, and a track that is listed twice in the CSV
appears only once in the playlist.

### Relative vs. absolute paths

By default the entries are **relative to the music directory** (`--music-dir`).
This keeps the playlist small and portable, and it is what
[MPD](https://www.musicpd.org/) resolves against its `music_directory`.

Some players instead resolve relative entries against the directory of the
playlist file itself. Since the playlist usually lives in a different
directory than the music, such a player would not find the files. For those,
pass `--absolute-paths`:

```bash
python3 playlist_pirater.py example.csv \
    --music-dir /home/joshua/projects/playlist-pirater/music \
    --m3u-playlist-dir /home/joshua/projects/playlist-pirater/playlists \
    --absolute-paths
```

```
# local_example.csv
/home/joshua/projects/playlist-pirater/music/Dance Gavin Dance - Ember.mp3
/home/joshua/projects/playlist-pirater/music/Circa Survive - Get Out.mp3
```

### The playlist grows while downloading

The playlist is not written once at the end — it is built up live:

1. **Before the first download**, every track of the input CSV that is already
   in the music directory is written into the playlist, at its CSV position.
2. **After every finished track**, the track is added to the playlist and the
   file is rewritten immediately. The rewrite is atomic (write to a temp file,
   then `os.replace`), so a player never sees a half-written playlist.

This means you can open the playlist in your player right after starting the
script and already play the first tracks while the rest is still downloading.

By default the playlist is written to `./playlists`; use `--m3u-playlist-dir`
to put it somewhere else:

```bash
python3 playlist_pirater.py example.csv \
    --music-dir /home/joshua/projects/playlist-pirater/music \
    --m3u-playlist-dir /home/joshua/projects/playlist-pirater/playlists
```

## ID3 tags

Every downloaded MP3 gets ID3 metadata taken from the playlist CSV, written
with `ffmpeg` as a **stream copy** (`-c copy`): the audio frames are moved
unchanged, nothing is re-encoded, and attached cover art survives the remux.

| ID3 frame | Source |
| --- | --- |
| `title` | `title` column |
| `artist` (TPE1) | `artist` column, exactly as written (collaborators kept) |
| `album` (TALB) | `album` column |
| `album artist` (TPE2) | the first artist of the credit, so the album groups correctly in players |
| `track` (TRCK) | recomputed **inside the album** (`3/12`), not the playlist position |
| `disc` (TPOS) | `1/1` for album tracks |
| `date` (TYER) | optional `year`/`date` column, when the CSV has one |

Notes:

* **Track numbers are album-relative.** The CSV `position` column is the order
  of the playlist, so it is not used for the tags; rows are grouped by
  `(album, primary artist)` and numbered `1..N` within the group, in CSV order.
* **A track that appears twice in the CSV** (or twice on the same album) is
  downloaded once and gets one set of tags, so duplicates never get
  conflicting track numbers.
* Tags are written as **ID3v2.3** without an ID3v1 block, because ID3v1 would
  truncate long and non-ASCII values. Unicode in artist/title/album is stored
  correctly.
* **Old tags are cleared first** (`-map_metadata -1`), so whatever yt-dlp
  left behind cannot survive next to the new frames.
* Tags are written to a temporary file in the same directory and then moved
  into place, so an interrupted run never leaves a half-written MP3.
* Nothing is fetched from the network for tagging — an album's year can only
  come from the CSV.

### Tag files that are already downloaded

Tagging does not touch the network, so it can be run on its own over an
existing library:

```bash
# Write tags for everything already in ./music
python3 playlist_pirater.py example.csv --music-dir music --tag-only

# See what would change first
python3 playlist_pirater.py example.csv --music-dir music --tag-only --dry-run
```

The summary reports `tagged` (the file was rewritten), `already-tagged`
(matching tags were already present, so the file was left alone), `missing`
(a CSV track with no file) and `failed`. The exit code is `1` if anything was
missing or failed.

Re-tagging is idempotent: files that already carry the requested tags are
left untouched, so running `--tag-only` again costs nothing.

## CSV format

The `artist` and `title` columns are required. `album` and `position` are
optional, and `album` is what the ID3 tags are built from. An optional
`year`/`date` column supplies the release year. A two-column
`Artist - Title` file also works.

```csv
position,artist,title,album,track_uri
1,Dance Gavin Dance,Ember,Jackpot Juicer,spotify:track:5xkN2eW9a0SKG23FYpWC5L
```

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The tests are fully offline — network access is stubbed out. The tagging
tests build a short MP3 with `ffmpeg -f lavfi -i sine=...` and verify the
result with `ffprobe`; they are skipped automatically when ffmpeg is not
installed.

## Notes and limitations

* YouTube Music URLs are used directly; no API key or account is needed, but
  YouTube can change its internals at any time, which may break searching.
* The best available audio source is used and then transcoded to MP3 by
  ffmpeg. YouTube's highest bitrate stream is typically 128–160 kbps Opus/AAC,
  so the 320 kbps MP3 is a re-encode rather than a native 320 kbps source.
* Live/alternate versions are not distinguished; if a track's official audio
  is not on YouTube Music, a matching upload by another channel may be used.
* Files are named `Artist - Title.mp3`; illegal filename characters are
  replaced with `_`.
* Tags come only from the CSV: there is no Spotify/MusicBrainz lookup, so a
  release year is written only when the CSV provides one, and multi-artist
  credits are stored verbatim with the first name used as the album artist.
* `disc` is always `1/1` because the CSV carries no disc information.
