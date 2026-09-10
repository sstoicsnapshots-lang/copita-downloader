"""
Protected Video & Movie Embed Downloader for Copita.
Resolves and downloads protected streaming video and movies from embed providers (e.g. viduki.net, vidsrc, etc.).
Uses headless CDP to intercept dynamically decrypted Master M3U8 playlists (1080p/720p/480p)
and downloads using TurboHlsDownloader with multi-threaded chunking.
"""

import os
import re
import time
import json
import ssl
import certifi
import asyncio
import tempfile
import subprocess
import urllib.parse
import aiohttp
from typing import Dict, Any, List, Optional, Callable
from copita.core.hls_downloader import TurboHlsDownloader
from copita.core.flipbook_downloader import find_browser_binary, find_free_port

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

class VideoEmbedDownloader:
    """Resolves and downloads media from protected video streaming embeds."""

    def __init__(
        self,
        url: str,
        output_dir: str = "downloads",
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        options: Optional[Dict[str, Any]] = None
    ):
        self.url = url.strip()
        self.output_dir = output_dir
        self.progress_callback = progress_callback
        self.options = options or {}
        self._is_cancelled = False
        self._hls_worker: Optional[TurboHlsDownloader] = None
        os.makedirs(self.output_dir, exist_ok=True)
        self._ssl_ctx = ssl.create_default_context(cafile=certifi.where())

    def _report(self, status: str, percent: float, speed: float = 0.0, msg: str = ""):
        if self.progress_callback:
            self.progress_callback({
                "status": status,
                "percent": percent,
                "speed": speed,
                "message": msg
            })

    def cancel(self):
        self._is_cancelled = True
        if self._hls_worker:
            self._hls_worker.cancel()

    @classmethod
    async def inspect(cls, url: str) -> Dict[str, Any]:
        """Inspects the embed link to discover title, metadata, and available stream qualities."""
        inst = cls(url)
        return await inst._inspect_embed()

    async def _inspect_embed(self) -> Dict[str, Any]:
        parsed = urllib.parse.urlparse(self.url)
        netloc = parsed.netloc.lower()

        title = "Streaming Video"
        overview = ""
        tmdb_id = None

        # 1. Try TMDB metadata API if viduki.net
        if "viduki.net" in netloc:
            m_movie = re.search(r'/movie/(\d+)', self.url)
            m_tv = re.search(r'/tv/(\d+)(?:/(\d+)/(\d+))?', self.url)
            if m_movie:
                tmdb_id = m_movie.group(1)
                tmdb_url = f"https://www.viduki.net/api/tmdb/movie/{tmdb_id}"
            elif m_tv:
                tmdb_id = m_tv.group(1)
                tmdb_url = f"https://www.viduki.net/api/tmdb/tv/{tmdb_id}"
            else:
                tmdb_url = None

            if tmdb_url:
                connector = aiohttp.TCPConnector(ssl=self._ssl_ctx)
                try:
                    async with aiohttp.ClientSession(connector=connector) as session:
                        async with session.get(tmdb_url, headers={"Referer": self.url, "User-Agent": USER_AGENT}, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                            if resp.status == 200:
                                d = await resp.json()
                                t = d.get("title") or d.get("name")
                                yr = (d.get("release_date") or d.get("first_air_date") or "")[:4]
                                if t:
                                    title = f"{t} ({yr})" if yr else t
                                overview = d.get("overview", "")
                except Exception:
                    pass

        # 2. Intercept Master M3U8 via Headless CDP
        stream_info = await self._capture_stream_via_cdp()
        master_url = stream_info.get("master_url")
        formats = stream_info.get("formats", [])

        return {
            "title": title,
            "overview": overview,
            "master_url": master_url,
            "formats": formats,
            "provider": "viduki" if "viduki" in netloc else "video_embed"
        }

    async def _capture_stream_via_cdp(self) -> Dict[str, Any]:
        """Launches headless Chrome/Brave to capture the decrypted streaming m3u8 playlist."""
        browser_bin = find_browser_binary()
        if not browser_bin:
            raise RuntimeError("Headless browser (Chrome/Brave) required to decrypt protected video embeds.")

        port = find_free_port()
        user_data = tempfile.mkdtemp(prefix="copita_embed_")
        cmd = [
            browser_bin,
            "--headless=new",
            "--disable-gpu",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={user_data}",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank"
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        captured_m3u8 = None
        master_url = None

        try:
            await asyncio.sleep(1.2)
            async with aiohttp.ClientSession() as session:
                async with session.put(f"http://127.0.0.1:{port}/json/new") as r:
                    tab = await r.json()
                    ws_url = tab["webSocketDebuggerUrl"]

                async with session.ws_connect(ws_url) as ws:
                    await ws.send_json({"id": 1, "method": "Network.enable"})
                    await ws.send_json({"id": 2, "method": "Page.enable"})
                    await ws.send_json({"id": 3, "method": "Page.navigate", "params": {"url": self.url}})

                    start_t = time.time()
                    while time.time() - start_t < 9.0:
                        try:
                            msg = await asyncio.wait_for(ws.receive_json(), timeout=1.5)
                        except asyncio.TimeoutError:
                            if master_url:
                                break
                            continue

                        if msg.get("method") == "Network.requestWillBeSent":
                            u = msg.get("params", {}).get("request", {}).get("url", "")
                            if ".m3u8" in u:
                                if "index.m3u8" in u and not any(res in u for res in ["/1080/", "/720/", "/480/", "/360/"]):
                                    master_url = u
                                    break
                                elif not captured_m3u8:
                                    captured_m3u8 = u

        finally:
            proc.kill()

        final_master = master_url or captured_m3u8
        if not final_master:
            raise RuntimeError("Could not intercept streaming video playlist. The server may be rate-limiting or down.")

        # Parse variants from Master M3U8
        formats = []
        connector = aiohttp.TCPConnector(ssl=self._ssl_ctx)
        try:
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(final_master, headers={"Referer": self.url, "User-Agent": USER_AGENT}, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status == 200:
                        content = await resp.text(errors="ignore")
                        lines = [l.strip() for l in content.splitlines() if l.strip()]
                        for i, line in enumerate(lines):
                            if line.startswith("#EXT-X-STREAM-INF:"):
                                res_m = re.search(r'RESOLUTION=(\d+)x(\d+)', line)
                                bw_m = re.search(r'BANDWIDTH=(\d+)', line)
                                w = int(res_m.group(1)) if res_m else 0
                                h = int(res_m.group(2)) if res_m else 0
                                res_str = f"{w}x{h}" if w and h else "Unknown"
                                bw = int(bw_m.group(1)) if bw_m else 0
                                if i + 1 < len(lines) and not lines[i+1].startswith('#'):
                                    variant_path = lines[i+1]
                                    variant_url = urllib.parse.urljoin(final_master, variant_path)
                                    if w >= 1900 or h >= 800:
                                        fid, q_label = "1080", "1080p"
                                    elif w >= 1200 or h >= 500:
                                        fid, q_label = "720", "720p"
                                    elif w >= 800 or h >= 350:
                                        fid, q_label = "480", "480p"
                                    elif w >= 600 or h >= 250:
                                        fid, q_label = "360", "360p"
                                    else:
                                        fid, q_label = str(h), f"{h}p" if h else "Unknown"
                                    formats.append({
                                        "format_id": fid,
                                        "quality": q_label,
                                        "resolution": res_str,
                                        "bandwidth": bw,
                                        "url": variant_url
                                    })
        except Exception:
            pass

        # Sort formats descending by bandwidth
        formats.sort(key=lambda x: x.get("bandwidth", 0), reverse=True)
        if not formats and final_master:
            formats.append({
                "format_id": "1080",
                "quality": "1080p",
                "resolution": "Original",
                "url": final_master
            })

        return {
            "master_url": final_master,
            "formats": formats
        }

    async def download(self) -> str:
        """Resolves stream and downloads multi-threaded with TurboHlsDownloader."""
        self._report("analyzing", 5.0, msg="Intercepting protected video stream via headless CDP...")
        info = await self._inspect_embed()

        title = info.get("title", "Video_Stream")
        clean_title = re.sub(r'[\/*?:"<>|]', '_', title)
        formats = info.get("formats", [])
        master_url = info.get("master_url")

        # Choose best or requested format
        target_stream_url = None
        requested_fid = self.options.get("format_id")
        if requested_fid:
            for f in formats:
                if str(f.get("format_id")) == str(requested_fid):
                    target_stream_url = f.get("url")
                    break

        if not target_stream_url:
            target_stream_url = formats[0]["url"] if formats else master_url

        if not target_stream_url:
            raise RuntimeError("No downloadable video stream URL found.")

        final_mp4_path = os.path.join(self.output_dir, f"{clean_title}.mp4")
        self._report("downloading", 15.0, msg=f"Starting multi-threaded HLS download: {clean_title}.mp4...")

        def _hls_cb(data: Dict[str, Any]):
            if self.progress_callback:
                self.progress_callback(data)

        self._hls_worker = TurboHlsDownloader(
            m3u8_url=target_stream_url,
            output_path=final_mp4_path,
            concurrency=min(self.options.get("threads") or 16, 16),
            headers={"Referer": self.url, "User-Agent": USER_AGENT},
            progress_callback=_hls_cb
        )

        return await self._hls_worker.start()
