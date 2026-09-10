"""
Keep yt-dlp fresh.

YouTube/TikTok/etc. change constantly and break yt-dlp roughly weekly — a
stale yt-dlp is the #1 cause of "it stopped working". This checks PyPI on a
background thread at startup (and once a day after) and pip-installs a newer
release if there is one. Never blocks; failures are logged and ignored.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from typing import Optional

_CHECK_INTERVAL = 24 * 3600
_last_check = 0.0
_lock = threading.Lock()
_last_result: dict = {"checked_at": 0, "current": None, "latest": None, "updated": False}
_listeners: list = []


def register_listener(cb) -> None:
    """cb(dict) is called (from a background thread) with each update event:
    {event:'component_update', component:'yt-dlp', state, from, to, message}."""
    _listeners.append(cb)


def _emit(state: str, message: str, frm: Optional[str] = None, to: Optional[str] = None) -> None:
    payload = {"event": "component_update", "component": "yt-dlp",
               "state": state, "message": message, "from": frm, "to": to}
    for cb in list(_listeners):
        try:
            cb(payload)
        except Exception:
            pass


def _installed_version() -> Optional[str]:
    try:
        import yt_dlp
        return yt_dlp.version.__version__
    except Exception:
        return None


def _latest_pypi_version(timeout: float = 8.0) -> Optional[str]:
    try:
        from curl_cffi import requests as _rq
        r = _rq.get("https://pypi.org/pypi/yt-dlp/json", impersonate="chrome", timeout=timeout)
        if r.status_code == 200:
            return r.json().get("info", {}).get("version")
    except Exception:
        pass
    try:
        import urllib.request
        import ssl
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
        req = urllib.request.Request("https://pypi.org/pypi/yt-dlp/json",
                                     headers={"User-Agent": "Copita/1.0"})
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return json.load(resp).get("info", {}).get("version")
    except Exception:
        return None


def _version_tuple(v: str):
    # yt-dlp versions are date-based: 2026.08.19, sometimes with a suffix.
    parts = []
    for chunk in v.replace("-", ".").split("."):
        parts.append(int(chunk) if chunk.isdigit() else 0)
    return tuple(parts)


def _do_check_and_update() -> None:
    global _last_result
    current = _installed_version()
    latest = _latest_pypi_version()
    result = {"checked_at": int(time.time()), "current": current,
              "latest": latest, "updated": False}
    if current and latest and _version_tuple(latest) > _version_tuple(current):
        _emit("updating", f"Updating media engine {current} → {latest}…", current, latest)
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-U", "--disable-pip-version-check",
                 "yt-dlp"],
                capture_output=True, timeout=180,
            )
            new = _installed_version()
            result["updated"] = bool(new and new != current)
            result["current"] = new or current
            if result["updated"]:
                _emit("updated", f"Media engine updated to {new}", current, new)
            else:
                _emit("failed", "Media-engine update didn't apply — will retry later", current, latest)
        except Exception:
            _emit("failed", "Couldn't update the media engine — will retry later", current, latest)
    else:
        _emit("current", f"Media engine is up to date ({current})" if current else "Media engine not installed",
              current, current)
    with _lock:
        _last_result = result


def status() -> dict:
    with _lock:
        return dict(_last_result)


def maybe_check(force: bool = False) -> None:
    """Fire a check on a daemon thread if it's due. Safe to call often."""
    global _last_check
    now = time.time()
    if not force and now - _last_check < _CHECK_INTERVAL:
        return
    _last_check = now
    threading.Thread(target=_do_check_and_update, daemon=True,
                     name="copita-ytdlp-autoupdate").start()


def start_periodic() -> None:
    """Called once at backend startup — checks shortly after launch (so the
    UI has connected and can show the banner), then every 24 h."""
    def _loop():
        time.sleep(6)  # let a frontend connect its WebSocket first
        while True:
            _do_check_and_update()
            time.sleep(_CHECK_INTERVAL)
    threading.Thread(target=_loop, daemon=True, name="copita-ytdlp-autoupdate-loop").start()
