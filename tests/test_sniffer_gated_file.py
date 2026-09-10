"""A download page that names a concrete file (e.g. `Some Book.epub`) but is
behind a captcha / ad-gate the scan can't pass should fail with a clear
message — NOT silently "succeed" by handing back the page's og:image cover
thumbnail. Reproduces the workupload.com report: a `.epub` link came back
as a 40 KB `.png`.
"""
import threading
import unittest
from http.server import HTTPServer, BaseHTTPRequestHandler
from unittest.mock import patch

from copita.core.sniffer import MediaSniffer


class _Handler(BaseHTTPRequestHandler):
    BODY = b""

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/cover"):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", "40000")
            self.end_headers()
            self.wfile.write(b"\x89PNG\r\n\x1a\n" + b"0" * 39992)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(self.BODY)


def _serve(body):
    _Handler.BODY = body
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


async def _false(*a, **k):
    return False


class GatedFilePageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Keep the headless-browser fallback out of the test.
        self._cdp = patch.object(MediaSniffer, "_sniff_browser_cdp", new=_false)
        self._cdp.start()

    async def asyncTearDown(self):
        self._cdp.stop()

    async def test_epub_download_page_behind_a_gate_fails_clean(self):
        srv, base = _serve(
            b'<html><head><title>George R.R. Martin - Sandkings (novelette).epub</title>'
            b'<meta property="og:title" content="Download Sandkings.epub (108 KB) now">'
            b'<meta property="og:image" content="/cover.png"></head>'
            b'<body><a href="/start/RJz4kKYTCXd" class="btn">Download</a></body></html>'
        )
        try:
            res = await MediaSniffer().sniff_url(base + "/start/RJz4kKYTCXd")
        finally:
            srv.shutdown()
        self.assertEqual(res.get("media"), [])
        self.assertIn("Sandkings", res.get("note", ""))
        self.assertIn(".epub", res.get("note", ""))

    async def test_real_image_page_is_unaffected(self):
        # A genuine single-image page (pixabay-style) names no downloadable
        # file — the og:image IS the content and must still come back.
        srv, base = _serve(
            b'<html><head><title>Sunset over the hills - free photo</title>'
            b'<meta property="og:image" content="/cover.png"></head>'
            b'<body>a nice picture</body></html>'
        )
        try:
            res = await MediaSniffer().sniff_url(base + "/photo/123")
        finally:
            srv.shutdown()
        self.assertTrue(res.get("media"))
        self.assertEqual(res["media"][0]["type"], "image")


if __name__ == "__main__":
    unittest.main()
