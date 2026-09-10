"""Copita scans the download folder on launch and shows what's there as
past downloads. It must only adopt things that are actually downloads — not
the logs / notes / CSVs / editor scratch files a real ~/Downloads collects,
which used to clutter the task list on every launch.
"""
import os
import tempfile
import unittest

from copita.core.task_manager import TaskManager
from copita.core.ytdlp_wrapper import YtDlpWrapper


class LibraryScanTests(unittest.TestCase):
    def test_only_real_download_types_are_adopted(self):
        with tempfile.TemporaryDirectory() as tmp:
            dl = os.path.join(tmp, "downloads")
            os.makedirs(dl)
            keep = ["Movie.mp4", "Song.m4a", "Album Track.mp3", "book.epub",
                    "photo.JPG", "archive.zip", "installer.dmg", "report.pdf"]
            drop = ["notes.txt", "run.log", "harness.py", "urls.csv",
                    "data.json", "README.md", "noextension", ".hidden",
                    "video.mp4.part", "clip_metadata.json"]
            for name in keep + drop:
                with open(os.path.join(dl, name), "wb") as f:
                    f.write(b"x" * 32)

            mgr = TaskManager(dl)
            titles = {t.title for t in mgr.tasks.values()}
            for name in keep:
                self.assertIn(name, titles, f"{name} should be adopted")
            for name in drop:
                self.assertNotIn(name, titles, f"{name} should NOT be adopted")

    def test_subfolder_of_tracks_is_adopted_as_one_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            dl = os.path.join(tmp, "downloads")
            book = os.path.join(dl, "The Ice Dragon (Audiobook)")
            os.makedirs(book)
            for n in range(1, 6):
                with open(os.path.join(book, f"Chapter {n}.mp3"), "wb") as f:
                    f.write(b"x" * 64)
            # a loose top-level file alongside
            with open(os.path.join(dl, "loose.mp4"), "wb") as f:
                f.write(b"x" * 64)

            mgr = TaskManager(dl)
            chapters = [t for t in mgr.tasks.values() if t.title.startswith("Chapter ")]
            self.assertEqual(len(chapters), 5)
            gids = {t.group_id for t in chapters}
            self.assertEqual(len(gids), 1, "all chapters share one group id")
            self.assertTrue(next(iter(gids)).startswith("dir-"))
            self.assertEqual({t.group_title for t in chapters},
                             {"The Ice Dragon (Audiobook)"})
            self.assertEqual({t.group_total for t in chapters}, {5})
            self.assertEqual(sorted(t.group_index for t in chapters), [1, 2, 3, 4, 5])
            loose = next(t for t in mgr.tasks.values() if t.title == "loose.mp4")
            self.assertIsNone(loose.group_id)

    def test_folder_a_torrent_task_already_owns_is_not_re_scanned(self):
        from copita.core.task_manager import DownloadTask
        with tempfile.TemporaryDirectory() as tmp:
            dl = os.path.join(tmp, "downloads")
            os.makedirs(dl)
            mgr = TaskManager(dl)  # empty folder, nothing adopted yet
            book = os.path.join(dl, "Some Audiobook")
            os.makedirs(book)
            for n in range(1, 4):
                with open(os.path.join(book, f"ch{n}.mp3"), "wb") as f:
                    f.write(b"x" * 64)
            # a completed multi-file torrent whose output IS the folder,
            # the way it comes back from state on the next launch
            t = DownloadTask("tor1", "magnet:?xt=urn:btih:abc")
            t.status, t.category, t.output_path = "completed", "torrent", book
            mgr.tasks["tor1"] = t
            mgr._load_existing_files()
            adopted = [x for x in mgr.tasks.values() if x.title.startswith("ch")]
            self.assertEqual(adopted, [], "torrent's own files must not be re-adopted")

    def test_lone_file_in_a_folder_is_not_grouped(self):
        with tempfile.TemporaryDirectory() as tmp:
            dl = os.path.join(tmp, "downloads")
            os.makedirs(os.path.join(dl, "solo"))
            with open(os.path.join(dl, "solo", "only.mp3"), "wb") as f:
                f.write(b"x" * 64)
            mgr = TaskManager(dl)
            only = next(t for t in mgr.tasks.values() if t.title == "only.mp3")
            self.assertIsNone(only.group_id)

    def test_category_follows_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            dl = os.path.join(tmp, "downloads")
            os.makedirs(dl)
            for name in ("track.m4a", "movie.mkv", "book.epub"):
                with open(os.path.join(dl, name), "wb") as f:
                    f.write(b"x" * 16)
            mgr = TaskManager(dl)
            cats = {t.title: t.category for t in mgr.tasks.values()}
            self.assertEqual(cats["track.m4a"], "audio")
            self.assertEqual(cats["movie.mkv"], "video")
            self.assertEqual(cats["book.epub"], "document")


class AudioOnlyDetectionTests(unittest.TestCase):
    def test_audio_only_when_no_format_has_video(self):
        info = {"formats": [
            {"format_id": "hls_mp3_128", "vcodec": "none", "acodec": "mp3"},
            {"format_id": "hls_aac_96", "vcodec": "none", "acodec": "aac"},
        ]}
        self.assertTrue(YtDlpWrapper._info_is_audio_only(info))

    def test_not_audio_only_when_a_video_format_exists(self):
        info = {"formats": [
            {"format_id": "audio", "vcodec": "none", "acodec": "aac"},
            {"format_id": "720p", "vcodec": "avc1", "acodec": "none"},
        ]}
        self.assertFalse(YtDlpWrapper._info_is_audio_only(info))

    def test_playlist_is_never_audio_only(self):
        self.assertFalse(YtDlpWrapper._info_is_audio_only(
            {"_type": "playlist", "entries": []}))


if __name__ == "__main__":
    unittest.main()
