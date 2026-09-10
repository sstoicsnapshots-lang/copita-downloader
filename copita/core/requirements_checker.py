"""
Dependency checker/installer for Copita's Settings panel.

Not everyone who uses this app knows how to open a terminal and run `brew
install ffmpeg`. This lets Settings show what Copita needs, whether it's
already there, and install what's missing with one click — checking real
system state each time (not a trusted cache) so it never re-offers to
install something that's already present, and never claims something is
installed when it isn't.
"""
import asyncio
import os
import shutil
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple

# The same interpreter the running app uses (see BackendManager.swift's
# resolveWorkspacePath()) — installs must land where the app will actually
# look for them, not wherever a bare `python3` happens to resolve on PATH.
PYTHON_BIN = sys.executable


def _run(cmd: List[str], timeout: int = 15) -> Optional[str]:
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, timeout=timeout)
        return res.stdout.strip() if res.returncode == 0 else None
    except Exception:
        return None


def _check_homebrew() -> Tuple[bool, Optional[str], str]:
    path = shutil.which("brew")
    if path:
        return True, None, f"Found at {path}"
    return False, None, "Not installed"


def _check_ffmpeg() -> Tuple[bool, Optional[str], str]:
    path = shutil.which("ffmpeg")
    if not path:
        return False, None, "Not found on PATH"
    out = _run([path, "-version"])
    version = out.splitlines()[0].replace("ffmpeg version ", "").split()[0] if out else None
    return True, version, f"Found at {path}"


def _check_deno() -> Tuple[bool, Optional[str], str]:
    path = shutil.which("deno")
    if not path:
        return False, None, "Not found on PATH"
    out = _run([path, "--version"])
    version = out.splitlines()[0].replace("deno ", "").strip() if out else None
    return True, version, f"Found at {path}"


def _check_ytdlp() -> Tuple[bool, Optional[str], str]:
    try:
        import yt_dlp
        return True, yt_dlp.version.__version__, "Installed"
    except Exception:
        return False, None, "Not installed"


def _check_curl_cffi() -> Tuple[bool, Optional[str], str]:
    try:
        import curl_cffi
        return True, getattr(curl_cffi, "__version__", "installed"), "Installed"
    except Exception:
        return False, None, "Not installed"


def _check_libtorrent() -> Tuple[bool, Optional[str], str]:
    try:
        import libtorrent as lt
        ver = str(getattr(lt, "__version__", "") or "installed")
        return True, ver, "Installed"
    except Exception as e:
        return False, None, f"Not available ({e})"


def _check_playwright_chromium() -> Tuple[bool, Optional[str], str]:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
        return True, None, "Installed"
    except Exception:
        return False, None, "Playwright or its Chromium browser is missing"


def find_system_browser_binary() -> Optional[str]:
    """Same search Copita's scrapers already use (scribd/flipbook/video-embed
    downloaders) — kept here too so Settings reports the same answer they'd get."""
    candidates = [
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        shutil.which("brave"),
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def _check_system_browser() -> Tuple[bool, Optional[str], str]:
    path = find_system_browser_binary()
    if path:
        return True, os.path.basename(path.rstrip("/")), f"Found: {path}"
    return False, None, "No Chrome/Brave/Chromium found"


# Each entry: check() returns (installed, version, detail). install_cmd is
# None for things this panel can't safely auto-install (Homebrew's own
# installer is a bigger, harder-to-reverse system change than a scoped
# `brew install <formula>` — point the user at it instead of running it
# unattended; libtorrent ships vendored with the app, not user-installable
# here). `requires` names another entry's id that must already be installed.
REQUIREMENTS: List[Dict[str, Any]] = [
    {
        "id": "homebrew",
        "label": "Homebrew",
        "description": "Package manager macOS doesn't ship with — needed to install FFmpeg and a browser for you.",
        "check": _check_homebrew,
        "install_cmd": None,
        "manual_url": "https://brew.sh",
        "required": False,
    },
    {
        "id": "ffmpeg",
        "label": "FFmpeg",
        "description": "Required to remux and transcode downloaded video/audio streams.",
        "check": _check_ffmpeg,
        "install_cmd": ["brew", "install", "ffmpeg"],
        "requires": "homebrew",
        "required": True,
    },
    {
        "id": "ytdlp",
        "label": "yt-dlp",
        "description": "Powers YouTube/TikTok/Twitter/Reddit and other video-platform downloads.",
        "check": _check_ytdlp,
        "install_cmd": [PYTHON_BIN, "-m", "pip", "install", "-U", "yt-dlp"],
        "required": True,
    },
    {
        "id": "curl_cffi",
        "label": "Browser-fingerprint HTTP",
        "description": "Gives direct downloads a real Chrome TLS fingerprint so sites behind Cloudflare/Akamai stop rejecting them.",
        "check": _check_curl_cffi,
        "install_cmd": [PYTHON_BIN, "-m", "pip", "install", "-U", "curl_cffi"],
        "required": True,
    },
    {
        "id": "deno",
        "label": "Deno (JS runtime)",
        "description": "Lets yt-dlp solve YouTube's signature challenge — without it, many videos fail with HTTP 403 instead of downloading.",
        "check": _check_deno,
        "install_cmd": ["brew", "install", "deno"],
        "requires": "homebrew",
        "required": False,
    },
    {
        "id": "libtorrent",
        "label": "BitTorrent engine",
        "description": "Powers magnet link and .torrent downloads.",
        "check": _check_libtorrent,
        # There's no pip wheel for libtorrent on this app's Python version
        # yet (checked live: PyPI publishes cp39-cp313, nothing for cp314) --
        # Homebrew's own libtorrent-rasterbar formula already builds real
        # Python bindings against the exact same Python version (it depends
        # on python@3.14), so this installs through Homebrew instead of pip
        # like the other requirements here. See the matching PYTHONPATH
        # addition in BackendManager.swift that makes the installed module
        # actually importable by this app's own Python afterward.
        "install_cmd": ["brew", "install", "libtorrent-rasterbar"],
        "requires": "homebrew",
        "required": True,
    },
    {
        "id": "playwright",
        "label": "Playwright + Chromium",
        "description": "Needed for a few JS-heavy sites (manga scraping, protected documents).",
        "check": _check_playwright_chromium,
        "pre_install_cmd": [PYTHON_BIN, "-m", "pip", "install", "-U", "playwright"],
        "install_cmd": [PYTHON_BIN, "-m", "playwright", "install", "chromium"],
        "required": False,
    },
    {
        "id": "system_browser",
        "label": "Chrome / Brave browser",
        "description": "Needed to unlock some protected video embeds and document previews.",
        "check": _check_system_browser,
        "install_cmd": ["brew", "install", "--cask", "brave-browser"],
        "requires": "homebrew",
        "required": False,
    },
]

_BY_ID = {r["id"]: r for r in REQUIREMENTS}


class RequirementsManager:
    def __init__(self):
        self.listeners: List[Callable[[Dict[str, Any]], None]] = []
        self.state: Dict[str, Dict[str, Any]] = {}
        self._installing: set = set()
        # asyncio only holds a *weak* reference to a task — if nothing else
        # keeps it alive, it can be (and was, before this fix) garbage
        # collected before the event loop ever gets to run it, so every
        # install silently did nothing at all despite the endpoint
        # returning 200 "started". This set is that strong reference.
        self._background_tasks: set = set()
        self.refresh_all()

    def start_install(self, req_id: str):
        """Schedules install() and keeps a live reference to the task so it
        can't be garbage-collected before it runs — see __init__ note."""
        task = asyncio.create_task(self.install(req_id))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def register_listener(self, cb: Callable[[Dict[str, Any]], None]):
        self.listeners.append(cb)

    def _broadcast(self, req_id: str):
        data = {"event": "requirement_updated", "requirement": self.state[req_id]}
        for listener in list(self.listeners):
            try:
                listener(data)
            except Exception:
                pass

    def _apply_check_result(self, req: Dict[str, Any], installed: bool, version: Optional[str], detail: str):
        status = "installing" if req["id"] in self._installing else ("installed" if installed else "missing")
        self.state[req["id"]] = {
            "id": req["id"],
            "label": req["label"],
            "description": req["description"],
            "installed": installed,
            "version": version,
            "detail": detail,
            "installable": req.get("install_cmd") is not None,
            "required": req["required"],
            "manual_url": req.get("manual_url"),
            "status": status,
        }

    def _refresh_one(self, req: Dict[str, Any]):
        """Sync, direct call — only safe where no asyncio loop is already
        running (module import / construction time). Playwright's sync API
        (used by the playwright check) raises immediately if called from
        inside a running loop, so anywhere a loop might already be running
        must go through _refresh_one_async instead."""
        installed, version, detail = req["check"]()
        self._apply_check_result(req, installed, version, detail)

    async def _refresh_one_async(self, req: Dict[str, Any]):
        loop = asyncio.get_running_loop()
        installed, version, detail = await loop.run_in_executor(None, req["check"])
        self._apply_check_result(req, installed, version, detail)

    def refresh_all(self):
        for req in REQUIREMENTS:
            self._refresh_one(req)

    async def refresh_all_async(self):
        for req in REQUIREMENTS:
            await self._refresh_one_async(req)

    def get_all(self) -> List[Dict[str, Any]]:
        return [self.state[r["id"]] for r in REQUIREMENTS]

    def validate_install(self, req_id: str):
        """Fast, synchronous checks so a bad request fails immediately with a
        clear reason instead of a background task silently going nowhere."""
        req = _BY_ID.get(req_id)
        if not req:
            raise ValueError(f"Unknown requirement: {req_id}")
        if not req.get("install_cmd"):
            raise ValueError(f"{req['label']} can't be installed from here.")
        if self.state[req_id]["installed"]:
            raise ValueError(f"{req['label']} is already installed.")
        if req_id in self._installing:
            raise ValueError(f"{req['label']} is already installing.")
        needs = req.get("requires")
        if needs and not self.state[needs]["installed"]:
            raise ValueError(f"Install {_BY_ID[needs]['label']} first.")

    async def install(self, req_id: str):
        req = _BY_ID[req_id]
        self._installing.add(req_id)
        self.state[req_id]["status"] = "installing"
        self._broadcast(req_id)
        error: Optional[str] = None
        try:
            for cmd in filter(None, [req.get("pre_install_cmd"), req["install_cmd"]]):
                proc = await asyncio.create_subprocess_exec(
                    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
                )
                out, _ = await proc.communicate()
                if proc.returncode != 0:
                    tail = (out or b"").decode(errors="ignore").strip().splitlines()[-1:] or [""]
                    raise RuntimeError(f"`{' '.join(cmd)}` failed: {tail[0]}")
        except Exception as e:
            error = str(e)
        finally:
            # However install went, _installing must always be cleared here —
            # otherwise a crash mid-install would leave this requirement
            # permanently stuck showing "Installing…" with no way to retry.
            self._installing.discard(req_id)
            await self._refresh_one_async(req)
            if error:
                self.state[req_id]["status"] = "error"
                self.state[req_id]["detail"] = error
            self._broadcast(req_id)
