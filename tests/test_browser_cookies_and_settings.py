"""Preferences now persist across backend restarts, and a picked browser's
logged-in session reaches the non-yt-dlp engines (chunk / HLS / sniffer) as
a Cookie header — previously it only reached yt-dlp."""
import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

from copita.core.task_manager import TaskManager
from copita.core import browser_cookies


class SettingsPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dl = os.path.join(self._tmp.name, "downloads")

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_preferences_survive_a_restart(self):
        m = TaskManager(self.dl)
        m.cookies_browser = "brave"
        m.default_threads = 8
        await m.set_max_concurrent(5)  # calls save_settings()

        m2 = TaskManager(self.dl)  # fresh "process"
        self.assertEqual(m2.cookies_browser, "brave")
        self.assertEqual(m2.default_threads, 8)
        self.assertEqual(m2.max_concurrent_downloads, 5)

    async def test_defaults_when_no_settings_file(self):
        m = TaskManager(self.dl)
        self.assertEqual(m.cookies_browser, "auto")
        self.assertEqual(m.max_concurrent_downloads, 3)

    async def test_proxy_speed_and_verify_persist_and_apply(self):
        from copita.core import http_client
        from copita.core.speed_gate import GLOBAL
        m = TaskManager(self.dl)
        m.proxy_url = "socks5://127.0.0.1:9050"
        m.verify_ssl = False
        m.max_speed_kbps = 500
        m.apply_http_config()
        m.save_settings()
        self.assertEqual(http_client.current_proxy(), "socks5://127.0.0.1:9050")
        self.assertFalse(http_client.current_verify_ssl())
        self.assertEqual(GLOBAL.rate_bytes, 500 * 1024)

        m2 = TaskManager(self.dl)  # fresh process
        self.assertEqual(m2.proxy_url, "socks5://127.0.0.1:9050")
        self.assertFalse(m2.verify_ssl)
        self.assertEqual(m2.max_speed_kbps, 500)
        # cleanup shared module state
        http_client.configure(proxy=None, verify_ssl=True)
        GLOBAL.set_limit_kbps(0)

    async def test_corrupt_settings_file_does_not_crash_launch(self):
        os.makedirs(self.dl, exist_ok=True)
        with open(os.path.join(os.path.dirname(self.dl), ".copita_settings.json"), "w") as f:
            f.write("{not json")
        m = TaskManager(self.dl)  # must not raise
        self.assertEqual(m.cookies_browser, "auto")


class BrowserCookieBridgeTests(unittest.TestCase):
    def test_unsupported_or_missing_browser_yields_empty(self):
        self.assertEqual(browser_cookies.cookie_header_for("https://x.com/", None), "")
        self.assertEqual(browser_cookies.cookie_header_for("https://x.com/", "netscape-navigator"), "")

    def test_jar_read_failure_is_swallowed(self):
        browser_cookies.clear_cache()
        with patch.object(browser_cookies._ytc, "extract_cookies_from_browser",
                          side_effect=Exception("locked DB")):
            self.assertEqual(browser_cookies.cookie_header_for("https://x.com/", "chrome"), "")

    def test_youtube_is_cookie_free_by_default(self):
        # YouTube is left cookie-free for the *first* attempt (on a clean IP a
        # logged-in session can trip a different check). When YouTube then
        # rate-limits with "confirm you're not a bot", ytdlp_wrapper escalates
        # to the browser session on the retry — that path bypasses this
        # resolver deliberately (see _extract_metadata). So the invariant here
        # is just: the default resolution is cookie-free.
        for yt in ("https://www.youtube.com/watch?v=abc",
                   "https://youtu.be/abc",
                   "https://music.youtube.com/watch?v=abc"):
            self.assertIsNone(browser_cookies.resolve_browser("brave", yt), yt)
            self.assertIsNone(browser_cookies.resolve_browser("auto", yt), yt)
            self.assertTrue(browser_cookies.is_cookie_averse(yt), yt)
        # every other host still honours an explicit pick
        self.assertEqual(browser_cookies.resolve_browser("brave", "https://vimeo.com/1"), "brave")
        self.assertFalse(browser_cookies.is_cookie_averse("https://vimeo.com/1"))

    def test_cookie_header_built_for_url_domain(self):
        import http.cookiejar as cj

        class FakeJar(cj.CookieJar):
            def add_cookie_header(self, request):
                request.add_unredirected_header("Cookie", "session=abc123")

        browser_cookies.clear_cache()
        with patch.object(browser_cookies._ytc, "extract_cookies_from_browser",
                          return_value=FakeJar()):
            h = browser_cookies.cookie_header_for("https://members.example.com/video", "firefox")
        self.assertEqual(h, "session=abc123")


class CookieInjectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dl = os.path.join(self._tmp.name, "downloads")

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_picked_browser_cookies_reach_the_chunk_engine(self):
        captured = {}

        class FakeChunk:
            def __init__(self, url, output_path, num_connections=1, headers=None, progress_callback=None):
                captured["headers"] = headers or {}
                self.output_path = output_path

            async def start(self):
                return self.output_path

        m = TaskManager(self.dl)
        m.cookies_browser = "brave"
        with patch("copita.core.task_manager.ChunkDownloader", FakeChunk), \
             patch("copita.core.browser_cookies.cookie_header_for", return_value="sid=XYZ"), \
             patch("copita.core.task_manager._correct_extension_from_content", side_effect=lambda p: p):
            task = m.add_task("https://members.example.com/lecture01.mp4")
            for _ in range(50):
                await asyncio.sleep(0.02)
                if task.status in ("completed", "failed"):
                    break

        self.assertEqual(task.status, "completed")
        self.assertEqual(captured["headers"].get("Cookie"), "sid=XYZ")


if __name__ == "__main__":
    unittest.main()
