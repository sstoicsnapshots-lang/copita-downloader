"""Regression checks for task persistence across app/backend restarts.

Previously TaskManager kept all tasks purely in memory, so quitting the app
(which kills the Python backend) lost every task — including ones that were
half-downloaded — with no way to resume.
"""
import json
import os
import tempfile
import time
import unittest

from copita.core.task_manager import DownloadTask, TaskManager


class TaskPersistenceTests(unittest.TestCase):
    def test_completed_task_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as workspace:
            download_dir = os.path.join(workspace, "downloads")
            manager1 = TaskManager(download_dir)
            task = DownloadTask("abc123", "https://example.com/f")
            task.status = "completed"
            task.percent = 100.0
            task.output_path = os.path.join(download_dir, "f.bin")
            manager1.tasks[task.id] = task
            manager1._save_state_now()

            manager2 = TaskManager(download_dir)
            self.assertIn("abc123", manager2.tasks)
            self.assertEqual(manager2.tasks["abc123"].status, "completed")
            self.assertEqual(manager2.tasks["abc123"].percent, 100.0)

    def test_in_progress_task_becomes_a_retryable_failure_on_reload(self):
        """Regression: a task that was 'downloading' when the app closed
        used to vanish entirely. It should instead reappear as a clear,
        actionable failure the user can hit Retry on."""
        with tempfile.TemporaryDirectory() as workspace:
            download_dir = os.path.join(workspace, "downloads")
            manager1 = TaskManager(download_dir)
            task = DownloadTask("halfdone", "https://example.com/huge.zip")
            task.status = "downloading"
            task.percent = 42.0
            task.downloaded_bytes = 4200
            task.total_bytes = 10000
            manager1.tasks[task.id] = task
            manager1._save_state_now()

            manager2 = TaskManager(download_dir)
            reloaded = manager2.tasks["halfdone"]
            self.assertEqual(reloaded.status, "failed")
            self.assertIn("Retry", reloaded.error)
            self.assertEqual(reloaded.url, "https://example.com/huge.zip")

    def test_deleted_task_does_not_reappear_after_restart(self):
        with tempfile.TemporaryDirectory() as workspace:
            download_dir = os.path.join(workspace, "downloads")
            manager1 = TaskManager(download_dir)
            task = DownloadTask("gone", "https://example.com/f")
            manager1.tasks[task.id] = task
            manager1._save_state_now()
            del manager1.tasks["gone"]
            manager1._save_state_now()

            manager2 = TaskManager(download_dir)
            self.assertNotIn("gone", manager2.tasks)

    def test_completed_file_on_disk_is_not_duplicated_by_filesystem_scan(self):
        """A completed task loaded from state already accounts for its
        output file; the startup filesystem scan must not add a second,
        differently-ID'd task for that same physical file."""
        with tempfile.TemporaryDirectory() as workspace:
            download_dir = os.path.join(workspace, "downloads")
            manager1 = TaskManager(download_dir)  # constructed before the file exists
            fpath = os.path.join(download_dir, "movie.mp4")
            with open(fpath, "wb") as f:
                f.write(b"x" * 10)

            task = DownloadTask("movietask", "https://example.com/movie.mp4")
            task.status = "completed"
            task.percent = 100.0
            task.output_path = fpath
            manager1.tasks[task.id] = task
            manager1._save_state_now()

            manager2 = TaskManager(download_dir)
            paths = [t.output_path for t in manager2.tasks.values()]
            self.assertEqual(paths.count(fpath), 1)

    def test_save_state_is_throttled_but_terminal_events_save_immediately(self):
        with tempfile.TemporaryDirectory() as workspace:
            download_dir = os.path.join(workspace, "downloads")
            manager = TaskManager(download_dir)
            task = DownloadTask("t1", "https://example.com/f")
            manager.tasks[task.id] = task

            manager.broadcast("task_updated", task)  # non-terminal, in-progress default status "queued"...
            # Force the throttle window closed, then confirm a terminal
            # status change writes immediately regardless.
            manager._last_state_save = time.time()
            task.status = "completed"
            manager.broadcast("task_updated", task)

            with open(manager._state_path) as f:
                saved = json.load(f)
            self.assertEqual(saved[0]["status"], "completed")


if __name__ == "__main__":
    unittest.main()
