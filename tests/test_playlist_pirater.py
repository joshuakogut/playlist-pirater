"""Unit tests for playlist_pirater.

Network access is stubbed out, so these tests run offline and quickly.
Run with:  python3 -m unittest discover -s tests -v
"""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import playlist_pirater as pp


class NormalizationTests(unittest.TestCase):
    def test_normalize_lowercases_and_strips_punctuation(self):
        self.assertEqual(pp.normalize("Get Out!"), "get out")
        self.assertEqual(pp.normalize("  Multiple   Spaces "), "multiple spaces")

    def test_normalize_strips_accents(self):
        self.assertEqual(pp.normalize("Björk"), "bjork")
        self.assertEqual(pp.normalize("Sigur Rós"), "sigur ros")

    def test_normalize_removes_noise_annotations(self):
        self.assertEqual(pp.normalize("Ember (Remastered 2011)"), "ember")
        self.assertEqual(pp.normalize("Get Out [Live]"), "get out")
        self.assertEqual(pp.normalize("Song (feat. Someone)"), "song")

    def test_title_similarity_exact(self):
        self.assertEqual(pp.title_similarity("Ember", "Ember"), 1.0)

    def test_title_similarity_ignores_live_suffix(self):
        self.assertGreater(pp.title_similarity("Get Out", "Get Out (Live)"), 0.9)

    def test_title_similarity_rejects_different_songs(self):
        self.assertLess(pp.title_similarity("Ember", "Strawberry's Wake"), 0.5)

    def test_artist_similarity_handles_collab(self):
        self.assertGreater(pp.artist_similarity("A, B", "B"), 0.8)
        self.assertGreater(pp.artist_similarity("Circa Survive", "circa survive"), 0.99)


class SanitizeTests(unittest.TestCase):
    def test_removes_illegal_characters(self):
        self.assertEqual(pp.sanitize_filename('AC/DC - Back: in Black?'), "AC_DC - Back_ in Black_")

    def test_never_returns_empty(self):
        self.assertEqual(pp.sanitize_filename("///"), "untitled")


class CsvLoadingTests(unittest.TestCase):
    def _write(self, content):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="")
        tmp.write(content)
        tmp.close()
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        return Path(tmp.name)

    def test_loads_example_columns(self):
        path = self._write(
            "position,artist,title,album,track_uri\n"
            "1,Dance Gavin Dance,Ember,Jackpot Juicer,spotify:track:abc\n"
        )
        tracks = pp.load_tracks(path)
        self.assertEqual(len(tracks), 1)
        track = tracks[0]
        self.assertEqual(track.artist, "Dance Gavin Dance")
        self.assertEqual(track.title, "Ember")
        self.assertEqual(track.album, "Jackpot Juicer")
        self.assertEqual(track.position, 1)

    def test_falls_back_to_combined_column(self):
        path = self._write("Artist - Title\nCirca Survive - Get Out\n")
        tracks = pp.load_tracks(path)
        self.assertEqual(tracks[0].artist, "Circa Survive")
        self.assertEqual(tracks[0].title, "Get Out")

    def test_skips_rows_without_artist_or_title(self):
        path = self._write("artist,title\n,, \n")
        self.assertEqual(pp.load_tracks(path), [])

    def test_handles_real_example_csv(self):
        example = Path(__file__).resolve().parent.parent / "example.csv"
        if not example.exists():
            self.skipTest("example.csv not present")
        tracks = pp.load_tracks(example)
        self.assertGreater(len(tracks), 100)
        self.assertTrue(all(t.artist and t.title for t in tracks))


class MusicLibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_creates_directory(self):
        target = self.root / "nested" / "music"
        library = pp.MusicLibrary(target)
        self.assertTrue(target.is_dir())
        self.assertEqual(library.root, target.resolve())

    def test_detects_existing_file_by_artist_title(self):
        library = pp.MusicLibrary(self.root)
        track = pp.Track(artist="Circa Survive", title="Get Out")
        library.root.joinpath("Circa Survive - Get Out.mp3").write_bytes(b"x" * 100)

        # A fresh library must pick the file up from the directory scan.
        rescanned = pp.MusicLibrary(self.root)
        self.assertIsNotNone(rescanned.existing(track))

    def test_missing_track_returns_none(self):
        library = pp.MusicLibrary(self.root)
        self.assertIsNone(library.existing(pp.Track(artist="A", title="B")))

    def test_ignores_zero_byte_files(self):
        library = pp.MusicLibrary(self.root)
        library.root.joinpath("A - B.mp3").write_bytes(b"")
        self.assertIsNone(library.existing(pp.Track(artist="A", title="B")))

    def test_path_for_uses_artist_dash_title(self):
        library = pp.MusicLibrary(self.root)
        track = pp.Track(artist="I The Mighty", title="Symphony of Skin")
        self.assertEqual(
            library.path_for(track).name, "I The Mighty - Symphony of Skin.mp3"
        )

    def test_title_containing_dash_round_trips(self):
        """A title with " - " inside must still be found by the index."""
        track = pp.Track(artist="Foo", title="Bar - Baz")
        expected = pp.MusicLibrary(self.root).path_for(track)
        expected.write_bytes(b"x" * 100)

        rescanned = pp.MusicLibrary(self.root)
        found = rescanned.existing(track)
        self.assertIsNotNone(found)
        self.assertEqual(found.name, "Foo - Bar - Baz.mp3")


class FakeYouTubeDL:
    """Minimal stand-in for yt_dlp.YoutubeDL used by the finder tests."""

    responses = {}
    calls = []

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        FakeYouTubeDL.calls.append(url)
        for pattern, payload in FakeYouTubeDL.responses.items():
            if pattern in url:
                return payload
        return {"entries": []}


class FinderTests(unittest.TestCase):
    def setUp(self):
        FakeYouTubeDL.calls = []
        FakeYouTubeDL.responses = {}
        self._real = pp.yt_dlp.YoutubeDL
        pp.yt_dlp.YoutubeDL = FakeYouTubeDL
        self.addCleanup(lambda: setattr(pp.yt_dlp, "YoutubeDL", self._real))

    def test_search_returns_candidates(self):
        FakeYouTubeDL.responses["music.youtube.com/search"] = {
            "entries": [
                {"id": "4ZYsNXDJrZ4", "title": "Ember", "ie_key": "Youtube"},
                {"id": "5tR5mjHhxrg", "title": "Ember (Instrumental)", "ie_key": "Youtube"},
            ]
        }
        finder = pp.YouTubeMusicFinder()
        candidates = finder.search("Dance Gavin Dance", "Ember")
        self.assertEqual(len(candidates), 2)
        self.assertTrue(candidates[0].url.endswith("v=4ZYsNXDJrZ4"))

    def test_search_ignores_non_video_entries(self):
        FakeYouTubeDL.responses["music.youtube.com/search"] = {
            "entries": [
                {"id": "UCm3F5ZIYJ4CcDCmtgGhOFXQ", "title": "x", "ie_key": "YoutubeTab"},
                {"id": "short", "title": "y", "ie_key": "Youtube"},
            ]
        }
        finder = pp.YouTubeMusicFinder()
        self.assertEqual(finder.search("a", "b"), [])

    def test_search_raises_runtime_error_on_failure(self):
        class Boom(FakeYouTubeDL):
            def extract_info(self, url, download=False):
                raise ValueError("network down")

        pp.yt_dlp.YoutubeDL = Boom
        finder = pp.YouTubeMusicFinder()
        with self.assertRaises(RuntimeError):
            finder.search("a", "b")

    def test_find_picks_best_matching_candidate(self):
        def payload(url, download=False):
            if "search" in url:
                return {
                    "entries": [
                        {"id": "aaaaaaaaaaa", "title": "Ember", "ie_key": "Youtube"},
                        {"id": "bbbbbbbbbbb", "title": "Totally Different", "ie_key": "Youtube"},
                    ]
                }
            if "aaaaaaaaaaa" in url:
                return {
                    "track": "Ember",
                    "artist": "Dance Gavin Dance",
                    "album": "Jackpot Juicer",
                    "duration": 226,
                }
            return {"track": "Totally Different", "artist": "Someone Else"}

        class Stub(FakeYouTubeDL):
            def extract_info(self, url, download=False):
                return payload(url, download)

        pp.yt_dlp.YoutubeDL = Stub
        finder = pp.YouTubeMusicFinder()
        track = pp.Track(artist="Dance Gavin Dance", title="Ember")
        best = finder.find(track)
        self.assertIsNotNone(best)
        self.assertEqual(best.video_id, "aaaaaaaaaaa")
        self.assertEqual(best.album, "Jackpot Juicer")

    def test_find_returns_none_without_results(self):
        FakeYouTubeDL.responses["music.youtube.com/search"] = {"entries": []}
        finder = pp.YouTubeMusicFinder()
        self.assertIsNone(finder.find(pp.Track(artist="A", title="B")))


class DownloaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_download_moves_file_to_final_name(self):
        """The audio is written to a temp dir, then moved to the target name."""
        written = {}

        class FakeYDL:
            def __init__(self, opts):
                written["opts"] = opts

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def extract_info(self, url, download=False):
                # Simulate yt-dlp writing the post-processed MP3.
                template = written["opts"]["outtmpl"]
                out = Path(template.replace("%(ext)s", "mp3"))
                out.write_bytes(b"audio-data")
                return {"id": "x"}

        real = pp.yt_dlp.YoutubeDL
        pp.yt_dlp.YoutubeDL = FakeYDL
        self.addCleanup(lambda: setattr(pp.yt_dlp, "YoutubeDL", real))

        downloader = pp.Downloader()
        candidate = pp.Candidate(video_id="4ZYsNXDJrZ4")
        destination = self.root / "Foo - 100% Pure Love.mp3"
        result = downloader.download(candidate, destination)

        self.assertEqual(result, destination)
        self.assertTrue(result.exists())
        self.assertEqual(result.read_bytes(), b"audio-data")
        # The output template must not contain the final (unsafe) name.
        self.assertNotIn("%", written["opts"]["outtmpl"].replace("%(ext)s", ""))
        # No stray files are left behind.
        self.assertEqual([p.name for p in self.root.iterdir()], [destination.name])

    def test_raises_when_no_mp3_produced(self):
        class FakeYDL:
            def __init__(self, opts):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def extract_info(self, url, download=False):
                return {"id": "x"}

        real = pp.yt_dlp.YoutubeDL
        pp.yt_dlp.YoutubeDL = FakeYDL
        self.addCleanup(lambda: setattr(pp.yt_dlp, "YoutubeDL", real))

        with self.assertRaises(RuntimeError):
            pp.Downloader().download(
                pp.Candidate(video_id="x"), self.root / "A - B.mp3"
            )


class PipelineTests(unittest.TestCase):
    """Tests the orchestration logic with stubbed finder/downloader."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.library = pp.MusicLibrary(self.root)

    def _pirater(self, finder, downloader, **kwargs):
        return pp.PlaylistPirater(
            library=self.library, finder=finder, downloader=downloader, **kwargs
        )

    def test_skips_track_already_in_library(self):
        track = pp.Track(artist="A", title="B")
        self.library.root.joinpath("A - B.mp3").write_bytes(b"x" * 10)

        class Finder:
            def find(self, track):
                raise AssertionError("should not search for an existing track")

        result = self._pirater(Finder(), None).process(track)
        self.assertEqual(result.status, "skipped")
        self.assertIsNotNone(result.path)

    def test_downloads_and_registers_track(self):
        class Finder:
            def find(self, track):
                return pp.Candidate(video_id="4ZYsNXDJrZ4", title="B", artist="A")

        class Downloader:
            def download(self, candidate, destination):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.with_suffix(".mp3").write_bytes(b"audio" * 100)
                return destination.with_suffix(".mp3")

        track = pp.Track(artist="A", title="B")
        result = self._pirater(Finder(), Downloader()).process(track)
        self.assertEqual(result.status, "downloaded")
        self.assertTrue(result.path.exists())
        # A second pass must now skip it.
        again = self._pirater(Finder(), Downloader()).process(track)
        self.assertEqual(again.status, "skipped")

    def test_reports_not_found(self):
        class Finder:
            def find(self, track):
                return None

        result = self._pirater(Finder(), None).process(pp.Track(artist="A", title="B"))
        self.assertEqual(result.status, "not-found")

    def test_reports_error_and_retries(self):
        attempts = {"n": 0}

        class Finder:
            def find(self, track):
                return pp.Candidate(video_id="4ZYsNXDJrZ4")

        class Downloader:
            def download(self, candidate, destination):
                attempts["n"] += 1
                raise OSError("boom")

        pirater = self._pirater(Finder(), Downloader(), retries=1)
        result = pirater.process(pp.Track(artist="A", title="B"))
        self.assertEqual(result.status, "error")
        self.assertEqual(attempts["n"], 2)  # initial attempt + 1 retry

    def test_dry_run_does_not_download(self):
        class Finder:
            def find(self, track):
                return pp.Candidate(video_id="4ZYsNXDJrZ4", title="B", artist="A")

        class Downloader:
            def download(self, candidate, destination):
                raise AssertionError("must not download in dry-run mode")

        result = self._pirater(Finder(), Downloader(), dry_run=True).process(
            pp.Track(artist="A", title="B")
        )
        self.assertEqual(result.status, "dry-run")
        self.assertEqual(list(self.root.glob("*.mp3")), [])

    def test_stats_counting(self):
        stats = pp.Stats()
        track = pp.Track(artist="A", title="B")
        stats.add(pp.Result(track, "downloaded"))
        stats.add(pp.Result(track, "skipped"))
        stats.add(pp.Result(track, "not-found"))
        stats.add(pp.Result(track, "error"))
        stats.add(pp.Result(track, "dry-run"))
        self.assertEqual(
            (stats.downloaded, stats.skipped, stats.not_found, stats.errors, stats.dry_run),
            (1, 1, 1, 1, 1),
        )


class StatusLineTests(unittest.TestCase):
    """The live bottom status line."""

    class FakeTTY(io.StringIO):
        def isatty(self):
            return True

    def test_tty_rewrites_line_in_place(self):
        stream = self.FakeTTY()
        status = pp.StatusLine(stream)
        status.update("searching YouTube Music for 'Ember'")
        status.update("downloading 50%")
        # Both updates live on the same physical line.
        self.assertEqual(stream.getvalue().count("\n"), 0)
        self.assertIn("\r", stream.getvalue())
        self.assertIn("downloading 50%", stream.getvalue())

    def test_tty_does_not_repeat_identical_text(self):
        stream = self.FakeTTY()
        status = pp.StatusLine(stream)
        status.update("downloading 50%")
        first = stream.getvalue()
        status.update("downloading 50%")
        self.assertEqual(stream.getvalue(), first)

    def test_tty_clear_erases_the_line(self):
        stream = self.FakeTTY()
        status = pp.StatusLine(stream)
        status.update("downloading 50%")
        status.clear()
        self.assertTrue(stream.getvalue().endswith("\r"))
        # The clearing blanks are written after the content.
        self.assertIn(" " * 10, stream.getvalue())

    def test_non_tty_prints_only_milestones(self):
        stream = io.StringIO()  # isatty() is False
        status = pp.StatusLine(stream)
        status.update("searching", milestone=True)
        status.update("downloading 10%")  # not a milestone
        status.update("downloading 50%")  # not a milestone
        value = stream.getvalue()
        self.assertIn("searching", value)
        self.assertNotIn("downloading", value)

    def test_non_tty_milestones_are_separate_lines(self):
        stream = io.StringIO()
        status = pp.StatusLine(stream)
        status.update("one", milestone=True)
        status.update("two", milestone=True)
        self.assertEqual(stream.getvalue().splitlines(), ["one", "two"])

    def test_whitespace_is_collapsed(self):
        stream = self.FakeTTY()
        status = pp.StatusLine(stream)
        status.update("a   b\nc")
        self.assertIn("a b c", stream.getvalue())


class StatusReporterTests(unittest.TestCase):
    def test_none_callback_is_a_noop(self):
        report = pp.make_status_reporter(None)
        report("hello", milestone=True)

    def test_one_argument_callback_is_supported(self):
        seen = []
        report = pp.make_status_reporter(seen.append)
        report("hello", milestone=True)
        self.assertEqual(seen, ["hello"])

    def test_milestone_callback_receives_the_flag(self):
        seen = []
        report = pp.make_status_reporter(
            lambda text, milestone=False: seen.append((text, milestone))
        )
        report("hello", milestone=True)
        report("bye", milestone=False)
        self.assertEqual(seen, [("hello", True), ("bye", False)])


class FormattingTests(unittest.TestCase):
    def test_format_bytes(self):
        self.assertEqual(pp.format_bytes(512), "512 B")
        self.assertEqual(pp.format_bytes(1024), "1.0 KiB")
        self.assertEqual(pp.format_bytes(1024 * 1024 * 3.5), "3.5 MiB")
        self.assertEqual(pp.format_bytes(None), "?")

    def test_format_eta(self):
        self.assertEqual(pp.format_eta(3), "00:03")
        self.assertEqual(pp.format_eta(75), "01:15")
        self.assertEqual(pp.format_eta(3661), "1:01:01")
        self.assertEqual(pp.format_eta(None), "--:--")


class DownloadProgressTests(unittest.TestCase):
    """The yt-dlp progress hook drives the status line."""

    def test_hook_reports_percentage_and_speed(self):
        seen = []
        downloader = pp.Downloader(status=seen.append)
        hook = downloader._opts("%(ext)s")["progress_hooks"][0]
        hook(
            {
                "status": "downloading",
                "downloaded_bytes": 1024 * 1024,
                "total_bytes": 2 * 1024 * 1024,
                "speed": 1024 * 1024,
                "eta": 1,
            }
        )
        self.assertIn("downloading", seen[0])
        self.assertIn("50%", seen[0])
        self.assertIn("1.0 MiB/2.0 MiB", seen[0])

    def test_hook_handles_unknown_total(self):
        seen = []
        downloader = pp.Downloader(status=seen.append)
        hook = downloader._opts("%(ext)s")["progress_hooks"][0]
        hook({"status": "downloading", "downloaded_bytes": 2048})
        self.assertIn("downloading", seen[0])
        self.assertNotIn("%", seen[0])

    def test_hook_reports_finished_and_error(self):
        seen = []
        downloader = pp.Downloader(status=seen.append)
        hook = downloader._opts("%(ext)s")["progress_hooks"][0]
        hook({"status": "finished"})
        hook({"status": "error"})
        self.assertIn("converting", seen[0])
        self.assertIn("failed", seen[1])

    def test_ytdlp_progress_bar_is_disabled(self):
        """yt-dlp's own bar would fight with the status line."""
        opts = pp.Downloader()._opts("%(ext)s")
        self.assertTrue(opts["noprogress"])
        self.assertIn("progress_hooks", opts)


class PipelineStatusTests(unittest.TestCase):
    """The pipeline keeps the permanent log and drives the status line."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.library = pp.MusicLibrary(Path(self.tmp.name) / "mus")

    def _tracks(self):
        return [pp.Track(artist="A", title="B")]

    def test_run_keeps_permanent_log_and_clears_status_line(self):
        messages = []

        class FakeStatus:
            def update(self, text, milestone=False):
                messages.append(text)

            def clear(self):
                messages.append("<clear>")

        class Finder:
            prefix = ""

            def find(self, track):
                return pp.Candidate(video_id="x", title="B", artist="A")

        class Downloader:
            prefix = ""

            def download(self, candidate, destination):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"audio")
                return destination

        pirater = pp.PlaylistPirater(
            library=self.library,
            finder=Finder(),
            downloader=Downloader(),
            status=FakeStatus(),
        )

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            stats = pirater.run(self._tracks())

        self.assertEqual(stats.downloaded, 1)
        # The permanent log still contains the numbered progress marker.
        self.assertIn("[   1/1]", out.getvalue())
        self.assertIn("A - B", out.getvalue())
        # The status line was cleared before the permanent log was written.
        self.assertIn("<clear>", messages)

    def test_run_sets_the_track_counter_on_the_components(self):
        seen = []

        class Finder:
            prefix = ""

            def find(self, track):
                return pp.Candidate(video_id="x", title="B", artist="A")

        class Downloader:
            prefix = ""

            def download(self, candidate, destination):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"audio")
                return destination

        finder = Finder()
        downloader = Downloader()
        pirater = pp.PlaylistPirater(
            library=self.library, finder=finder, downloader=downloader
        )
        pirater.run(self._tracks())

        # The components learn which track is being worked on.
        self.assertEqual(finder.prefix, "[1/1]")
        self.assertEqual(downloader.prefix, "[1/1]")


class FinderStatusTests(unittest.TestCase):
    def test_search_reports_what_it_is_looking_for(self):
        seen = []

        class FakeYDL:
            def __init__(self, opts):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def extract_info(self, url, download=False):
                return {"entries": []}

        real = pp.yt_dlp.YoutubeDL
        pp.yt_dlp.YoutubeDL = FakeYDL
        self.addCleanup(lambda: setattr(pp.yt_dlp, "YoutubeDL", real))

        finder = pp.YouTubeMusicFinder(status=seen.append)
        finder.search("Dance Gavin Dance", "Ember")
        self.assertTrue(seen)
        self.assertIn("Dance Gavin Dance - Ember", seen[0])
        self.assertIn("searching", seen[0].lower())


class ReportTests(unittest.TestCase):
    def test_writes_csv_report(self):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False)
        tmp.close()
        path = Path(tmp.name)
        self.addCleanup(lambda: path.unlink(missing_ok=True))

        track = pp.Track(artist="A", title="B", album="C", position=7)
        pp.write_report([pp.Result(track, "downloaded", "ok", path=path)], path)

        content = path.read_text()
        self.assertIn("artist", content)
        self.assertIn("A", content)
        self.assertIn("downloaded", content)


class M3uPlaylistTests(unittest.TestCase):
    def test_playlist_name_uses_local_prefix(self):
        self.assertEqual(pp.m3u_playlist_name(Path("example.csv")), "local_example.csv")

    def test_playlist_name_is_sanitised(self):
        name = pp.m3u_playlist_name(Path("a/b.csv"))
        self.assertNotIn("/", name)
        self.assertTrue(name.startswith("local_"))

    def test_entries_are_relative_to_music_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "music"
            root.mkdir()
            track = pp.Track(artist="A", title="B")
            path = root / "A - B.mp3"
            path.write_bytes(b"x")
            results = [pp.Result(track, "downloaded", "ok", path=path)]
            self.assertEqual(
                pp.m3u_playlist_entries(results, root), ["A - B.mp3"]
            )

    def test_entries_can_be_absolute(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "music"
            root.mkdir()
            track = pp.Track(artist="A", title="B")
            path = root / "A - B.mp3"
            path.write_bytes(b"x")
            results = [pp.Result(track, "downloaded", "ok", path=path)]

            entries = pp.m3u_playlist_entries(results, root, absolute=True)

            self.assertEqual(len(entries), 1)
            self.assertTrue(Path(entries[0]).is_absolute())
            self.assertTrue(Path(entries[0]).is_file())

    def test_entries_exclude_failed_tracks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "music"
            root.mkdir()
            track = pp.Track(artist="A", title="B")
            results = [
                pp.Result(track, "not-found", "no match"),
                pp.Result(track, "error", "boom"),
            ]
            self.assertEqual(pp.m3u_playlist_entries(results, root), [])

    def test_entries_include_skipped_tracks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "music"
            root.mkdir()
            track = pp.Track(artist="A", title="B")
            path = root / "A - B.mp3"
            path.write_bytes(b"x")
            results = [pp.Result(track, "skipped", "already downloaded", path=path)]
            self.assertEqual(pp.m3u_playlist_entries(results, root), ["A - B.mp3"])

    def test_entry_starting_with_hash_is_not_treated_as_comment(self):
        """A leading "#" would make a player skip the line, so avoid it."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "music"
            root.mkdir()
            path = root / "#1 - Song.mp3"
            path.write_bytes(b"x")
            track = pp.Track(artist="#1", title="Song")
            results = [pp.Result(track, "downloaded", "ok", path=path)]
            entries = pp.m3u_playlist_entries(results, root)
            self.assertEqual(len(entries), 1)
            self.assertFalse(entries[0].startswith("#"))
            self.assertTrue(Path(entries[0]).is_file())

    def test_writes_m3u_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "music"
            root.mkdir()
            playlist_dir = Path(tmp) / "playlists"
            path = root / "A - B.mp3"
            path.write_bytes(b"x")
            track = pp.Track(artist="A", title="B")
            results = [pp.Result(track, "downloaded", "ok", path=path)]

            target = pp.write_m3u_playlist(
                results, root, playlist_dir, "local_example.csv"
            )

            self.assertEqual(target.name, "local_example.csv.m3u")
            self.assertTrue(target.is_file())
            lines = target.read_text(encoding="utf-8").splitlines()
            self.assertIn("A - B.mp3", lines)
            # Comments are ignored by the players.
            self.assertTrue(lines[0].startswith("#"))

    def test_writer_can_write_absolute_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "music"
            root.mkdir()
            track = pp.Track(artist="A", title="B")
            path = root / "A - B.mp3"
            path.write_bytes(b"x")
            writer = pp.M3uPlaylistWriter(
                root, Path(tmp) / "playlists", "local_test.csv", absolute=True
            )
            writer.set_tracks([track])
            writer.add_track(track, path)

            target = writer.write()
            lines = [
                line
                for line in target.read_text(encoding="utf-8").splitlines()
                if line and not line.startswith("#")
            ]

            self.assertEqual(len(lines), 1)
            self.assertTrue(Path(lines[0]).is_absolute())

    def test_written_playlist_parses_like_a_player(self):
        """The file must follow the rules of a common M3U reader."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "music"
            root.mkdir()
            playlist_dir = Path(tmp) / "playlists"
            path = root / "A - B.mp3"
            path.write_bytes(b"x")
            track = pp.Track(artist="A", title="B")
            results = [pp.Result(track, "downloaded", "ok", path=path)]
            target = pp.write_m3u_playlist(
                results, root, playlist_dir, "local_example.csv"
            )

            entries = []
            for line in target.read_text(encoding="utf-8").splitlines():
                # Players skip empty lines and comments.
                if not line or line.startswith("#"):
                    continue
                entries.append(line)

            self.assertEqual(entries, ["A - B.mp3"])
            for entry in entries:
                self.assertTrue((root / entry).is_file())


class LivePlaylistTests(unittest.TestCase):
    """The playlist must grow while the download is still running."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.music = self.root / "music"
        self.music.mkdir()
        self.playlists = self.root / "playlists"

    def _writer(self, tracks=()):
        writer = pp.M3uPlaylistWriter(self.music, self.playlists, "local_test.csv")
        writer.set_tracks(tracks)
        return writer

    def _lines(self, writer):
        target = writer.write()
        return [
            line
            for line in target.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        ]

    def test_existing_files_are_added_before_downloading(self):
        (self.music / "A - B.mp3").write_bytes(b"x" * 10)
        (self.music / "C - D.mp3").write_bytes(b"x" * 10)
        track = pp.Track(artist="A", title="B", position=1)

        writer = self._writer([track])
        library = pp.MusicLibrary(self.music)
        pp.seed_playlist(writer, [track], library)
        writer.write()

        lines = self._lines(writer)
        self.assertEqual(len(lines), 1)
        self.assertIn("A - B.mp3", lines)

    def test_playlist_is_written_to_disk_when_seeded(self):
        (self.music / "A - B.mp3").write_bytes(b"x" * 10)
        track = pp.Track(artist="A", title="B", position=1)
        writer = self._writer([track])
        pp.seed_playlist(writer, [track], pp.MusicLibrary(self.music))
        writer.write()

        target = self.playlists / "local_test.csv.m3u"
        self.assertTrue(target.is_file())
        self.assertIn("A - B.mp3", target.read_text(encoding="utf-8"))

    def test_track_finished_during_download_is_added_immediately(self):
        track = pp.Track(artist="New", title="Song", position=1)
        writer = self._writer([track])
        writer.add_track(track, self.music / "New - Song.mp3")
        writer.write()

        lines = self._lines(writer)
        self.assertEqual(lines, ["New - Song.mp3"])

    def test_playlist_keeps_the_order_of_the_csv(self):
        first = pp.Track(artist="A", title="1", position=1)
        second = pp.Track(artist="B", title="2", position=2)
        third = pp.Track(artist="C", title="3", position=3)
        writer = self._writer([first, second, third])

        # The tracks are added in an arbitrary order.
        writer.add_track(third, self.music / "C - 3.mp3")
        writer.add_track(first, self.music / "A - 1.mp3")
        writer.add_track(second, self.music / "B - 2.mp3")

        self.assertEqual(
            self._lines(writer), ["A - 1.mp3", "B - 2.mp3", "C - 3.mp3"]
        )

    def test_unrelated_library_tracks_are_never_added(self):
        (self.music / "Z - Extra.mp3").write_bytes(b"x" * 10)
        track = pp.Track(artist="A", title="B", position=1)
        writer = self._writer([track])

        library = pp.MusicLibrary(self.music)
        pp.seed_playlist(writer, [track], library)

        self.assertEqual(self._lines(writer), [])

    def test_adding_the_same_track_twice_does_not_duplicate(self):
        track = pp.Track(artist="A", title="B", position=1)
        writer = self._writer([track])
        writer.add_track(track, self.music / "A - B.mp3")
        writer.add_track(track, self.music / "A - B.mp3")
        self.assertEqual(len(writer), 1)

    def test_a_track_of_the_csv_is_listed_once_even_when_added_twice(self):
        track = pp.Track(artist="A", title="B", position=1)
        writer = self._writer([track])
        writer.add_track(track, self.music / "A - B.mp3")
        writer.add_track(track, self.music / "A - B.mp3")
        self.assertEqual(self._lines(writer), ["A - B.mp3"])

    def test_a_track_that_is_not_in_the_csv_is_ignored(self):
        known = pp.Track(artist="A", title="B", position=1)
        unknown = pp.Track(artist="X", title="Y", position=2)
        writer = self._writer([known])

        writer.add_track(unknown, self.music / "X - Y.mp3")

        self.assertEqual(self._lines(writer), [])
        self.assertEqual(len(writer), 0)


class SeedPlaylistTests(unittest.TestCase):
    """Seeding must add the CSV tracks only and keep their order."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.music = Path(self.tmp.name) / "music"
        self.music.mkdir()

    def _seed(self, tracks):
        writer = pp.M3uPlaylistWriter(
            self.music, Path(self.tmp.name) / "playlists", "local_test.csv"
        )
        library = pp.MusicLibrary(self.music)
        added = pp.seed_playlist(writer, tracks, library)
        writer.write()
        target = writer.target
        lines = [
            line
            for line in target.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        ]
        return added, lines

    def test_csv_track_is_not_listed_twice(self):
        (self.music / "A - B.mp3").write_bytes(b"x" * 10)
        track = pp.Track(artist="A", title="B", position=1)

        added, lines = self._seed([track])

        self.assertEqual(added, 1)
        self.assertEqual(lines, ["A - B.mp3"])

    def test_extra_files_are_not_added(self):
        (self.music / "A - B.mp3").write_bytes(b"x" * 10)
        (self.music / "Z - Extra.mp3").write_bytes(b"x" * 10)
        track = pp.Track(artist="A", title="B", position=1)

        added, lines = self._seed([track])

        self.assertEqual(added, 1)
        self.assertEqual(lines, ["A - B.mp3"])

    def test_the_order_of_the_csv_is_kept(self):
        (self.music / "B - 2.mp3").write_bytes(b"x" * 10)
        (self.music / "A - 1.mp3").write_bytes(b"x" * 10)
        first = pp.Track(artist="A", title="1", position=1)
        second = pp.Track(artist="B", title="2", position=2)

        added, lines = self._seed([first, second])

        self.assertEqual(added, 2)
        self.assertEqual(lines, ["A - 1.mp3", "B - 2.mp3"])

    def test_two_csv_rows_matching_the_same_file_list_it_once(self):
        (self.music / "A - B.mp3").write_bytes(b"x" * 10)
        first = pp.Track(artist="A", title="B", position=1)
        second = pp.Track(artist="A", title="B", position=2)

        added, lines = self._seed([first, second])

        self.assertEqual(added, 1)
        self.assertEqual(lines, ["A - B.mp3"])

    def test_csv_rows_without_a_file_are_left_out(self):
        (self.music / "A - B.mp3").write_bytes(b"x" * 10)
        present = pp.Track(artist="A", title="B", position=1)
        missing = pp.Track(artist="C", title="D", position=2)

        added, lines = self._seed([present, missing])

        self.assertEqual(added, 1)
        self.assertEqual(lines, ["A - B.mp3"])

    def test_empty_files_are_not_added(self):
        (self.music / "A - B.mp3").write_bytes(b"")
        added, lines = self._seed([])
        self.assertEqual(added, 0)
        self.assertEqual(lines, [])

    def test_non_audio_files_are_not_added(self):
        (self.music / "notes.txt").write_text("hello")
        added, lines = self._seed([])
        self.assertEqual(added, 0)
        self.assertEqual(lines, [])


class OnReadyCallbackTests(unittest.TestCase):
    """The pirater must announce every finished track immediately."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.music = Path(self.tmp.name) / "music"
        self.music.mkdir()

    def _pirater(self, finder, downloader=None, **kwargs):
        return pp.PlaylistPirater(
            library=pp.MusicLibrary(self.music),
            finder=finder,
            downloader=downloader,
            **kwargs,
        )

    def test_called_for_downloaded_track(self):
        seen = []

        class Finder:
            def find(self, track):
                return pp.Candidate(video_id="4ZYsNXDJrZ4", title="B", artist="A")

        class Downloader:
            def download(self, candidate, destination):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.with_suffix(".mp3").write_bytes(b"audio" * 100)
                return destination.with_suffix(".mp3")

        pirater = self._pirater(
            Finder(),
            Downloader(),
            on_ready=lambda track, path: seen.append((track, path)),
        )
        result = pirater.process(pp.Track(artist="A", title="B"))

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][1], result.path)

    def test_called_for_skipped_track(self):
        seen = []
        (self.music / "A - B.mp3").write_bytes(b"x" * 10)

        class Finder:
            def find(self, track):
                raise AssertionError("should not search for an existing track")

        pirater = self._pirater(
            Finder(), None, on_ready=lambda track, path: seen.append((track, path))
        )
        pirater.process(pp.Track(artist="A", title="B"))

        self.assertEqual(len(seen), 1)

    def test_not_called_for_failed_track(self):
        seen = []

        class Finder:
            def find(self, track):
                return None

        pirater = self._pirater(
            Finder(), None, on_ready=lambda track, path: seen.append((track, path))
        )
        pirater.process(pp.Track(artist="A", title="B"))

        self.assertEqual(seen, [])

    def test_broken_callback_does_not_break_the_run(self):
        class Finder:
            def find(self, track):
                return None

        def boom(track, path):
            raise RuntimeError("nope")

        pirater = self._pirater(Finder(), None, on_ready=boom)
        result = pirater.process(pp.Track(artist="A", title="B"))
        self.assertEqual(result.status, "not-found")


class CliTests(unittest.TestCase):
    def test_missing_csv_exits_with_error(self):
        self.assertEqual(pp.main(["/nonexistent/file.csv"]), 2)

    def test_help_works(self):
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                pp.main(["--help"])
        self.assertEqual(ctx.exception.code, 0)

    def test_absolute_paths_flag_is_parsed(self):
        args = pp.build_parser().parse_args(["a.csv", "--absolute-paths"])
        self.assertTrue(args.absolute_paths)

    def test_absolute_paths_flag_is_off_by_default(self):
        args = pp.build_parser().parse_args(["a.csv"])
        self.assertFalse(args.absolute_paths)

    def test_playlist_dir_flag_is_parsed(self):
        args = pp.build_parser().parse_args(
            ["a.csv", "--m3u-playlist-dir", "/tmp/pl", "--no-m3u-playlist"]
        )
        self.assertEqual(args.m3u_playlist_dir, Path("/tmp/pl"))
        self.assertTrue(args.no_m3u_playlist)


if __name__ == "__main__":
    unittest.main()
