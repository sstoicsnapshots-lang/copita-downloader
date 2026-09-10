"""
Download Task Manager and State Machine for Copita.
Coordinates concurrent tasks, telemetry broadcasting, and worker engines.
"""

import os
import re
import json
import uuid
import time
import asyncio
import hashlib
import subprocess
from urllib.parse import urlparse, parse_qs, unquote_plus
from typing import Dict, Any, Optional, List, Callable
from copita.core.classifier import classify_url, LinkType
from copita.core.chunk_downloader import ChunkDownloader
from copita.core.hls_downloader import TurboHlsDownloader
from copita.core.ytdlp_wrapper import YtDlpWrapper
from copita.core.scribd_downloader import ScribdDownloader
from copita.core.flipbook_downloader import FlipbookDownloader
from copita.core.deepzoom_downloader import DeepZoomDownloader
from copita.core.sketchfab_downloader import SketchfabDownloader
from copita.core.spotify_downloader import SpotifyDownloader
from copita.core.filehoster_downloader import FileHosterDownloader
from copita.core.manga_downloader import MangaDownloader

class DownloadTask:
    def __init__(self, task_id: str, url: str, options: Optional[Dict[str, Any]] = None):
        self.id = task_id
        self.url = url
        self.options = options or {}
        self.classification = classify_url(url)
        self.title = self.classification.get("name") or "New Download"
        self.category = self.classification.get("category", "file")
        self.engine = self.classification.get("engine", "turbo_chunk")
        if self.engine == 'manga':
            self.category = 'manga'
        elif self.options.get('audio_only'):
            self.category = 'audio'
        # Playlist grouping: children of one expanded playlist carry the same
        # group id so the UI can fold them into a single collapsible row
        # ("<playlist> — 7 of 20").
        _g = self.options.get("group") or {}
        self.group_id: Optional[str] = _g.get("id")
        self.group_title: Optional[str] = _g.get("title")
        self.group_index: Optional[int] = _g.get("index")
        self.group_total: Optional[int] = _g.get("total")
        self.status = "queued"
        self.percent = 0.0
        self.downloaded_bytes = 0
        self.total_bytes: Optional[int] = None
        self.speed = 0.0
        self.speed_available = False
        self.eta: Optional[float] = None
        self.threads = 1
        self.output_path: Optional[str] = None
        self.error: Optional[str] = None
        self.error_details: Optional[str] = None
        self.created_at = time.time()
        self.worker_instance: Any = None
        self.async_task: Optional[asyncio.Task] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "url": self.url,
            "title": self.title,
            "category": self.category,
            "engine": self.engine,
            "status": self.status,
            "percent": round(self.percent, 1),
            "downloaded": self.downloaded_bytes,
            "total": self.total_bytes,
            "speed": round(self.speed, 1),
            "speed_available": self.speed_available,
            "eta": round(self.eta, 1) if self.eta is not None else None,
            "threads": self.threads,
            "output_path": self.output_path,
            "filename": os.path.basename(self.output_path) if self.output_path else None,
            "error": self.error,
            "error_details": self.error_details,
            "can_pause": self.status == 'downloading' and hasattr(self.worker_instance, 'pause'),
            "created_at": self.created_at,
            "group": ({
                "id": self.group_id,
                "title": self.group_title,
                "index": self.group_index,
                "total": self.group_total,
            } if self.group_id else None),
        }

    # Statuses that only make sense while a live async_task is actually
    # running them. If the app was closed with a task in one of these, on
    # reload there is no running worker behind it any more.
    _NON_TERMINAL_STATUSES = {"queued", "analyzing", "downloading", "merging", "compressing", "extracting", "paused"}

    def to_state_dict(self) -> Dict[str, Any]:
        """Everything needed to fully reconstruct this task across an app
        restart — to_dict() only carries what the UI needs to display it."""
        return {
            "id": self.id,
            "url": self.url,
            "options": self.options,
            "title": self.title,
            "category": self.category,
            "engine": self.engine,
            "status": self.status,
            "percent": self.percent,
            "downloaded_bytes": self.downloaded_bytes,
            "total_bytes": self.total_bytes,
            "output_path": self.output_path,
            "error": self.error,
            "error_details": self.error_details,
            "created_at": self.created_at,
        }

    @classmethod
    def from_state_dict(cls, data: Dict[str, Any]) -> "DownloadTask":
        task = cls(data["id"], data["url"], data.get("options"))
        task.title = data.get("title") or task.title
        task.category = data.get("category") or task.category
        task.engine = data.get("engine") or task.engine
        task.created_at = data.get("created_at", task.created_at)
        task.output_path = data.get("output_path")
        status = data.get("status", "queued")
        if status in cls._NON_TERMINAL_STATUSES:
            # Nothing is actually running any more after a restart. Chunked
            # downloads keep their partial `.chunks` files on disk though, so
            # Retry will resume from where this left off rather than
            # restarting the whole file.
            task.status = "failed"
            task.error = "Interrupted — Copita was closed while this was in progress. Click Retry to resume."
            task.percent = data.get("percent", 0.0)
            task.downloaded_bytes = data.get("downloaded_bytes", 0)
            task.total_bytes = data.get("total_bytes")
        else:
            task.status = status
            task.percent = data.get("percent", 0.0)
            task.downloaded_bytes = data.get("downloaded_bytes", 0)
            task.total_bytes = data.get("total_bytes")
            task.error = data.get("error")
            task.error_details = data.get("error_details")
        return task

# Some image CDNs serve WebP regardless of what extension the request URL
# ends in (content negotiation for browser compatibility) -- a URL ending
# in "foo.png" can come back as real WebP bytes. Trusting the URL's
# extension alone saved files with a mismatched extension: QuickLook (and
# any other app) correctly refuses to decode WebP data as PNG, so the file
# just looked broken with no preview/thumbnail even though it opens fine
# once renamed. Sniffing the real file signature after download and
# correcting the extension to match what's actually on disk fixes it at
# the source instead of working around it in the UI.
_MAGIC_BYTE_EXTENSIONS = [
    (b'\x89PNG\r\n\x1a\n', 0, 'png'),
    (b'\xff\xd8\xff', 0, 'jpg'),
    (b'GIF87a', 0, 'gif'),
    (b'GIF89a', 0, 'gif'),
    (b'WEBP', 8, 'webp'),
    (b'BM', 0, 'bmp'),
]


# Extensions worth surfacing as a past download when Copita scans the
# download folder on launch. Deliberately an allowlist of things people
# actually download — not "every file", which pulled in editor scratch
# files, logs, notes, CSVs etc. from a real ~/Downloads and cluttered the
# list on every launch.
_ADOPTABLE_EXTS = frozenset((
    # video
    'mp4', 'mkv', 'webm', 'mov', 'avi', 'm4v', 'flv', 'mpg', 'mpeg', 'ts',
    'wmv', 'm2ts',
    # audio
    'mp3', 'm4a', 'm4b', 'flac', 'wav', 'aac', 'ogg', 'opus', 'oga', 'wma',
    'aiff', 'alac',
    # image
    'jpg', 'jpeg', 'png', 'webp', 'gif', 'bmp', 'tiff', 'tif', 'heic',
    # books / documents
    'pdf', 'epub', 'mobi', 'azw', 'azw3', 'djvu', 'fb2', 'cbz', 'cbr',
    'docx', 'doc', 'pptx', 'ppt', 'xlsx', 'xls', 'odt', 'ods', 'odp',
    # archives / disk images / installers
    'zip', 'rar', '7z', 'tar', 'gz', 'xz', 'bz2', 'tgz', 'txz', 'iso',
    'dmg', 'pkg', 'exe', 'msi', 'deb', 'rpm', 'appimage', 'apk',
    # 3d / subtitles / torrents
    'glb', 'gltf', 'srt', 'vtt', 'ass', 'torrent',
))


def _category_for_filename(fname: str) -> str:
    f = fname.lower()
    if f.endswith(('.mp4', '.mkv', '.webm', '.mov', '.avi', '.m4v', '.flv')):
        return 'video'
    if f.endswith(('.mp3', '.m4a', '.flac', '.wav', '.aac', '.ogg', '.opus', '.oga')):
        return 'audio'
    if f.endswith(('.pdf', '.epub', '.mobi', '.azw3', '.djvu', '.docx', '.doc', '.txt', '.rtf')):
        return 'document'
    if f.endswith(('.cbz', '.cbr')):
        return 'manga'
    if f.endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.tiff', '.heic')):
        return 'image'
    if f.endswith(('.zip', '.rar', '.7z', '.tar', '.gz', '.xz', '.bz2', '.tgz')):
        return 'archive'
    if f.endswith(('.gltf', '.glb')):
        return 'model_3d'
    if f.endswith(('.torrent', '.magnet')):
        return 'torrent'
    return 'file'


def _correct_extension_from_content(path: str) -> str:
    """Renames path if its real file signature doesn't match its extension.
    Returns the (possibly new) path."""
    try:
        with open(path, 'rb') as f:
            head = f.read(16)
    except OSError:
        return path

    real_ext = None
    for magic, offset, ext in _MAGIC_BYTE_EXTENSIONS:
        if head[offset:offset + len(magic)] == magic:
            real_ext = ext
            break
    if not real_ext:
        return path

    current_ext = os.path.splitext(path)[1].lstrip('.').lower()
    if current_ext == real_ext or (current_ext in ('jpg', 'jpeg') and real_ext == 'jpg'):
        return path

    new_path = f"{os.path.splitext(path)[0]}.{real_ext}"
    try:
        if os.path.exists(new_path):
            new_path = f"{os.path.splitext(path)[0]}_{uuid.uuid4().hex[:6]}.{real_ext}"
        os.rename(path, new_path)
        return new_path
    except OSError:
        return path


# A pasted playlist link is expanded into one child download per item. Past
# this many items it's treated as a bulk operation the user should scope
# themselves (a specific track, or a smaller playlist) rather than silently
# queueing hundreds of files or silently grabbing only the first one.
_PLAYLIST_TASK_CAP = 200


def _safe_dir_component(name: str) -> str:
    """Turn a playlist/collection title into a single safe folder name.
    Strips path separators, reserved characters and leading dots so the
    result can only ever be one directory directly under the download
    folder — never a traversal (`..`) or an absolute path."""
    name = (name or "").strip()
    # kill anything that could make this more than one path segment
    name = re.sub(r'[\\/]+', ' ', name)
    name = re.sub(r'[*?:"<>|\x00-\x1f]+', '', name)
    name = name.strip(' .')
    name = re.sub(r'\s+', ' ', name)
    if name in ('', '.', '..'):
        return ''
    return name[:120].strip(' .')


class TaskManager:
    def __init__(self, download_dir: str):
        self.download_dir = os.path.abspath(download_dir)
        os.makedirs(self.download_dir, exist_ok=True)
        self.tasks: Dict[str, DownloadTask] = {}
        self.listeners: List[Callable[[Dict[str, Any]], None]] = []
        # "auto" = use whichever browser the user actually browses in (its
        # cookie DB was touched most recently); "" = off; a name = that one.
        # Defaulting to auto means signed-in downloads and much higher
        # YouTube rate limits work out of the box.
        self.cookies_browser: str = "auto"
        self.default_threads = 16
        # HTTP-client controls shared by every direct-download engine (see
        # copita.core.http_client). proxy_url = "" means direct.
        self.proxy_url: str = ""
        self.verify_ssl: bool = True
        self.identity_presets: List[Dict[str, Any]] = []
        # Global download speed cap in KB/s; 0 = unlimited.
        self.max_speed_kbps: int = 0
        # Every task used to fire off in full parallel the instant it was
        # added — paste 10 links and 10 downloads (each already using up to
        # 16 connections) started at once. IDM/JDownloader cap simultaneous
        # downloads and queue the rest; this does the same. A plain Semaphore
        # can't have its size changed at runtime, so this is a manual gate
        # instead, letting a settings change take effect immediately for
        # tasks already waiting in queue.
        self.max_concurrent_downloads = 3
        self._active_slots = 0
        self._queue_condition = asyncio.Condition()
        # Kept next to (not inside) downloads/ so it never gets swept up by
        # the "what files already exist" scan below or shown as a download.
        self._state_path = os.path.join(os.path.dirname(self.download_dir), ".copita_tasks.json")
        self._settings_path = os.path.join(os.path.dirname(self.download_dir), ".copita_settings.json")
        self._last_state_save = 0.0
        self._load_settings()
        self.apply_http_config()
        self._load_state()
        self._load_existing_files()

    def _load_settings(self):
        """Preferences (browser session, connections, simultaneous downloads)
        persist across restarts — previously they lived only in memory and
        reset to defaults every time the backend relaunched."""
        if not os.path.isfile(self._settings_path):
            return
        try:
            with open(self._settings_path, "r") as f:
                s = json.load(f)
        except (OSError, ValueError):
            return
        if isinstance(s.get("cookies_browser"), str):
            self.cookies_browser = s["cookies_browser"]
        if isinstance(s.get("default_threads"), int) and 1 <= s["default_threads"] <= 64:
            self.default_threads = s["default_threads"]
        if isinstance(s.get("max_concurrent_downloads"), int) and 1 <= s["max_concurrent_downloads"] <= 20:
            self.max_concurrent_downloads = s["max_concurrent_downloads"]
        if isinstance(s.get("proxy_url"), str):
            self.proxy_url = s["proxy_url"]
        if isinstance(s.get("verify_ssl"), bool):
            self.verify_ssl = s["verify_ssl"]
        if isinstance(s.get("identity_presets"), list):
            self.identity_presets = s["identity_presets"]
        if isinstance(s.get("max_speed_kbps"), (int, float)) and s["max_speed_kbps"] >= 0:
            self.max_speed_kbps = int(s["max_speed_kbps"])
        self.apply_http_config()

    def cookies(self, url: Optional[str] = None) -> Optional[str]:
        """The concrete browser name to pull cookies from, resolving the
        'auto' setting to the user's actual default browser. `url` matters:
        on 'auto', YouTube is deliberately left cookie-free (a logged-in
        session trips *stricter* bot checks there — verified)."""
        try:
            from copita.core.browser_cookies import resolve_browser
            return resolve_browser(self.cookies_browser, url)
        except Exception:
            return self.cookies_browser if self.cookies_browser not in ("auto", "") else None

    def auth_browser(self) -> Optional[str]:
        """The browser the user explicitly picked in Preferences (not 'auto'
        / off) — used only as the fallback for an auth-required retry
        (members-only / age-restricted), where even YouTube should honour the
        user's choice."""
        b = self.cookies_browser
        return b if b and b not in ("auto", "") else None

    def apply_http_config(self) -> None:
        """Push HTTP-client + speed-limit settings into the shared modules so
        every engine picks them up."""
        try:
            from copita.core import http_client
            http_client.configure(
                proxy=self.proxy_url or None,
                verify_ssl=self.verify_ssl,
                identity_presets=self.identity_presets,
            )
        except Exception:
            pass
        try:
            from copita.core.speed_gate import set_global_limit_kbps
            set_global_limit_kbps(self.max_speed_kbps)
        except Exception:
            pass

    def save_settings(self):
        try:
            data = {
                "cookies_browser": self.cookies_browser,
                "default_threads": self.default_threads,
                "max_concurrent_downloads": self.max_concurrent_downloads,
                "proxy_url": self.proxy_url,
                "verify_ssl": self.verify_ssl,
                "identity_presets": self.identity_presets,
                "max_speed_kbps": self.max_speed_kbps,
            }
            tmp = self._settings_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self._settings_path)
        except OSError:
            pass

    def _load_state(self):
        if not os.path.isfile(self._state_path):
            return
        try:
            with open(self._state_path, "r") as f:
                entries = json.load(f)
            for entry in entries:
                task = DownloadTask.from_state_dict(entry)
                self.tasks[task.id] = task
        except (OSError, ValueError, KeyError):
            pass  # corrupt or unreadable state file — start fresh rather than crash launch

    def save_state(self):
        # Cheap, and called from hot progress-update paths, so throttle to
        # avoid a disk write on every single progress tick.
        now = time.time()
        if now - self._last_state_save < 2.0:
            return
        self._last_state_save = now
        self._save_state_now()

    def _save_state_now(self):
        try:
            data = [t.to_state_dict() for t in self.tasks.values()]
            tmp_path = self._state_path + ".tmp"
            with open(tmp_path, "w") as f:
                json.dump(data, f)
            os.replace(tmp_path, self._state_path)
        except OSError:
            pass

    def _load_existing_files(self):
        if not os.path.exists(self.download_dir):
            return
        known_paths = {os.path.abspath(t.output_path)
                       for t in self.tasks.values() if t.output_path}
        # Folders an existing task already owns — a multi-file torrent whose
        # output_path IS the folder, or a playlist folder whose child tasks
        # point inside it. Don't re-scan those into loose rows.
        claimed_dirs = set()
        for p in known_paths:
            claimed_dirs.add(p.rstrip('/'))          # the torrent-folder case
            claimed_dirs.add(os.path.dirname(p))     # the file-inside-folder case

        def _adoptable(name: str) -> bool:
            b = os.path.basename(name)
            ext = b.rsplit('.', 1)[-1].lower() if '.' in b else ''
            # Allowlist, not "everything minus a few suffixes" — a real
            # ~/Downloads is full of logs, notes, CSVs, editor scratch files,
            # half-finished sidecars and no-extension junk that should never
            # show up as "completed downloads".
            return not b.startswith('.') and ext in _ADOPTABLE_EXTS

        # Top-level files, plus one level of subfolders. An expanded playlist /
        # audiobook lands as a folder of tracks exactly one level down — those
        # are adopted as one collapsible group (like a live playlist row),
        # not a flat list of loose chapter files.
        top_files = []          # [(name, path)]
        subfolders = []         # [(folder_name, [(name, path), ...])]
        for entry in sorted(os.listdir(self.download_dir)):
            epath = os.path.join(self.download_dir, entry)
            if os.path.isfile(epath):
                if _adoptable(entry):
                    top_files.append((entry, epath))
            elif os.path.isdir(epath) and not entry.startswith('.'):
                if os.path.abspath(epath) in claimed_dirs:
                    continue  # a torrent / playlist task already represents this folder
                inner = [(os.path.join(entry, s), os.path.join(epath, s))
                         for s in sorted(os.listdir(epath))
                         if os.path.isfile(os.path.join(epath, s)) and _adoptable(s)]
                if inner:
                    subfolders.append((entry, inner))

        def _add(fname, fpath, group=None):
            if os.path.abspath(fpath) in known_paths:
                return
            base = os.path.basename(fname)
            fsize = os.path.getsize(fpath)
            tid = hashlib.md5(fname.encode()).hexdigest()[:8]
            opts = {"group": group} if group else None
            t = DownloadTask(tid, f"local://{base}", opts)
            t.title = base
            t.status = "completed"
            t.percent = 100.0
            t.downloaded_bytes = fsize
            t.total_bytes = fsize
            t.output_path = fpath
            t.created_at = os.path.getmtime(fpath)
            t.category = _category_for_filename(fname)
            self.tasks[tid] = t

        for fname, fpath in top_files:
            _add(fname, fpath)
        for folder, inner in subfolders:
            # A lone file in a folder isn't a playlist — adopt it flat.
            group = None
            if len(inner) >= 2:
                gid = "dir-" + hashlib.md5(folder.encode()).hexdigest()[:8]
                group = {"id": gid, "title": folder, "index": 0, "total": len(inner)}
            for i, (fname, fpath) in enumerate(inner, 1):
                _add(fname, fpath, dict(group, index=i) if group else None)

    def register_listener(self, callback: Callable[[Dict[str, Any]], None]):
        self.listeners.append(callback)

    def unregister_listener(self, callback: Callable[[Dict[str, Any]], None]):
        if callback in self.listeners:
            self.listeners.remove(callback)

    def broadcast(self, event_type: str, task: DownloadTask):
        data = {
            "event": event_type,
            "task": task.to_dict()
        }
        for listener in list(self.listeners):
            try:
                listener(data)
            except Exception:
                pass
        # A task disappearing or reaching a terminal state is worth saving
        # immediately (nothing worse than losing that on the next restart);
        # everything else (in-progress percent ticks) is fine throttled.
        if event_type == "task_deleted" or task.status in ("completed", "failed", "cancelled"):
            self._save_state_now()
        else:
            self.save_state()

    async def _acquire_slot(self):
        async with self._queue_condition:
            while self._active_slots >= self.max_concurrent_downloads:
                await self._queue_condition.wait()
            self._active_slots += 1

    async def _release_slot(self):
        async with self._queue_condition:
            self._active_slots -= 1
            self._queue_condition.notify_all()

    async def set_max_concurrent(self, n: int):
        self.max_concurrent_downloads = max(1, n)
        async with self._queue_condition:
            self._queue_condition.notify_all()
        self.save_settings()

    def add_task(self, url: str, options: Optional[Dict[str, Any]] = None) -> DownloadTask:
        task_id = str(uuid.uuid4())[:8]
        task = DownloadTask(task_id, url, options)
        self.tasks[task_id] = task
        self.broadcast("task_added", task)
        task.async_task = asyncio.create_task(self._run_task(task))
        return task

    @staticmethod
    def _is_single_media_url(url: str) -> bool:
        """A watch/share URL that merely carries a playlist context (e.g.
        youtube.com/watch?v=X&list=Y, youtu.be/X?list=Y) is still a request
        for that one video — not the whole playlist."""
        p = urlparse(url)
        host = (p.hostname or "").lower()
        query = parse_qs(p.query)
        if host == "youtu.be" and p.path.strip("/"):
            return True
        if "youtube.com" in host and "v" in query:
            return True
        return False

    async def _maybe_expand_playlist(self, task: DownloadTask) -> bool:
        """If the pasted URL points at a playlist / multi-track collection,
        replace this task with one child task per item and return True.
        A single item, an unsupported URL, or a probe failure returns False
        and the task downloads normally."""
        if task.options.get("_playlist_child"):
            return False

        cls = task.classification
        parsed = urlparse(task.url)
        host = (parsed.hostname or "").lower()
        probe_url = task.url
        loop = asyncio.get_running_loop()

        # --- Spotify albums / playlists -> one child task per track ----------
        if task.engine == "spotify" and cls.get("spotify_kind") in ("album", "playlist"):
            from copita.core.spotify_downloader import SpotifyDownloader
            task.status = "analyzing"
            self.broadcast("task_updated", task)
            try:
                coll = await loop.run_in_executor(
                    None, SpotifyDownloader().get_collection, task.url)
            except Exception:
                return False
            if not coll or len(coll.get("tracks", [])) < 2:
                return False
            entries = [
                {"url": f"https://open.spotify.com/track/{t['id']}",
                 "title": f"{t['artist']} - {t['title']}".strip(" -")}
                for t in coll["tracks"]
            ]
            return await self._expand_into_children(task, coll.get("title") or task.title,
                                                    entries, len(entries))

        # archive.org item pages route to the generic sniffer (which only ever
        # grabs the first file); catch multi-track items here before that.
        is_archive = (host == "archive.org" or host.endswith(".archive.org")) and "/details/" in parsed.path
        if is_archive:
            # Normalise /details/<item>/<trail> down to /details/<item> unless
            # <trail> is a real filename (has an extension) — the browser
            # address bar and archive.org's own player append a non-file slug
            # for the currently-selected track (e.g.
            # /details/<item>/<item>), which yt-dlp then reads as a
            # single-file request and never sees the rest of the collection.
            segs = [s for s in parsed.path.split("/") if s]
            if len(segs) >= 2 and segs[0] == "details":
                trail = segs[-1] if len(segs) >= 3 else ""
                if trail and "." in trail:
                    # A genuine /details/<item>/<file.ext> link — one file.
                    return False
                probe_url = f"{parsed.scheme}://{parsed.netloc}/details/{segs[1]}"

        is_ytdlp = task.engine == "ytdlp" or cls.get("type") == LinkType.MEDIA_PLATFORM
        if not (is_ytdlp or is_archive):
            return False
        if self._is_single_media_url(task.url):
            return False

        # YouTube/youtu.be: only a real playlist URL expands. A search page,
        # channel, feed, or homepage is NOT a playlist — yt-dlp's flat probe
        # will happily return one as a "playlist" of hundreds of unrelated
        # results (a pasted /results?search_query=… page came back as "410
        # items"), which is never what the user meant.
        if is_ytdlp and ("youtube.com" in host or host == "youtu.be"):
            q = parse_qs(parsed.query)
            path = parsed.path.rstrip("/")
            if path == "/results" or "search_query" in q:
                task.status = "failed"
                task.error = (
                    "That's a YouTube search page, not a video or playlist. "
                    "Open the video or playlist you want and paste that link."
                )
                self.broadcast("task_updated", task)
                return True
            is_playlist_url = "/playlist" in parsed.path or (
                "list" in q and "v" not in q and host != "youtu.be")
            if not is_playlist_url:
                return False

        task.status = "analyzing"
        self.broadcast("task_updated", task)

        wrapper = YtDlpWrapper(cookies_browser=self.cookies(probe_url), auth_browser=self.auth_browser())
        try:
            probe = await loop.run_in_executor(None, lambda: wrapper.probe_entries(probe_url))
        except Exception:
            return False

        entries = probe.get("entries") or []
        if not probe.get("is_playlist") or len(entries) < 2:
            return False

        total = probe.get("total") or len(entries)
        pl_title = (probe.get("title") or "").strip() or task.title
        return await self._expand_into_children(task, pl_title, entries, total)

    async def _expand_into_children(self, task: DownloadTask, pl_title: str,
                                    entries: List[Dict[str, Any]], total: int) -> bool:
        """Turn `entries` ({url, title}) into one child task each and drop the
        pasted parent row. Over the cap, refuse visibly instead."""
        if len(entries) > _PLAYLIST_TASK_CAP:
            task.status = "failed"
            task.error = (
                f'This playlist has {total} items. Copita adds up to '
                f'{_PLAYLIST_TASK_CAP} at once — open a smaller playlist, or a '
                f'specific track, to download from here.'
            )
            self.broadcast("task_updated", task)
            return True

        inherited = {
            k: task.options[k]
            for k in ("audio_only", "audio_format", "video_quality", "threads", "headers")
            if k in task.options
        }
        # Every item of a playlist lands together in its own folder named
        # after the playlist, sorted by position, instead of being scattered
        # loose among unrelated downloads.
        subdir = _safe_dir_component(pl_title)
        pad = max(2, len(str(len(entries))))
        group_id = f"pl-{task.id}"
        total_entries = len(entries)
        # If the source already numbers its items (archive.org audiobook
        # tracks come titled "00 - Preface", "01 - Chapter One", …) don't add
        # a second number in front — the folder already sorts right.
        already_numbered = sum(
            1 for e in entries if re.match(r'^\s*\d{1,3}\s*[-.)_ ]', e.get("title") or "")
        ) >= max(2, len(entries) - 1)
        for idx, entry in enumerate(entries, 1):
            child_opts = dict(inherited)
            child_opts["_playlist_child"] = True
            child_opts["group"] = {
                "id": group_id, "title": pl_title,
                "index": idx, "total": total_entries,
            }
            # A numeric prefix keeps the folder listed in playlist order
            # (item 1, 2, 3 …) instead of alphabetically by title.
            name_prefix = f"{idx:0{pad}d} - " if (subdir and not already_numbered) else ""
            if subdir:
                child_opts["subdir"] = subdir
                child_opts["_name_prefix"] = name_prefix
            # For direct-file entries (e.g. archive.org's per-track .mp3 URLs)
            # the chunk engine names the file from options["filename"] — give
            # it the entry's real title so the saved file isn't a cryptic
            # CDN basename. yt-dlp entries ignore this and use their own
            # %(title)s template.
            ext = os.path.splitext(urlparse(entry["url"]).path)[1]
            if entry.get("title") and 1 < len(ext) <= 6:
                safe = re.sub(r'[\\/*?:"<>|]+', "_", entry["title"]).strip()[:100]
                if safe:
                    child_opts["filename"] = f"{name_prefix}{safe}{ext}"
            child = self.add_task(entry["url"], child_opts)
            if entry.get("title"):
                child.title = entry["title"]
                self.broadcast("task_updated", child)

        # The pasted row has done its job. Drop it rather than leave a fake
        # "completed" entry with no file — the child rows that just appeared
        # are the real result.
        self.tasks.pop(task.id, None)
        task.status = "completed"
        self.broadcast("task_deleted", task)
        return True

    # yt-dlp error messages for image-only posts name the URL they couldn't
    # handle — sometimes the real CDN image, sometimes reddit's own
    # /media?url=<encoded> fullscreen-viewer wrapper.
    _IMG_URL_RE = re.compile(r'https?://[^\s"\'<>]+', re.I)
    _IMG_EXT_RE = re.compile(r'\.(jpe?g|png|webp|gif|bmp|tiff?)(?:[?#]|$)', re.I)

    async def _fallback_from_ytdlp_failure(self, task: DownloadTask, err_msg: str,
                                           progress) -> bool:
        """Recover an image (or other direct file) from a URL yt-dlp reported
        as 'Unsupported'. Returns True if it took over the download."""
        if "unsupported url" not in err_msg.lower():
            return False

        # Pull every URL out of the message, unwrapping reddit's
        # /media?url=<encoded> viewer wrapper, and take the first that looks
        # like a real downloadable image/file.
        direct_url = None
        for cand in self._IMG_URL_RE.findall(err_msg):
            cand = cand.rstrip('.,)')
            p = urlparse(cand)
            if 'reddit.com' in (p.hostname or '') and p.path.rstrip('/') == '/media':
                inner = parse_qs(p.query).get('url', [None])[0]
                if inner:
                    cand = unquote_plus(inner)
            if self._IMG_EXT_RE.search(cand):
                direct_url = cand
                break

        headers = dict(task.options.get("headers") or {})
        headers.setdefault("Referer", task.url)

        if direct_url:
            fname = os.path.basename(urlparse(direct_url).path) or f"image_{task.id}"
            out_path = os.path.join(self.download_dir, fname)
            dl = ChunkDownloader(url=direct_url, output_path=out_path,
                                 num_connections=4, headers=headers, progress_callback=progress)
            task.worker_instance = dl
            res_path = await dl.start()
            res_path = _correct_extension_from_content(res_path)
            task.output_path = res_path
            task.title = os.path.basename(res_path)
            task.category = "image" if self._IMG_EXT_RE.search(res_path) else "file"
            task.status = "completed"
            task.percent = 100.0
            return True

        # No direct URL in the message — fall back to the page sniffer, which
        # now also finds a page's primary image (og:image / largest <img>).
        from copita.core.sniffer import MediaSniffer
        sniffer = MediaSniffer(headers=task.options.get("headers") or {})
        sniff_res = await sniffer.sniff_url(task.url)
        media = sniff_res.get("media") or []
        if task.options.get("media_hint") in ("audio", "video") and media and all(
                m.get("type") == "image" for m in media):
            raise ValueError(
                "Couldn't get the audio/video from this page — only its share "
                "image was found. It may not be served as a plain file."
            )
        if not media:
            host = (urlparse(task.url).hostname or "").lower()
            if "facebook.com" in host or "instagram.com" in host:
                raise ValueError(
                    "This looks like a single photo post. Facebook and Instagram "
                    "only show these to signed-in users, so Copita can't fetch the "
                    "image directly. Open the image itself in your browser and "
                    "paste that direct image link instead."
                )
            return False
        best = media[0]
        out_path = os.path.join(self.download_dir,
                                os.path.basename(urlparse(best["url"]).path) or f"download_{task.id}")
        dl = ChunkDownloader(url=best["url"], output_path=out_path, num_connections=4,
                             headers=headers, progress_callback=progress)
        task.worker_instance = dl
        res_path = await dl.start()
        res_path = _correct_extension_from_content(res_path)
        task.output_path = res_path
        task.title = os.path.basename(res_path)
        task.category = best.get("type") if best.get("type") in ("image", "video", "audio", "document") else "file"
        task.status = "completed"
        task.percent = 100.0
        return True

    @staticmethod
    def _ytdlp_outtmpl(target_dir: str, name_prefix: str, task: "DownloadTask") -> str:
        """`%(title).80B.%(ext)s`, but a clipped download gets a distinct
        `… [clip 5-12s]` suffix so it never collides with — and silently
        overwrites — a full download of the same video saved earlier."""
        base = f"{name_prefix}%(title).80B"
        cs, ce = task.options.get("clip_start"), task.options.get("clip_end")
        if cs is not None or ce is not None:
            def _t(v):
                v = int(round(float(v)))
                return f"{v // 60}m{v % 60:02d}s" if v >= 60 else f"{v}s"
            span = f"{_t(cs or 0)}-{_t(ce)}" if ce is not None else f"from {_t(cs or 0)}"
            base += f" [clip {span}]"
        return os.path.join(target_dir, f"{base}.%(ext)s")

    async def _run_task(self, task: DownloadTask):
        if await self._maybe_expand_playlist(task):
            return
        # Stays "queued" (its starting status) while waiting here — only
        # moves to "analyzing" once a concurrency slot actually opens up.
        await self._acquire_slot()
        task.status = "analyzing"
        self.broadcast("task_updated", task)

        def _progress(data: Dict[str, Any]):
            if task.status == 'cancelled':
                return
            if "status" in data:
                task.status = data["status"]
            if "percent" in data and data["percent"] is not None:
                try:
                    task.percent = float(data["percent"])
                except (ValueError, TypeError):
                    pass
            if "downloaded" in data:
                try:
                    task.downloaded_bytes = int(data["downloaded"])
                except (ValueError, TypeError):
                    pass
            if "total" in data:
                try:
                    task.total_bytes = int(data["total"]) if data["total"] is not None else None
                except (ValueError, TypeError):
                    pass
            if "speed" in data:
                try:
                    task.speed = float(data["speed"])
                    task.speed_available = True
                except (ValueError, TypeError):
                    task.speed = 0.0
                    task.speed_available = False
            if "eta" in data:
                try:
                    task.eta = float(data["eta"]) if data["eta"] is not None else None
                except (ValueError, TypeError):
                    task.eta = None
            if "threads" in data:
                task.threads = data["threads"]
            self.broadcast("task_updated", task)

        # Watchdog: nothing may sit in a non-terminal state making no
        # progress forever (a hung headless-browser sniff, a dead CDN, a
        # stalled connection). If it does, cancel the worker so the slot
        # frees and the row shows an honest failure instead of the app
        # looking frozen.
        async def _watchdog():
            last_change = time.time()
            snapshot = (task.status, task.downloaded_bytes)
            while True:
                await asyncio.sleep(15)
                if task.status in ("completed", "failed", "cancelled", "paused"):
                    return
                now = (task.status, task.downloaded_bytes)
                if now != snapshot:
                    snapshot, last_change = now, time.time()
                    continue
                # analyzing/queued with zero movement: 2 min. downloading but
                # frozen byte count: 5 min (slow CDNs, big fragment gaps).
                limit = 300 if task.status in ("downloading", "merging", "compressing") else 120
                if time.time() - last_change > limit:
                    task.error = ("This stalled with no progress and was stopped. "
                                  "Click Retry to try again.")
                    w = task.worker_instance
                    for m in ("cancel", "stop"):
                        if hasattr(w, m):
                            try:
                                getattr(w, m)()
                            except Exception:
                                pass
                    if task.async_task:
                        task.async_task.cancel()
                    return

        _wd = asyncio.create_task(_watchdog())

        try:
            target_dir = self.download_dir
            # Playlist items (and any task carrying an explicit `subdir`) are
            # filed into their own folder under the download dir. `subdir` is
            # sanitised to a single component at creation time; re-check here
            # so a hand-crafted option can never escape the download folder.
            subdir = task.options.get("subdir")
            if subdir:
                subdir = _safe_dir_component(subdir)
            if subdir:
                target_dir = os.path.join(self.download_dir, subdir)
                os.makedirs(target_dir, exist_ok=True)
            name_prefix = task.options.get("_name_prefix", "") if subdir else ""
            threads = task.options.get("threads", self.default_threads)
            audio_only = task.options.get("audio_only", False)
            format_id = task.options.get("format_id")
            custom_headers = dict(task.options.get("headers", {}))

            # If the user picked a browser in Preferences, hand its logged-in
            # session to the non-yt-dlp engines too (chunk / HLS / DASH / the
            # page sniffer) as a Cookie header — yt-dlp reads the browser
            # directly via cookiesfrombrowser and needs nothing here.
            _ck_browser = self.cookies(task.url)
            if _ck_browser and task.engine not in ("ytdlp",):
                try:
                    from copita.core.browser_cookies import cookie_header_for
                    ck = cookie_header_for(task.url, _ck_browser)
                except Exception:
                    ck = ""
                if ck:
                    existing = custom_headers.get("Cookie")
                    custom_headers["Cookie"] = f"{existing}; {ck}" if existing else ck

            # Routing to appropriate engine

            # Time-range clip of a direct video / HLS URL (yt-dlp handles its
            # own clips via download_ranges — this covers everything else).
            cs, ce = task.options.get("clip_start"), task.options.get("clip_end")
            if (cs is not None or ce is not None) and task.engine not in ("ytdlp",) \
                    and not task.options.get("merge"):
                start = max(0.0, float(cs or 0.0))
                end = float(ce) if ce is not None else None
                if end is not None and end <= start:
                    raise ValueError("Clip end time must be after the start time.")
                task.status = "downloading"
                task.category = "video"
                base = re.sub(r'[\\/*?:"<>|]+', "_", task.title or f"clip_{task.id}")[:80]
                base = re.sub(r'\.(mp4|mkv|webm)$', "", base, flags=re.I)

                def _t(v):
                    v = int(round(float(v)))
                    return f"{v // 60}m{v % 60:02d}s" if v >= 60 else f"{v}s"
                span = f"{_t(start)}-{_t(end)}" if end is not None else f"from {_t(start)}"
                out_path = os.path.join(target_dir, f"{name_prefix}{base} [clip {span}].mp4")
                loop = asyncio.get_running_loop()

                # A direct file: fetch it in full with the reliable
                # fingerprinted engine, then cut locally. ffmpeg's own HTTP
                # client is weak (no fingerprint, silent 403s → a stub file).
                # HLS / other engines: let ffmpeg pull the URL directly.
                clip_source = task.url
                tmp_full: Optional[str] = None
                if task.engine in ("turbo_chunk", "sniffer_or_ytdlp") and "." in os.path.basename(urlparse(task.url).path):
                    tmp_full = os.path.join(target_dir, f".{task.id}_full")
                    full_dl = ChunkDownloader(task.url, tmp_full, num_connections=threads,
                                              headers=custom_headers, progress_callback=_progress)
                    task.worker_instance = full_dl
                    await full_dl.start()
                    tmp_full = _correct_extension_from_content(tmp_full)
                    clip_source = tmp_full
                    task.status = "merging"
                    self.broadcast("task_updated", task)

                hdr_args: List[str] = []
                if clip_source == task.url:
                    for k, v in (custom_headers or {}).items():
                        hdr_args += ["-headers", f"{k}: {v}\r\n"]
                dur = ["-t", f"{end - start}"] if end is not None else []
                base_in = ["ffmpeg", "-y", "-ss", f"{start}", *hdr_args, "-i", clip_source, *dur]

                def _run(codec_args):
                    return subprocess.run(base_in + codec_args +
                                          ["-movflags", "+faststart", out_path],
                                          capture_output=True)

                # A too-small output means ffmpeg wrote a header but got no
                # media (e.g. a silent 403 pulling a URL directly).
                _ok = lambda p: p.returncode == 0 and os.path.isfile(out_path) and os.path.getsize(out_path) > 4096
                proc = await loop.run_in_executor(None, lambda: _run(["-c", "copy"]))
                if not _ok(proc):
                    proc = await loop.run_in_executor(
                        None, lambda: _run(["-c:v", "libx264", "-c:a", "aac", "-preset", "veryfast"]))
                if tmp_full:
                    try:
                        os.remove(tmp_full)
                    except OSError:
                        pass
                if not _ok(proc):
                    try:
                        os.remove(out_path)
                    except OSError:
                        pass
                    raise RuntimeError("Couldn't cut that clip from the video — the source may block direct access.")
                task.output_path = out_path
                task.title = os.path.basename(out_path)
                task.status = "completed"
                task.percent = 100.0
                return

            # DASH video+audio pair the browser extension resolved: fetch
            # both tracks, then ffmpeg-mux into one MP4.
            merge = task.options.get("merge")
            if merge and merge.get("video") and merge.get("audio"):
                task.status = "downloading"
                task.category = "video"
                base = re.sub(r'[\\/*?:"<>|]+', "_", task.title or f"video_{task.id}")[:80]
                v_tmp = os.path.join(target_dir, f".{task.id}_v")
                a_tmp = os.path.join(target_dir, f".{task.id}_a")
                out_path = os.path.join(target_dir, f"{name_prefix}{base}.mp4")
                vdl = ChunkDownloader(merge["video"], v_tmp, num_connections=threads,
                                      headers=custom_headers, progress_callback=_progress)
                task.worker_instance = vdl
                await vdl.start()
                adl = ChunkDownloader(merge["audio"], a_tmp, num_connections=threads,
                                      headers=custom_headers, progress_callback=_progress)
                task.worker_instance = adl
                await adl.start()
                task.status = "merging"
                self.broadcast("task_updated", task)
                loop = asyncio.get_running_loop()
                proc = await loop.run_in_executor(None, lambda: subprocess.run(
                    ["ffmpeg", "-y", "-i", v_tmp, "-i", a_tmp, "-c", "copy",
                     "-movflags", "+faststart", out_path],
                    capture_output=True))
                for p in (v_tmp, a_tmp):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
                if proc.returncode != 0 or not os.path.isfile(out_path):
                    raise RuntimeError("Couldn't merge the video and audio tracks.")
                task.output_path = out_path
                task.title = os.path.basename(out_path)
                task.status = "completed"
                task.percent = 100.0
                return

            if task.engine == "telegram_link":
                raise ValueError(
                    "Telegram media can't be downloaded from a link — it's "
                    "delivered inside the app, not as a file URL. Open the "
                    "message in Telegram Web (web.telegram.org), start the "
                    "download there, and the Copita browser extension will "
                    "pull the finished file into Copita."
                )

            if task.engine == "turbo_hls":
                task.threads = threads
                task.status = "downloading"
                safe_name = task.options.get("filename") or f"hls_{task.id}.mp4"
                if not safe_name.endswith(".mp4"):
                    safe_name += ".mp4"
                out_path = os.path.join(target_dir, safe_name)
                downloader = TurboHlsDownloader(
                    m3u8_url=task.url,
                    output_path=out_path,
                    concurrency=threads,
                    headers=custom_headers,
                    progress_callback=_progress
                )
                task.worker_instance = downloader
                res_path = await downloader.start()
                task.output_path = res_path
                task.status = "completed"
                task.percent = 100.0

            elif task.engine == "turbo_chunk":
                task.threads = threads
                task.status = "downloading"
                safe_name = task.options.get("filename") or task.title
                out_path = os.path.join(target_dir, safe_name)
                downloader = ChunkDownloader(
                    url=task.url,
                    output_path=out_path,
                    num_connections=threads,
                    headers=custom_headers,
                    progress_callback=_progress
                )
                task.worker_instance = downloader
                res_path = await downloader.start()
                task.output_path = res_path
                task.status = "completed"
                task.percent = 100.0

            elif task.engine == "scribd":
                downloader = ScribdDownloader(
                    url=task.url,
                    output_dir=target_dir,
                    progress_callback=_progress
                )
                task.worker_instance = downloader
                res_path = await downloader.download()
                task.output_path = res_path
                task.title = os.path.basename(res_path)
                task.status = "completed"
                task.percent = 100.0

            elif task.engine == "scratch":
                from copita.core.scratch_downloader import ScratchDownloader
                downloader = ScratchDownloader(
                    url=task.url, output_dir=target_dir, progress_callback=_progress
                )
                task.worker_instance = downloader
                res_path = await downloader.download()
                task.output_path = res_path
                task.title = os.path.basename(res_path)
                task.category = "file"
                task.status = "completed"
                task.percent = 100.0

            elif task.engine == "flipbook":
                downloader = FlipbookDownloader(
                    url=task.url,
                    output_dir=target_dir,
                    progress_callback=_progress,
                    provider=task.classification.get("provider")
                )
                task.worker_instance = downloader
                res_path = await downloader.download()
                task.output_path = res_path
                task.title = os.path.basename(res_path)
                task.category = "document"
                task.status = "completed"
                task.percent = 100.0

            elif task.engine == "deep_zoom":
                downloader = DeepZoomDownloader(
                    url=task.url,
                    output_dir=target_dir,
                    progress_callback=_progress,
                    options=task.options
                )
                task.worker_instance = downloader
                res_path = await downloader.download()
                task.output_path = res_path
                task.title = os.path.basename(res_path)
                task.category = "image"
                task.status = "completed"
                task.percent = 100.0

            elif task.engine == "sketchfab_3d":
                downloader = SketchfabDownloader(
                    url=task.url,
                    output_dir=target_dir,
                    progress_callback=_progress,
                    options=task.options
                )
                task.worker_instance = downloader
                res_path = await downloader.download()
                task.output_path = res_path
                task.title = os.path.basename(res_path)
                task.category = "model_3d"
                task.status = "completed"
                task.percent = 100.0

            elif task.engine == "spotify":
                downloader = SpotifyDownloader(download_dir=target_dir)
                def _sp_progress(data):
                    task.status = data.get("status", "downloading")
                    task.percent = float(data.get("percent", 0.0))
                    msg = data.get("message")
                    if msg:
                        task.title = msg
                    self.broadcast("task_updated", task)
                loop = asyncio.get_running_loop()
                res_path = await loop.run_in_executor(None, downloader.download_track, task.url, _sp_progress)
                task.output_path = res_path
                task.title = os.path.basename(res_path)
                task.category = "audio"
                task.status = "completed"
                task.percent = 100.0

            elif task.engine == "file_hoster":
                downloader = FileHosterDownloader(
                    url=task.url,
                    output_dir=target_dir,
                    num_connections=threads,
                    progress_callback=_progress
                )
                task.worker_instance = downloader
                res_path = await downloader.download()
                task.output_path = res_path
                task.title = os.path.basename(res_path)
                task.status = "completed"
                task.percent = 100.0

            elif task.engine == "manga":
                task.category = "manga"
                downloader = MangaDownloader(
                    url=task.url,
                    output_dir=target_dir,
                    concurrency=threads,
                    progress_callback=_progress,
                    options=task.options
                )
                task.worker_instance = downloader
                res_path = await downloader.download()
                task.output_path = res_path
                task.title = os.path.basename(res_path)
                task.category = "manga"
                task.status = "completed"
                task.percent = 100.0

            elif task.engine == "video_embed":
                from copita.core.video_embed_downloader import VideoEmbedDownloader
                downloader = VideoEmbedDownloader(
                    url=task.url,
                    output_dir=target_dir,
                    progress_callback=_progress,
                    options=task.options
                )
                task.worker_instance = downloader
                res_path = await downloader.download()
                task.output_path = res_path
                task.title = os.path.basename(res_path)
                task.category = "video"
                task.status = "completed"
                task.percent = 100.0

            elif task.engine == "ytdlp" or task.classification.get("type") == LinkType.MEDIA_PLATFORM:
                # Specialized TikTok fallback
                if "tiktok.com" in task.url:
                    try:
                        wrapper = YtDlpWrapper(cookies_browser=self.cookies(task.url), auth_browser=self.auth_browser())
                        task.worker_instance = wrapper
                        task.status = "downloading"
                        out_tmpl = self._ytdlp_outtmpl(target_dir, name_prefix, task)
                        loop = asyncio.get_running_loop()
                        res_path = await loop.run_in_executor(
                            None,
                            lambda: wrapper.download(
                                url=task.url,
                                output_template=out_tmpl,
                                format_id=format_id,
                                audio_only=audio_only,
                                audio_format=task.options.get("audio_format", "mp3"),
                                progress_callback=_progress,
                                clip_start=task.options.get("clip_start"),
                                clip_end=task.options.get("clip_end"),
                            )
                        )
                        task.output_path = res_path
                        task.title = os.path.basename(res_path)
                        task.status = "completed"
                        task.percent = 100.0
                    except Exception as yt_err:
                        # Fallback to direct no-watermark API
                        import httpx
                        r = httpx.get(f"https://www.tikwm.com/api/?url={task.url}", timeout=10)
                        data = r.json()
                        if data.get("code") == 0:
                            v_url = data["data"]["play"]
                            title = data["data"].get("title", f"tiktok_{task.id}")[:50]
                            clean_title = re.sub(r'[\/*?:"<>|]', '_', title)
                            out_file = os.path.join(target_dir, f"{clean_title}.mp4")
                            dl = ChunkDownloader(v_url, out_file, num_connections=8, progress_callback=_progress)
                            task.worker_instance = dl
                            res_path = await dl.start()
                            task.output_path = res_path
                            task.title = os.path.basename(res_path)
                            task.status = "completed"
                            task.percent = 100.0
                        else:
                            raise yt_err
                else:
                    wrapper = YtDlpWrapper(cookies_browser=self.cookies(task.url), auth_browser=self.auth_browser())
                    task.worker_instance = wrapper
                    task.status = "downloading"
                    out_tmpl = self._ytdlp_outtmpl(target_dir, name_prefix, task)
                    loop = asyncio.get_running_loop()
                    try:
                        res_path = await loop.run_in_executor(
                            None,
                            lambda: wrapper.download(
                                url=task.url,
                                output_template=out_tmpl,
                                format_id=format_id,
                                audio_only=audio_only,
                                audio_format=task.options.get("audio_format", "mp3"),
                                video_quality=task.options.get("video_quality", "best"),
                                progress_callback=_progress,
                                clip_start=task.options.get("clip_start"),
                                clip_end=task.options.get("clip_end"),
                            )
                        )
                    except Exception as yt_err:
                        # yt-dlp has no image extractor — a Reddit / Twitter /
                        # Facebook post that is just a photo fails here with
                        # "Unsupported URL", and the URL it choked on (often the
                        # real i.redd.it / pbs.twimg CDN image, sometimes wrapped
                        # as reddit.com/media?url=) is right there in the message.
                        if not await self._fallback_from_ytdlp_failure(task, str(yt_err), _progress):
                            raise
                        return
                    task.output_path = res_path
                    if audio_only:
                        task.category = 'audio'
                    else:
                        # SoundCloud / Bandcamp / a plain audio link classify
                        # as a generic "video" platform up front — correct the
                        # category from what actually landed on disk so a music
                        # track is filed and iconified as audio, not video.
                        _cat = _category_for_filename(res_path)
                        if _cat != 'file':
                            task.category = _cat
                    task.title = os.path.basename(res_path)
                    task.status = "completed"
                    task.percent = 100.0

            elif task.engine == "torrent":
                from copita.core.torrent_downloader import TorrentDownloader
                task.status = "analyzing"
                downloader = TorrentDownloader(
                    url=task.url,
                    output_dir=target_dir,
                    progress_callback=_progress
                )
                task.worker_instance = downloader
                res_path = await downloader.start()
                task.output_path = res_path
                # The magnet's "dn=" display name can be missing or messy —
                # the torrent's own metadata name (known once resolved) is
                # authoritative, so use it once we actually have it.
                task.title = os.path.basename(res_path.rstrip('/')) or task.title
                task.category = "torrent"
                task.status = "completed"
                task.percent = 100.0

            else:
                # Direct download fallback with intelligent filename and Content-Type inspection
                is_html = False
                content_type = ""
                _url_path = urlparse(task.url).path
                _looks_like_file = bool(re.search(r'\.[a-z0-9]{2,5}$', _url_path, re.I))
                # A generic-webpage engine + a URL with no file extension is
                # almost always a page → go straight to the sniffer, no probe
                # (deterministic, and faster). Only HEAD-probe when the URL
                # could plausibly be a direct file.
                if task.engine in ("sniffer_or_ytdlp",) and not _looks_like_file:
                    is_html = True
                else:
                    try:
                        from copita.core.http_client import HttpClient
                        async with HttpClient(headers=custom_headers, timeout=10.0) as _hc:
                            head_resp = await _hc.head(task.url, allow_redirects=True)
                            content_type = head_resp.header("content-type").lower()
                            if "text/html" in content_type or "application/xhtml" in content_type:
                                is_html = True
                    except Exception:
                        pass

                # A sniffed page's "best" candidate can itself turn out to be
                # another HTML page (e.g. a nav link that redirects back to
                # a page we already scanned) — without a cap, that reruns
                # the sniffer, which can rediscover an equivalent candidate
                # and recurse into _run_task again indefinitely.
                sniff_depth = task.options.get("_sniff_depth", 0)
                if is_html and sniff_depth >= 3:
                    raise ValueError(
                        "This URL kept resolving to more webpages instead of a "
                        "downloadable file after several attempts — no direct "
                        "media could be found."
                    )

                if is_html:
                    # Webpage URL was pasted without direct media. Run deep media sniffer
                    from copita.core.sniffer import MediaSniffer
                    # Deep sniffing can fall back to spawning a real headless
                    # browser and take 30+ seconds with zero built-in status
                    # updates — indistinguishable from the app being frozen.
                    # Surface what phase it's in via task.title, the same
                    # trick the Spotify engine uses since there's no
                    # dedicated status-message field on the task model.
                    def _sniff_progress(data):
                        msg = data.get("message")
                        if msg:
                            task.title = msg
                            self.broadcast("task_updated", task)
                    sniffer = MediaSniffer(headers=custom_headers, progress_callback=_sniff_progress)
                    try:
                        sniff_res = await asyncio.wait_for(sniffer.sniff_url(task.url), timeout=75)
                    except asyncio.TimeoutError:
                        raise RuntimeError(
                            "Scanning this page took too long and was stopped. "
                            "If it plays in your browser, try the Copita browser "
                            "extension's button on the page instead."
                        )
                    found_media = sniff_res.get("media", [])
                    # The extension says the user clicked an audio/video player.
                    # If the only thing the sniffer could turn up is the page's
                    # share image, that's not what they asked for — fail clean
                    # instead of "downloading" a banner/thumbnail.
                    media_hint = task.options.get("media_hint")
                    if (media_hint in ("audio", "video") and found_media
                            and all(m.get("type") == "image" for m in found_media)):
                        raise ValueError(
                            "Couldn't get the audio/video from this page — it isn't "
                            "served as a plain file the page URL can reach. Try "
                            "clicking the Copita button again once the player has "
                            "actually started playing."
                        )
                    if found_media:
                        best_media = found_media[0]
                        task.url = best_media["url"]
                        task.classification = classify_url(task.url)
                        task.engine = task.classification.get("engine", "turbo_chunk")
                        # task.category was likewise set from the original
                        # page's generic classification (e.g. "webpage") and
                        # never refreshed — so a real, correctly-downloaded
                        # audio/video/document ended up filed and iconified
                        # as a plain generic file in the UI even though the
                        # content itself was right.
                        task.category = task.classification.get("category", best_media.get("category", "file"))
                        # task.title was set from the *original* page's
                        # generic classification — for an unrecognized site
                        # that's just the bare domain (e.g. "www.audible.com")
                        # since we didn't know what was on the page yet. Now
                        # that the sniffer found the real media, use the
                        # page's actual title so the saved file gets a
                        # sensible name instead of the domain.
                        page_title = (sniff_res.get("title") or "").strip()
                        if page_title and page_title.lower() != urlparse(task.url).netloc.lower():
                            new_title = re.sub(r'[\\/*?:"<>|]', '_', page_title)[:120]
                            # The page title never carries the file's real
                            # extension (e.g. a generic app-shell title like
                            # "Audible Cloud Player" for an .mp3 sample) — use
                            # it as the display title, but the saved filename
                            # still needs the actual extension appended, or
                            # Finder/other apps see an unopenable extensionless
                            # "file" even though the category is correctly audio.
                            real_ext = task.classification.get("extension")
                            if real_ext and not new_title.lower().endswith(f".{real_ext.lower()}"):
                                new_title = f"{new_title}.{real_ext}"
                            task.title = new_title
                        # Some gated download links only work with the
                        # cookies established while the sniffer was browsing
                        # the page (e.g. a Cloudflare-issued token) — without
                        # this, a fresh cookie-less request to the same URL
                        # gets rejected even though the link itself is real.
                        sniff_cookies = sniff_res.get("cookies")
                        if sniff_cookies:
                            headers = dict(task.options.get("headers") or {})
                            existing_cookie = headers.get("Cookie")
                            headers["Cookie"] = f"{existing_cookie}; {sniff_cookies}" if existing_cookie else sniff_cookies
                            task.options["headers"] = headers
                        task.options["_sniff_depth"] = sniff_depth + 1
                        return await self._run_task(task)
                    else:
                        raise ValueError(sniff_res.get("note") or "This URL is an HTML webpage with no direct downloadable media streams found.")

                filename_cand = task.options.get("filename")
                if not filename_cand:
                    url_p = urlparse(task.url).path.strip('/')
                    base_candidate = os.path.basename(url_p) if url_p else ""
                    if base_candidate and '.' in base_candidate and len(base_candidate.split('.')[-1]) <= 5:
                        filename_cand = f"{name_prefix}{base_candidate}"
                    else:
                        ext = "mp4" if "video" in content_type else ("mp3" if "audio" in content_type else ("pdf" if "pdf" in content_type else "download"))
                        filename_cand = f"{name_prefix}download_{task.id}.{ext}"

                out_path = os.path.join(target_dir, filename_cand)
                downloader = ChunkDownloader(
                    url=task.url,
                    output_path=out_path,
                    num_connections=threads,
                    headers=custom_headers,
                    progress_callback=_progress
                )
                task.worker_instance = downloader
                res_path = await downloader.start()
                res_path = _correct_extension_from_content(res_path)
                task.output_path = res_path
                task.title = os.path.basename(res_path)
                task.status = "completed"
                task.percent = 100.0

        except asyncio.CancelledError:
            # The watchdog sets task.error before cancelling a stalled task;
            # keep that message and mark it failed rather than "cancelled".
            if task.error and "stalled" in task.error:
                task.status = "failed"
            else:
                task.status = "cancelled"
        except Exception as e:
            task.status = "failed"
            task.error = str(e)
            if e.__cause__:
                task.error_details = str(e.__cause__)
        finally:
            _wd.cancel()
            await self._release_slot()
            if task.status == 'completed' and task.output_path and os.path.isfile(task.output_path):
                task.downloaded_bytes = os.path.getsize(task.output_path)
                task.total_bytes = task.downloaded_bytes
            task.speed = 0.0
            task.eta = None
            self.broadcast("task_updated", task)

    def pause_task(self, task_id: str):
        task = self.tasks.get(task_id)
        if task and hasattr(task.worker_instance, "pause"):
            task.worker_instance.pause()
            task.status = "paused"
            self.broadcast("task_updated", task)

    def retry_task(self, task_id: str):
        task = self.tasks.get(task_id)
        if not task or task.status not in ('failed', 'cancelled'):
            raise ValueError('Only failed or cancelled downloads can be retried')
        if task.async_task and not task.async_task.done():
            raise ValueError('The previous download is still stopping')
        task.error = None
        task.error_details = None
        task.percent = 0
        task.speed_available = False
        task.downloaded_bytes = 0
        task.total_bytes = None
        task.output_path = None
        task.worker_instance = None
        task.status = 'queued'
        self.broadcast('task_updated', task)
        task.async_task = asyncio.create_task(self._run_task(task))
        return task

    def resume_task(self, task_id: str):
        task = self.tasks.get(task_id)
        if task and hasattr(task.worker_instance, "resume"):
            task.worker_instance.resume()
            task.status = "downloading"
            self.broadcast("task_updated", task)

    def cancel_task(self, task_id: str):
        task = self.tasks.get(task_id)
        if task:
            if hasattr(task.worker_instance, "cancel"):
                task.worker_instance.cancel()
            if task.async_task and not task.async_task.done():
                task.async_task.cancel()
            task.status = "cancelled"
            self.broadcast("task_updated", task)

    def delete_task(self, task_id: str, delete_file: bool = False):
        task = self.tasks.get(task_id)
        if task:
            self.cancel_task(task_id)
            if delete_file and task.output_path and os.path.exists(task.output_path):
                try:
                    os.remove(task.output_path)
                except OSError:
                    pass
            del self.tasks[task_id]
            self.broadcast("task_deleted", task)

    def get_all_tasks(self) -> List[Dict[str, Any]]:
        return [t.to_dict() for t in sorted(self.tasks.values(), key=lambda x: x.created_at, reverse=True)]

    def import_local_file(self, src_path: str, source_url: Optional[str] = None,
                          move: bool = True) -> DownloadTask:
        """Adopt a file the browser already downloaded (a blob: download the
        extension can't hand off as a URL — Telegram Web media, and other
        client-side-assembled downloads) into Copita's library. With
        move=True it relocates the file into the download folder; with
        move=False (the browser-adoption path) it copies, leaving the
        browser's own saved file untouched. Adds a completed task row."""
        src_path = os.path.abspath(os.path.expanduser(src_path))
        if not os.path.isfile(src_path):
            raise ValueError("That file no longer exists.")
        # Only adopt files the browser could plausibly have saved — a user
        # directory or a temp dir — never a system path like /etc or /usr.
        home = os.path.expanduser("~")
        allowed_prefixes = (home, "/var/folders/", "/private/var/folders/",
                            "/tmp/", "/private/tmp/")
        if not src_path.startswith(allowed_prefixes):
            raise ValueError("Refusing to import a file from outside your home folder.")

        fname = os.path.basename(src_path)
        dest = os.path.join(self.download_dir, fname)
        if os.path.abspath(src_path) == os.path.abspath(dest):
            pass  # already in place (browser saves straight into Copita's folder)
        else:
            if os.path.exists(dest):
                stem, ext = os.path.splitext(fname)
                dest = os.path.join(self.download_dir, f"{stem}_{uuid.uuid4().hex[:6]}{ext}")
            try:
                if move:
                    os.replace(src_path, dest)  # same-filesystem fast path
                else:
                    import shutil
                    shutil.copy2(src_path, dest)
            except OSError:
                import shutil
                shutil.copy2(src_path, dest)  # cross-device / perms — fall back to copy
                if move:
                    try:
                        os.remove(src_path)
                    except OSError:
                        pass

        dest = _correct_extension_from_content(dest)
        fname = os.path.basename(dest)
        fsize = os.path.getsize(dest)
        task_id = str(uuid.uuid4())[:8]
        task = DownloadTask(task_id, source_url or f"local://{fname}")
        task.title = fname
        task.status = "completed"
        task.percent = 100.0
        task.downloaded_bytes = fsize
        task.total_bytes = fsize
        task.output_path = dest
        task.category = _category_for_filename(fname)
        self.tasks[task_id] = task
        self.broadcast("task_added", task)
        return task
