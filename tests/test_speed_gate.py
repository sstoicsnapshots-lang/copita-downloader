"""The global speed limiter (copita/core/speed_gate.py)."""
import asyncio
import os
import tempfile
import time
import unittest

from aiohttp import web

from copita.core.speed_gate import SpeedGate, GLOBAL, set_global_limit_kbps
from copita.core.chunk_downloader import ChunkDownloader


class TokenBucketTests(unittest.IsolatedAsyncioTestCase):
    async def test_unlimited_is_a_noop(self):
        g = SpeedGate()
        g.set_limit_kbps(0)
        t = time.monotonic()
        for _ in range(50):
            await g.throttle(1_000_000)
        self.assertLess(time.monotonic() - t, 0.05)

    async def test_limit_holds_the_average_rate(self):
        g = SpeedGate()
        g.set_limit_kbps(100)  # 100 KB/s
        t = time.monotonic()
        # "transfer" 300 KB in 30 KB writes -> should take ~2s (first ~1s of
        # burst allowance is free, then throttled).
        for _ in range(10):
            await g.throttle(30 * 1024)
        elapsed = time.monotonic() - t
        self.assertGreater(elapsed, 1.4)
        self.assertLess(elapsed, 3.5)

    async def test_raising_the_limit_takes_effect_immediately(self):
        g = SpeedGate()
        g.set_limit_kbps(10)
        await g.throttle(10 * 1024)      # drains the bucket
        g.set_limit_kbps(0)             # unlimited
        t = time.monotonic()
        await g.throttle(10_000_000)
        self.assertLess(time.monotonic() - t, 0.05)


class ChunkDownloaderThrottleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        set_global_limit_kbps(0)  # never leave a cap set for other tests

    async def test_global_cap_slows_a_real_download(self):
        content = (b"x" * 1024) * 400  # ~400 KB
        async def handler(request):
            return web.Response(body=content, headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(content)),
                "Accept-Ranges": "bytes",
            })
        app = web.Application()
        app.router.add_route("GET", "/f", handler)
        app.router.add_route("HEAD", "/f", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = list(runner.sites)[0]._server.sockets[0].getsockname()[1]
        try:
            set_global_limit_kbps(200)  # 200 KB/s -> ~400 KB should take ~1s+
            with tempfile.TemporaryDirectory() as d:
                out = os.path.join(d, "f.bin")
                t = time.monotonic()
                await ChunkDownloader(f"http://127.0.0.1:{port}/f", out,
                                      num_connections=4).start()
                elapsed = time.monotonic() - t
                with open(out, "rb") as fh:
                    self.assertEqual(fh.read(), content)
                self.assertGreater(elapsed, 0.7)
        finally:
            await runner.cleanup()


if __name__ == "__main__":
    unittest.main()
