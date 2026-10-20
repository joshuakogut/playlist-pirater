#!/usr/bin/env python3
"""playlist-pirater -- turn a playlist CSV into a local MP3 library.

Pipeline (see plans.md):
  1. Read a CSV of tracks (artist / title, plus optional album / position).
  2. For each track, find it on YouTube Music and grab the highest quality
     audio available, transcoded to MP3.
  3. Store everything in a configurable ``music`` directory named
     ``Artist - Title.mp3`` so that a later run can detect that a track has
     already been fetched and skip it.
  4. Write a stored playlist ``local_<inputfilename>.m3u`` in the widely
     supported M3U format, so that any media player can play the result.
     The playlist contains *only* tracks from the input CSV and keeps their
     order. It is written before the first download (containing the CSV tracks
     that are already in the library) and is then updated after every finished
     track, so the playlist grows while the download is still running.

By default the entries are relative to the music directory. That is what
MPD resolves against its ``music_directory``; players that resolve relative
entries against the playlist file itself need absolute paths instead, which
``--absolute-paths`` provides.

Only the artist/title pair is used for matching -- live versions, covers and
remasters are deliberately ignored, exactly as the plan requires.

Usage:
    python3 playlist_pirater.py example.csv --music-dir ~/Music/pirated
    python3 playlist_pirater.py example.csv --music-dir music --dry-run
    python3 playlist_pirater.py example.csv --music-dir music --report report.csv
    python3 playlist_pirater.py example.csv --music-dir music \\
        --m3u-playlist-dir ./playlists
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import shutil
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional
from urllib.parse import quote_plus

try:
    import yt_dlp
except ImportError:  # pragma: no cover - environment problem, not a logic problem
    sys.exit(
        "yt-dlp is required. Install it with:\n"
        "    python3 -m pip install yt-dlp\n"
        "or use the standalone binary: https://github.com/yt-dlp/yt-dlp/releases"
    )


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: YouTube Music "songs" filter. Keeps search results to actual tracks instead
#: of videos, playlists and podcast episodes.
YTM_SONGS_FILTER = "EgWKAQIIAWoKEAkQBRAKEAMQBA%3D%3D"

#: Base URL of the YouTube Music search page.
YTM_SEARCH_URL = "https://music.youtube.com/search"

#: Annotations that should not influence the artist/title comparison.
NOISE_PATTERNS = [
    r"\(.*?remaster.*?\)",
    r"\[.*?remaster.*?\]",
    r"\(.*?remix.*?\)",
    r"\[.*?remix.*?\]",
    r"\(.*?live.*?\)",
    r"\[.*?live.*?\]",
    r"\(.*?mono.*?\)",
    r"\(.*?explicit.*?\)",
    r"\(.*?clean.*?\)",
    r"\(.*?radio edit.*?\)",
    r"\(.*?edit.*?\)",
    r"\(.*?version.*?\)",
    r"\(.*?deluxe.*?\)",
    r"\(.*?bonus track.*?\)",
    r"\(.*?anniversary.*?\)",
    r"\(feat\.?.*?\)",
    r"\[feat\.?.*?\]",
    r"\(ft\.?.*?\)",
    r"\[ft\.?.*?\]",
    r"\(with .*?\)",
    r"\[with .*?\]",
]

#: Characters that are illegal in filenames on common platforms.
ILLEGAL_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

#: Audio file extensions that are considered part of the library. The
#: downloader always produces MP3, but a music directory may well hold other
#: formats as well.
AUDIO_EXTENSIONS = {
    ".mp3",
    ".m4a",
    ".aac",
    ".ogg",
    ".opus",
    ".flac",
    ".wav",
    ".wma",
}

#: Punctuation/whitespace runs used while normalising strings for comparison.
NON_ALNUM = re.compile(r"[^a-z0-9]+")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Track:
    """A single row of the playlist CSV."""

    artist: str
    title: str
    album: str = ""
    position: int = 0
    row: int = 0

    @property
    def key(self) -> str:
        """Normalised ``artist - title`` key used for matching and lookups."""
        return f"{normalize(self.artist)}\t{normalize(self.title)}"

    @property
    def display(self) -> str:
        return f"{self.artist} - {self.title}"


@dataclass
class Candidate:
    """A YouTube Music search result that may correspond to a track."""

    video_id: str
    title: str = ""
    artist: str = ""
    album: str = ""
    duration: Optional[float] = None
    url: str = ""

    def __post_init__(self) -> None:
        if not self.url:
            self.url = f"https://music.youtube.com/watch?v={self.video_id}"


@dataclass
class Result:
    """Outcome of processing a single track."""

    track: Track
    status: str  # downloaded | skipped | not-found | error
    message: str = ""
    path: Optional[Path] = None
    candidate: Optional[Candidate] = None

    @property
    def ok(self) -> bool:
        return self.status in ("downloaded", "skipped")


@dataclass
class Stats:
    downloaded: int = 0
    skipped: int = 0
    not_found: int = 0
    errors: int = 0
    dry_run: int = 0
    results: list[Result] = field(default_factory=list)

    #: Maps a result status onto the counter attribute it increments.
    STATUS_FIELDS = {
        "downloaded": "downloaded",
        "skipped": "skipped",
        "not-found": "not_found",
        "error": "errors",
        "dry-run": "dry_run",
    }

    def add(self, result: Result) -> None:
        self.results.append(result)
        field_name = self.STATUS_FIELDS.get(result.status)
        if field_name is None:
            raise ValueError(f"unknown result status: {result.status}")
        setattr(self, field_name, getattr(self, field_name) + 1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def strip_noise(text: str) -> str:
    """Remove remasters/live/feature annotations from a title."""
    cleaned = text
    for pattern in NOISE_PATTERNS:
        cleaned = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE)
    return cleaned


def normalize(text: str) -> str:
    """Normalise a string for fuzzy comparison.

    Lower-cases, strips accents and noise annotations, and collapses every
    non-alphanumeric run into a single space.
    """
    if not text:
        return ""
    text = strip_noise(text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = NON_ALNUM.sub(" ", text)
    return " ".join(text.split())


def title_similarity(a: str, b: str) -> float:
    """Return a 0..1 similarity score between two titles.

    Uses ``difflib.SequenceMatcher`` on the normalised forms, but gives a
    near-perfect score when the normalised title is a substring of the other
    ("Ember" vs "Ember (feat. X)" -> 0.95).
    """
    from difflib import SequenceMatcher

    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.95
    return SequenceMatcher(None, na, nb).ratio()


def artist_similarity(a: str, b: str) -> float:
    """Compare two artist fields, tolerating multi-artist credits."""
    from difflib import SequenceMatcher

    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    parts_a = {
        normalize(p)
        for p in re.split(r"\s*(?:,|&|feat\.|ft\.|with|and)\s*", a)
        if normalize(p)
    }
    parts_b = {
        normalize(p)
        for p in re.split(r"\s*(?:,|&|feat\.|ft\.|with|and)\s*", b)
        if normalize(p)
    }
    if parts_a & parts_b:
        return 0.9
    if na in nb or nb in na:
        return 0.85
    return SequenceMatcher(None, na, nb).ratio()


def sanitize_filename(name: str) -> str:
    """Make ``name`` safe to use as a filename on Linux/macOS/Windows."""
    name = ILLEGAL_FILENAME_CHARS.sub("_", name).strip().strip(".")
    name = re.sub(r"\s+", " ", name).strip()
    # A name made purely of placeholder characters carries no information.
    if not name.strip("_ ."):
        return "untitled"
    return name or "untitled"


# ---------------------------------------------------------------------------
# Terminal output
# ---------------------------------------------------------------------------


def format_eta(seconds: Optional[float]) -> str:
    """Format a remaining time as ``MM:SS`` (or ``H:MM:SS`` when long)."""
    if seconds is None:
        return "--:--"
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_bytes(size: Optional[float]) -> str:
    """Format a byte count as a short human readable string."""
    if size is None:
        return "?"
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            if unit == "B":
                return f"{value:.0f} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


def make_status_reporter(
    callback: Optional[Callable[..., None]],
) -> Callable[[str, bool], None]:
    """Adapt ``callback`` into a ``(text, milestone) -> None`` reporter.

    Callbacks that only accept the message are supported as well; they are
    simply called without the ``milestone`` flag.
    """
    if callback is None:
        return lambda text, milestone=False: None

    try:
        import inspect

        parameters = inspect.signature(callback).parameters
        accepts_milestone = "milestone" in parameters or any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()
        )
    except (TypeError, ValueError):  # pragma: no cover - builtins etc.
        accepts_milestone = False

    if accepts_milestone:
        return lambda text, milestone=False: callback(text, milestone=milestone)
    return lambda text, milestone=False: callback(text)


class StatusLine:
    """A live status line shown at the bottom of the terminal.

    The permanent per-track log ("[ 1/120] OK  Artist - Title") scrolls above
    it; the status line shows what the script is doing *right now* -- for
    example "searching YouTube Music for 'Ember'" or "downloading 45%".

    On a terminal the line is rewritten in place.  When the output is not a
    terminal (piped to a file, run under CI) in-place rewriting would be
    garbage, so only milestone messages are printed as ordinary lines.
    """

    def __init__(self, stream=None) -> None:
        self._stream = stream if stream is not None else sys.stderr
        try:
            self._tty = self._stream.isatty()
        except Exception:  # pragma: no cover - exotic stream
            self._tty = False
        self._current = ""

    @property
    def is_tty(self) -> bool:
        return self._tty

    def _width(self) -> int:
        return max(20, shutil.get_terminal_size((80, 24)).columns - 1)

    def update(self, text: str, milestone: bool = False) -> None:
        """Show ``text`` as the current status.

        ``milestone`` marks a message that is worth keeping when the output
        is not a terminal (the start of a phase, for instance).
        """
        text = " ".join(str(text).split())
        if not text:
            return

        if not self._tty:
            if milestone and text != self._current:
                self._stream.write(text + "\n")
                self._stream.flush()
            self._current = text
            return

        if text == self._current:
            return
        width = self._width()
        self._stream.write("\r" + text[: width - 1].ljust(width - 1))
        self._stream.flush()
        self._current = text

    def clear(self) -> None:
        """Erase the status line, leaving the cursor at the start of the row."""
        if not self._tty:
            return
        self._stream.write("\r" + " " * (self._width() - 1) + "\r")
        self._stream.flush()
        self._current = ""


# ---------------------------------------------------------------------------
# CSV loading
# ---------------------------------------------------------------------------


def _pick(row: dict, *names: str) -> str:
    """Return the first non-empty value among ``names`` (case-insensitive)."""
    lowered = {k.strip().lower(): (v or "") for k, v in row.items() if k}
    for name in names:
        value = lowered.get(name.lower(), "").strip()
        if value:
            return value
    return ""


def load_tracks(csv_path: Path) -> list[Track]:
    """Parse the playlist CSV into :class:`Track` objects.

    Expected columns are ``artist`` and ``title``; ``album`` and ``position``
    are optional and only used for reporting. A two-column ``Artist - Title``
    file is also accepted as a fallback.
    """
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        sample = handle.read(4096)
        handle.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(handle, dialect=dialect)

        tracks: list[Track] = []
        for index, row in enumerate(reader, start=1):
            artist = _pick(row, "artist", "artists", "album artist", "creator")
            title = _pick(row, "title", "song", "name", "track name")
            if not artist or not title:
                # Fall back to a combined "Artist - Title" column.
                combined = _pick(row, "artist - title", "track", "song", "name")
                if " - " in combined:
                    artist, title = combined.split(" - ", 1)
                    artist, title = artist.strip(), title.strip()
                else:
                    continue

            position_raw = _pick(
                row, "position", "index", "#", "track number", "tracknumber"
            )
            try:
                position = int(float(position_raw)) if position_raw else index
            except ValueError:
                position = index

            tracks.append(
                Track(
                    artist=artist.strip(),
                    title=title.strip(),
                    album=_pick(row, "album", "album name").strip(),
                    position=position,
                    row=index,
                )
            )
    return tracks


# ---------------------------------------------------------------------------
# The music library (destination directory)
# ---------------------------------------------------------------------------


class MusicLibrary:
    """The destination directory, indexed by ``Artist - Title``.

    Files are stored as ``<music-dir>/<Artist> - <Title>.mp3``. The index is
    built by scanning the directory once, so a rerun of the script can detect
    that a track was already downloaded and skip it.
    """

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._index: dict[str, Path] = {}
        self._scan()

    @staticmethod
    def key_for_path(path: Path) -> str:
        """Return the normalised ``artist\\ttitle`` key of a library file.

        The file name is expected to be ``Artist - Title.<ext>``; a name
        without a dash is treated as a title without an artist.
        """
        stem = path.stem
        if " - " in stem:
            artist, title = stem.split(" - ", 1)
        else:
            artist, title = "", stem
        return f"{normalize(artist)}\t{normalize(title)}"

    def _scan(self) -> None:
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            self._index[self.key_for_path(path)] = path

    def files(self) -> list[Path]:
        """Return every audio file currently in the library.

        The list is sorted by file name so that the generated playlist is
        stable between runs. Empty files are left out -- they are either
        still being written or the leftovers of a failed download.
        """
        found = [
            path
            for path in self.root.rglob("*")
            if path.is_file()
            and path.suffix.lower() in AUDIO_EXTENSIONS
            and path.stat().st_size > 0
        ]
        return sorted(found, key=lambda p: p.name.lower())

    def path_for(self, track: Track) -> Path:
        filename = sanitize_filename(f"{track.artist} - {track.title}") + ".mp3"
        return self.root / filename

    def existing(self, track: Track) -> Optional[Path]:
        """Return the path of an already-downloaded copy of ``track``."""
        key = track.key
        if key in self._index:
            path = self._index[key]
            if path.exists() and path.stat().st_size > 0:
                return path
        # Fall back to a direct path check (covers files added after the scan).
        candidate = self.path_for(track)
        if candidate.exists() and candidate.stat().st_size > 0:
            self._index[key] = candidate
            return candidate
        return None

    def register(self, track: Track, path: Path) -> None:
        self._index[track.key] = path


# ---------------------------------------------------------------------------
# YouTube Music search
# ---------------------------------------------------------------------------


class YouTubeMusicFinder:
    """Searches YouTube Music for a track and returns matching candidates."""

    def __init__(
        self,
        search_limit: int = 10,
        metadata_limit: int = 8,
        sleep: float = 0.0,
        status: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.search_limit = search_limit
        self.metadata_limit = metadata_limit
        self.sleep = sleep
        self.status = status
        self.prefix = ""
        self._reporter = make_status_reporter(status)
        self._flat_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": True,
            "noplaylist": True,
        }
        self._full_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
        }

    def _report(self, text: str, milestone: bool = False) -> None:
        self._reporter(f"{self.prefix} {text}".strip(), milestone)

    # -- searching ---------------------------------------------------------

    def search(self, artist: str, title: str) -> list[Candidate]:
        """Return raw YouTube Music candidates for ``artist - title``."""
        query = f"{artist} {title}".strip()
        url = f"{YTM_SEARCH_URL}?q={quote_plus(query)}&sp={YTM_SONGS_FILTER}"

        self._report(
            f"searching YouTube Music for '{artist} - {title}'", milestone=True
        )
        try:
            with yt_dlp.YoutubeDL(self._flat_opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:  # network / extraction problems
            raise RuntimeError(f"YouTube Music search failed: {exc}") from exc

        entries = (info or {}).get("entries") or []
        candidates: list[Candidate] = []
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            video_id = entry.get("id") or ""
            if not video_id or len(video_id) != 11 or video_id in seen:
                continue
            if entry.get("ie_key") not in (None, "Youtube"):
                continue
            seen.add(video_id)
            candidates.append(
                Candidate(video_id=video_id, title=entry.get("title") or "")
            )
            if len(candidates) >= self.search_limit:
                break
        return candidates

    # -- metadata ----------------------------------------------------------

    def fetch_metadata(self, candidate: Candidate) -> Candidate:
        """Fill in artist/album/duration for a candidate (best effort)."""
        try:
            with yt_dlp.YoutubeDL(self._full_opts) as ydl:
                info = ydl.extract_info(candidate.url, download=False)
        except Exception:
            return candidate
        if not isinstance(info, dict):
            return candidate
        candidate.title = info.get("track") or info.get("title") or candidate.title
        candidate.artist = info.get("artist") or info.get("creator") or candidate.artist
        candidate.album = info.get("album") or candidate.album
        candidate.duration = info.get("duration") or candidate.duration
        return candidate

    # -- matching ----------------------------------------------------------

    def find(self, track: Track) -> Optional[Candidate]:
        """Return the best YouTube Music match for ``track``, or ``None``.

        Matching is done on artist + title only, per the plan: live versions,
        instrumentals and remasters are ignored.
        """
        candidates = self.search(track.artist, track.title)
        if not candidates:
            return None

        # Cheap pre-filter: rank flat search titles, then only fetch full
        # metadata for the most promising ones.
        candidates.sort(key=lambda c: title_similarity(track.title, c.title), reverse=True)

        best: Optional[Candidate] = None
        best_score = 0.0
        checked = 0
        for candidate in candidates:
            if checked >= self.metadata_limit:
                break
            if title_similarity(track.title, candidate.title) < 0.4:
                # The title is unrelated; keep scanning only while the flat
                # list still holds plausible items.
                continue
            checked += 1
            self._report(
                f"checking candidate {checked}/{self.metadata_limit}: "
                f"{candidate.title}"
            )
            self.fetch_metadata(candidate)
            if self.sleep:
                time.sleep(self.sleep)
            score = self._score(track, candidate)
            if score > best_score:
                best, best_score = candidate, score
            if best_score >= 0.98:
                break
        return best

    @staticmethod
    def _score(track: Track, candidate: Candidate) -> float:
        title_score = title_similarity(track.title, candidate.title)
        artist_score = artist_similarity(track.artist, candidate.artist)
        # The title matters most, the artist confirms the match.
        score = 0.65 * title_score + 0.35 * artist_score
        if title_score >= 0.95 and artist_score >= 0.85:
            score = min(1.0, score + 0.05)
        return score


# ---------------------------------------------------------------------------
# Downloader
# ---------------------------------------------------------------------------


class Downloader:
    """Downloads the best available audio and converts it to MP3."""

    def __init__(
        self,
        audio_quality: str = "320",
        ffmpeg_location: Optional[str] = None,
        status: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.audio_quality = audio_quality
        self.ffmpeg_location = ffmpeg_location
        self.status = status
        self.prefix = ""
        self._reporter = make_status_reporter(status)

    def _report(self, text: str, milestone: bool = False) -> None:
        self._reporter(f"{self.prefix} {text}".strip(), milestone)

    def _progress_hook(self) -> Callable[[dict], None]:
        """Build a yt-dlp progress hook that drives the status line.

        yt-dlp's own progress bar is switched off (``noprogress``); this hook
        renders the same information on the shared status line instead.
        """
        started = time.monotonic()
        last_step = -1

        def hook(data: dict) -> None:
            nonlocal last_step
            state = data.get("status")
            if state == "downloading":
                total = data.get("total_bytes") or data.get("total_bytes_estimate")
                done = data.get("downloaded_bytes") or 0
                speed = data.get("speed")
                eta = data.get("eta")

                parts = ["downloading"]
                percent = None
                if total:
                    percent = min(100.0, done * 100.0 / total)
                    parts.append(f"{percent:.0f}%")
                    parts.append(f"({format_bytes(done)}/{format_bytes(total)})")
                else:
                    parts.append(format_bytes(done))
                if speed:
                    parts.append(f"at {format_bytes(speed)}/s")
                if eta is not None:
                    parts.append(f"ETA {format_eta(eta)}")

                # When the output is not a terminal only every 10% step is
                # worth a line of its own.
                step = int(percent // 10) if percent is not None else -1
                self._report(" ".join(parts), milestone=step != last_step)
                if step != last_step:
                    last_step = step
            elif state == "finished":
                elapsed = time.monotonic() - started
                self._report(f"downloaded, converting to MP3 ({elapsed:.0f}s)")
            elif state == "error":
                self._report("download failed")

        return hook

    def _opts(self, outtmpl: str) -> dict:
        opts = {
            "format": "bestaudio/best",
            "outtmpl": outtmpl,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            # We render the progress ourselves on the status line.
            "noprogress": True,
            "progress_hooks": [self._progress_hook()],
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": self.audio_quality,
                }
            ],
        }
        if self.ffmpeg_location:
            opts["ffmpeg_location"] = self.ffmpeg_location
        return opts

    def download(self, candidate: Candidate, destination: Path) -> Path:
        """Download ``candidate`` as MP3 to ``destination`` (without suffix).

        The audio is first written to a temporary file and only then moved to
        its final name. This keeps the output template free of characters that
        yt-dlp would interpret (a title containing "%" for instance).
        """
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._report("starting download")
        with tempfile.TemporaryDirectory(prefix="pp-download-") as tmp_dir:
            outtmpl = str(Path(tmp_dir) / "audio.%(ext)s")
            with yt_dlp.YoutubeDL(self._opts(outtmpl)) as ydl:
                ydl.extract_info(candidate.url, download=True)

            produced = Path(tmp_dir) / "audio.mp3"
            if not produced.exists():
                candidates = sorted(Path(tmp_dir).glob("*.mp3"))
                if not candidates:
                    raise RuntimeError("download finished but no MP3 was produced")
                produced = candidates[0]

            final = destination.with_suffix(".mp3")
            if final.exists():
                final.unlink()
            shutil.move(str(produced), str(final))
        return final


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


class PlaylistPirater:
    """Runs the three-step plan over a list of tracks."""

    def __init__(
        self,
        library: MusicLibrary,
        finder: YouTubeMusicFinder,
        downloader: Downloader,
        dry_run: bool = False,
        overwrite: bool = False,
        retries: int = 2,
        status: Optional[StatusLine] = None,
        on_ready: Optional[Callable[[Track, Path], None]] = None,
    ) -> None:
        self.library = library
        self.finder = finder
        self.downloader = downloader
        self.dry_run = dry_run
        self.overwrite = overwrite
        self.retries = retries
        self.status = status
        #: Called with (track, path) as soon as a track has a file on disk.
        self.on_ready = on_ready

    def _report(self, text: str, milestone: bool = False) -> None:
        if self.status is not None:
            self.status.update(text, milestone=milestone)

    def _announce(self, track: Track, path: Path) -> None:
        """Tell the caller that ``track`` is available as ``path``."""
        if self.on_ready is None:
            return
        try:
            self.on_ready(track, path)
        except Exception as exc:  # a broken callback must not kill the run
            self._report(f"playlist update failed: {exc}", milestone=True)

    def _set_context(self, index: int, total: int) -> None:
        """Tell the finder/downloader which track is being worked on.

        The status line then reads "[2/120] downloading ..." so the bottom line
        always says both *what* is happening and *which* track it refers to.
        """
        prefix = f"[{index}/{total}]"
        for component in (self.finder, self.downloader):
            if component is None:
                continue
            if hasattr(component, "prefix"):
                component.prefix = prefix

    def process(self, track: Track) -> Result:
        if not self.overwrite:
            existing = self.library.existing(track)
            if existing is not None:
                self._report("already downloaded, skipping", milestone=True)
                self._announce(track, existing)
                return Result(track, "skipped", "already downloaded", path=existing)

        try:
            candidate = self.finder.find(track)
        except Exception as exc:
            self._report(f"search failed: {exc}", milestone=True)
            return Result(track, "error", str(exc))

        if candidate is None:
            self._report("no YouTube Music match found", milestone=True)
            return Result(track, "not-found", "no YouTube Music match")

        if self.dry_run:
            return Result(
                track,
                "dry-run",
                f"would download {candidate.artist} - {candidate.title}",
                candidate=candidate,
            )

        destination = self.library.path_for(track)
        last_error: Optional[Exception] = None
        for attempt in range(1, self.retries + 2):
            if attempt > 1:
                self._report(
                    f"retrying download ({attempt}/{self.retries + 1}): {last_error}"
                )
            try:
                path = self.downloader.download(candidate, destination)
                self.library.register(track, path)
                # The playlist is updated right away, so the track can be
                # played while the rest of the playlist is still downloading.
                self._announce(track, path)
                return Result(
                    track,
                    "downloaded",
                    f"{candidate.artist} - {candidate.title}",
                    path=path,
                    candidate=candidate,
                )
            except Exception as exc:  # network hiccup, geo-block, ...
                last_error = exc
                if attempt <= self.retries:
                    time.sleep(2 * attempt)
        self._report(f"download failed: {last_error}", milestone=True)
        return Result(
            track, "error", f"download failed: {last_error}", candidate=candidate
        )

    def run(self, tracks: Iterable[Track], progress: bool = True) -> Stats:
        tracks = list(tracks)
        stats = Stats()
        total = len(tracks)
        for index, track in enumerate(tracks, start=1):
            self._set_context(index, total)
            result = self.process(track)
            stats.add(result)
            if progress:
                self._log(index, total, result)
        return stats

    def _log(self, index: int, total: int, result: Result) -> None:
        icons = {
            "downloaded": "OK  ",
            "skipped": "SKIP",
            "not-found": "MISS",
            "error": "FAIL",
            "dry-run": "DRY ",
        }
        # Erase the status line first so the permanent log is not written
        # over it.
        if self.status is not None:
            self.status.clear()
        print(
            f"[{index:>4}/{total}] {icons.get(result.status, '????')} "
            f"{result.track.display} -- {result.message}",
            flush=True,
        )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def write_report(results: list[Result], path: Path) -> None:
    """Write a CSV report of what happened to every track."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "position",
                "artist",
                "title",
                "album",
                "status",
                "message",
                "file",
                "video_id",
            ]
        )
        for result in results:
            writer.writerow(
                [
                    result.track.position,
                    result.track.artist,
                    result.track.title,
                    result.track.album,
                    result.status,
                    result.message,
                    str(result.path) if result.path else "",
                    result.candidate.video_id if result.candidate else "",
                ]
            )


# ---------------------------------------------------------------------------
# M3U playlist output
# ---------------------------------------------------------------------------

#: Suffix of the generated playlist files.
M3U_PLAYLIST_SUFFIX = ".m3u"

#: Prefix of the generated playlist. The full name is ``local_<inputfilename>``.
M3U_PLAYLIST_PREFIX = "local_"


def m3u_playlist_name(csv_path: Path) -> str:
    """Return the playlist name for the CSV ``csv_path``.

    The plan asks for a playlist called ``local_$inputfilename``, so
    ``example.csv`` becomes ``local_example.csv``.

    Names containing ``/`` (and ``\\`` on Windows) or newlines are folded away
    here, because such a name cannot be used as a file name.
    """
    name = f"{M3U_PLAYLIST_PREFIX}{csv_path.name}"
    name = name.replace("/", "_").replace("\\", "_")
    name = "".join(ch for ch in name if ch.isprintable() and ch not in "\r\n")
    return name.strip() or f"{M3U_PLAYLIST_PREFIX}playlist"


def m3u_entry_for_path(
    path: Path, music_root: Path, absolute: bool = False
) -> str:
    """Return the playlist entry for a single file.

    By default the entry is a path relative to ``music_root``, which is what
    MPD resolves against its ``music_directory`` and what keeps the playlist
    portable. A file outside the music directory falls back to an absolute
    path.

    With ``absolute=True`` the entry is always an absolute path. Players that
    resolve relative entries against the playlist file instead of against a
    music directory need this.
    """
    root = music_root.resolve()
    resolved = path.resolve()
    if absolute:
        return resolved.as_posix()
    try:
        entry = resolved.relative_to(root).as_posix()
    except ValueError:
        # Outside the music directory: fall back to an absolute path,
        # which every player accepts as well.
        entry = resolved.as_posix()
    if entry.startswith("#"):
        # Players treat a line starting with "#" as a comment and would
        # silently drop the track. An absolute path avoids the clash.
        entry = resolved.as_posix()
    return entry


def m3u_playlist_entries(
    results: list[Result], music_root: Path, absolute: bool = False
) -> list[str]:
    """Return the playlist entries for ``results``.

    Every track that is available on disk (freshly downloaded or already
    present) contributes one entry, expressed as a path relative to
    ``music_root`` -- or as an absolute path if ``absolute`` is set. Tracks
    that could not be fetched have no file and are therefore left out. A track
    that is listed more than once in the input CSV contributes a single entry.
    """
    entries: list[str] = []
    seen: set[Path] = set()
    for result in results:
        if not result.ok or result.path is None:
            continue
        resolved = result.path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        entries.append(m3u_entry_for_path(result.path, music_root, absolute))
    return entries


class M3uPlaylistWriter:
    """Writes an M3U playlist that grows while the download is running.

    The playlist is rewritten after every change, so a track shows up in the
    playlist as soon as its file has been downloaded instead of only at the
    very end of the run.

    Ordering: the playlist contains *only* tracks of the input CSV and keeps
    their CSV order. Every CSV row owns a slot, the slots are handed out in
    the order of the rows, and a file is listed in the slot of its row. A
    track that is listed twice in the CSV therefore yields a single playlist
    entry. Audio files that are in the library but not in the CSV are never
    added.
    """

    def __init__(
        self,
        music_root: Path,
        playlist_dir: Path,
        name: str,
        absolute: bool = False,
    ) -> None:
        self.music_root = music_root.expanduser().resolve()
        self.playlist_dir = playlist_dir.expanduser()
        self.name = name
        self.absolute = absolute
        self.target = self.playlist_dir / f"{name}{M3U_PLAYLIST_SUFFIX}"
        # track key -> slot number (the position in the CSV)
        self._slots: dict[str, int] = {}
        # slot number -> the file of that track
        self._files: dict[int, Path] = {}
        # file -> slot number, so that a file is never listed twice
        self._slot_of_file: dict[Path, int] = {}

    # -- content -----------------------------------------------------------

    def set_tracks(self, tracks: Iterable[Track]) -> None:
        """Register the rows of the input CSV and their order.

        Every row gets a slot; the slot number is the index of the row, so
        the slots reproduce the order of the CSV. A track that appears more
        than once in the CSV keeps its first slot, which means the playlist
        lists it only once.
        """
        self._slots.clear()
        for index, track in enumerate(tracks):
            self._slots.setdefault(track.key, index)
        # The slots changed, so the files collected so far no longer apply.
        self._files.clear()
        self._slot_of_file.clear()

    def add_track(self, track: Track, path: Path) -> None:
        """Add the file of a track of the input CSV.

        The file is placed in the slot of its CSV row, so the playlist keeps
        the order of the CSV. A file that is already in the playlist keeps its
        place, so a track is never listed twice. Files of tracks that are not
        part of the input CSV are ignored.
        """
        slot = self._slots.get(track.key)
        if slot is None:
            # Not a track of the input CSV -- it does not belong in the
            # playlist.
            return

        resolved = path.resolve()
        known = self._slot_of_file.get(resolved)
        if known is not None and known != slot:
            # The file is already listed for another row; keep the first one.
            return

        previous = self._files.get(slot)
        if previous is not None and previous.resolve() != resolved:
            self._slot_of_file.pop(previous.resolve(), None)
        self._files[slot] = path
        self._slot_of_file[resolved] = slot

    def __len__(self) -> int:
        return len(self._files)

    def entries(self) -> list[str]:
        """Return the current playlist entries, in playlist order."""
        return [
            m3u_entry_for_path(self._files[slot], self.music_root, self.absolute)
            for slot in sorted(self._files)
        ]

    # -- output ------------------------------------------------------------

    def write(self) -> Path:
        """Write the playlist to disk and return its path.

        The file is replaced atomically so that no player ever reads a
        partially written playlist.
        """
        self.playlist_dir.mkdir(parents=True, exist_ok=True)
        lines = [f"# {self.name}", *self.entries()]
        tmp = self.target.with_name(self.target.name + ".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(tmp, self.target)
        return self.target


def seed_playlist(
    writer: M3uPlaylistWriter, tracks: Iterable[Track], library: MusicLibrary
) -> int:
    """Fill ``writer`` with the files that are already in the library.

    Only the tracks of the input CSV are placed in the playlist, using the
    copy that is already on disk (if there is one). Rows of the CSV that
    have no file in the library are left out, and audio files of the library
    that are not part of the CSV are never added. Returns the number of
    files in the playlist afterwards.
    """
    tracks = list(tracks)
    # The rows of the CSV define the slots and therefore the order.
    writer.set_tracks(tracks)

    for track in tracks:
        existing = library.existing(track)
        if existing is None:
            continue
        writer.add_track(track, existing)

    return len(writer)


def write_m3u_playlist(
    results: list[Result],
    music_root: Path,
    playlist_dir: Path,
    name: str,
    absolute: bool = False,
) -> Path:
    """Write a playlist and return its path.

    The file is written in the M3U format: UTF-8 text with one song path per
    line, relative to the music directory (or absolute if ``absolute`` is
    set). Blank lines and lines starting with ``#`` are comments and are
    ignored by the players.
    """
    playlist_dir = playlist_dir.expanduser()
    playlist_dir.mkdir(parents=True, exist_ok=True)
    target = playlist_dir / f"{name}{M3U_PLAYLIST_SUFFIX}"

    entries = m3u_playlist_entries(results, music_root, absolute)
    lines = [f"# {name}", *entries]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="playlist-pirater",
        description="Download playlist CSV entries from YouTube Music as MP3s.",
    )
    parser.add_argument("csv", type=Path, help="playlist CSV file (artist, title, ...)")
    parser.add_argument(
        "--music-dir",
        type=Path,
        default=Path(os.environ.get("PLAYLIST_PIRATER_MUSIC_DIR", "music")),
        help="destination directory for the MP3 files (default: ./music)",
    )
    parser.add_argument(
        "--quality",
        default="320",
        choices=["320", "256", "192", "128", "96"],
        help="MP3 bitrate in kbps (default: 320)",
    )
    parser.add_argument(
        "--search-limit",
        type=int,
        default=10,
        help="max YouTube Music search results to consider (default: 10)",
    )
    parser.add_argument(
        "--metadata-limit",
        type=int,
        default=8,
        help="max candidates to fetch full metadata for (default: 8)",
    )
    parser.add_argument(
        "--m3u-playlist-dir",
        type=Path,
        default=Path(
            os.environ.get("PLAYLIST_PIRATER_M3U_PLAYLIST_DIR", "playlists")
        ),
        help="directory for the generated M3U playlist (default: ./playlists). "
        "Point this at the playlist directory of your player.",
    )
    parser.add_argument(
        "--no-m3u-playlist",
        action="store_true",
        help="do not write an M3U playlist",
    )
    parser.add_argument(
        "--absolute-paths",
        action="store_true",
        help="write absolute paths into the playlist instead of paths relative "
        "to the music directory (needed by players that resolve relative "
        "entries against the playlist file itself)",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="write a CSV report of the run to this path",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="search and report matches without downloading anything",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="re-download tracks even if they already exist",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=2,
        help="download retries per track (default: 2)",
    )
    parser.add_argument(
        "--ffmpeg-location",
        default=None,
        help="path to the ffmpeg binary (default: found on PATH)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="only process the first N tracks (useful for testing)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="do not print per-track progress",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.csv.is_file():
        print(f"error: CSV not found: {args.csv}", file=sys.stderr)
        return 2

    tracks = load_tracks(args.csv)
    if not tracks:
        print(f"error: no usable rows found in {args.csv}", file=sys.stderr)
        return 2
    if args.limit:
        tracks = tracks[: args.limit]

    library = MusicLibrary(args.music_dir)

    status = StatusLine(sys.stderr)
    status_fn = status.update if not args.quiet else None

    # -- the M3U playlist ---------------------------------------------------
    # It is written before the download starts and after every finished
    # track, so the playlist grows while it is being filled.
    writer: Optional[M3uPlaylistWriter] = None
    if not args.no_m3u_playlist and not args.dry_run:
        name = m3u_playlist_name(args.csv)
        writer = M3uPlaylistWriter(
            library.root, args.m3u_playlist_dir, name, absolute=args.absolute_paths
        )
        # The rows of the CSV define the slots and therefore the order of
        # the playlist.
        writer.set_tracks(tracks)

    finder = YouTubeMusicFinder(
        search_limit=args.search_limit,
        metadata_limit=args.metadata_limit,
        status=status_fn,
    )
    downloader = Downloader(
        audio_quality=args.quality,
        ffmpeg_location=args.ffmpeg_location,
        status=status_fn,
    )

    def on_ready(track: Track, path: Path) -> None:
        """Add a finished track to the playlist and flush it to disk."""
        if writer is None:
            return
        writer.add_track(track, path)
        writer.write()

    pirater = PlaylistPirater(
        library=library,
        finder=finder,
        downloader=downloader,
        dry_run=args.dry_run,
        overwrite=args.overwrite,
        retries=args.retries,
        status=status if not args.quiet else None,
        on_ready=on_ready if writer is not None else None,
    )

    # Every audio file that is already in the library goes into the playlist
    # before the first download starts.
    if writer is not None:
        seeded = seed_playlist(writer, tracks, library)
        writer.write()
        print(
            f"M3U playlist: {seeded} existing track(s) -> {writer.target}",
            flush=True,
        )

    print(f"Processing {len(tracks)} track(s) -> {library.root}\n", flush=True)
    stats = pirater.run(tracks, progress=not args.quiet)
    if not args.quiet:
        status.clear()

    print(
        f"\nDone. downloaded={stats.downloaded} skipped={stats.skipped} "
        f"not-found={stats.not_found} errors={stats.errors}",
        flush=True,
    )
    if stats.dry_run:
        print(f"dry-run matches: {stats.dry_run}")

    if args.report:
        write_report(stats.results, args.report)
        print(f"Report written to {args.report}")

    if writer is not None:
        print(
            f"M3U playlist written to {writer.target} "
            f"({len(writer)} track(s))"
        )

    return 0 if stats.errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
