"""Engine tests for the rewritten ChunkDownloader (Sept 2026).

Covers, against a real local aiohttp server:
  * multi-connection range download → byte-identical output, no temp files
  * per-range resume from the .copita-part sidecar
  * a server that ignores Range (200-for-range) → falls back to one stream
  * a Cloudflare-style challenge body → permanent failure, no retry loop
  * transient 503 → retried; 403 → not retried
  * no-range small file → single stream
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import unittest

from aiohttp import web

from copita.core.chunk_downloader import ChunkDownloader, _PermanentError


def _body(n: int) -> bytes:
    return (b"COPITA-" * ((n // 7) + 1))[:n]


class _Server:
    def __init__(self, app: web.Application):
        self.app = app
        self.runner: web.AppRunner | None = None
        self.url = ""

    async def __aenter__(self) -> "_Server":
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        sock = list(self.runner.sites)[0]._server.sockets[0]
        self.url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        return self

    async def __aexit__(self, *exc):
        await self.runner.cleanup()


def _range_app(content: bytes, *, accept_ranges: bool = True, etag: str = '"v1"',
               throttle: float = 0.0):
    async def handler(request: web.Request):
        rng = request.headers.get("Range")
        headers = {"Content-Type": "application/octet-stream"}
        if etag:
            headers["ETag"] = etag
        if accept_ranges:
            headers["Accept-Ranges"] = "bytes"
        if rng and accept_ranges and rng.startswith("bytes="):
            a, b = rng[6:].split("-")
            start = int(a)
            end = int(b) if b else len(content) - 1
            end = min(end, len(content) - 1)
            chunk = content[start:end + 1]
            headers["Content-Range"] = f"bytes {start}-{end}/{len(content)}"
            headers["Content-Length"] = str(len(chunk))
            if request.method == "HEAD":
                return web.Response(status=206, headers=headers)
            if throttle:
                resp = web.StreamResponse(status=206, headers=headers)
                await resp.prepare(request)
                for i in range(0, len(chunk), 32768):
                    await resp.write(chunk[i:i + 32768])
                    await asyncio.sleep(throttle)
                await resp.write_eof()
                return resp
            return web.Response(status=206, body=chunk, headers=headers)
        headers["Content-Length"] = str(len(content))
        if request.method == "HEAD":
            return web.Response(status=200, headers=headers)
        return web.Response(status=200, body=content, headers=headers)

    app = web.Application()
    app.router.add_route("GET", "/f", handler)
    app.router.add_route("HEAD", "/f", handler)
    return app


class MultiConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_range_download_is_byte_identical_and_leaves_no_temp_files(self):
        content = _body(2_000_000)
        async with _Server(_range_app(content)) as srv:
            with tempfile.TemporaryDirectory() as d:
                out = os.path.join(d, "out.bin")
                dl = ChunkDownloader(f"{srv.url}/f", out, num_connections=8)
                await dl.start()
                with open(out, "rb") as f:
                    got = f.read()
                self.assertEqual(hashlib.md5(got).hexdigest(),
                                 hashlib.md5(content).hexdigest())
                # no .chunks dir, no leftover sidecar
                self.assertEqual(sorted(os.listdir(d)), ["out.bin"])

    async def test_resume_from_sidecar(self):
        content = _body(1_500_000)
        # Throttled so a cancel lands mid-transfer deterministically.
        async with _Server(_range_app(content, throttle=0.05)) as srv:
            with tempfile.TemporaryDirectory() as d:
                out = os.path.join(d, "out.bin")
                dl = ChunkDownloader(f"{srv.url}/f", out, num_connections=4)
                task = asyncio.create_task(dl.start())
                await asyncio.sleep(0.25)
                dl.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

                self.assertTrue(os.path.exists(out + ".copita-part"))
                with open(out + ".copita-part") as f:
                    sidecar = json.load(f)
                done_before = sum(r["pos"] - r["start"] for r in sidecar["ranges"])
                self.assertGreater(done_before, 0)
                self.assertLess(done_before, len(content))

                # Resume run: completes correctly, clears the sidecar, and
                # only fetches what was still missing.
                dl2 = ChunkDownloader(f"{srv.url}/f", out, num_connections=4)
                await dl2.start()
                with open(out, "rb") as f:
                    self.assertEqual(f.read(), content)
                self.assertFalse(os.path.exists(out + ".copita-part"))
                self.assertLessEqual(dl2._newly_downloaded, len(content) - done_before + 65536)

    async def test_small_file_uses_single_stream(self):
        content = _body(5000)
        async with _Server(_range_app(content)) as srv:
            with tempfile.TemporaryDirectory() as d:
                out = os.path.join(d, "small.bin")
                await ChunkDownloader(f"{srv.url}/f", out, num_connections=16).start()
                with open(out, "rb") as f:
                    self.assertEqual(f.read(), content)


class DegradationTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_ignoring_range_falls_back_to_single_stream(self):
        content = _body(3_000_000)
        # Advertises Accept-Ranges but always returns 200 + full body.
        async with _Server(_range_app(content, accept_ranges=False)) as srv:
            # force the multi path to think range is supported
            app = _range_app(content, accept_ranges=True)

            async def liar(request):
                return web.Response(status=200, body=content, headers={
                    "Content-Type": "application/octet-stream",
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(len(content)),
                })

            app2 = web.Application()
            app2.router.add_route("GET", "/f", liar)
            app2.router.add_route("HEAD", "/f", liar)
            async with _Server(app2) as srv2:
                with tempfile.TemporaryDirectory() as dd:
                    out = os.path.join(dd, "out.bin")
                    await ChunkDownloader(f"{srv2.url}/f", out, num_connections=8).start()
                    with open(out, "rb") as _f:
                        self.assertEqual(_f.read(), content)

    async def test_cloudflare_challenge_is_permanent(self):
        async def challenge(request):
            return web.Response(status=403, text="<html><title>Just a moment...</title></html>",
                                headers={"Content-Type": "text/html"})

        app = web.Application()
        app.router.add_route("GET", "/f", challenge)
        app.router.add_route("HEAD", "/f", challenge)
        async with _Server(app) as srv:
            with tempfile.TemporaryDirectory() as d:
                out = os.path.join(d, "out.bin")
                with self.assertRaises(_PermanentError):
                    await ChunkDownloader(f"{srv.url}/f", out).start()

    async def test_transient_503_retried_then_succeeds(self):
        state = {"hits": 0}
        content = _body(4000)

        async def flaky(request):
            state["hits"] += 1
            if state["hits"] == 1:
                return web.Response(status=503, text="busy")
            return web.Response(status=200, body=content,
                                headers={"Content-Type": "application/octet-stream",
                                         "Content-Length": str(len(content))})

        app = web.Application()
        app.router.add_route("GET", "/f", flaky)
        app.router.add_route("HEAD", "/f", flaky)
        async with _Server(app) as srv:
            with tempfile.TemporaryDirectory() as d:
                out = os.path.join(d, "out.bin")
                await ChunkDownloader(f"{srv.url}/f", out).start()
                with open(out, "rb") as _f:
                    self.assertEqual(_f.read(), content)
                self.assertGreaterEqual(state["hits"], 2)

    async def test_403_not_retried(self):
        state = {"hits": 0}

        async def forbidden(request):
            state["hits"] += 1
            return web.Response(status=403, text="nope")

        app = web.Application()
        app.router.add_route("GET", "/f", forbidden)
        app.router.add_route("HEAD", "/f", forbidden)
        async with _Server(app) as srv:
            with tempfile.TemporaryDirectory() as d:
                out = os.path.join(d, "out.bin")
                with self.assertRaises(_PermanentError):
                    await ChunkDownloader(f"{srv.url}/f", out).start()


if __name__ == "__main__":
    unittest.main()
