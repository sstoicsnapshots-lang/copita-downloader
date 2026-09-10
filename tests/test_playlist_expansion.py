"""Regression checks for playlist / multi-track link expansion.

Pasting a playlist link (a YouTube playlist, a SoundCloud set, an archive.org
item with several audio tracks, ...) used to silently download only the first
item — `_extract_metadata` forces `noplaylist: True`, and archive.org item
pages fell through to the sniffer which grabs the first media it finds.

TaskManager now flat-probes such URLs first: more than one entry -> one child
download per item, and the pasted row is dropped; a single item -> normal
download; an oversized playlist -> a clear, visible refusal instead of a
partial, silent result.
"""
import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

from copita.core.task_manager import TaskManager, _PLAYLIST_TASK_CAP


class _InstantDownloader:
    """Stands in for ChunkDownloader: completes immediately."""
    instances = []

    def __init__(self, url, output_path, num_connections=1, headers=None, progress_callback=None):
        self.url = url
        self.output_path = output_path
        _InstantDownloader.instances.append(self)

    async def start(self):
        return self.output_path


def _probe(entries, title="A Playlist", total=None):
    return {
        "is_playlist": len(entries) > 1,
        "title": title,
        "entries": [{"url": u, "title": t, "id": None} for u, t in entries],
        "total": total if total is not None else len(entries),
    }


class PlaylistExpansionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _InstantDownloader.instances = []
        self._tmp = tempfile.TemporaryDirectory()
        # Nest the download dir so TaskManager's sibling state file
        # (.copita_tasks.json, written next to the download dir) lands inside
        # this test's own temp tree, not the shared system temp root.
        self.download_dir = os.path.join(self._tmp.name, "downloads")
        self._p1 = patch("copita.core.task_manager.ChunkDownloader", _InstantDownloader)
        self._p1.start()

    async def asyncTearDown(self):
        self._p1.stop()
        self._tmp.cleanup()

    async def test_playlist_link_expands_into_one_task_per_item(self):
        manager = TaskManager(self.download_dir)
        probe = _probe([
            ("https://archive.org/download/item/00_track.mp3", "00 - Preface"),
            ("https://archive.org/download/item/01_track.mp3", "01 - Chapter One"),
            ("https://archive.org/download/item/02_track.mp3", "02 - Chapter Two"),
        ], title="John G. Paton, Missionary")

        with patch("copita.core.task_manager.YtDlpWrapper.probe_entries", return_value=probe):
            parent = manager.add_task("https://archive.org/details/johngpatonmissionary3")
            for _ in range(50):
                await asyncio.sleep(0.02)
                others = [t for t in manager.tasks.values() if t.id != parent.id]
                if others and all(t.status in ("completed", "failed") for t in others):
                    break

        # Parent row is gone; one child per entry remains.
        self.assertNotIn(parent.id, manager.tasks)
        children = list(manager.tasks.values())
        self.assertEqual(len(children), 3)
        self.assertEqual(
            sorted(c.title for c in children),
            ["00 - Preface", "01 - Chapter One", "02 - Chapter Two"],
        )
        for c in children:
            self.assertTrue(c.options.get("_playlist_child"))
        # Child filenames carry the entry title + the real extension.
        saved = sorted(c.options.get("filename") for c in children)
        self.assertEqual(saved, ["00 - Preface.mp3", "01 - Chapter One.mp3", "02 - Chapter Two.mp3"])
        # Every item is filed into one folder named after the playlist, and
        # the actual file on disk lands there.
        for c in children:
            self.assertEqual(c.options.get("subdir"), "John G. Paton, Missionary")
            self.assertTrue(
                os.path.dirname(c.output_path).endswith("John G. Paton, Missionary"),
                c.output_path,
            )
            self.assertTrue(os.path.isdir(os.path.dirname(c.output_path)))

    async def test_children_share_a_group_id_for_the_folded_ui_row(self):
        manager = TaskManager(self.download_dir)
        probe = _probe([
            ("https://archive.org/download/item/a.mp3", "Ep 1"),
            ("https://archive.org/download/item/b.mp3", "Ep 2"),
            ("https://archive.org/download/item/c.mp3", "Ep 3"),
        ], title="Abu Antar")
        with patch("copita.core.task_manager.YtDlpWrapper.probe_entries", return_value=probe):
            parent = manager.add_task("https://archive.org/details/abuantar")
            for _ in range(50):
                await asyncio.sleep(0.02)
                others = [t for t in manager.tasks.values() if t.id != parent.id]
                if others and all(t.status in ("completed", "failed") for t in others):
                    break

        children = list(manager.tasks.values())
        gids = {c.group_id for c in children}
        self.assertEqual(len(gids), 1)                       # one shared group
        self.assertTrue(next(iter(gids)).startswith("pl-"))
        self.assertTrue(all(c.group_title == "Abu Antar" for c in children))
        self.assertEqual(sorted(c.group_index for c in children), [1, 2, 3])
        self.assertTrue(all(c.group_total == 3 for c in children))
        # and it survives the to_dict the UI actually reads
        d = next(c.to_dict() for c in children)
        self.assertEqual(d["group"]["title"], "Abu Antar")
        self.assertEqual(d["group"]["total"], 3)

    async def test_playlist_folder_prefixes_unnumbered_items_for_ordering(self):
        # A YouTube-style playlist whose entries are NOT already numbered gets
        # a positional prefix so the folder lists in playlist order.
        manager = TaskManager(self.download_dir)
        probe = _probe([
            ("https://archive.org/download/item/a.mp3", "Intro"),
            ("https://archive.org/download/item/b.mp3", "The Middle Bit"),
            ("https://archive.org/download/item/c.mp3", "Wrap Up"),
        ], title="My Course")
        with patch("copita.core.task_manager.YtDlpWrapper.probe_entries", return_value=probe):
            parent = manager.add_task("https://archive.org/details/mycourse")
            for _ in range(50):
                await asyncio.sleep(0.02)
                others = [t for t in manager.tasks.values() if t.id != parent.id]
                if others and all(t.status in ("completed", "failed") for t in others):
                    break
        saved = sorted(c.options.get("filename") for c in manager.tasks.values())
        self.assertEqual(saved, ["01 - Intro.mp3", "02 - The Middle Bit.mp3", "03 - Wrap Up.mp3"])
        for c in manager.tasks.values():
            self.assertEqual(c.options.get("subdir"), "My Course")

    async def test_archive_details_url_with_trailing_slug_is_normalised_and_expands(self):
        # The browser address bar / archive.org's player append a non-file
        # slug for the selected track: /details/<item>/<item>. yt-dlp reads
        # that as a single-file request; we must normalise it back to the
        # item page before probing.
        manager = TaskManager(self.download_dir)
        probe = _probe([
            ("https://archive.org/download/item/00_track.mp3", "00 - Preface"),
            ("https://archive.org/download/item/01_track.mp3", "01 - Chapter One"),
        ], title="An Audiobook")
        seen = {}

        def _fake_probe(self, url):
            seen["url"] = url
            return probe

        with patch("copita.core.task_manager.YtDlpWrapper.probe_entries", _fake_probe):
            parent = manager.add_task(
                "https://archive.org/details/johngpatonmissionary3_2606_librivox/johngpatonmissionary")
            for _ in range(50):
                await asyncio.sleep(0.02)
                others = [t for t in manager.tasks.values() if t.id != parent.id]
                if others and all(t.status in ("completed", "failed") for t in others):
                    break

        self.assertEqual(seen["url"],
                         "https://archive.org/details/johngpatonmissionary3_2606_librivox")
        self.assertNotIn(parent.id, manager.tasks)
        self.assertEqual(len(manager.tasks), 2)

    async def test_archive_download_file_url_is_left_as_a_single_file(self):
        manager = TaskManager(self.download_dir)
        with patch("copita.core.task_manager.YtDlpWrapper.probe_entries") as probe:
            manager.add_task(
                "https://archive.org/details/some_item/some_item_01_track.mp3")
            await asyncio.sleep(0.1)
        probe.assert_not_called()

    async def test_single_item_is_not_expanded(self):
        manager = TaskManager(self.download_dir)
        with patch("copita.core.task_manager.YtDlpWrapper.probe_entries",
                   return_value={"is_playlist": False, "entries": []}) as probe, \
             patch("copita.core.task_manager.YtDlpWrapper.download", return_value="/x/out.mp3"):
            parent = manager.add_task("https://soundcloud.com/artist/one-track")
            await asyncio.sleep(0.1)
        probe.assert_called_once()
        # The task stays — it wasn't torn apart into children.
        self.assertIn(parent.id, manager.tasks)
        self.assertEqual(len(manager.tasks), 1)

    async def test_youtube_watch_url_with_list_param_is_left_alone(self):
        manager = TaskManager(self.download_dir)
        with patch("copita.core.task_manager.YtDlpWrapper.probe_entries") as probe, \
             patch("copita.core.task_manager.YtDlpWrapper.download", return_value="/x/out.mp4"):
            manager.add_task("https://www.youtube.com/watch?v=abc123&list=PL0000000000")
            await asyncio.sleep(0.1)
        # A single-video watch URL must never trigger a playlist probe.
        probe.assert_not_called()

    async def test_oversized_playlist_is_refused_not_partially_downloaded(self):
        manager = TaskManager(self.download_dir)
        big = [(f"https://youtube.com/watch?v=v{i}", f"Video {i}")
               for i in range(_PLAYLIST_TASK_CAP + 5)]
        probe = _probe(big, title="Huge Playlist", total=_PLAYLIST_TASK_CAP + 5)

        with patch("copita.core.task_manager.YtDlpWrapper.probe_entries", return_value=probe):
            parent = manager.add_task("https://www.youtube.com/playlist?list=PLbig")
            await asyncio.sleep(0.15)

        self.assertIn(parent.id, manager.tasks)
        self.assertEqual(parent.status, "failed")
        self.assertIn(str(_PLAYLIST_TASK_CAP + 5), parent.error)
        # Nothing was queued behind the refusal.
        self.assertEqual(len(manager.tasks), 1)
        self.assertEqual(len(_InstantDownloader.instances), 0)

    async def test_spotify_album_expands_into_one_task_per_track(self):
        manager = TaskManager(self.download_dir)
        coll = {
            "kind": "album",
            "title": "Scorpion",
            "tracks": [
                {"id": "aaa", "title": "Survival", "artist": "Drake", "duration": 136},
                {"id": "bbb", "title": "Nonstop", "artist": "Drake", "duration": 238},
                {"id": "ccc", "title": "Elevate", "artist": "Drake", "duration": 184},
            ],
        }
        with patch("copita.core.spotify_downloader.SpotifyDownloader.get_collection",
                   return_value=coll), \
             patch("copita.core.spotify_downloader.SpotifyDownloader.download_track",
                   return_value="/x/out.mp3"):
            parent = manager.add_task("https://open.spotify.com/album/1ATL5GLyefJaxhQzSPVrLX")
            for _ in range(50):
                await asyncio.sleep(0.02)
                others = [t for t in manager.tasks.values() if t.id != parent.id]
                if len(others) == 3:
                    break

        self.assertNotIn(parent.id, manager.tasks)
        children = list(manager.tasks.values())
        self.assertEqual(len(children), 3)
        self.assertEqual(
            sorted(c.url for c in children),
            ["https://open.spotify.com/track/aaa",
             "https://open.spotify.com/track/bbb",
             "https://open.spotify.com/track/ccc"],
        )
        for c in children:
            self.assertTrue(c.options.get("_playlist_child"))
            self.assertEqual(c.engine, "spotify")

    async def test_spotify_single_track_is_not_expanded(self):
        manager = TaskManager(self.download_dir)
        with patch("copita.core.spotify_downloader.SpotifyDownloader.get_collection") as gc, \
             patch("copita.core.spotify_downloader.SpotifyDownloader.download_track",
                   return_value="/x/out.mp3"):
            parent = manager.add_task("https://open.spotify.com/track/2BVGOALdQbEoHdTDRnblPO")
            await asyncio.sleep(0.1)
        gc.assert_not_called()
        self.assertIn(parent.id, manager.tasks)

    async def test_youtube_search_results_page_is_rejected_not_treated_as_a_playlist(self):
        manager = TaskManager(self.download_dir)
        with patch("copita.core.task_manager.YtDlpWrapper.probe_entries") as probe:
            parent = manager.add_task("https://www.youtube.com/results?search_query=abou+antar")
            await asyncio.sleep(0.1)
        probe.assert_not_called()
        self.assertEqual(parent.status, "failed")
        self.assertIn("search page", parent.error)

    async def test_youtube_channel_url_is_not_expanded(self):
        manager = TaskManager(self.download_dir)
        with patch("copita.core.task_manager.YtDlpWrapper.probe_entries") as probe, \
             patch("copita.core.task_manager.YtDlpWrapper.download", return_value="/x/out.mp4"):
            manager.add_task("https://www.youtube.com/@SomeChannel")
            await asyncio.sleep(0.1)
        probe.assert_not_called()

    async def test_real_youtube_playlist_url_still_expands(self):
        manager = TaskManager(self.download_dir)
        probe = _probe([
            ("https://www.youtube.com/watch?v=a", "One"),
            ("https://www.youtube.com/watch?v=b", "Two"),
        ], title="A real playlist")
        with patch("copita.core.task_manager.YtDlpWrapper.probe_entries", return_value=probe), \
             patch("copita.core.task_manager.YtDlpWrapper.download", return_value="/x/o.mp4"):
            parent = manager.add_task(
                "https://youtube.com/playlist?list=PLnCGW8ADU1c9qufia82e7puyIwR4SrY_1&si=x")
            for _ in range(50):
                await asyncio.sleep(0.02)
                if parent.id not in manager.tasks:
                    break
        self.assertNotIn(parent.id, manager.tasks)
        self.assertGreaterEqual(len(manager.tasks), 2)

    async def test_child_task_does_not_re_expand(self):
        manager = TaskManager(self.download_dir)
        with patch("copita.core.task_manager.YtDlpWrapper.probe_entries") as probe, \
             patch("copita.core.task_manager.YtDlpWrapper.download", return_value="/x/out.mp4"):
            manager.add_task("https://youtube.com/playlist?list=PLx",
                             {"_playlist_child": True})
            await asyncio.sleep(0.05)
        probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
