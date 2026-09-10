"""Regression checks for downloading plain images.

Image-only posts (a pixabay illustration page, a Reddit `i.redd.it` post, a
Facebook/Twitter single photo) used to fail: they route to yt-dlp, which has
no image extractor and raises "Unsupported URL", and the generic sniffer only
looked for video/audio/streams — never the page's own picture.

Now:
  * classify_url unwraps reddit.com/media?url=<encoded image>
  * the sniffer pulls og:image / twitter:image / largest <img>
  * on a yt-dlp "Unsupported URL" failure, TaskManager recovers the image
    URL from the error message (unwrapping /media?url=) and downloads it
    directly, or falls back to the sniffer; a login-walled FB/IG photo gets
    a clear message instead of a raw error.
"""
import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

from copita.core.classifier import classify_url
from copita.core.sniffer import MediaSniffer, _image_res_score
from copita.core.task_manager import TaskManager, DownloadTask


class _InstantDownloader:
    instances = []

    def __init__(self, url, output_path, num_connections=1, headers=None, progress_callback=None):
        self.url = url
        self.output_path = output_path
        self.headers = headers or {}
        _InstantDownloader.instances.append(self)

    async def start(self):
        with open(self.output_path, "wb") as f:
            f.write(b"\xff\xd8\xff\xe0fake-jpeg-bytes")
        return self.output_path


class ClassifierImageTests(unittest.TestCase):
    def test_reddit_media_wrapper_is_unwrapped_to_the_real_image(self):
        r = classify_url(
            "https://www.reddit.com/media?url=https%3A%2F%2Fi.redd.it%2Fnpa2ufbwnvnh1.jpeg")
        self.assertEqual(r["url"], "https://i.redd.it/npa2ufbwnvnh1.jpeg")
        self.assertEqual(r["engine"], "turbo_chunk")
        self.assertEqual(r["category"], "image")

    def test_direct_image_url_is_categorised_as_image(self):
        self.assertEqual(classify_url("https://i.redd.it/x.png")["category"], "image")

    def test_res_score_prefers_the_bigger_variant(self):
        small = "https://cdn.pixabay.com/photo/2026/08/23/x-1044_640.png"
        big = "https://cdn.pixabay.com/photo/2026/08/23/x-1044_1280.png"
        self.assertLess(_image_res_score(small), _image_res_score(big))


class SnifferImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_og_image_is_returned_as_an_image_candidate(self):
        html = (
            '<html><head>'
            '<meta property="og:image" content="https://cdn.example.com/pic_1280.jpg">'
            '<title>A picture</title></head><body><img src="/tiny_100.png"></body></html>'
        )

        class _Resp:
            status = 200
            _headers = {"content-type": "text/html"}
            def header(self, k, d=""): return self._headers.get(k.lower(), d)
            @property
            def headers(self): return self._headers
            async def read(self): return html.encode()
            async def text(self, *a, **k): return html
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False

        class _FpClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, *a, **k): return _Resp()

        class _Session:
            def get(self, *a, **k): return _Resp()
            def head(self, *a, **k): return _Resp()
            cookie_jar = []
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False

        with patch("copita.core.http_client.HttpClient", _FpClient), \
             patch("copita.core.sniffer.aiohttp.ClientSession", lambda *a, **k: _Session()):
            res = await MediaSniffer().sniff_url("https://example.com/photo/123")

        imgs = [m for m in res["media"] if m["type"] == "image"]
        self.assertTrue(any("pic_1280.jpg" in m["url"] for m in imgs))


class FallbackFromYtdlpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _InstantDownloader.instances = []
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self._tmp.name, "downloads")
        self._p = patch("copita.core.task_manager.ChunkDownloader", _InstantDownloader)
        self._p.start()
        self.mgr = TaskManager(self.dir)

    async def asyncTearDown(self):
        self._p.stop()
        self._tmp.cleanup()

    async def _fallback(self, url, err):
        task = DownloadTask("t1", url)
        took_over = await self.mgr._fallback_from_ytdlp_failure(task, err, lambda d: None)
        return task, took_over

    async def test_recovers_image_from_reddit_media_wrapper_in_error(self):
        task, ok = await self._fallback(
            "https://www.reddit.com/r/x/comments/abc/",
            "ERROR: Unsupported URL: https://www.reddit.com/media?url=https%3A%2F%2Fi.redd.it%2Fz.jpeg",
        )
        self.assertTrue(ok)
        self.assertEqual(task.status, "completed")
        self.assertEqual(_InstantDownloader.instances[-1].url, "https://i.redd.it/z.jpeg")
        self.assertEqual(task.category, "image")

    async def test_recovers_direct_image_url_from_error(self):
        task, ok = await self._fallback(
            "https://twitter.com/u/status/1",
            "ERROR: Unsupported URL: https://pbs.twimg.com/media/AbCd.jpg?name=large",
        )
        self.assertTrue(ok)
        self.assertEqual(_InstantDownloader.instances[-1].url,
                         "https://pbs.twimg.com/media/AbCd.jpg?name=large")

    async def test_audio_hint_page_with_only_an_image_fails_clean(self):
        # The extension says the user clicked an <audio> player; if the sniffer
        # only finds the page's og:image, that's not what they wanted.
        task = DownloadTask("t9", "https://www.coursera.org/learn/x/supplement/y",
                            {"media_hint": "audio"})
        with patch("copita.core.sniffer.MediaSniffer.sniff_url",
                   return_value={"media": [{"url": "https://coursera.org/og.png", "type": "image"}]}):
            with self.assertRaises(ValueError) as cm:
                await self.mgr._fallback_from_ytdlp_failure(
                    task, "ERROR: Unsupported URL: https://www.coursera.org/learn/x", lambda d: None)
        self.assertIn("share image", str(cm.exception))

    async def test_non_unsupported_error_is_not_handled(self):
        _, ok = await self._fallback(
            "https://youtube.com/watch?v=x",
            "ERROR: HTTP Error 403: Forbidden",
        )
        self.assertFalse(ok)

    async def test_login_walled_facebook_photo_gets_a_clear_message(self):
        with patch("copita.core.sniffer.MediaSniffer.sniff_url",
                   return_value={"media": []}):
            with self.assertRaises(ValueError) as cm:
                await self._fallback(
                    "https://www.facebook.com/photo/?fbid=1&set=a.2",
                    "ERROR: Unsupported URL: https://www.facebook.com/photo/?fbid=1&set=a.2",
                )
        self.assertIn("signed-in", str(cm.exception).lower().replace("-", "-"))


if __name__ == "__main__":
    unittest.main()
