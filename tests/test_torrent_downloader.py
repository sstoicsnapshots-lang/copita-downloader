"""Regression checks for magnet/torrent link classification and the
TorrentDownloader wrapper around libtorrent.

Live, network-dependent end-to-end verification (DHT metadata resolution,
real peer/seed discovery, real download progress, and clean cancellation)
was done manually against the canonical open-source Big Buck Bunny magnet
(CC-BY, the standard test torrent used across the BitTorrent ecosystem) —
these tests cover the fast, deterministic logic: URL routing, timeouts, and
cancellation, using a fake libtorrent session so they don't need network
access or a live swarm.
"""
import asyncio
import tempfile
import unittest
from unittest.mock import patch

from copita.core.classifier import classify_url
from copita.core.torrent_downloader import TorrentDownloader

MAGNET = "magnet:?xt=urn:btih:dd8255ecdc7ca55fb0bbf81323d87062db1f6d1c&dn=Big+Buck+Bunny"


class ClassifierRoutingTests(unittest.TestCase):
    def test_magnet_link_routes_to_torrent_engine(self):
        result = classify_url(MAGNET)
        self.assertEqual(result["engine"], "torrent")
        self.assertEqual(result["category"], "torrent")
        self.assertEqual(result["name"], "Big Buck Bunny")

    def test_torrent_file_url_routes_to_torrent_engine(self):
        result = classify_url("https://example.com/files/some-file.torrent?x=1")
        self.assertEqual(result["engine"], "torrent")
        self.assertEqual(result["category"], "torrent")
        self.assertEqual(result["name"], "some-file.torrent")


class _FakeStatus:
    def __init__(self, has_metadata=False, progress=0.0, is_seeding=False,
                 total_wanted_done=0, total_wanted=1000, download_rate=0,
                 num_peers=0, num_seeds=0, name="Fake Torrent", save_path="/tmp"):
        self.has_metadata = has_metadata
        self.progress = progress
        self.is_seeding = is_seeding
        self.total_wanted_done = total_wanted_done
        self.total_wanted = total_wanted
        self.download_rate = download_rate
        self.num_peers = num_peers
        self.num_seeds = num_seeds
        self.name = name
        self.save_path = save_path


class _FakeHandle:
    def __init__(self, statuses):
        self._statuses = list(statuses)
        self._i = 0

    def status(self):
        # Repeats the last status once the scripted sequence is exhausted.
        s = self._statuses[min(self._i, len(self._statuses) - 1)]
        self._i += 1
        return s

    def torrent_file(self):
        return None


class TorrentDownloaderTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_raises_clear_error_when_metadata_never_resolves(self):
        """Regression target: a dead/unseeded magnet must fail with an
        honest error instead of hanging the task forever."""
        handle = _FakeHandle([_FakeStatus(has_metadata=False)])
        with tempfile.TemporaryDirectory() as out_dir:
            downloader = TorrentDownloader(MAGNET, output_dir=out_dir)
            downloader.METADATA_TIMEOUT = 0.05
            downloader.POLL_INTERVAL = 0.01
            with self.assertRaisesRegex(RuntimeError, "metadata"):
                await asyncio.get_running_loop().run_in_executor(
                    None, downloader._wait_for_metadata, handle)

    async def test_raises_clear_error_when_download_stalls(self):
        """A torrent that finds metadata but then has no seeders left to
        actually download from must also fail clearly, not hang forever."""
        handle = _FakeHandle([_FakeStatus(has_metadata=True, progress=0.1,
                                           total_wanted_done=100, num_peers=2, num_seeds=0)])
        with tempfile.TemporaryDirectory() as out_dir:
            downloader = TorrentDownloader(MAGNET, output_dir=out_dir)
            downloader.STALL_TIMEOUT = 0.05
            downloader.POLL_INTERVAL = 0.01
            with self.assertRaisesRegex(RuntimeError, "No download progress"):
                await asyncio.get_running_loop().run_in_executor(
                    None, downloader._wait_for_completion, handle, "Fake Torrent")

    async def test_cancellation_stops_metadata_wait_immediately(self):
        handle = _FakeHandle([_FakeStatus(has_metadata=False)])
        with tempfile.TemporaryDirectory() as out_dir:
            downloader = TorrentDownloader(MAGNET, output_dir=out_dir)
            downloader.METADATA_TIMEOUT = 999
            downloader.POLL_INTERVAL = 0.01
            downloader.cancelled = True
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                await asyncio.get_running_loop().run_in_executor(
                    None, downloader._wait_for_metadata, handle)

    async def test_reports_progress_with_peer_and_seed_counts(self):
        statuses = [
            _FakeStatus(has_metadata=True, progress=0.5, total_wanted_done=500,
                        total_wanted=1000, download_rate=12345, num_peers=5, num_seeds=3),
            _FakeStatus(has_metadata=True, progress=1.0, is_seeding=True,
                        total_wanted_done=1000, total_wanted=1000),
        ]
        handle = _FakeHandle(statuses)
        events = []
        with tempfile.TemporaryDirectory() as out_dir:
            downloader = TorrentDownloader(MAGNET, output_dir=out_dir, progress_callback=events.append)
            downloader.POLL_INTERVAL = 0.01
            await asyncio.get_running_loop().run_in_executor(
                None, downloader._wait_for_completion, handle, "Fake Torrent")

        self.assertTrue(any(e["status"] == "downloading" and e["speed"] == 12345.0 for e in events))
        self.assertTrue(any("5 peers, 3 seeds" in e["message"] for e in events))


if __name__ == "__main__":
    unittest.main()
