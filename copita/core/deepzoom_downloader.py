"""
Deep-Zoom Gigapixel Tile Pyramid & Master Image Downloader for Copita.
Extracts ultra-high-resolution imagery from Google Arts & Culture, IIIF,
and deep-zoom gigapixel viewers.
"""

import os
import re
import ssl
import time
import certifi
import aiohttp
import asyncio
from typing import Dict, Any, Optional, Callable
from PIL import Image
import io

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

def sanitize_filename(title: str, max_length: int = 80) -> str:
    """Clean and truncate filename to avoid Errno 63."""
    cleaned = re.sub(r'[\/*?:"<>|\\#%&{}\<\>\*\?\/$!\'":@+`|=]', '_', title)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    if not cleaned:
        cleaned = "artwork"
    return cleaned[:max_length].strip()

class DeepZoomDownloader:
    def __init__(
        self,
        url: str,
        output_dir: str,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        options: Optional[Dict[str, Any]] = None
    ):
        self.url = url
        self.output_dir = os.path.abspath(output_dir)
        self.progress_callback = progress_callback
        self.options = options or {}
        self.ssl_context = ssl.create_default_context(cafile=certifi.where())
        self._cancel_requested = False
        os.makedirs(self.output_dir, exist_ok=True)

    def cancel(self):
        self._cancel_requested = True

    def _report(
        self,
        status: str,
        percent: float = 0.0,
        downloaded: int = 0,
        total: Optional[int] = None,
        speed: float = 0.0,
        threads: int = 1
    ):
        if self.progress_callback:
            self.progress_callback({
                "status": status,
                "percent": min(100.0, max(0.0, round(percent, 1))),
                "downloaded": downloaded,
                "total": total,
                "speed": round(speed, 1),
                "threads": threads
            })

    async def download(self) -> str:
        """Download deep-zoom gigapixel artwork or high-resolution imagery."""
        self._report("analyzing", percent=5.0)
        connector = aiohttp.TCPConnector(ssl=self.ssl_context)
        async with aiohttp.ClientSession(connector=connector) as session:
            headers = {"User-Agent": USER_AGENT}
            async with session.get(self.url, headers=headers) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Deep-zoom source returned HTTP {resp.status}")
                html = await resp.text(errors="ignore")

            # Extract title
            title_m = re.search(r'<meta\s+property=["\']og:title["\']\s+content=["\'](.*?)["\']', html)
            if not title_m:
                title_m = re.search(r'<title>(.*?)</title>', html)
            raw_title = title_m.group(1) if title_m else "Google Arts Artwork"
            raw_title = re.sub(r'\s*[-—|]\s*Google Arts\s*&\s*Culture.*', '', raw_title, flags=re.IGNORECASE)
            title = sanitize_filename(raw_title)
            output_file = os.path.join(self.output_dir, f"{title}.jpg")

            # Extract master content image URL
            ci_matches = re.findall(r'https://lh\d+\.googleusercontent\.com/ci/([a-zA-Z0-9_\-]+)', html)
            if not ci_matches:
                # Try generic googleusercontent image URL
                gen_matches = re.findall(r'(https://lh\d+\.googleusercontent\.com/[a-zA-Z0-9_\-]+)', html)
                if gen_matches:
                    ci_base = gen_matches[0]
                else:
                    raise RuntimeError("Could not locate high-resolution image stream in Google Arts & Culture page.")
            else:
                ci_base = f"https://lh3.googleusercontent.com/ci/{ci_matches[0]}"

            master_url = f"{ci_base}=s0"
            self._report("downloading", percent=20.0, threads=1)
            start_time = time.time()

            async with session.get(master_url, headers=headers) as img_resp:
                if img_resp.status != 200:
                    # Fallback to =s3840
                    fallback_url = f"{ci_base}=s3840"
                    img_resp = await session.get(fallback_url, headers=headers)
                    if img_resp.status != 200:
                        raise RuntimeError(f"Failed to fetch master artwork image: HTTP {img_resp.status}")

                total_size = int(img_resp.headers.get("Content-Length", 0))
                chunks = []
                downloaded = 0

                async for chunk in img_resp.content.iter_chunked(65536):
                    if self._cancel_requested:
                        raise asyncio.CancelledError()
                    chunks.append(chunk)
                    downloaded += len(chunk)
                    elapsed = max(0.1, time.time() - start_time)
                    pct = 20.0 + (downloaded / max(1, total_size)) * 75.0 if total_size else 50.0
                    self._report(
                        "downloading",
                        percent=pct,
                        downloaded=downloaded,
                        total=total_size or downloaded,
                        speed=downloaded / elapsed,
                        threads=1
                    )

            img_bytes = b''.join(chunks)
            with open(output_file, "wb") as f:
                f.write(img_bytes)

            self._report("completed", percent=100.0)
            return output_file
