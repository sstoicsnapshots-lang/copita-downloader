"""Adopting browser-downloaded files (blob: downloads the extension can't
forward as a URL — Telegram Web media, etc.) into Copita's library, and the
clear failure a pasted t.me / web.telegram.org link now gives instead of
saving a 16 KB channel-avatar thumbnail.
"""
import asyncio
import os
import tempfile
import unittest

from copita.core.classifier import classify_url
from copita.core.task_manager import TaskManager, _category_for_filename


class CategoryHelperTests(unittest.TestCase):
    def test_maps_extensions_to_categories(self):
        self.assertEqual(_category_for_filename("clip.MP4"), "video")
        self.assertEqual(_category_for_filename("song.m4a"), "audio")
        self.assertEqual(_category_for_filename("book.epub"), "document")
        self.assertEqual(_category_for_filename("pack.zip"), "archive")
        self.assertEqual(_category_for_filename("pic.webp"), "image")
        self.assertEqual(_category_for_filename("mystery.xyz"), "file")


class TelegramClassifyTests(unittest.TestCase):
    def test_telegram_links_route_to_a_dedicated_engine(self):
        for u in ("https://t.me/somechannel/1234",
                  "https://t.me/c/1234567890/55",
                  "https://web.telegram.org/k/#-1001234567890"):
            self.assertEqual(classify_url(u)["engine"], "telegram_link", u)


class ImportLocalFileTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dl = os.path.join(self._tmp.name, "downloads")
        self.mgr = TaskManager(self.dl)
        # somewhere under a "home-like" path the importer will accept
        self.src_dir = os.path.join(self._tmp.name, "browser-downloads")
        os.makedirs(self.src_dir)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    def _make(self, name, data=b"\x00\x00\x00 ftypisom" + b"x" * 4000):
        p = os.path.join(self.src_dir, name)
        with open(p, "wb") as f:
            f.write(data)
        return p

    async def test_moves_file_into_library_and_adds_completed_task(self):
        src = self._make("Telegram.mp4")
        task = self.mgr.import_local_file(src, source_url="https://web.telegram.org/k/")
        self.assertFalse(os.path.exists(src), "source should be moved, not left behind")
        self.assertTrue(os.path.isfile(task.output_path))
        self.assertEqual(os.path.dirname(task.output_path), os.path.abspath(self.dl))
        self.assertEqual(task.status, "completed")
        self.assertEqual(task.category, "video")
        self.assertEqual(task.percent, 100.0)
        self.assertIn(task.id, self.mgr.tasks)

    async def test_move_false_copies_and_leaves_the_browser_file(self):
        # The /api/import-file path passes move=False so a browser download
        # doesn't get yanked out from under the browser (Chrome then shows
        # it as "Deleted").
        src = self._make("KeptInBrowser.mp4")
        task = self.mgr.import_local_file(src, move=False)
        self.assertTrue(os.path.isfile(src), "browser's copy must stay put")
        self.assertTrue(os.path.isfile(task.output_path))
        self.assertNotEqual(os.path.abspath(src), os.path.abspath(task.output_path))
        self.assertEqual(task.status, "completed")

    async def test_name_collision_gets_a_suffix(self):
        self.mgr.import_local_file(self._make("dup.mp4"))
        t2 = self.mgr.import_local_file(self._make("dup.mp4"))
        self.assertTrue(os.path.basename(t2.output_path).startswith("dup_"))
        self.assertEqual(len(os.listdir(self.dl)), 2)

    async def test_rejects_a_path_outside_home(self):
        with self.assertRaises(ValueError):
            self.mgr.import_local_file("/etc/hosts")

    async def test_rejects_a_missing_file(self):
        with self.assertRaises(ValueError):
            self.mgr.import_local_file(os.path.join(self.src_dir, "nope.mp4"))


if __name__ == "__main__":
    unittest.main()
