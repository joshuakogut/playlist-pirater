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
| `--quiet` | off | Suppress the per-track log and the live status line. |

Exit code is `0` when no errors occurred, `1` when one or more tracks failed,
and `2` for usage problems (missing/invalid CSV).

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

### Using it with MPD

[MPD](https://www.musicpd.org/) reads M3U playlists from its
`playlist_directory`. Point `--m3u-playlist-dir` at that directory and make
sure MPD's `music_directory` matches `--music-dir`:

```ini
# ~/.config/mpd/mpd.conf
music_directory    "/home/joshua/projects/playlist-pirater/music"
playlist_directory "/home/joshua/projects/playlist-pirater/playlists"
```

The playlist then shows up in any MPD client, e.g.:

```
$ mpc lsplaylists
local_example.csv
$ mpc load local_example.csv
```

## Playing it with MPD

The sections below are a setup guide for using
[MPD](https://www.musicpd.org/) as the player. They are not required — the
generated `.m3u` works with any media player.

### Running MPD as your own user

MPD is normally started as the system-wide `mpd` user, which **cannot read
files under your home directory** if your home is `0700` (the default on
Arch). The symptom is a stream of these lines in `journalctl -u mpd`:

```
exception: Failed to decode ".../Ember.mp3"; Failed to open "...": Permission denied
```

The fix is to run MPD from the systemd *user* unit instead, so it runs as you
and can reach your music directory and your PipeWire/PulseAudio session:

```bash
systemctl --user enable --now mpd
```

`~/.config/mpd/mpd.conf` is then read instead of `/etc/mpd.conf`. A working
configuration for a PipeWire desktop looks like this:

```ini
music_directory     "/home/joshua/projects/playlist-pirater/music"
playlist_directory  "/home/joshua/projects/playlist-pirater/playlists"
db_file             "/home/joshua/.local/share/mpd/mpd.db"
state_file          "/home/joshua/.local/share/mpd/mpdstate"
bind_to_address     "127.0.0.1"
port                "6600"

audio_output {
    type   "pulse"
    name   "PipeWire/PulseAudio"
    server "/run/user/1000/pulse/native"
    sink   "auto"
}
```

Two things to watch out for:

* The system-wide MPD unit must be **disabled**, or it will fight the user
  instance for port 6600 and for the audio device:
  `sudo systemctl disable --now mpd`.
* MPD should start *after* PipeWire, otherwise the Pulse output fails with
  "Connection refused" and the daemon sits there mute. Add a user drop-in:

  ```ini
  # ~/.config/systemd/user/mpd.service.d/override.conf
  [Unit]
  After=pipewire-pulse.service pipewire.service
  Wants=pipewire-pulse.service
  ```

  Then run `systemctl --user daemon-reload && systemctl --user restart mpd`.

Check that audio is really flowing, not just that MPD thinks it is playing:

```bash
$ pactl list sink-inputs | grep -A2 'application.name'
    application.name = "Music Player Daemon"
    Corked: no
```

### Starting MPD automatically

`systemctl --user enable mpd` links the unit into `default.target.wants`, so
MPD starts when you log in. To have it start at **boot**, before you log in,
enable lingering for your user:

```bash
loginctl enable-linger "$USER"
```

With lingering enabled, systemd keeps your user manager running after logout
and starts it at boot, so MPD (and PipeWire) are up regardless of whether
anyone is signed in.

`pipewire.service` and `pipewire-pulse.service` should be enabled too, so
the audio server is available when MPD comes up:

```bash
systemctl --user enable pipewire.service pipewire-pulse.service
```

### Connecting ncmpc

`ncmpc` does **not** default to `localhost`. It defaults to the Unix socket
`/run/mpd/socket`, which is only provided by the system-wide MPD package.
Since our MPD is a user service listening on TCP, point ncmpc at the
loopback address in `~/.config/ncmpc/config`:

```ini
host = "127.0.0.1"
port = "6600"
```

Without this, ncmpc reports `Failed to connect: Connection refused` even
though MPD is running perfectly.

Note that `localhost` resolves to `::1` before `127.0.0.1` on many systems,
and MPD clients do not necessarily fall back to IPv4 when the IPv6
connection is refused. MPD is therefore configured to listen on both:

```ini
bind_to_address "127.0.0.1"
bind_to_address "::1"
```

## CSV format

The `artist` and `title` columns are required; `album` and `position` are
optional and only used for reporting. A two-column `Artist - Title` file also
works.

```csv
position,artist,title,album,track_uri
1,Dance Gavin Dance,Ember,Jackpot Juicer,spotify:track:5xkN2eW9a0SKG23FYpWC5L
```

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The tests are fully offline — network access is stubbed out.

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
