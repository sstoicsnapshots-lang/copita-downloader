"""Regression checks for MEGA URL parsing and folder downloads in FileHosterDownloader."""
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from copita.core.filehoster_downloader import FileHosterDownloader


class MegaUrlParsingTests(unittest.IsolatedAsyncioTestCase):
    async def _expect_error(self, url, pattern):
        with tempfile.TemporaryDirectory() as out_dir:
            downloader = FileHosterDownloader(url, out_dir)
            with self.assertRaisesRegex(ValueError, pattern):
                await downloader._resolve_mega()

    async def test_legacy_co_nz_domain_is_not_rejected_as_invalid(self):
        # Regression: the classifier and dispatcher both accept mega.co.nz,
        # but the id/key regex only matched mega.nz, so every legacy-domain
        # link failed with "Invalid MEGA URL" regardless of being well-formed.
        url = "https://mega.co.nz/file/sampleId#sampleKey1234567890123456789012"
        with tempfile.TemporaryDirectory() as out_dir:
            downloader = FileHosterDownloader(url, out_dir)
            # A real network call follows successful parsing; we only assert
            # it gets past the "Invalid MEGA URL" validation for this domain.
            with self.assertRaises(Exception) as ctx:
                await downloader._resolve_mega()
            self.assertNotIn("Invalid MEGA URL", str(ctx.exception))

    async def test_legacy_bang_format_on_co_nz_domain(self):
        url = "https://mega.co.nz/#!sampleId!sampleKey1234567890123456789012"
        with tempfile.TemporaryDirectory() as out_dir:
            downloader = FileHosterDownloader(url, out_dir)
            with self.assertRaises(Exception) as ctx:
                await downloader._resolve_mega()
            self.assertNotIn("Invalid MEGA URL", str(ctx.exception))

    async def test_garbage_url_gets_generic_format_error(self):
        await self._expect_error(
            "https://mega.nz/not-a-real-path",
            "Invalid MEGA URL",
        )

    async def test_folder_link_routes_to_folder_resolver(self):
        with tempfile.TemporaryDirectory() as out_dir:
            downloader = FileHosterDownloader("https://mega.nz/folder/abcd1234#efgh5678", out_dir)
            with patch.object(FileHosterDownloader, "_resolve_mega_folder", return_value={"provider": "mega_folder"}) as mocked:
                result = await downloader._resolve_mega()
                mocked.assert_awaited_once_with("abcd1234", "efgh5678")
                self.assertEqual(result["provider"], "mega_folder")

    async def test_legacy_folder_bang_format_routes_to_folder_resolver(self):
        with tempfile.TemporaryDirectory() as out_dir:
            downloader = FileHosterDownloader("https://mega.nz/#F!abcd1234!efgh5678", out_dir)
            with patch.object(FileHosterDownloader, "_resolve_mega_folder", return_value={"provider": "mega_folder"}) as mocked:
                await downloader._resolve_mega()
                mocked.assert_awaited_once_with("abcd1234", "efgh5678")

    async def test_mega_folder_provider_dispatches_to_folder_downloader(self):
        with tempfile.TemporaryDirectory() as out_dir:
            downloader = FileHosterDownloader("https://mega.nz/folder/abcd1234#efgh5678", out_dir)
            with patch.object(FileHosterDownloader, "resolve", return_value={"provider": "mega_folder"}), \
                 patch.object(FileHosterDownloader, "_download_mega_folder", return_value="unused") as mocked:
                await downloader.download()
                mocked.assert_awaited_once()


def _encrypt_node_key(folder_key_bytes: bytes, file_key_a32: tuple) -> bytes:
    """Wraps a file's raw key the way MEGA stores it in a folder listing:
    AES-ECB(folder_key, file_key_bytes). A real node key is 8 uint32s (32
    bytes = 2 AES blocks) — 4 for the raw key, 4 more XORed with it to
    derive the content AES key/IV, matching the single-file key format."""
    plaintext = struct.pack(f">{len(file_key_a32)}I", *file_key_a32)
    encryptor = Cipher(algorithms.AES(folder_key_bytes), modes.ECB()).encryptor()
    return encryptor.update(plaintext) + encryptor.finalize()


def _encrypt_attrs(aes_key: bytes, filename: str) -> bytes:
    payload = f'MEGA{{"n":"{filename}"}}'.encode("utf-8")
    payload += b"\x00" * ((16 - len(payload) % 16) % 16)
    encryptor = Cipher(algorithms.AES(aes_key), modes.CBC(b"\x00" * 16)).encryptor()
    return encryptor.update(payload) + encryptor.finalize()


class MegaFolderCryptoTests(unittest.IsolatedAsyncioTestCase):
    """Exercises the real folder-key-unwrap and attribute-decrypt logic
    against synthetically constructed (but correctly MEGA-formatted) node
    data, so this doesn't depend on network access or a live folder."""

    def _make_node(self, folder_key_bytes, handle, raw_key_a32, filename, size):
        aes_key = FileHosterDownloader._mega_a32_to_str((
            raw_key_a32[0] ^ raw_key_a32[4], raw_key_a32[1] ^ raw_key_a32[5],
            raw_key_a32[2] ^ raw_key_a32[6], raw_key_a32[3] ^ raw_key_a32[7],
        ))
        wrapped_key = _encrypt_node_key(folder_key_bytes, raw_key_a32)
        import base64
        key_b64 = base64.urlsafe_b64encode(wrapped_key).rstrip(b"=").decode()
        attr_b64 = base64.urlsafe_b64encode(_encrypt_attrs(aes_key, filename)).rstrip(b"=").decode()
        return {"h": handle, "t": 0, "k": f"{handle}:{key_b64}", "a": attr_b64, "s": size}

    async def test_resolve_folder_decrypts_real_file_keys_and_names(self):
        folder_key_a32 = (0x11111111, 0x22222222, 0x33333333, 0x44444444)
        folder_key_bytes = FileHosterDownloader._mega_a32_to_str(folder_key_a32)
        import base64
        folder_key_b64 = base64.urlsafe_b64encode(folder_key_bytes).rstrip(b"=").decode()

        raw_key_a32 = (1, 2, 3, 4, 5, 6, 7, 8)
        node = self._make_node(folder_key_bytes, "handleAB", raw_key_a32, "report.pdf", 12345)
        folder_node = {"h": "folderRoot", "t": 1}  # a subfolder entry that must be skipped

        with tempfile.TemporaryDirectory() as out_dir:
            downloader = FileHosterDownloader("https://mega.nz/folder/x#y", out_dir)
            fake_response = [{"f": [folder_node, node]}]

            class _ListingSession:
                def post(self, url, data=None, headers=None):
                    return _FakeAsyncCM(_FakeResponse(json_data=fake_response))

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *exc):
                    return False

            with patch("aiohttp.ClientSession", return_value=_ListingSession()):
                result = await downloader._resolve_mega_folder("x", folder_key_b64)

        self.assertEqual(len(result["files"]), 1)  # subfolder excluded
        self.assertEqual(result["files"][0]["filename"], "report.pdf")
        self.assertEqual(result["files"][0]["size"], 12345)
        self.assertEqual(result["files"][0]["handle"], "handleAB")

    def _make_folder_node(self, folder_key_bytes, handle, parent, name):
        import base64
        plaintext = os.urandom(16)  # a real folder's own AES key, any 16 bytes
        wrapped = Cipher(algorithms.AES(folder_key_bytes), modes.ECB()).encryptor()
        wrapped_key = wrapped.update(plaintext) + wrapped.finalize()
        key_b64 = base64.urlsafe_b64encode(wrapped_key).rstrip(b"=").decode()
        attr_b64 = base64.urlsafe_b64encode(_encrypt_attrs(plaintext, name)).rstrip(b"=").decode()
        return {"h": handle, "p": parent, "t": 1, "k": f"{handle}:{key_b64}", "a": attr_b64}

    async def test_resolve_folder_preserves_subfolder_structure_and_avoids_collisions(self):
        """Regression: files used to be dumped flat regardless of which
        subfolder they came from, so identically-named files in different
        subfolders silently overwrote each other on disk."""
        folder_key_a32 = (0x11111111, 0x22222222, 0x33333333, 0x44444444)
        folder_key_bytes = FileHosterDownloader._mega_a32_to_str(folder_key_a32)
        import base64
        folder_key_b64 = base64.urlsafe_b64encode(folder_key_bytes).rstrip(b"=").decode()

        # root -> "Course" ; Course -> "Module 1" and "Module 2", each with
        # a same-named file, plus one file directly under "Course" itself.
        root = self._make_folder_node(folder_key_bytes, "root", "outside-the-subtree", "Course")
        mod1 = self._make_folder_node(folder_key_bytes, "mod1", "root", "Module 1")
        mod2 = self._make_folder_node(folder_key_bytes, "mod2", "root", "Module 2")

        raw_key_a32 = (1, 2, 3, 4, 5, 6, 7, 8)
        file_root = self._make_node(folder_key_bytes, "fRoot", raw_key_a32, "readme.txt", 10)
        file_root["p"] = "root"
        file_mod1 = self._make_node(folder_key_bytes, "fMod1", raw_key_a32, "notes.pdf", 20)
        file_mod1["p"] = "mod1"
        file_mod2 = self._make_node(folder_key_bytes, "fMod2", raw_key_a32, "notes.pdf", 30)
        file_mod2["p"] = "mod2"

        fake_response = [{"f": [root, mod1, mod2, file_root, file_mod1, file_mod2]}]

        class _ListingSession:
            def post(self, url, data=None, headers=None):
                return _FakeAsyncCM(_FakeResponse(json_data=fake_response))

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        with tempfile.TemporaryDirectory() as out_dir:
            downloader = FileHosterDownloader("https://mega.nz/folder/x#y", out_dir)
            with patch("aiohttp.ClientSession", return_value=_ListingSession()):
                result = await downloader._resolve_mega_folder("x", folder_key_b64)

        self.assertEqual(result["folder_name"], "Course")
        by_handle = {f["handle"]: f for f in result["files"]}
        self.assertEqual(by_handle["fRoot"]["rel_dir"], [])
        self.assertEqual(by_handle["fMod1"]["rel_dir"], ["Module 1"])
        self.assertEqual(by_handle["fMod2"]["rel_dir"], ["Module 2"])
        # Same filename, different subfolders — must not collide.
        full_paths = {tuple(f["rel_dir"] + [f["filename"]]) for f in result["files"]}
        self.assertEqual(len(full_paths), 3)


class _FakeAsyncCM:
    """A plain object usable as `async with obj as x: ...`, returning itself."""
    def __init__(self, result):
        self._result = result

    async def __aenter__(self):
        return self._result

    async def __aexit__(self, *exc):
        return False


class _FakeContent:
    def __init__(self, chunks):
        self._chunks = chunks

    def iter_chunked(self, size):
        async def _gen():
            for c in self._chunks:
                yield c
        return _gen()


class _FakeResponse:
    def __init__(self, json_data=None, status=200, chunks=None):
        self.status = status
        self._json_data = json_data
        self.content = _FakeContent(chunks or [])

    async def json(self):
        return self._json_data


class _FakeSession:
    """Stands in for aiohttp.ClientSession, routing .post (MEGA API calls)
    and .get (CDN content) by handle so each simulated file can behave
    differently (one succeeds, one fails with an HTTP error)."""
    def __init__(self, post_by_handle, get_by_url):
        self._post_by_handle = post_by_handle
        self._get_by_url = get_by_url

    def post(self, url, data=None, headers=None):
        import json as _json
        payload = _json.loads(data)[0]
        handle = payload.get("n")
        return _FakeAsyncCM(self._post_by_handle[handle])

    def get(self, url):
        return _FakeAsyncCM(self._get_by_url[url])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class MegaFolderDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_failure_reports_which_files_succeeded(self):
        zero_key = b"\x00" * 16
        # AES-CTR(key=0, iv=0) is its own inverse, so "encrypting" here with
        # the decryptor's exact configuration produces bytes that decrypt
        # back to "hello" during the real _download_mega_folder call.
        encryptor = Cipher(algorithms.AES(zero_key), modes.CTR(zero_key)).encryptor()
        ciphertext = encryptor.update(b"hello") + encryptor.finalize()

        files = [
            {"handle": "h1", "filename": "ok.txt", "aes_key": zero_key, "iv": zero_key, "size": 5},
            {"handle": "h2", "filename": "bad.txt", "aes_key": zero_key, "iv": zero_key, "size": 5},
        ]

        post_by_handle = {
            "h1": _FakeResponse(json_data=[{"g": "http://fake/h1", "s": 5}]),
            "h2": _FakeResponse(json_data=[{"g": "http://fake/h2", "s": 5}]),
        }
        get_by_url = {
            "http://fake/h1": _FakeResponse(status=200, chunks=[ciphertext]),
            "http://fake/h2": _FakeResponse(status=404),
        }
        fake_session = _FakeSession(post_by_handle, get_by_url)

        with tempfile.TemporaryDirectory() as out_dir:
            downloader = FileHosterDownloader("https://mega.nz/folder/x#y", out_dir)
            with patch("aiohttp.ClientSession", return_value=fake_session):
                with self.assertRaises(ValueError) as ctx:
                    await downloader._download_mega_folder({
                        "folder_id": "x", "folder_name": "MEGA_x", "files": files,
                    })

            self.assertIn("1 of 2 files could not be downloaded", str(ctx.exception))
            saved = Path(out_dir, "MEGA_x", "ok.txt")
            self.assertTrue(saved.exists())
            self.assertEqual(saved.read_bytes(), b"hello")
            self.assertFalse(Path(out_dir, "MEGA_x", "bad.txt").exists())

    async def test_retry_skips_files_already_downloaded_correctly(self):
        """Regression: retrying a partially-completed folder used to
        re-download every file from scratch, including ones that already
        finished — wasteful, and it re-hits any active MEGA throttle
        needlessly for files that don't need it."""
        zero_key = b"\x00" * 16
        files = [{"handle": "h1", "filename": "already-done.txt", "aes_key": zero_key, "iv": zero_key, "size": 5}]

        with tempfile.TemporaryDirectory() as out_dir:
            existing = Path(out_dir, "MEGA_x", "already-done.txt")
            existing.parent.mkdir(parents=True)
            existing.write_bytes(b"hello")  # already complete, matches size=5

            downloader = FileHosterDownloader("https://mega.nz/folder/x#y", out_dir)
            # No entries in these dicts — any network attempt raises KeyError,
            # proving the skip check fires before any request is made.
            fake_session = _FakeSession(post_by_handle={}, get_by_url={})
            with patch("aiohttp.ClientSession", return_value=fake_session):
                result_dir = await downloader._download_mega_folder({
                    "folder_id": "x", "folder_name": "MEGA_x", "files": files,
                })

            self.assertEqual(result_dir, str(Path(out_dir, "MEGA_x")))
            self.assertEqual(existing.read_bytes(), b"hello")  # untouched

    async def test_stops_hammering_mega_after_first_throttle_is_detected(self):
        """Regression: after one file hit MEGA's 509 throttle, every other
        queued file was still attempted (each failing near-instantly),
        hammering MEGA further and flooding progress callbacks in a burst.
        Only the first file should ever reach the network."""
        zero_key = b"\x00" * 16
        files = [
            {"handle": "h1", "filename": "a.txt", "aes_key": zero_key, "iv": zero_key, "size": 5},
            {"handle": "h2", "filename": "b.txt", "aes_key": zero_key, "iv": zero_key, "size": 5},
            {"handle": "h3", "filename": "c.txt", "aes_key": zero_key, "iv": zero_key, "size": 5},
        ]
        # Only h1 has a scripted response; h2/h3 would raise KeyError in the
        # fake session if _download_one ever tried to contact the network
        # for them, proving they were skipped instead.
        post_by_handle = {"h1": _FakeResponse(json_data=[{"g": "http://fake/h1", "s": 5}])}
        get_by_url = {"http://fake/h1": _FakeResponse(status=509)}
        fake_session = _FakeSession(post_by_handle, get_by_url)

        with tempfile.TemporaryDirectory() as out_dir:
            downloader = FileHosterDownloader("https://mega.nz/folder/x#y", out_dir)
            with patch("aiohttp.ClientSession", return_value=fake_session):
                with self.assertRaises(ValueError) as ctx:
                    await downloader._download_mega_folder({
                        "folder_id": "x", "folder_name": "MEGA_x", "files": files,
                    })

            self.assertIn("3 of 3 files could not be downloaded", str(ctx.exception))
            self.assertIn("throttled", str(ctx.exception))

    async def test_downloads_smallest_files_first_to_maximize_completions(self):
        """With a fixed total-byte quota shared across the whole folder,
        downloading in listing order could burn it on one huge file and
        finish nothing else. Smallest-first should get more small files
        fully done before a throttle (simulated here on the biggest file)
        cuts things off."""
        zero_key = b"\x00" * 16

        def _file(handle, size):
            return {"handle": handle, "filename": f"{handle}.txt", "aes_key": zero_key, "iv": zero_key, "size": size}

        # Listed biggest-first, as MEGA's API order is arbitrary.
        files = [_file("big", 3_000_000), _file("small", 10), _file("medium", 1000)]

        encryptor = Cipher(algorithms.AES(zero_key), modes.CTR(zero_key)).encryptor()

        def _ok_response(size):
            ct = encryptor.update(b"x" * size)
            return _FakeResponse(status=200, chunks=[ct])

        post_by_handle = {
            "small": _FakeResponse(json_data=[{"g": "http://fake/small", "s": 10}]),
            "medium": _FakeResponse(json_data=[{"g": "http://fake/medium", "s": 1000}]),
            "big": _FakeResponse(json_data=[{"g": "http://fake/big", "s": 3_000_000}]),
        }
        get_by_url = {
            "http://fake/small": _ok_response(10),
            "http://fake/medium": _ok_response(1000),
            "http://fake/big": _FakeResponse(status=509),  # quota exhausted by the time the big file is reached
        }
        fake_session = _FakeSession(post_by_handle, get_by_url)

        with tempfile.TemporaryDirectory() as out_dir:
            downloader = FileHosterDownloader("https://mega.nz/folder/x#y", out_dir)
            with patch("aiohttp.ClientSession", return_value=fake_session):
                with self.assertRaises(ValueError):
                    await downloader._download_mega_folder({
                        "folder_id": "x", "folder_name": "MEGA_x", "files": files,
                    })

            # The two small files completed; only the large one was lost.
            self.assertTrue(Path(out_dir, "MEGA_x", "small.txt").exists())
            self.assertTrue(Path(out_dir, "MEGA_x", "medium.txt").exists())
            self.assertFalse(Path(out_dir, "MEGA_x", "big.txt").exists())


if __name__ == "__main__":
    unittest.main()
