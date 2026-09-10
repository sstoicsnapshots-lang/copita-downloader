"""Regression checks for recursive Google Drive folder downloads.

Previously _download_folder filtered `res["children"]` down to
`not c.get("is_folder")`, silently discarding every subfolder — a Drive
folder with subfolders would only ever download its top-level files.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from copita.core.filehoster_downloader import FileHosterDownloader


def _file(name, url="https://example.com/f"):
    return {"id": name, "name": name, "is_folder": False, "url": url, "category": "file"}


def _folder(name, node_id=None):
    return {"id": node_id or name, "name": name, "is_folder": True, "url": "unused", "category": "file"}


class GdriveRecursiveCrawlTests(unittest.IsolatedAsyncioTestCase):
    async def test_crawl_descends_into_subfolders_instead_of_dropping_them(self):
        top_children = [_file("root.txt"), _folder("Day 1", "day1id")]
        listings = {"day1id": ("Day 1", [_file("lecture.mp4")])}

        with tempfile.TemporaryDirectory() as out_dir:
            downloader = FileHosterDownloader("https://drive.google.com/drive/folders/x", out_dir)
            with patch.object(FileHosterDownloader, "_list_gdrive_folder_items",
                               side_effect=lambda fid: listings[fid]):
                jobs, skipped = await downloader._crawl_gdrive_folder(top_children)

        self.assertEqual(skipped, 0)
        by_name = {j["name"]: j for j in jobs}
        self.assertEqual(by_name["root.txt"]["rel_dir"], [])
        self.assertEqual(by_name["lecture.mp4"]["rel_dir"], ["Day 1"])

    async def test_crawl_reconstructs_multi_level_nesting(self):
        top_children = [_folder("Day 2", "day2id")]
        listings = {
            "day2id": ("Day 2", [_folder("Challenge", "challengeid")]),
            "challengeid": ("Challenge", [_folder("Agents", "agentsid")]),
            "agentsid": ("Agents", [_file("agent.json")]),
        }

        with tempfile.TemporaryDirectory() as out_dir:
            downloader = FileHosterDownloader("https://drive.google.com/drive/folders/x", out_dir)
            with patch.object(FileHosterDownloader, "_list_gdrive_folder_items",
                               side_effect=lambda fid: listings[fid]):
                jobs, skipped = await downloader._crawl_gdrive_folder(top_children)

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["rel_dir"], ["Day 2", "Challenge", "Agents"])

    async def test_crawl_stops_at_max_depth_and_reports_skipped_count(self):
        with tempfile.TemporaryDirectory() as out_dir:
            downloader = FileHosterDownloader("https://drive.google.com/drive/folders/x", out_dir)
            downloader._GDRIVE_MAX_DEPTH = 2

            def _fake_list(fid):
                # Each folder contains one more nested folder, forever.
                return (fid, [_folder("next", f"{fid}_next")])

            with patch.object(FileHosterDownloader, "_list_gdrive_folder_items", side_effect=_fake_list):
                jobs, skipped = await downloader._crawl_gdrive_folder([_folder("a", "a")])

        self.assertEqual(jobs, [])
        self.assertGreaterEqual(skipped, 1)

    async def test_download_folder_writes_nested_files_and_avoids_collisions(self):
        top_children = [_file("notes.pdf"), _folder("ModA", "a"), _folder("ModB", "b")]
        listings = {
            "a": ("ModA", [_file("notes.pdf")]),
            "b": ("ModB", [_file("notes.pdf")]),
        }

        async def fake_start(self):
            Path(self.output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(self.output_path).write_bytes(b"x")
            return self.output_path

        with tempfile.TemporaryDirectory() as out_dir:
            downloader = FileHosterDownloader("https://drive.google.com/drive/folders/x", out_dir)
            res = {
                "folder_name": "MyFolder", "folder_id": "x",
                "children": top_children, "headers": {},
            }
            with patch.object(FileHosterDownloader, "_list_gdrive_folder_items",
                               side_effect=lambda fid: listings[fid]), \
                 patch("copita.core.chunk_downloader.ChunkDownloader.start", fake_start):
                target_dir = await downloader._download_folder(res)

            self.assertTrue(Path(target_dir, "notes.pdf").exists())
            self.assertTrue(Path(target_dir, "ModA", "notes.pdf").exists())
            self.assertTrue(Path(target_dir, "ModB", "notes.pdf").exists())


if __name__ == "__main__":
    unittest.main()
