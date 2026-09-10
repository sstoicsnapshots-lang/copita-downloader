"""Regression check: when a generic/unrecognized URL is resolved via the
deep media sniffer, the task's title and category (and therefore the saved
filename and how it's filed/iconified in the UI) should reflect what the
sniffer actually found — not the original page's generic "webpage"/domain
classification made before we knew what was really there. A real, correctly
downloaded MP3 was showing up filed as a plain generic "file" because
task.category was never refreshed alongside the URL/engine.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from copita.core.task_manager import TaskManager


class TaskTitleFromSnifferTests(unittest.IsolatedAsyncioTestCase):
    async def test_title_updates_to_sniffed_page_title_not_bare_domain(self):
        fake_sniff_result = {
            "title": "Wizard's First Rule Audiobook by Terry Goodkind",
            "media": [{"url": "https://cdn.example.com/preview123.mp3", "type": "audio", "label": "Sample"}],
        }

        async def fake_start(self):
            Path(self.output_path).write_bytes(b"fake mp3 bytes")
            return self.output_path

        with tempfile.TemporaryDirectory() as _tmp:
            manager = TaskManager(os.path.join(_tmp, "dl"))
            with patch("copita.core.sniffer.MediaSniffer.sniff_url", return_value=fake_sniff_result), \
                 patch("copita.core.chunk_downloader.ChunkDownloader.start", fake_start):
                task = manager.add_task("https://www.audible.com/pd/Some-Book/B00X")
                await task.async_task

            self.assertNotEqual(task.title, "www.audible.com")
            self.assertIn("Wizard's First Rule", task.title)
            self.assertEqual(task.status, "completed")
            # Regression: this used to stay "webpage" (from the original
            # generic classification), filing a real, correctly downloaded
            # MP3 as a plain generic file in the UI instead of as audio.
            self.assertEqual(task.category, "audio")
            saved_path = Path(task.output_path)
            self.assertTrue(saved_path.exists())
            self.assertNotEqual(saved_path.name, "www.audible.com")
            # Regression: the page title never carries the real file
            # extension, so the saved file ended up extensionless (e.g.
            # "Audible Cloud Player" with no ".mp3") — correct category
            # internally, but Finder/other apps saw an unopenable generic
            # file since there was no extension to associate it with anything.
            self.assertTrue(saved_path.name.lower().endswith(".mp3"), saved_path.name)

    async def test_generic_app_shell_title_still_gets_real_extension(self):
        """Reproduces the exact real-world case: a JS single-page-app's
        static <title> is a generic app name ("Audible Cloud Player"), not
        the actual book/content title — and it has no file extension at all."""
        fake_sniff_result = {
            "title": "Audible Cloud Player",
            "media": [{"url": "https://samples.audible.com/bk/potr/000573/bk_potr_000573_sample.mp3",
                       "type": "audio", "label": "Sample"}],
        }

        async def fake_start(self):
            Path(self.output_path).write_bytes(b"fake mp3 bytes")
            return self.output_path

        with tempfile.TemporaryDirectory() as _tmp:
            manager = TaskManager(os.path.join(_tmp, "dl"))
            with patch("copita.core.sniffer.MediaSniffer.sniff_url", return_value=fake_sniff_result), \
                 patch("copita.core.chunk_downloader.ChunkDownloader.start", fake_start):
                task = manager.add_task("https://www.audible.com/webplayer?asin=B0F14RPXHR&isSample=true")
                await task.async_task

            self.assertEqual(task.category, "audio")
            self.assertEqual(Path(task.output_path).name, "Audible Cloud Player.mp3")


if __name__ == "__main__":
    unittest.main()
