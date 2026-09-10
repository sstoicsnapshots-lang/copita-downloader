"""
Browser-cookie bridge.

yt-dlp can read the user's logged-in browser session natively
(`cookiesfrombrowser`), but Copita's other engines — the chunked HTTP
downloader, the HLS/DASH downloaders, the page sniffer — just make plain
requests and never saw those cookies. So a site the user is signed into
that serves a direct .mp4 / .m3u8 (or needs a sniff first) would still
fail even with a browser picked in Preferences.

This module extracts the chosen browser's cookie jar once (via yt-dlp's
own extractor, which handles every supported browser's on-disk format and
OS keychain) and hands back a per-URL `Cookie:` header string for those
other engines to attach.
"""

import os
import time
import threading
from typing import Optional
from urllib.request import Request

try:
    import yt_dlp.cookies as _ytc
    _SUPPORTED = set(_ytc.SUPPORTED_BROWSERS)
except Exception:  # pragma: no cover - yt-dlp always present in practice
    _ytc = None
    _SUPPORTED = set()

# Extracting a browser jar touches an on-disk SQLite DB and sometimes the OS
# keychain — slow, and on macOS it can prompt. Cache the jar per browser for
# a short while so a batch of downloads doesn't re-extract per task.
_CACHE_TTL = 300.0
_lock = threading.Lock()
_cache: dict = {}  # browser_name -> (expiry_ts, jar_or_None)


def supported_browser(name: Optional[str]) -> bool:
    return bool(name) and name.lower() in _SUPPORTED


# macOS cookie-DB locations, newest-mtime wins (that's the browser actually
# in use). yt-dlp's own extractor knows how to read each of these.
_MACOS_COOKIE_DBS = {
    "chrome": "~/Library/Application Support/Google/Chrome/Default/Cookies",
    "brave": "~/Library/Application Support/BraveSoftware/Brave-Browser/Default/Cookies",
    "edge": "~/Library/Application Support/Microsoft Edge/Default/Cookies",
    "vivaldi": "~/Library/Application Support/Vivaldi/Default/Cookies",
    "chromium": "~/Library/Application Support/Chromium/Default/Cookies",
    "opera": "~/Library/Application Support/com.operasoftware.Opera/Cookies",
    "firefox": "~/Library/Application Support/Firefox/Profiles",
}

_detect_cache: Optional[tuple] = None  # (expiry, name_or_None)


def detect_default_browser() -> Optional[str]:
    """The supported browser whose cookie store was touched most recently —
    i.e. the one the user actually browses in. Cached for 60 s."""
    global _detect_cache
    now = time.time()
    if _detect_cache and _detect_cache[0] > now:
        return _detect_cache[1]
    best_name, best_mtime = None, 0.0
    for name, path in _MACOS_COOKIE_DBS.items():
        if name not in _SUPPORTED:
            continue
        p = os.path.expanduser(path)
        try:
            m = os.path.getmtime(p)
        except OSError:
            # Firefox: the Profiles dir exists but the cookies file is nested.
            if name == "firefox" and os.path.isdir(p):
                m = os.path.getmtime(p)
            else:
                continue
        if m > best_mtime:
            best_name, best_mtime = name, m
    _detect_cache = (now + 60.0, best_name)
    return best_name


# YouTube specifically does BETTER anonymous — sending it a logged-in
# browser session ties the automated request to an account and trips
# *stricter* bot checks ("The page needs to be reloaded"). Verified: same
# video, anonymous succeeds in ~1s, brave-cookies fails. So YouTube is
# ALWAYS cookie-free here, even when the user has explicitly picked a
# browser — a plain YouTube video never needs auth, and a members-only /
# private / age-gated one is handled by ytdlp_wrapper retrying *once* with
# cookies only after it sees an auth-specific error.
_COOKIE_AVERSE = ("youtube.com", "youtu.be", "youtube-nocookie.com")


def is_cookie_averse(url: Optional[str]) -> bool:
    if not url:
        return False
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return any(host == h or host.endswith("." + h) for h in _COOKIE_AVERSE)


def resolve_browser(setting: Optional[str], url: Optional[str] = None) -> Optional[str]:
    """Turn the stored `cookies_browser` setting into a concrete browser name.
      "auto"       -> the detected default browser (or None)
      "" / None    -> cookies disabled
      "<name>"     -> that browser, if supported
    YouTube is forced cookie-free regardless of the setting (see above).
    """
    if is_cookie_averse(url):
        return None
    if setting == "auto":
        if os.environ.get("COPITA_NO_BROWSER_COOKIES"):
            return None
        return detect_default_browser()
    if not setting:
        return None
    return setting if supported_browser(setting) else None



def _get_jar(browser_name: str):
    now = time.time()
    key = browser_name.lower()
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    jar = None
    if _ytc is not None:
        try:
            jar = _ytc.extract_cookies_from_browser(key)
        except Exception:
            jar = None  # locked DB, browser not installed, permission denied…
    with _lock:
        _cache[key] = (now + _CACHE_TTL, jar)
    return jar


def clear_cache(browser_name: Optional[str] = None) -> None:
    with _lock:
        if browser_name:
            _cache.pop(browser_name.lower(), None)
        else:
            _cache.clear()


def cookie_header_for(url: str, browser_name: Optional[str]) -> str:
    """Return a `Cookie:` header value for `url` from the given browser's
    session, or "" when there's nothing (or the jar can't be read)."""
    if not supported_browser(browser_name) or not url:
        return ""
    jar = _get_jar(browser_name)
    if jar is None:
        return ""
    try:
        req = Request(url)
        jar.add_cookie_header(req)
        return req.get_header("Cookie") or ""
    except Exception:
        return ""
