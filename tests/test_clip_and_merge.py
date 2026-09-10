"""Time-range clip downloads (yt-dlp download_ranges) and DASH track merge."""
import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

from copita.core.ytdlp_wrapper import YtDlpWrapper
from copita.core.task_manager import TaskManager


class ClipOptionTests(unittest.TestCase):
    def test_clip_times_produce_download_ranges_and_keyframe_cuts(self):
        captured = {}

        class _FakeYDL:
            def __init__(self, opts): captured.update(opts)
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def extract_info(self, url, download=False):
                return {"title": "V", "duration": 100, "id": "x"}
            def prepare_filename(self, info): return "/tmp/x.mp4"
            def process_ie_result(self, info, download=True): return info

        with patch("copita.core.ytdlp_wrapper.yt_dlp.YoutubeDL", _FakeYDL), \
             patch("os.path.isfile", return_value=True):
            try:
                YtDlpWrapper().download("u", "/tmp/x.%(ext)s", clip_start=5, clip_end=12)
            except Exception:
                pass  # we only care about the opts that were built

        self.assertIn("download_ranges", captured)
        self.assertTrue(captured.get("force_keyframes_at_cuts"))

    def test_end_before_start_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "after the start"):
            YtDlpWrapper().download("u", "/tmp/x.%(ext)s", clip_start=30, clip_end=10)

    def test_clip_gets_a_distinct_output_name_from_the_full_download(self):
        # A clip of a video whose full version was saved earlier must not
        # resolve to the same filename — yt-dlp's clip re-encode would
        # overwrite the full download (real bug, 2026-09-08).
        class _Full:  options = {}
        class _Clip:  options = {"clip_start": 5, "clip_end": 12}
        class _Open:  options = {"clip_start": 90, "clip_end": None}
        full = TaskManager._ytdlp_outtmpl("/dl", "", _Full())
        clip = TaskManager._ytdlp_outtmpl("/dl", "", _Clip())
        self.assertNotEqual(full, clip)
        self.assertIn("[clip 5s-12s]", clip)
        self.assertIn("[clip from 1m30s]", TaskManager._ytdlp_outtmpl("/dl", "", _Open()))
        self.assertEqual(full, "/dl/%(title).80B.%(ext)s")


class MergeBranchTests(unittest.IsolatedAsyncioTestCase):
    async def test_merge_downloads_both_tracks_and_muxes(self):
        calls = []

        class _FakeChunk:
            def __init__(self, url, out, num_connections=1, headers=None, progress_callback=None):
                self.url, self.out = url, out
                calls.append(url)

            async def start(self):
                with open(self.out, "wb") as f:
                    f.write(b"x" * 10)
                return self.out

        def _fake_ffmpeg(cmd, capture_output=False):
            out = cmd[-1]
            with open(out, "wb") as f:
                f.write(b"MUXED")

            class R: returncode = 0
            return R()

        with tempfile.TemporaryDirectory() as d:
            m = TaskManager(os.path.join(d, "dl"))
            with patch("copita.core.task_manager.ChunkDownloader", _FakeChunk), \
                 patch("copita.core.task_manager.subprocess.run", _fake_ffmpeg):
                task = m.add_task("https://site.example/video-page", {
                    "merge": {"video": "https://cdn/v.mp4", "audio": "https://cdn/a.m4a"},
                })
                for _ in range(50):
                    await asyncio.sleep(0.02)
                    if task.status in ("completed", "failed"):
                        break

            self.assertEqual(task.status, "completed")
            self.assertEqual(calls, ["https://cdn/v.mp4", "https://cdn/a.m4a"])
            self.assertTrue(task.output_path.endswith(".mp4"))
            with open(task.output_path, "rb") as f:
                self.assertEqual(f.read(), b"MUXED")


if __name__ == "__main__":
    unittest.main()
