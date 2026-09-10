"""
Global download speed limiter.

One token-bucket shared by every downloading engine (chunk / HLS / file
hosts). Each writer calls `await GLOBAL.throttle(nbytes)` right after it
writes a chunk; when a cap is set, that call sleeps just enough to hold the
aggregate transfer rate at the limit. A cap of 0 means unlimited and the
call is a cheap no-op.

Modelled on Ghost-Downloader-3's global SpeedMeter gate, but as a proper
token bucket rather than a 1-second polling loop, so it doesn't burst a
full second of data and then stall.
"""

from __future__ import annotations

import asyncio
import time


class SpeedGate:
    def __init__(self) -> None:
        self._rate = 0.0            # bytes/sec; 0 = unlimited
        self._allowance = 0.0
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    @property
    def rate_bytes(self) -> float:
        return self._rate

    def set_limit_kbps(self, kbps: float) -> None:
        """kbps here means kilobytes/sec (KB/s), matching how download
        managers label it. 0 / negative disables the limit."""
        rate = max(0.0, float(kbps)) * 1024.0
        self._rate = rate
        # Reset the bucket so a fresh limit takes effect immediately without
        # a stored surplus or deficit from a previous setting.
        self._allowance = rate
        self._last = time.monotonic()

    async def throttle(self, nbytes: int) -> None:
        if self._rate <= 0 or nbytes <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            self._allowance += (now - self._last) * self._rate
            self._last = now
            if self._allowance > self._rate:
                self._allowance = self._rate      # cap burst at ~1s of data
            self._allowance -= nbytes
            sleep_for = (-self._allowance / self._rate) if self._allowance < 0 else 0.0
        if sleep_for > 0:
            await asyncio.sleep(min(sleep_for, 5.0))


# Process-wide instance every engine imports.
GLOBAL = SpeedGate()


def set_global_limit_kbps(kbps: float) -> None:
    GLOBAL.set_limit_kbps(kbps)
