"""Scratch project → .sb3 — real download against Scratch's public API."""
import asyncio
import os
import tempfile
import unittest
import zipfile

from copita.core.classifier import classify_url
from copita.core.scratch_downloader import ScratchDownloader, project_id

# A large, shared, public project (Uncannyblocks Band Different).
LIVE_PROJECT = "https://scratch.mit.edu/projects/822188577/editor/"


class ScratchClassifierTests(unittest.TestCase):
    def test_editor_and_plain_urls_route_to_scratch_engine(self):
        for u in ("https://scratch.mit.edu/projects/822188577/editor/",
                  "https://scratch.mit.edu/projects/822188577",
                  "http://scratch.mit.edu/projects/1/"):
            self.assertEqual(classify_url(u)["engine"], "scratch", u)

    def test_non_project_scratch_url_does_not_match(self):
        self.assertNotEqual(classify_url("https://scratch.mit.edu/")["engine"], "scratch")

    def test_project_id_extraction(self):
        self.assertEqual(project_id(LIVE_PROJECT), "822188577")
        self.assertIsNone(project_id("https://scratch.mit.edu/search/"))


class ScratchDownloadLiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_rebuilds_a_valid_sb3_from_the_api(self):
        with tempfile.TemporaryDirectory() as d:
            try:
                path = await ScratchDownloader(LIVE_PROJECT, d).download()
            except Exception as e:
                self.skipTest(f"Scratch API unreachable / rate-limited: {e}")
            self.assertTrue(path.endswith(".sb3"))
            self.assertGreater(os.path.getsize(path), 100_000)
            with zipfile.ZipFile(path) as z:
                self.assertIsNone(z.testzip(), "corrupt .sb3")
                names = z.namelist()
                self.assertIn("project.json", names)
                # every other entry is an asset named <md5>.<ext>
                assets = [n for n in names if n != "project.json"]
                self.assertGreater(len(assets), 50)
                self.assertTrue(all("." in n for n in assets))


if __name__ == "__main__":
    unittest.main()
