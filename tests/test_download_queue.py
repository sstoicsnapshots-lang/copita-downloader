"""Regression checks for the max-simultaneous-downloads queue.

Previously every task fired off in full parallel the instant it was added —
paste 10 links and 10 downloads started at once, each already using up to 16
HTTP connections. IDM/JDownloader cap simultaneous downloads and queue the
rest; TaskManager now does the same via a manual gate (a plain asyncio
Semaphore can't be resized at runtime, which matters for the "raise the
limit while downloads are already queued" case).
"""
import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

from copita.core.task_manager import TaskManager


class _ControlledDownloader:
    """Stands in for ChunkDownloader: blocks in start() until the test lets
    it finish, so concurrency can be observed and controlled deterministically."""
    instances = []

    def __init__(self, url, output_path, num_connections=1, headers=None, progress_callback=None):
        self.url = url
        self.output_path = output_path
        self.release_event = asyncio.Event()
        _ControlledDownloader.instances.append(self)

    async def start(self):
        await self.release_event.wait()
        return self.output_path


class DownloadQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _ControlledDownloader.instances = []
        self._tmp = tempfile.TemporaryDirectory()
        # Nest the download dir so TaskManager's sibling .copita_tasks.json
        # lands inside this tempdir (and gets cleaned up), not in the shared
        # /var/folders temp root where it would accumulate forever.
        self.download_dir = os.path.join(self._tmp.name, "dl")
        self.patcher = patch("copita.core.task_manager.ChunkDownloader", _ControlledDownloader)
        self.patcher.start()

    async def asyncTearDown(self):
        self.patcher.stop()
        self._tmp.cleanup()

    async def test_only_max_concurrent_tasks_run_at_once_the_rest_queue(self):
        manager = TaskManager(self.download_dir)
        manager.max_concurrent_downloads = 2

        tasks = [manager.add_task(f"https://example.com/file{i}.bin") for i in range(3)]
        # Let the event loop run each task up to the point it either starts
        # downloading or blocks waiting for a slot.
        await asyncio.sleep(0.05)

        statuses = [t.status for t in tasks]
        self.assertEqual(statuses.count("queued"), 1,
                          f"expected exactly 1 task still queued, got statuses={statuses}")
        self.assertEqual(len(_ControlledDownloader.instances), 2,
                          "only 2 ChunkDownloaders should have been constructed while the 3rd waits")

        # Finish one of the two running downloads — the queued one should
        # immediately take its place.
        _ControlledDownloader.instances[0].release_event.set()
        await asyncio.sleep(0.05)

        self.assertEqual(len(_ControlledDownloader.instances), 3,
                          "the queued task should start as soon as a slot frees up")

        for inst in _ControlledDownloader.instances:
            inst.release_event.set()
        await asyncio.sleep(0.05)
        self.assertTrue(all(t.status == "completed" for t in tasks))

    async def test_raising_the_limit_immediately_unblocks_queued_tasks(self):
        manager = TaskManager(self.download_dir)
        manager.max_concurrent_downloads = 1

        tasks = [manager.add_task(f"https://example.com/file{i}.bin") for i in range(3)]
        await asyncio.sleep(0.05)
        self.assertEqual(len(_ControlledDownloader.instances), 1)

        # Raising the cap should let the other two start without needing an
        # existing download to finish first.
        await manager.set_max_concurrent(3)
        await asyncio.sleep(0.05)
        self.assertEqual(len(_ControlledDownloader.instances), 3)

        for inst in _ControlledDownloader.instances:
            inst.release_event.set()
        await asyncio.sleep(0.05)
        self.assertTrue(all(t.status == "completed" for t in tasks))


if __name__ == "__main__":
    unittest.main()
