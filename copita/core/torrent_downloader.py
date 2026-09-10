"""
BitTorrent / Magnet Link Downloader for Copita.

Wraps libtorrent (libtorrent-rasterbar) — the same engine that powers
qBittorrent and Deluge — to give Copita real BitTorrent capability: DHT-based
magnet resolution, peer/seed discovery, piece-based downloading, and clean
cancellation. This replaces the previous placeholder that just saved the
magnet URI as a .txt file without downloading anything.

libtorrent's Python API is synchronous (poll `handle.status()` in a loop),
not asyncio-native, so the whole download runs in a worker thread via
`loop.run_in_executor` and reports progress back through the same
`progress_callback` convention every other downloader in this codebase uses.
"""

import os
import re
import time
import asyncio
from typing import Dict, Any, Optional, Callable

import libtorrent as lt


class TorrentDownloader:
    # A torrent with no seeders left will otherwise wait forever — these
    # bound how long we'll wait for metadata and for download progress
    # before failing with a clear, honest error instead of hanging the task.
    METADATA_TIMEOUT = 120
    STALL_TIMEOUT = 300
    POLL_INTERVAL = 1.0

    def __init__(
        self,
        url: str,
        output_dir: str = "downloads",
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self.url = url.strip()
        self.output_dir = os.path.abspath(output_dir)
        self.progress_callback = progress_callback
        self.cancelled = False
        self._session: Optional["lt.session"] = None
        os.makedirs(self.output_dir, exist_ok=True)

    def cancel(self):
        self.cancelled = True

    @staticmethod
    def _apply_proxy(settings: dict) -> None:
        """Route peer + tracker traffic through the configured proxy, if any.
        libtorrent only supports http / socks4 / socks5 (with optional auth)."""
        try:
            from copita.core.http_client import current_proxy
            url = current_proxy()
        except Exception:
            url = None
        if not url:
            return
        from urllib.parse import urlparse
        p = urlparse(url)
        kind = {"http": 1, "socks4": 4, "socks5": 2, "socks5h": 2}.get((p.scheme or "").lower())
        if not kind or not p.hostname:
            return
        if (p.username or p.password) and kind == 2:
            kind = 3  # socks5 with credentials
        elif (p.username or p.password) and kind == 1:
            kind = 2  # http with credentials (libtorrent enum: http_pw)
        settings.update({
            "proxy_type": kind,
            "proxy_hostname": p.hostname,
            "proxy_port": p.port or (1080 if kind in (2, 3, 4) else 8080),
            "proxy_username": p.username or "",
            "proxy_password": p.password or "",
            "proxy_peer_connections": True,
            "proxy_tracker_connections": True,
        })

    def _report(self, status: str, percent: float, speed: Optional[float] = None,
                msg: str = "", downloaded: int = 0, total: int = 0):
        if self.progress_callback:
            self.progress_callback({
                "status": status,
                "percent": percent,
                "speed": speed,
                "message": msg,
                "downloaded": downloaded,
                "total": total,
            })

    async def start(self) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._download_blocking)

    def _build_params(self):
        if self.url.startswith("magnet:"):
            params = lt.parse_magnet_uri(self.url)
        elif re.match(r'^https?://', self.url, re.I):
            # A direct link to a .torrent metadata file.
            import httpx
            resp = httpx.get(self.url, timeout=20.0, follow_redirects=True)
            resp.raise_for_status()
            info = lt.torrent_info(lt.bdecode(resp.content))
            params = lt.add_torrent_params()
            params.ti = info
        else:
            # A local .torrent file path.
            info = lt.torrent_info(self.url)
            params = lt.add_torrent_params()
            params.ti = info
        params.save_path = self.output_dir
        params.storage_mode = lt.storage_mode_t.storage_mode_sparse
        return params

    def _download_blocking(self) -> str:
        self._report("analyzing", 0.0, msg="Starting BitTorrent session…")

        sess_settings = {
            "listen_interfaces": "0.0.0.0:6881,[::]:6881",
            "enable_dht": True,
            "enable_lsd": True,
            "enable_upnp": True,
            "enable_natpmp": True,
        }
        self._apply_proxy(sess_settings)
        self._session = lt.session(sess_settings)

        try:
            params = self._build_params()
        except Exception as e:
            raise ValueError(f"Invalid torrent/magnet link: {e}") from e

        handle = self._session.add_torrent(params)

        try:
            self._wait_for_metadata(handle)
            meta_status = handle.status()
            name = meta_status.name
            self._report("downloading", 0.0, msg=f"Found “{name}” — downloading…",
                         total=meta_status.total_wanted)
            self._wait_for_completion(handle, name)

            final = handle.status()
            output_path = self._resolve_output_path(handle, final)
            self._report("completed", 100.0, downloaded=final.total_wanted_done,
                         total=final.total_wanted, msg=f"Completed: {name}")
            return output_path
        finally:
            # Detach cleanly so the torrent stops seeding and the session's
            # background network thread doesn't keep running after we return.
            try:
                self._session.remove_torrent(handle)
            except Exception:
                pass
            self._session = None

    def _wait_for_metadata(self, handle):
        self._report("analyzing", 0.0, msg="Resolving torrent metadata from peers (DHT)…")
        start_time = time.time()
        while not handle.status().has_metadata:
            if self.cancelled:
                raise RuntimeError("Download cancelled")
            if time.time() - start_time > self.METADATA_TIMEOUT:
                raise RuntimeError(
                    f"Could not find this torrent's metadata after {self.METADATA_TIMEOUT}s "
                    "— the swarm may have no seeders left, or the magnet link is invalid/dead."
                )
            time.sleep(self.POLL_INTERVAL)

    def _wait_for_completion(self, handle, name: str):
        last_progress_time = time.time()
        last_downloaded = 0

        while True:
            if self.cancelled:
                raise RuntimeError("Download cancelled")

            s = handle.status()
            if s.is_seeding or s.progress >= 1.0:
                return

            now = time.time()
            if s.total_wanted_done > last_downloaded:
                last_downloaded = s.total_wanted_done
                last_progress_time = now
            elif now - last_progress_time > self.STALL_TIMEOUT:
                raise RuntimeError(
                    f"No download progress for {self.STALL_TIMEOUT}s "
                    f"({s.num_peers} peers, {s.num_seeds} seeds found) — this torrent's "
                    "swarm appears to have no one left to download the remaining pieces from."
                )

            self._report(
                "downloading",
                s.progress * 100.0,
                speed=float(s.download_rate),
                downloaded=s.total_wanted_done,
                total=s.total_wanted,
                msg=f"{name} — {s.num_peers} peers, {s.num_seeds} seeds",
            )
            time.sleep(self.POLL_INTERVAL)

    @staticmethod
    def _resolve_output_path(handle, status) -> str:
        """A single-file torrent's content lands directly at save_path/name;
        a multi-file torrent creates save_path/name/ as a folder — libtorrent
        handles the actual layout, this just reports which one happened."""
        ti = handle.torrent_file()
        save_path = status.save_path
        name = status.name
        candidate_dir = os.path.join(save_path, name)
        if ti is not None and ti.num_files() > 1 and os.path.isdir(candidate_dir):
            return candidate_dir
        candidate_file = os.path.join(save_path, name)
        return candidate_file
