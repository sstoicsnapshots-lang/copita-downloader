"""
High-Speed Multi-Connection HTTP Range Downloader.

Design (rewritten Sept 2026, informed by Ghost-Downloader-3):
  * Every request carries a real browser TLS fingerprint (via
    `copita.core.http_client`) — most "site blocked a direct request"
    failures were the non-browser handshake, not a real block.
  * Connections write their byte range **straight into the final file** at
    the right offset (`os.pwrite` into a pre-sized file). No per-chunk temp
    files, no stitch/merge step, no 2x disk headroom.
  * A `<file>.copita-part` sidecar records each range's progress, so an
    interrupted transfer resumes to the byte, not from a chunk boundary.
  * Explicit bad-server handling: a server that ignores `Range` and returns
    200 + the whole body is detected and downgraded to a single stream
    instead of corrupting a range slot; a Cloudflare challenge page is a
    permanent failure, not a retry loop.
  * Connection count grows only while it's actually helping (throughput
    still rising, no 429s), instead of firing a fixed N at every host.
"""

import os
import json
import time
import asyncio
from typing import Callable, Optional, Dict, Any, List

from copita.core.http_client import HttpClient, looks_like_bot_wall, NETWORK_ERRORS
from copita.core.speed_gate import GLOBAL as _speed_gate

# Transient server-side errors (rate limiting, momentary overload) are worth
# retrying with backoff; anything else (403/404/410/etc.) won't be fixed by
# retrying and should fail immediately.
_TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}

_SIDECAR_SUFFIX = ".copita-part"
_MIN_MULTI_SIZE = 1024 * 1024          # below this, one connection is plenty
_INITIAL_CONNECTIONS = 4               # ramp up from here, don't fire all at once
_MIN_SPLIT_REMAINDER = 2 * 1024 * 1024  # don't split a range with less than this left
_SIDECAR_FLUSH_BYTES = 4 * 1024 * 1024  # persist progress at most every 4 MB / worker


class _PermanentError(RuntimeError):
    """A failure retrying can't fix — 403/404, a bot wall, a size mismatch."""


class _RangeUnsupported(RuntimeError):
    """The server ignored our Range header (answered 200, not 206). Abort the
    multi-connection attempt and fall back to a single stream."""


class _TransientHTTPError(RuntimeError):
    """A retryable HTTP failure (see _TRANSIENT_HTTP_STATUSES)."""


class _Range:
    __slots__ = ("start", "end", "pos")

    def __init__(self, start: int, end: int, pos: Optional[int] = None):
        self.start = start
        self.end = end                       # inclusive; may shrink if split
        self.pos = start if pos is None else pos

    @property
    def done(self) -> bool:
        return self.pos > self.end

    @property
    def remaining(self) -> int:
        return max(0, self.end - self.pos + 1)

    def as_dict(self) -> Dict[str, int]:
        return {"start": self.start, "end": self.end, "pos": self.pos}


class ChunkDownloader:
    def __init__(
        self,
        url: str,
        output_path: str,
        num_connections: int = 16,
        headers: Optional[Dict[str, str]] = None,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self.url = url
        self.output_path = output_path
        self.num_connections = max(1, min(num_connections, 64))
        self.headers = headers or {}
        self.progress_callback = progress_callback

        self.total_size: Optional[int] = None
        self.supports_range: bool = False
        self.etag: str = ""
        self.downloaded_bytes = 0        # total on disk (resumed + new)
        self._newly_downloaded = 0       # bytes actually transferred this run
        self.is_cancelled = False
        self.is_paused = False

        self._start_time = 0.0
        self._last_speed_time = 0.0
        self._last_bytes = 0
        self.current_speed = 0.0

        self._ranges: List[_Range] = []
        self._fd: Optional[int] = None
        self._sidecar_dirty = 0
        self._saw_rate_limit = False
        self._last_supervisor_speed = 0.0

    # ---- sidecar (per-range resume) ------------------------------------

    @property
    def _sidecar_path(self) -> str:
        return self.output_path + _SIDECAR_SUFFIX

    def _load_sidecar(self) -> Optional[List[_Range]]:
        try:
            with open(self._sidecar_path, "r") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        if data.get("url") != self.url or data.get("total") != self.total_size:
            return None
        if self.etag and data.get("etag") and data["etag"] != self.etag:
            return None
        if not os.path.isfile(self.output_path):
            return None
        try:
            ranges = [_Range(r["start"], r["end"], r["pos"]) for r in data["ranges"]]
        except (KeyError, TypeError):
            return None
        # sanity: ranges must still cover [0, total)
        if not ranges or ranges[0].start != 0 or ranges[-1].end != self.total_size - 1:
            return None
        for a, b in zip(ranges, ranges[1:]):
            if b.start != a.end + 1:
                return None
        return ranges

    def _write_sidecar(self, force: bool = False) -> None:
        if not force and self._sidecar_dirty < _SIDECAR_FLUSH_BYTES:
            return
        self._sidecar_dirty = 0
        payload = {
            "url": self.url,
            "total": self.total_size,
            "etag": self.etag,
            "ranges": [r.as_dict() for r in self._ranges],
        }
        try:
            tmp = self._sidecar_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(payload, f)
            os.replace(tmp, self._sidecar_path)
        except OSError:
            pass

    def _clear_sidecar(self) -> None:
        for p in (self._sidecar_path, self._sidecar_path + ".tmp"):
            try:
                os.remove(p)
            except OSError:
                pass

    # ---- progress -----------------------------------------------------

    def _validate_not_html(self, content_type: str) -> None:
        ct = (content_type or "").lower()
        if ("text/html" in ct or "application/xhtml" in ct) and not \
                self.output_path.lower().endswith((".html", ".htm")):
            raise _PermanentError(
                "This link returned a webpage instead of a file. Open the page "
                "and copy its direct download link, then try again."
            )

    def _update_progress(self, force: bool = False):
        now = time.time()
        dt = now - self._last_speed_time
        if not force and dt < 0.5:
            return
        if dt > 0:
            self.current_speed = (self.downloaded_bytes - self._last_bytes) / dt
        self._last_speed_time = now
        self._last_bytes = self.downloaded_bytes
        eta = None
        if self.total_size and self.current_speed > 0:
            eta = max(0, self.total_size - self.downloaded_bytes) / self.current_speed
        if self.progress_callback:
            self.progress_callback({
                "downloaded": self.downloaded_bytes,
                "total": self.total_size,
                "speed": max(0.0, self.current_speed),
                "eta": eta,
                "threads": len(self._ranges) or self.num_connections,
                "percent": (self.downloaded_bytes / self.total_size * 100) if self.total_size else None,
            })

    async def _wait_if_paused(self) -> bool:
        while self.is_paused and not self.is_cancelled:
            await asyncio.sleep(0.3)
        return not self.is_cancelled

    # ---- probe -------------------------------------------------------

    def _origin(self) -> str:
        p = self.url.split("://", 1)
        if len(p) == 2 and "/" in p[1]:
            return f"{p[0]}://{p[1].split('/', 1)[0]}/"
        return self.url

    async def _probe_once(self, client: HttpClient) -> "tuple[int, dict]":
        resp = await client.head(self.url, allow_redirects=True)
        status, headers = resp.status, resp.headers
        # Some hosts refuse HEAD (405) or don't send length on it — fall back
        # to a 1-byte ranged GET to learn size + range support.
        if status in (403, 405, 501) or "content-length" not in headers:
            probe = await client.get(self.url, range_header="bytes=0-0", allow_redirects=True)
            async with probe:
                status, headers = probe.status, dict(probe.headers)
                cr = headers.get("content-range", "")
                if status == 206 and "/" in cr:
                    self.total_size = int(cr.rsplit("/", 1)[-1]) if cr.rsplit("/", 1)[-1].isdigit() else None
                    self.supports_range = True
                elif status == 200 and headers.get("content-length", "").isdigit():
                    self.total_size = int(headers["content-length"])
                    self.supports_range = headers.get("accept-ranges", "").lower() == "bytes"
        else:
            cl = headers.get("content-length", "")
            self.total_size = int(cl) if cl.isdigit() else None
            self.supports_range = headers.get("accept-ranges", "").lower() == "bytes"
        return status, dict(headers)

    async def _probe(self, client: HttpClient) -> Dict[str, Any]:
        status, headers = await self._probe_once(client)

        # Hotlink protection (Wikimedia, pixabay CDN, many image/file hosts)
        # 403s a request with no Referer — a browser always has one because
        # it loaded the file from a page. Retry once with a same-origin
        # Referer, which most such checks accept.
        if status in (401, 403) and not self.total_size and "referer" not in \
                {k.lower() for k in self.headers}:
            self.headers["Referer"] = self._origin()
            client.base_headers["Referer"] = self._origin()
            self.total_size = None
            self.supports_range = False
            status, headers = await self._probe_once(client)

        self.etag = headers.get("etag", "")
        self._validate_not_html(headers.get("content-type", ""))

        cd = headers.get("content-disposition", "")
        suggested = None
        if "filename=" in cd:
            suggested = cd.split("filename=")[-1].strip('"\' ;')

        if status in (401, 403, 404, 410) and not self.total_size:
            raise _PermanentError(f"The server refused this download (HTTP {status}).")

        return {"status": status, "suggested_filename": suggested,
                "content_type": headers.get("content-type", "")}

    # ---- single stream (no range support / small file) ---------------

    async def _stream_single(self, client: HttpClient):
        for attempt in range(4):
            self.downloaded_bytes = 0
            try:
                async with await client.get(self.url, allow_redirects=True) as resp:
                    if resp.status != 200:
                        sample = await resp.peek(8192)
                        if looks_like_bot_wall(resp.status, sample):
                            raise _PermanentError(
                                "This site is behind a bot check Copita can't pass "
                                "with a direct request. Try the browser extension."
                            )
                        if resp.status in _TRANSIENT_HTTP_STATUSES:
                            raise _TransientHTTPError(f"HTTP {resp.status}")
                        raise _PermanentError(f"Download failed with HTTP {resp.status}")
                    self._validate_not_html(resp.header("content-type"))

                    os.makedirs(os.path.dirname(os.path.abspath(self.output_path)) or ".", exist_ok=True)
                    with open(self.output_path, "wb") as f:
                        async for chunk in resp.aiter_bytes(128 * 1024):
                            if self.is_cancelled:
                                return
                            if not await self._wait_if_paused():
                                return
                            f.write(chunk)
                            self.downloaded_bytes += len(chunk)
                            self._newly_downloaded += len(chunk)
                            self._update_progress()
                            await _speed_gate.throttle(len(chunk))
                return
            except _PermanentError:
                raise
            except (_TransientHTTPError, *NETWORK_ERRORS):
                if attempt == 3:
                    raise
                await asyncio.sleep(0.6 * (attempt + 1))

    # ---- multi-connection ------------------------------------------

    async def _worker(self, rng: _Range, client: HttpClient):
        backoff_attempt = 0
        while not rng.done:
            if self.is_cancelled:
                return
            if not await self._wait_if_paused():
                return
            want_from, want_to = rng.pos, rng.end
            try:
                async with await client.get(
                    self.url, range_header=f"bytes={want_from}-{want_to}",
                    allow_redirects=True,
                ) as resp:
                    if resp.status == 200:
                        # Server ignored the Range header entirely.
                        raise _RangeUnsupported()
                    if resp.status not in (206, 200):
                        sample = await resp.peek(8192)
                        if looks_like_bot_wall(resp.status, sample):
                            raise _PermanentError(
                                "This site is behind a bot check Copita can't pass "
                                "with a direct request."
                            )
                        if resp.status in _TRANSIENT_HTTP_STATUSES:
                            if resp.status == 429:
                                self._saw_rate_limit = True
                            raise _TransientHTTPError(f"HTTP {resp.status}")
                        raise _PermanentError(f"Range request failed with HTTP {resp.status}")

                    async for chunk in resp.aiter_bytes(64 * 1024):
                        if self.is_cancelled:
                            return
                        if not await self._wait_if_paused():
                            return
                        # The supervisor may have handed the tail of this range
                        # to a new worker — stop at the (possibly shrunk) end.
                        if rng.pos > rng.end:
                            break
                        writeable = chunk[: rng.end - rng.pos + 1]
                        os.pwrite(self._fd, writeable, rng.pos)
                        rng.pos += len(writeable)
                        self.downloaded_bytes += len(writeable)
                        self._newly_downloaded += len(writeable)
                        self._sidecar_dirty += len(writeable)
                        self._write_sidecar()
                        self._update_progress()
                        await _speed_gate.throttle(len(writeable))
                backoff_attempt = 0
            except (_PermanentError, _RangeUnsupported):
                raise
            except (_TransientHTTPError, *NETWORK_ERRORS):
                backoff_attempt += 1
                if backoff_attempt > 5:
                    raise
                self._saw_rate_limit = True
                await asyncio.sleep(0.6 * backoff_attempt)

    async def _supervisor(self, client: HttpClient, tasks: List[asyncio.Task]):
        """Add a connection only while it's still helping: throughput up since
        last check, no 429/timeout seen, and a big enough range left to split."""
        while not self.is_cancelled:
            await asyncio.sleep(3.0)
            active = [r for r in self._ranges if not r.done]
            if not active:
                return
            if len(self._ranges) >= self.num_connections or self._saw_rate_limit:
                continue
            speed_up = self.current_speed > self._last_supervisor_speed * 1.05
            self._last_supervisor_speed = self.current_speed
            if not speed_up:
                continue
            biggest = max(active, key=lambda r: r.remaining)
            if biggest.remaining < _MIN_SPLIT_REMAINDER:
                continue
            mid = biggest.pos + biggest.remaining // 2
            tail = _Range(mid, biggest.end, mid)
            biggest.end = mid - 1
            self._ranges.append(tail)
            tasks.append(asyncio.create_task(self._worker(tail, client)))

    async def _run_multi(self, client: HttpClient):
        assert self.total_size is not None
        resumed = self._load_sidecar()
        if resumed:
            self._ranges = resumed
            self.downloaded_bytes = sum(r.pos - r.start for r in self._ranges)
        else:
            n = max(1, min(_INITIAL_CONNECTIONS, self.num_connections))
            step = self.total_size // n
            self._ranges = []
            for i in range(n):
                s = i * step
                e = (self.total_size - 1) if i == n - 1 else ((i + 1) * step - 1)
                self._ranges.append(_Range(s, e))
            self.downloaded_bytes = 0

        os.makedirs(os.path.dirname(os.path.abspath(self.output_path)) or ".", exist_ok=True)
        self._fd = os.open(self.output_path, os.O_WRONLY | os.O_CREAT, 0o644)
        try:
            os.ftruncate(self._fd, self.total_size)
            self._write_sidecar(force=True)

            workers = [asyncio.create_task(self._worker(r, client))
                       for r in list(self._ranges) if not r.done]
            supervisor = asyncio.create_task(self._supervisor(client, workers))
            try:
                # workers is appended to by the supervisor — loop until all done
                while True:
                    pending = [t for t in workers if not t.done()]
                    if not pending:
                        break
                    done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_EXCEPTION)
                    for t in done:
                        exc = t.exception()
                        if exc:
                            raise exc
            finally:
                supervisor.cancel()
                for t in workers:
                    t.cancel()
                await asyncio.gather(supervisor, *workers, return_exceptions=True)
                # Persist exactly where every range stopped so a retry / restart
                # resumes to the byte instead of from a chunk boundary.
                self._write_sidecar(force=True)

            if self.is_cancelled:
                raise asyncio.CancelledError("Download cancelled by user")

            os.fsync(self._fd)
        finally:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None

        actual = os.path.getsize(self.output_path)
        if actual != self.total_size:
            raise _PermanentError(
                f"Downloaded size ({actual}) doesn't match the expected size "
                f"({self.total_size}). The file may be truncated — retry."
            )
        self._clear_sidecar()

    # ---- entry point ----------------------------------------------

    async def start(self) -> str:
        self._start_time = self._last_speed_time = time.time()
        self._last_bytes = 0
        self.downloaded_bytes = 0
        self._newly_downloaded = 0
        self.is_cancelled = False
        self.is_paused = False

        async with HttpClient(headers=self.headers, timeout=30.0) as client:
            probe = await self._probe(client)

            if self.progress_callback and self.total_size:
                self.progress_callback({
                    "downloaded": self.downloaded_bytes, "total": self.total_size,
                    "speed": 0.0, "eta": None,
                    "threads": self.num_connections, "percent":
                    (self.downloaded_bytes / self.total_size * 100) if self.total_size else 0.0,
                })

            use_multi = (self.supports_range and self.total_size
                         and self.total_size > _MIN_MULTI_SIZE and self.num_connections > 1)
            if use_multi:
                try:
                    await self._run_multi(client)
                except _RangeUnsupported:
                    # Server lied about Accept-Ranges — start over as one stream.
                    self._clear_sidecar()
                    self.downloaded_bytes = 0
                    await self._stream_single(client)
            else:
                await self._stream_single(client)

        if self.is_cancelled:
            raise asyncio.CancelledError("Download cancelled by user")

        self._update_progress(force=True)
        if self.progress_callback:
            self.progress_callback({
                "downloaded": self.downloaded_bytes,
                "total": self.total_size or self.downloaded_bytes,
                "speed": 0.0, "eta": 0.0,
                "threads": len(self._ranges) or self.num_connections,
                "percent": 100.0, "status": "completed",
            })
        return self.output_path

    def pause(self):
        self.is_paused = True

    def resume(self):
        self.is_paused = False

    def cancel(self):
        self.is_cancelled = True
