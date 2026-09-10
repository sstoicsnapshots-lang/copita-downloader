"""
Interactive 3D Model Asset Bundle Extractor for Copita.
Extracts 3D geometry (OSGJS, BINZ), multi-channel texture maps (diffuse, normal, specular),
and scene metadata from Sketchfab into a complete 3D model archive (.zip).
"""

import os
import re
import ssl
import json
import gzip
import time
import shutil
import zipfile
import certifi
import aiohttp
import asyncio
import tempfile
from typing import Dict, Any, Optional, Callable, List

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

def sanitize_filename(title: str, max_length: int = 80) -> str:
    """Clean and truncate filename to avoid Errno 63."""
    cleaned = re.sub(r'[\/*?:"<>|\\#%&{}\<\>\*\?\/$!\'":@+`|=]', '_', title)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    if not cleaned:
        cleaned = "sketchfab_model"
    return cleaned[:max_length].strip()

class SketchfabDownloader:
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
        """Download 3D model geometry and textures, packaging into a .zip bundle."""
        self._report("analyzing", percent=5.0)

        # Extract 32-character hexadecimal model ID
        m_id = re.search(r'(?:3d-models/[a-zA-Z0-9_\-]+-|models/)([a-f0-9]{32})', self.url)
        if not m_id:
            m_id = re.search(r'([a-f0-9]{32})', self.url)
        if not m_id:
            raise RuntimeError("Could not determine Sketchfab 3D Model ID from URL.")

        model_id = m_id.group(1)
        embed_url = f"https://sketchfab.com/models/{model_id}/embed"

        connector = aiohttp.TCPConnector(ssl=self.ssl_context)
        async with aiohttp.ClientSession(connector=connector) as session:
            headers = {"User-Agent": USER_AGENT}
            async with session.get(embed_url, headers=headers) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Sketchfab embed viewer returned HTTP {resp.status}")
                html = await resp.text(errors="ignore")

            # Extract title
            title_m = re.search(r'<title>(.*?)</title>', html)
            raw_title = title_m.group(1) if title_m else "3D Model"
            raw_title = re.sub(r'\s*[-—|]\s*Sketchfab.*', '', raw_title, flags=re.IGNORECASE)
            title = sanitize_filename(raw_title)
            final_zip_path = os.path.join(self.output_dir, f"{title}.zip")

            # Discover model geometry and texture URLs
            osgjs_matches = re.findall(r'(https://media\.sketchfab\.com/models/[^"\'\s]+\.osgjs(?:\.gz)?)', html)
            binz_matches = re.findall(r'(https://media\.sketchfab\.com/models/[^"\'\s]+/file\.binz)', html)
            texture_matches = re.findall(r'(https://media\.sketchfab\.com/models/[^"\'\s]+/textures/[^"\'\s]+\.(?:jpeg|jpg|png))', html)

            # Deduplicate URLs
            osgjs_url = osgjs_matches[0] if osgjs_matches else None
            binz_url = binz_matches[0] if binz_matches else None
            texture_urls = list(set(texture_matches))

            if not osgjs_url and not binz_url and not texture_urls:
                raise RuntimeError("Could not find 3D geometry or texture streams in Sketchfab viewer.")

            # Create temp workspace
            temp_dir = tempfile.mkdtemp(prefix="copita_sketchfab_")
            textures_dir = os.path.join(temp_dir, "textures")
            os.makedirs(textures_dir, exist_ok=True)

            try:
                download_queue: List[tuple[str, str]] = []
                if osgjs_url:
                    ext = ".osgjs.gz" if osgjs_url.endswith(".gz") else ".osgjs"
                    download_queue.append((osgjs_url, os.path.join(temp_dir, f"model{ext}")))
                if binz_url:
                    download_queue.append((binz_url, os.path.join(temp_dir, "model.binz")))

                for i, tex_url in enumerate(texture_urls):
                    fname = tex_url.split('/')[-1]
                    download_queue.append((tex_url, os.path.join(textures_dir, fname)))

                total_items = len(download_queue)
                self._report("downloading", percent=15.0, total=total_items * 300000, threads=8)

                # Download all assets concurrently
                sem = asyncio.Semaphore(8)
                downloaded_bytes = 0
                completed_items = 0
                start_time = time.time()

                async def fetch_asset(asset_url: str, dest_path: str):
                    nonlocal downloaded_bytes, completed_items
                    async with sem:
                        if self._cancel_requested:
                            return
                        try:
                            async with session.get(asset_url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as a_resp:
                                if a_resp.status == 200:
                                    content = await a_resp.read()
                                    with open(dest_path, "wb") as f:
                                        f.write(content)
                                    downloaded_bytes += len(content)
                                    completed_items += 1
                                    elapsed = max(0.1, time.time() - start_time)
                                    pct = 15.0 + (completed_items / total_items) * 75.0
                                    self._report(
                                        "downloading",
                                        percent=pct,
                                        downloaded=downloaded_bytes,
                                        total=total_items * (downloaded_bytes // max(1, completed_items)),
                                        speed=downloaded_bytes / elapsed,
                                        threads=8
                                    )
                        except Exception as e:
                            print(f"[Sketchfab] Error fetching {asset_url}: {e}")

                await asyncio.gather(*(fetch_asset(u, p) for u, p in download_queue))

                # Decompress .osgjs.gz if present
                gz_path = os.path.join(temp_dir, "model.osgjs.gz")
                if os.path.exists(gz_path):
                    try:
                        with gzip.open(gz_path, 'rb') as f_in:
                            with open(os.path.join(temp_dir, "model.osgjs"), 'wb') as f_out:
                                shutil.copyfileobj(f_in, f_out)
                    except Exception:
                        pass

                # Write metadata manifest
                manifest = {
                    "model_id": model_id,
                    "title": raw_title,
                    "source_url": self.url,
                    "total_textures": len(texture_urls),
                    "extracted_by": "Copita Universal Downloader",
                    "exported_at": time.strftime("%Y-%m-%d %H:%M:%S")
                }
                with open(os.path.join(temp_dir, "metadata.json"), "w") as f:
                    json.dump(manifest, f, indent=2)

                # Package into ZIP archive
                self._report("compiling", percent=92.0)
                loop = asyncio.get_running_loop()

                def _zip_bundle():
                    with zipfile.ZipFile(final_zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                        for root, _, files in os.walk(temp_dir):
                            for file in files:
                                full_p = os.path.join(root, file)
                                rel_p = os.path.relpath(full_p, temp_dir)
                                zf.write(full_p, rel_p)

                await loop.run_in_executor(None, _zip_bundle)

            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)

            self._report("completed", percent=100.0)
            return final_zip_path
