"""Regression checks for the Comix.to manga downloader."""
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import httpx

from copita.core.classifier import classify_url
from copita.core.manga_downloader import MangaDownloader


CHAPTER_URL = "https://comix.to/title/pvry-one-piece/11126545-chapter-0"
TITLE_URL = "https://comix.to/title/pvry-one-piece"

FAKE_CHAPTER_PAYLOAD = {
    "id": 11126545,
    "name": "",
    "number": "0",
    "pages": {
        "baseUrl": "https://80pd.wowpic2.store",
        "items": [{"url": "/i5/page1.webp"}, {"url": "/i5/page2.webp"}, {"url": "/i5/page3.webp"}],
    },
}

FAKE_CHAPTERS_LIST = [
    {"number": "0", "name": "", "id": 11126545, "url": "/title/pvry-one-piece/11126545-chapter-0"},
    {"number": "1", "name": "", "id": 11126546, "url": "/title/pvry-one-piece/11126546-chapter-1"},
]


class _FakeNextButton:
    def __init__(self, disabled=False):
        self.disabled = disabled
        self.clicks = 0

    def get_attribute(self, name):
        return "npager__nav is-disabled" if self.disabled else "npager__nav"

    def click(self, force=True, timeout=5000):
        self.clicks += 1


class _FakePage:
    """Simulates comix.to's paginated chapter list: each `pages` entry is one
    page's batch of items (with duplicate chapter numbers across groups, like
    the real site), consumed one per Next-page click."""

    def __init__(self, pages):
        self._pages = list(pages)
        self._cursor = 0
        self.next_button = _FakeNextButton(disabled=False)

    def goto(self, url, timeout=30000):
        pass

    def evaluate(self, script):
        if "window.__chaptersList = []" in script:
            return None
        # "() => window.__chaptersList"
        if self._cursor < len(self._pages):
            return self._pages[self._cursor]
        return []

    def query_selector(self, selector):
        if self._cursor >= len(self._pages) - 1:
            return None
        return self.next_button

    def click_next(self):
        self._cursor += 1


class _FakeContext:
    def __init__(self, page):
        self._page = page

    def add_init_script(self, script):
        pass

    def new_page(self):
        return self._page


class _FakeBrowser:
    def __init__(self, context):
        self._context = context
        self.closed = False

    def new_context(self, **kw):
        return self._context

    def close(self):
        self.closed = True


class _FakeChromium:
    def __init__(self, browser):
        self._browser = browser

    def launch(self, **kw):
        return self._browser


class _FakePlaywrightCM:
    """Stands in for `with sync_playwright() as p: ...` and wires
    next-button clicks to advance the fake page's cursor."""

    def __init__(self, pages):
        self.page = _FakePage(pages)
        real_click = self.page.next_button.click

        def click_and_advance(force=True, timeout=5000):
            real_click(force=force, timeout=timeout)
            self.page.click_next()

        self.page.next_button.click = click_and_advance
        self.context = _FakeContext(self.page)
        self.browser = _FakeBrowser(self.context)
        self.chromium = _FakeChromium(self.browser)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ClassifierRoutingTests(unittest.TestCase):
    def test_chapter_url_routes_to_manga_engine_with_clean_name(self):
        result = classify_url(CHAPTER_URL)
        self.assertEqual(result["engine"], "manga")
        self.assertEqual(result["provider"], "comix")
        self.assertIn("One Piece - Chapter 0", result["name"])

    def test_title_url_routes_to_manga_engine(self):
        result = classify_url(TITLE_URL)
        self.assertEqual(result["engine"], "manga")
        self.assertEqual(result["provider"], "comix")


class ComixDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_chapter_url_dispatches_to_chapter_downloader(self):
        with tempfile.TemporaryDirectory() as out_dir:
            downloader = MangaDownloader(CHAPTER_URL, out_dir)
            with patch.object(MangaDownloader, "_download_comix_chapter") as chapter_fn:
                chapter_fn.return_value = "unused"
                await downloader._download_comix()
                chapter_fn.assert_awaited_once_with(CHAPTER_URL)

    async def test_title_url_dispatches_to_title_downloader(self):
        with tempfile.TemporaryDirectory() as out_dir:
            downloader = MangaDownloader(TITLE_URL, out_dir)
            with patch.object(MangaDownloader, "_download_comix_title") as title_fn:
                title_fn.return_value = "unused"
                await downloader._download_comix()
                title_fn.assert_awaited_once_with(TITLE_URL)

    async def test_unrecognized_comix_url_raises(self):
        with tempfile.TemporaryDirectory() as out_dir:
            downloader = MangaDownloader("https://comix.to/", out_dir)
            with self.assertRaises(ValueError):
                await downloader._download_comix()


class ComixChapterDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_chapter_pages_are_packaged_into_named_cbz(self):
        with tempfile.TemporaryDirectory() as out_dir:
            downloader = MangaDownloader(CHAPTER_URL, out_dir)
            events = []
            downloader.progress_callback = events.append

            def fake_request(request):
                assert request.headers.get("referer") == "https://comix.to/"
                return httpx.Response(200, content=b"fake-webp-bytes")

            transport = httpx.MockTransport(fake_request)
            real_async_client = httpx.AsyncClient

            def mock_client(**kw):
                kw.pop("verify", None)
                return real_async_client(transport=transport, **kw)

            with patch.object(MangaDownloader, "_extract_comix_chapter_payload", return_value=FAKE_CHAPTER_PAYLOAD), \
                 patch("httpx.AsyncClient", mock_client):
                cbz_path = await downloader._download_comix_chapter(CHAPTER_URL)

            self.assertTrue(cbz_path.endswith("One Piece - Chapter 0.cbz"))
            self.assertTrue(Path(cbz_path).exists())
            with zipfile.ZipFile(cbz_path) as zf:
                names = sorted(zf.namelist())
                self.assertEqual(names, ["page_001.webp", "page_002.webp", "page_003.webp"])
                self.assertEqual(zf.read("page_001.webp"), b"fake-webp-bytes")
            self.assertTrue(any(e.get("status") == "completed" for e in events))

    async def test_missing_payload_raises_clear_error(self):
        with tempfile.TemporaryDirectory() as out_dir:
            downloader = MangaDownloader(CHAPTER_URL, out_dir)
            with patch.object(MangaDownloader, "_extract_comix_chapter_payload", return_value=None):
                with self.assertRaisesRegex(ValueError, "Could not extract chapter data"):
                    await downloader._download_comix_chapter(CHAPTER_URL)

    async def test_empty_pages_raises_clear_error(self):
        with tempfile.TemporaryDirectory() as out_dir:
            downloader = MangaDownloader(CHAPTER_URL, out_dir)
            empty_payload = {"pages": {"baseUrl": "https://x", "items": []}}
            with patch.object(MangaDownloader, "_extract_comix_chapter_payload", return_value=empty_payload):
                with self.assertRaisesRegex(ValueError, "No pages found"):
                    await downloader._download_comix_chapter(CHAPTER_URL)


class ComixTitleDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_title_download_zips_all_chapter_cbz_files(self):
        with tempfile.TemporaryDirectory() as out_dir:
            downloader = MangaDownloader(TITLE_URL, out_dir)

            async def fake_chapter(url, target_dir=None, sub_progress=None):
                path = Path(target_dir) / f"{url.rsplit('/', 1)[-1]}.cbz"
                path.write_bytes(b"PK\x03\x04fake")
                if sub_progress:
                    sub_progress({"percent": 100.0})
                return str(path)

            with patch.object(MangaDownloader, "_extract_comix_chapters_list", return_value=FAKE_CHAPTERS_LIST), \
                 patch.object(MangaDownloader, "_download_comix_chapter", side_effect=fake_chapter):
                zip_path = await downloader._download_comix_title(TITLE_URL)

            self.assertTrue(zip_path.endswith("One Piece.zip"))
            with zipfile.ZipFile(zip_path) as zf:
                self.assertEqual(len(zf.namelist()), 2)

    async def test_no_chapters_found_raises_clear_error(self):
        with tempfile.TemporaryDirectory() as out_dir:
            downloader = MangaDownloader(TITLE_URL, out_dir)
            with patch.object(MangaDownloader, "_extract_comix_chapters_list", return_value=[]):
                with self.assertRaisesRegex(ValueError, "Could not find any chapters"):
                    await downloader._download_comix_title(TITLE_URL)

    async def test_all_chapters_failing_raises_clear_error(self):
        with tempfile.TemporaryDirectory() as out_dir:
            downloader = MangaDownloader(TITLE_URL, out_dir)

            async def failing_chapter(url, target_dir=None, sub_progress=None):
                raise RuntimeError("boom")

            # _download_comix_title prints a per-chapter warning on failure
            # (useful in real usage); this test deliberately fails every
            # chapter, so silence that expected noise here.
            with patch.object(MangaDownloader, "_extract_comix_chapters_list", return_value=FAKE_CHAPTERS_LIST), \
                 patch.object(MangaDownloader, "_download_comix_chapter", side_effect=failing_chapter), \
                 patch("builtins.print"):
                with self.assertRaisesRegex(ValueError, "Failed to download any chapters"):
                    await downloader._download_comix_title(TITLE_URL)


class ComixChapterListPaginationTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)

    """comix.to lists ~3-4 scanlation groups per chapter number and only
    renders 20 items per page, so a long series spans hundreds of pages.
    These verify pagination advances, duplicates are dropped, and a runaway
    series (like a 1000+ chapter One Piece) is bounded by the safety cap."""

    def _run_extraction(self, pages, **options):
        fake_cm = _FakePlaywrightCM(pages)
        with patch("playwright.sync_api.sync_playwright", return_value=fake_cm):
            downloader = MangaDownloader(TITLE_URL, self._td.name, options=options)
            chapters = downloader._extract_comix_chapters_list(TITLE_URL)
        return chapters, fake_cm

    def test_pagination_advances_and_dedupes_by_chapter_number(self):
        pages = [
            [{"number": 3, "id": 1}, {"number": 3, "id": 2}, {"number": 2, "id": 3}],
            [{"number": 2, "id": 4}, {"number": 1, "id": 5}],
        ]
        chapters, fake_cm = self._run_extraction(pages)
        self.assertEqual([c["number"] for c in chapters], [3, 2, 1])
        self.assertEqual(fake_cm.page.next_button.clicks, 1)
        self.assertTrue(fake_cm.browser.closed)

    def test_pagination_stops_at_max_chapters_cap(self):
        pages = [[{"number": n, "id": n} for n in range(page * 20, page * 20 + 20)] for page in range(30)]
        chapters, fake_cm = self._run_extraction(pages, max_chapters=25)
        self.assertLessEqual(len(chapters), 25)
        # Should stop after ~2 pages (40 items covers the 25-chapter cap),
        # nowhere near clicking through all 30 simulated pages.
        self.assertLess(fake_cm.page.next_button.clicks, 5)

    def test_pagination_stops_when_next_button_disabled(self):
        pages = [
            [{"number": 2, "id": 1}],
            [{"number": 1, "id": 2}],
        ]
        fake_cm = _FakePlaywrightCM(pages)
        fake_cm.page.next_button.disabled = True
        with patch("playwright.sync_api.sync_playwright", return_value=fake_cm):
            downloader = MangaDownloader(TITLE_URL, self._td.name)
            chapters = downloader._extract_comix_chapters_list(TITLE_URL)
        # The disabled attribute on page 1's button should stop pagination
        # before a second click, even though a next button element exists.
        self.assertEqual([c["number"] for c in chapters], [2])
        self.assertEqual(fake_cm.page.next_button.clicks, 0)


if __name__ == "__main__":
    unittest.main()
