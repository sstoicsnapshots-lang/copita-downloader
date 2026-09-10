"""
Unified HTTP client for Copita's direct-download engines.

Every engine that fetches over plain HTTP (the chunk downloader, the page
sniffer, the file-host resolvers, HLS segment/manifest fetches) goes through
here so that:

  * requests carry a **real browser TLS/JA3 + HTTP-2 fingerprint** via
    `curl_cffi` (libcurl-impersonate). `aiohttp`/`httpx` have a distinctive
    non-browser handshake that Cloudflare, Akamai, PerimeterX etc. reject
    outright — most of Copita's "site blocked a direct request" failures are
    that, not a real block. A genuine Chrome fingerprint walks past them.
  * one place controls the impersonation profile, the proxy, TLS
    verification, and per-host identity overrides.

`curl_cffi` is a first-class dependency (Settings -> Requirements installs
it). The `aiohttp` backend here is only a crash-guard for the short window
before it's installed — not a normal mode.
"""

from __future__ import annotations

import asyncio
import re
import ssl
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple
from urllib.parse import urlparse

try:
    from curl_cffi.requests import AsyncSession as _CurlSession
    from curl_cffi.requests.exceptions import RequestException as _CurlRequestError
    CURL_AVAILABLE = True
except Exception:  # pragma: no cover - curl_cffi is a declared requirement
    _CurlSession = None
    _CurlRequestError = ()
    CURL_AVAILABLE = False

import aiohttp
import certifi


# --- module configuration (updated from app settings) -----------------------

_DEFAULT_PROFILE = "chrome"  # curl_cffi alias -> latest Chrome it ships

_config: Dict[str, Any] = {
    "impersonate": _DEFAULT_PROFILE,
    "proxy": None,          # e.g. "socks5://127.0.0.1:1080" or "http://user:pass@host:port"
    "verify_ssl": True,
    # [{"hosts": ["*.example.com", "foo.com"], "profile": "safari", "headers": {...}}]
    "identity_presets": [],
}


def configure(*, impersonate: Optional[str] = None, proxy: Optional[str] = ...,
              verify_ssl: Optional[bool] = None,
              identity_presets: Optional[List[Dict[str, Any]]] = None) -> None:
    """Apply settings changes. `proxy` uses a sentinel so `None` can clear it."""
    if impersonate:
        _config["impersonate"] = impersonate
    if proxy is not ...:
        _config["proxy"] = proxy or None
    if verify_ssl is not None:
        _config["verify_ssl"] = bool(verify_ssl)
    if identity_presets is not None:
        _config["identity_presets"] = list(identity_presets)


def current_proxy() -> Optional[str]:
    return _config["proxy"]


def current_verify_ssl() -> bool:
    return _config["verify_ssl"]


def _host_matches(pattern: str, host: str) -> bool:
    pattern = pattern.strip().lower()
    if pattern.startswith("*."):
        suffix = pattern[2:]
        return host == suffix or host.endswith("." + suffix)
    return host == pattern


def identity_for(url: str) -> Tuple[str, Dict[str, str]]:
    """(impersonation profile, extra headers) for this URL — a per-host
    preset if one matches, otherwise the global default."""
    host = (urlparse(url).hostname or "").lower()
    for preset in _config["identity_presets"]:
        for pat in preset.get("hosts", []):
            if _host_matches(pat, host):
                return (preset.get("profile") or _config["impersonate"],
                        dict(preset.get("headers") or {}))
    return _config["impersonate"], {}


_DEFAULT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


# --- response wrapper ------------------------------------------------------

class HttpResponse:
    """Backend-agnostic response. Headers are case-insensitive."""

    def __init__(self, status: int, headers: Dict[str, str]):
        self.status = status
        self._headers = {k.lower(): v for k, v in headers.items()}

    def header(self, name: str, default: str = "") -> str:
        return self._headers.get(name.lower(), default)

    @property
    def headers(self) -> Dict[str, str]:
        return self._headers

    async def aiter_bytes(self, chunk_size: int = 65536) -> AsyncIterator[bytes]:
        raise NotImplementedError

    async def read(self) -> bytes:
        buf = bytearray()
        async for chunk in self.aiter_bytes():
            buf.extend(chunk)
        return bytes(buf)

    async def peek(self, limit: int = 8192) -> bytes:
        """Read up to `limit` bytes for inspection (e.g. an error body), then
        drain and close the rest so no stream generator is left dangling."""
        buf = bytearray()
        try:
            async for chunk in self.aiter_bytes(min(limit, 16384)):
                buf.extend(chunk)
                if len(buf) >= limit:
                    break
        except Exception:
            pass
        return bytes(buf[:limit])

    async def text(self, encoding: str = "utf-8") -> str:
        return (await self.read()).decode(encoding, errors="replace")

    async def aclose(self) -> None:
        pass

    async def __aenter__(self) -> "HttpResponse":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()


class _CurlResponse(HttpResponse):
    def __init__(self, raw):
        super().__init__(raw.status_code, dict(raw.headers.items()))
        self._raw = raw

    async def aiter_bytes(self, chunk_size: int = 65536) -> AsyncIterator[bytes]:
        # curl_cffi streams at libcurl's own buffer size; chunk_size is a hint
        # the other backend honours, not this one.
        async for chunk in self._raw.aiter_content():
            if chunk:
                yield chunk

    async def aclose(self) -> None:
        try:
            await self._raw.aclose()
        except Exception:
            pass


class _AioResponse(HttpResponse):
    def __init__(self, raw, session_ctx):
        super().__init__(raw.status, dict(raw.headers))
        self._raw = raw
        self._session_ctx = session_ctx

    async def aiter_bytes(self, chunk_size: int = 65536) -> AsyncIterator[bytes]:
        async for chunk in self._raw.content.iter_chunked(chunk_size):
            if chunk:
                yield chunk

    async def aclose(self) -> None:
        try:
            self._raw.release()
        except Exception:
            pass
        try:
            await self._session_ctx.close()
        except Exception:
            pass


# --- client --------------------------------------------------------------

# Errors any backend can raise that mean "transport failure, maybe retryable".
NETWORK_ERRORS: tuple = tuple(
    e for e in (
        aiohttp.ClientError, asyncio.TimeoutError,
        *( (_CurlRequestError,) if CURL_AVAILABLE else () ),
        ConnectionError, OSError,
    )
)


class HttpClient:
    """One-shot-ish async HTTP client. Open with `async with`, or let each
    call manage its own connection."""

    def __init__(self, *, headers: Optional[Dict[str, str]] = None,
                 timeout: float = 30.0, force_backend: Optional[str] = None):
        self.base_headers = dict(headers or {})
        self.timeout = timeout
        self._backend = force_backend or ("curl" if CURL_AVAILABLE else "aiohttp")
        self._curl: Any = None

    async def __aenter__(self) -> "HttpClient":
        if self._backend == "curl":
            self._curl = _CurlSession(max_clients=32)
            await self._curl.__aenter__()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._curl is not None:
            try:
                await self._curl.__aexit__(*exc)
            except Exception:
                pass
            self._curl = None

    def _merged_headers(self, url: str, extra: Optional[Dict[str, str]]) -> Dict[str, str]:
        _, preset_headers = identity_for(url)
        h: Dict[str, str] = {}
        if self._backend != "curl":
            h["User-Agent"] = _DEFAULT_UA
        h.update(self.base_headers)
        h.update(preset_headers)
        if extra:
            h.update(extra)
        return h

    async def request(self, method: str, url: str, *, headers: Optional[Dict[str, str]] = None,
                      range_header: Optional[str] = None, allow_redirects: bool = True,
                      stream: bool = True) -> HttpResponse:
        # Reject a malformed URL here with a clear message instead of letting
        # curl_cffi surface a cryptic "Port number was not a decimal number".
        u = urlparse((url or "").strip())
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError(f"Not a valid http(s) URL: {url!r}")
        try:
            _ = u.port  # raises ValueError on a non-numeric port
        except ValueError:
            raise ValueError(f"URL has an invalid port: {url!r}")
        url = url.strip()
        h = self._merged_headers(url, headers)
        if range_header:
            h["Range"] = range_header

        if self._backend == "curl":
            profile, _ = identity_for(url)
            owns = self._curl is None
            sess = self._curl or _CurlSession(max_clients=4)
            if owns:
                await sess.__aenter__()
            try:
                raw = await sess.request(
                    method, url, headers=h, stream=stream,
                    impersonate=profile, allow_redirects=allow_redirects,
                    proxy=_config["proxy"], verify=_config["verify_ssl"],
                    timeout=self.timeout,
                )
            except BaseException:
                if owns:
                    await sess.__aexit__(None, None, None)
                raise
            resp = _CurlResponse(raw)
            if owns:
                _wrap_close(resp, sess)
            return resp

        # aiohttp crash-guard backend
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        if not _config["verify_ssl"]:
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
        connector = aiohttp.TCPConnector(ssl=ssl_ctx)
        session = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=self.timeout,
                                          sock_read=self.timeout),
        )
        try:
            raw = await session.request(method, url, headers=h,
                                        allow_redirects=allow_redirects,
                                        proxy=_config["proxy"])
        except BaseException:
            await session.close()
            raise
        return _AioResponse(raw, session)

    async def head(self, url: str, *, headers: Optional[Dict[str, str]] = None,
                   allow_redirects: bool = True) -> HttpResponse:
        """Self-contained: status + headers only, every connection closed
        before returning. The result has no body to iterate."""
        raw = await self.request("HEAD", url, headers=headers,
                                 allow_redirects=allow_redirects, stream=True)
        detached = HttpResponse(raw.status, dict(raw.headers))
        await raw.aclose()
        return detached

    async def get(self, url: str, *, headers: Optional[Dict[str, str]] = None,
                  range_header: Optional[str] = None,
                  allow_redirects: bool = True) -> HttpResponse:
        return await self.request("GET", url, headers=headers,
                                  range_header=range_header,
                                  allow_redirects=allow_redirects, stream=True)

    async def get_bytes(self, url: str, *, headers: Optional[Dict[str, str]] = None) -> bytes:
        async with await self.get(url, headers=headers) as resp:
            return await resp.read()

    async def get_text(self, url: str, *, headers: Optional[Dict[str, str]] = None) -> str:
        async with await self.get(url, headers=headers) as resp:
            return await resp.text()


def _wrap_close(resp: HttpResponse, sess) -> None:
    orig = resp.aclose

    async def _close():
        await orig()
        try:
            await sess.__aexit__(None, None, None)
        except Exception:
            pass

    resp.aclose = _close  # type: ignore[method-assign]


# --- Cloudflare / bot-wall detection ------------------------------------

_CF_MARKERS = (
    b"cf-mitigated", b"__cf_chl", b"cf_chl_opt", b"Just a moment...",
    b"Attention Required! | Cloudflare", b"Checking your browser before",
    b"Enable JavaScript and cookies to continue",
)


def looks_like_bot_wall(status: int, body_sample: bytes) -> bool:
    """A 403/503 whose body is a challenge page, not the file. Retrying is
    pointless — the caller should fail permanently with a clear message."""
    if status not in (403, 429, 503):
        return False
    return any(m.lower() in body_sample.lower() for m in _CF_MARKERS)
