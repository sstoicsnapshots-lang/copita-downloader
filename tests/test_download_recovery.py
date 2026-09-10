"""Deterministic regression checks for connection recovery and task retries."""
import asyncio
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from yt_dlp.utils import DownloadError
from copita.core.ytdlp_wrapper import YtDlpWrapper
from copita.core.task_manager import DownloadTask, TaskManager
from copita.core.chunk_downloader import ChunkDownloader


class MetadataRecoveryTests(unittest.TestCase):
    @patch('copita.core.ytdlp_wrapper.yt_dlp.YoutubeDL')
    def test_timeout_retries_metadata_over_ipv4(self, factory):
        ydl = factory.return_value.__enter__.return_value
        ydl.extract_info.side_effect = [DownloadError('Connection timed out'), {'title': 'Clip'}]
        info, opts = YtDlpWrapper()._extract_metadata('https://www.reddit.com/comments/1w773bo/', {})
        self.assertEqual(info['title'], 'Clip')
        self.assertEqual(opts['source_address'], '0.0.0.0')
        self.assertEqual(ydl.extract_info.call_count, 2)
        self.assertTrue(all(call.kwargs['download'] is False for call in ydl.extract_info.call_args_list))

    @patch('copita.core.ytdlp_wrapper.yt_dlp.YoutubeDL')
    def test_persistent_timeout_is_bounded_and_keeps_details(self, factory):
        ydl = factory.return_value.__enter__.return_value
        ydl.extract_info.side_effect = DownloadError('Connection timed out')
        with self.assertRaisesRegex(RuntimeError, 'Reddit did not respond') as error:
            YtDlpWrapper()._extract_metadata('https://www.reddit.com/comments/1w773bo/', {})
        self.assertIsInstance(error.exception.__cause__, DownloadError)
        self.assertEqual(ydl.extract_info.call_count, 2)

    @patch('copita.core.ytdlp_wrapper.yt_dlp.YoutubeDL')
    def test_access_restriction_is_not_retried_as_network_failure(self, factory):
        ydl = factory.return_value.__enter__.return_value
        ydl.extract_info.side_effect = DownloadError('HTTP Error 403: Forbidden')
        with self.assertRaisesRegex(RuntimeError, 'Reddit restricted this request'):
            YtDlpWrapper()._extract_metadata('https://www.reddit.com/comments/1w773bo/', {})
        self.assertEqual(ydl.extract_info.call_count, 1)

    @patch('copita.core.ytdlp_wrapper.yt_dlp.YoutubeDL')
    def test_unavailable_media_is_not_retried(self, factory):
        ydl = factory.return_value.__enter__.return_value
        ydl.extract_info.side_effect = DownloadError('Video has been deleted')
        with self.assertRaises(DownloadError):
            YtDlpWrapper()._extract_metadata('https://www.reddit.com/comments/1w773bo/', {})
        self.assertEqual(ydl.extract_info.call_count, 1)

    @patch('copita.core.browser_cookies.detect_default_browser', return_value=None)
    @patch('copita.core.ytdlp_wrapper.yt_dlp.YoutubeDL')
    def test_vimeo_login_wall_gets_a_clean_message(self, factory, _br):
        ydl = factory.return_value.__enter__.return_value
        ydl.extract_info.side_effect = DownloadError(
            'ERROR: [vimeo] 76979871: The web client only works when logged-in.')
        with self.assertRaisesRegex(RuntimeError, 'Vimeo now requires a signed-in session'):
            YtDlpWrapper()._extract_metadata('https://vimeo.com/76979871', {})

    @patch('copita.core.browser_cookies.detect_default_browser', return_value='brave')
    @patch('copita.core.ytdlp_wrapper.yt_dlp.YoutubeDL')
    def test_youtube_age_restricted_gets_a_clean_message_after_cookie_retry(self, factory, _br):
        ydl = factory.return_value.__enter__.return_value
        ydl.extract_info.side_effect = [
            DownloadError('ERROR: [youtube] 7qy30k22sYE: Sign in to confirm your age.'),
            DownloadError('ERROR: [youtube] 7qy30k22sYE: Sorry, this content is age-restricted'),
        ]
        with self.assertRaisesRegex(RuntimeError, 'age-restricted'):
            YtDlpWrapper()._extract_metadata('https://www.youtube.com/watch?v=7qy30k22sYE', {})
        # it must have actually tried once with cookies before giving up
        self.assertEqual(ydl.extract_info.call_count, 2)

    @patch('copita.core.ytdlp_wrapper.time.sleep', lambda *_: None)
    @patch('copita.core.browser_cookies.detect_default_browser', return_value='brave')
    @patch('copita.core.ytdlp_wrapper.yt_dlp.YoutubeDL')
    def test_youtube_bot_check_retries_with_cookies_and_succeeds(self, factory, _br):
        ydl = factory.return_value.__enter__.return_value
        bot = DownloadError("ERROR: [youtube] x: Sign in to confirm you’re not a bot.")
        ydl.extract_info.side_effect = [bot, {'title': 'Clip'}]
        info, opts = YtDlpWrapper()._extract_metadata(
            'https://www.youtube.com/watch?v=cookretry', {})
        self.assertEqual(info['title'], 'Clip')
        self.assertEqual(ydl.extract_info.call_count, 2)
        # the retry that worked was the one carrying the browser session
        self.assertEqual(opts.get('cookiesfrombrowser'), ('brave',))

    @patch('copita.core.ytdlp_wrapper.time.sleep', lambda *_: None)
    @patch('copita.core.browser_cookies.detect_default_browser', return_value='brave')
    @patch('copita.core.ytdlp_wrapper.yt_dlp.YoutubeDL')
    def test_youtube_bot_check_that_beats_cookies_too_gets_a_clean_message(self, factory, _br):
        ydl = factory.return_value.__enter__.return_value
        ydl.extract_info.side_effect = DownloadError(
            "ERROR: [youtube] x: Sign in to confirm you’re not a bot.")
        with self.assertRaisesRegex(RuntimeError, "rate-limiting this network"):
            YtDlpWrapper()._extract_metadata('https://www.youtube.com/watch?v=hardblock', {})
        # cookie-free, then cookie retry, then give up — not an endless loop
        self.assertEqual(ydl.extract_info.call_count, 2)

    @patch('copita.core.browser_cookies.detect_default_browser', return_value=None)
    @patch('copita.core.ytdlp_wrapper.yt_dlp.YoutubeDL')
    def test_instagram_extract_failure_gets_a_clean_message(self, factory, _br):
        ydl = factory.return_value.__enter__.return_value
        ydl.extract_info.side_effect = DownloadError(
            'ERROR: [instagram:user] nasa: Unable to extract data')
        with self.assertRaisesRegex(RuntimeError, 'Instagram needs a signed-in session'):
            YtDlpWrapper()._extract_metadata('https://www.instagram.com/nasa/', {})

    @patch('copita.core.ytdlp_wrapper.yt_dlp.YoutubeDL')
    def test_cancelled_download_does_not_start_network_request(self, factory):
        wrapper = YtDlpWrapper()
        wrapper.cancel()
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            wrapper._extract_metadata('https://example.com/video', {})
        factory.assert_not_called()


class RetryTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_html_get_response_is_rejected_before_writing_file(self):
        from pathlib import Path
        from aiohttp import web

        async def html(request):
            return web.Response(status=200, text="<html>not a file</html>",
                                headers={"Content-Type": "text/html; charset=utf-8",
                                         "Content-Length": "23"})

        app = web.Application()
        app.router.add_route("GET", "/book", html)
        app.router.add_route("HEAD", "/book", html)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = list(runner.sites)[0]._server.sockets[0].getsockname()[1]
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'book.download'
                downloader = ChunkDownloader(f'http://127.0.0.1:{port}/book', str(path))
                with self.assertRaisesRegex(RuntimeError, 'webpage instead of a file'):
                    await downloader.start()
                self.assertFalse(path.exists())
        finally:
            await runner.cleanup()

    async def test_retry_preserves_options_and_clears_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = TaskManager(os.path.join(directory, "dl"))
            task = DownloadTask('test', 'https://reddit.com/comments/1w773bo/', {'audio_only': True, 'audio_format': 'm4a'})
            manager.tasks[task.id] = task
            task.status, task.error, task.error_details = 'failed', 'Timed out', 'TransportError'
            task.percent, task.downloaded_bytes = 54, 1200
            ran = []
            async def run(retried):
                ran.append(retried)
            manager._run_task = run
            retried = manager.retry_task(task.id)
            self.assertIs(retried, task)
            self.assertEqual(task.options['audio_format'], 'm4a')
            self.assertIsNone(task.error)
            self.assertIsNone(task.error_details)
            self.assertEqual(task.percent, 0)
            await task.async_task
            self.assertEqual(ran, [task])

    async def test_cannot_retry_an_active_task(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = TaskManager(os.path.join(directory, "dl"))
            task = DownloadTask('test', 'https://example.com/test.mp4')
            manager.tasks[task.id] = task
            task.status = 'downloading'
            with self.assertRaises(ValueError):
                manager.retry_task(task.id)


if __name__ == '__main__':
    unittest.main()
