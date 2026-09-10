"""
Universal Flipbook & Protected Publication Downloader for Copita.
Extracts multi-page publications from protected viewers into authentic,
full-resolution multi-page PDFs without .bin fallback.

Supported Platforms:
- Issuu (Direct high-res page CDN pipeline)
- Flipsnack (Signed CloudFront original page pipeline)
- FlipHTML5 & AnyFlip & PubHTML5 (Headless CDP configuration & page interceptor)
- Heyzine (Authentic source PDF extractor)
- Calaméo & Yumpu (Automated CDP page stepping and asset capture)
"""

import os
import re
import ssl
import json
import gzip
import time
import shutil
import socket
import base64
import urllib.parse
import asyncio
import tempfile
import subprocess
from typing import Dict, Any, Optional, Callable, List
import certifi
import aiohttp
from PIL import Image

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

def find_browser_binary() -> Optional[str]:
    """Find available Chromium or Brave headless binary."""
    candidates = [
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        shutil.which("brave"),
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser")
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None

def find_free_port() -> int:
    """Find an available TCP port for CDP."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]

def sanitize_filename(title: str, max_length: int = 80) -> str:
    """Clean and truncate filename to avoid Errno 63."""
    cleaned = re.sub(r'[\/*?:"<>|\\#%&{}\<\>\*\?\/$!\'":@+`|=]', '_', title)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    if not cleaned:
        cleaned = "publication"
    return cleaned[:max_length].strip()

class FlipbookDownloader:
    def __init__(
        self,
        url: str,
        output_dir: str,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        provider: Optional[str] = None
    ):
        self.url = url
        self.output_dir = os.path.abspath(output_dir)
        self.progress_callback = progress_callback
        self.provider = provider
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
        """Main dispatcher for flipbook downloads."""
        url_lower = self.url.lower()

        if "issuu.com" in url_lower:
            return await self._download_issuu()
        elif "flipsnack.com" in url_lower:
            return await self._download_flipsnack()
        elif "heyzine.com" in url_lower:
            return await self._download_heyzine()
        else:
            # FlipHTML5, AnyFlip, PubHTML5, Calaméo, Yumpu or generic flipbooks
            return await self._download_cdp_flipbook()

    # --------------------------------------------------------------------------
    # 1. ISSUU PROVIDER
    # --------------------------------------------------------------------------
    async def _download_issuu(self) -> str:
        self._report("analyzing", percent=5.0)
        connector = aiohttp.TCPConnector(ssl=self.ssl_context)
        async with aiohttp.ClientSession(connector=connector) as session:
            # Step 1: Fetch HTML to locate doc_id and title
            headers = {"User-Agent": USER_AGENT}
            async with session.get(self.url, headers=headers) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Issuu returned HTTP {resp.status}")
                html = await resp.text(errors="ignore")

            doc_match = re.search(r'image\.isu\.pub/(\d+-[a-f0-9]+)', html)
            if not doc_match:
                # Try finding from embed or meta
                doc_match = re.search(r'(\d{12}-[a-f0-9]{32})', html)
            if not doc_match:
                raise RuntimeError("Could not find Issuu document ID in page.")

            doc_id = doc_match.group(1)
            title_match = re.search(r'<meta\s+property=["\']og:title["\']\s+content=["\'](.*?)["\']', html)
            title = title_match.group(1) if title_match else "Issuu Publication"
            title = sanitize_filename(title)
            final_pdf_path = os.path.join(self.output_dir, f"{title}.pdf")

            # Step 2: Probe total page count
            self._report("analyzing", percent=10.0)
            total_pages = await self._probe_issuu_page_count(session, doc_id)
            if total_pages == 0:
                raise RuntimeError("Could not detect any pages for this Issuu document.")

            # Step 3: Download all page images concurrently
            self._report("downloading", percent=15.0, total=total_pages * 600000, threads=16)
            temp_dir = tempfile.mkdtemp(prefix="copita_issuu_")
            try:
                page_files = await self._download_issuu_pages(session, doc_id, total_pages, temp_dir)
                if not page_files:
                    raise RuntimeError("Failed to download Issuu page images.")

                # Step 4: Compile to PDF
                self._report("compiling", percent=90.0)
                await self._compile_images_to_pdf(page_files, final_pdf_path)
            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)

            self._report("completed", percent=100.0)
            return final_pdf_path

    async def _probe_issuu_page_count(self, session: aiohttp.ClientSession, doc_id: str) -> int:
        """Find total pages using concurrent batch probing."""
        headers = {"User-Agent": USER_AGENT}
        low = 1
        high = 500
        last_valid = 0

        # Check in chunks of 20
        batch_size = 20
        current_start = 1

        while current_start <= high:
            if self._cancel_requested:
                raise asyncio.CancelledError()

            tasks = []
            pages_to_check = list(range(current_start, min(current_start + batch_size, high + 1)))
            for p in pages_to_check:
                u = f"https://image.isu.pub/{doc_id}/jpg/page_{p}.jpg"
                tasks.append(session.head(u, headers=headers, timeout=aiohttp.ClientTimeout(total=8)))

            results = await asyncio.gather(*tasks, return_exceptions=True)
            batch_ended = False
            for p, res in zip(pages_to_check, results):
                if not isinstance(res, Exception) and res.status == 200:
                    last_valid = max(last_valid, p)
                else:
                    batch_ended = True
                    break

            if batch_ended or len(pages_to_check) < batch_size:
                break
            current_start += batch_size

        return last_valid

    async def _download_issuu_pages(
        self, session: aiohttp.ClientSession, doc_id: str, total_pages: int, temp_dir: str
    ) -> List[str]:
        headers = {"User-Agent": USER_AGENT}
        sem = asyncio.Semaphore(16)
        downloaded_bytes = 0
        completed_pages = 0
        start_time = time.time()
        file_map: Dict[int, str] = {}

        async def fetch_page(p_idx: int):
            nonlocal downloaded_bytes, completed_pages
            async with sem:
                if self._cancel_requested:
                    return
                url = f"https://image.isu.pub/{doc_id}/jpg/page_{p_idx}.jpg"
                try:
                    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                        if resp.status == 200:
                            content = await resp.read()
                            p_path = os.path.join(temp_dir, f"page_{p_idx:04d}.jpg")
                            with open(p_path, "wb") as f:
                                f.write(content)
                            file_map[p_idx] = p_path
                            downloaded_bytes += len(content)
                            completed_pages += 1
                            elapsed = max(0.1, time.time() - start_time)
                            pct = 15.0 + (completed_pages / total_pages) * 75.0
                            self._report(
                                "downloading",
                                percent=pct,
                                downloaded=downloaded_bytes,
                                total=total_pages * (downloaded_bytes // max(1, completed_pages)),
                                speed=downloaded_bytes / elapsed,
                                threads=16
                            )
                except Exception as e:
                    print(f"[Issuu] Error page {p_idx}: {e}")

        await asyncio.gather(*(fetch_page(i) for i in range(1, total_pages + 1)))
        return [file_map[i] for i in sorted(file_map.keys())]

    # --------------------------------------------------------------------------
    # 2. FLIPSNACK PROVIDER
    # --------------------------------------------------------------------------
    async def _download_flipsnack(self) -> str:
        self._report("analyzing", percent=5.0)
        connector = aiohttp.TCPConnector(ssl=self.ssl_context)
        async with aiohttp.ClientSession(connector=connector) as session:
            # Step 1: Scrape page to find flipbook hash and account
            headers = {"User-Agent": USER_AGENT}
            async with session.get(self.url, headers=headers) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Flipsnack returned HTTP {resp.status}")
                html = await resp.text(errors="ignore")

            m_hash = re.search(r'[?&]hash=([a-zA-Z0-9%]+)', html)
            raw_hash = urllib.parse.unquote(m_hash.group(1)) if m_hash else None
            account_id = None
            flipbook_hash = None

            if raw_hash:
                try:
                    dec = base64.b64decode(raw_hash).decode('utf-8')
                    if '+' in dec:
                        account_id, flipbook_hash = dec.split('+', 1)
                except Exception:
                    pass

            if not raw_hash or not account_id:
                m_acc = re.search(r'accountId["\']?\s*:\s*["\']([a-zA-Z0-9]+)["\']', html)
                m_flp = re.search(r'flipbookHash["\']?\s*:\s*["\']([a-zA-Z0-9]+)["\']', html)
                if m_acc and m_flp:
                    account_id = m_acc.group(1)
                    flipbook_hash = m_flp.group(1)
                    raw_hash = base64.b64encode(f"{account_id}+{flipbook_hash}".encode()).decode()

            if not raw_hash or not account_id or not flipbook_hash:
                raise RuntimeError("Could not extract Flipsnack authorization hash from page.")

            # Step 2: Request signed authorization
            self._report("analyzing", percent=10.0)
            auth_url = f"https://content-private.flipsnack.com/authorization?hash={raw_hash}&domain=www.flipsnack.com"
            async with session.get(auth_url, headers=headers) as auth_resp:
                if auth_resp.status != 200:
                    raise RuntimeError(f"Flipsnack authorization returned HTTP {auth_resp.status}")
                auth_data = await auth_resp.json()

            sig = auth_data.get("signature", {}).get(flipbook_hash)
            if not sig:
                raise RuntimeError("Flipsnack authorization did not return valid CloudFront signature.")

            # Step 3: Fetch collection data.json
            data_url = f"https://d3u72tnj701eui.cloudfront.net/{account_id}/collections/{flipbook_hash}/data.json?{sig}"
            async with session.get(data_url, headers=headers) as data_resp:
                raw = await data_resp.read()
                try:
                    raw = gzip.decompress(raw)
                except Exception:
                    pass
                collection = json.loads(raw.decode('utf-8'))

            # Extract title & pages
            title = collection.get("properties", {}).get("title")
            if not title:
                title_match = re.search(r'<title>(.*?)</title>', html)
                title = title_match.group(1) if title_match else "Flipsnack Publication"
            title = sanitize_filename(title)
            final_pdf_path = os.path.join(self.output_dir, f"{title}.pdf")

            pages_dict = collection.get("pages", {})
            page_order = pages_dict.get("order", [])
            page_data = pages_dict.get("data", {})

            if not page_order:
                raise RuntimeError("No pages found in Flipsnack collection data.")

            # Prepare image download URLs and extract embedded videos
            image_candidates: List[List[str]] = []
            extracted_videos: List[Dict[str, Any]] = []

            for idx, pid in enumerate(page_order, 1):
                item = page_data.get(pid, {})
                src_hash = item.get("source", {}).get("hash")
                p_val = item.get("source", {}).get("page", 0)
                try:
                    p_num = int(p_val) + 1
                except (ValueError, TypeError):
                    p_num = idx

                version = item.get("version", 1)
                page_key = item.get("hash") or pid

                candidates = []
                if src_hash:
                    # High-res uncompressed cover on d1dhn91mufybwl
                    candidates.append(f"https://d1dhn91mufybwl.cloudfront.net/collections/items/{src_hash}/covers/page_{p_num}/original?version={version}")
                    # Signed medium cover on d3u72tnj701eui
                    candidates.append(f"https://d3u72tnj701eui.cloudfront.net/{account_id}/collections/{flipbook_hash}/items/{src_hash}/covers/{page_key}/medium?{sig}")
                    # Signed thumb cover on d3u72tnj701eui
                    candidates.append(f"https://d3u72tnj701eui.cloudfront.net/{account_id}/collections/{flipbook_hash}/items/{src_hash}/covers/{page_key}/thumb?{sig}")
                image_candidates.append(candidates)

                # Discover embedded videos on this page
                for el in item.get("elements", []):
                    attrs = el.get("attributes", {})
                    src = attrs.get("src") or attrs.get("url") or ""
                    src_clean = src.split("?")[0]
                    if src_clean:
                        if src_clean.startswith("http"):
                            v_url = src_clean
                        elif re.match(r'^[a-f0-9]{32}$', src_clean):
                            v_url = f"https://d1dhn91mufybwl.cloudfront.net/collections/uploads/{src_clean}"
                        else:
                            continue
                        if el.get("type") == 46 or any(ext in src.lower() for ext in [".mp4", "video", "pexels"]):
                            extracted_videos.append({
                                "page": idx,
                                "url": v_url,
                                "title": f"{title} - Page {idx} Video"
                            })

            if not any(image_candidates):
                raise RuntimeError("Could not construct page URLs for Flipsnack.")

            # Step 4: Download pages concurrently
            self._report("downloading", percent=15.0, total=len(image_candidates) * 500000, threads=16)
            temp_dir = tempfile.mkdtemp(prefix="copita_flipsnack_")
            try:
                page_files = await self._download_candidate_images(session, image_candidates, temp_dir)
                if not page_files:
                    raise RuntimeError("Failed to download Flipsnack page images.")

                # Step 5: Compile to PDF
                self._report("compiling", percent=85.0)
                await self._compile_images_to_pdf(page_files, final_pdf_path)

                # Step 6: Download any embedded videos playing in the publication
                if extracted_videos:
                    self._report("downloading", percent=90.0)
                    sem_vid = asyncio.Semaphore(4)
                    async def fetch_video(v_info: Dict[str, Any]):
                        v_num = v_info["page"]
                        v_link = v_info["url"]
                        v_target = os.path.join(self.output_dir, f"{title} - Page {v_num} Video.mp4")
                        async with sem_vid:
                            try:
                                async with session.get(v_link, headers=headers, timeout=aiohttp.ClientTimeout(total=180)) as v_resp:
                                    if v_resp.status == 200:
                                        with open(v_target, "wb") as vf:
                                            while True:
                                                chunk = await v_resp.content.read(65536)
                                                if not chunk:
                                                    break
                                                vf.write(chunk)
                            except Exception as e:
                                print(f"[Flipsnack] Video download failed (page {v_num}): {e}")

                    await asyncio.gather(*(fetch_video(v) for v in extracted_videos))

            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)

            self._report("completed", percent=100.0)
            return final_pdf_path

    # --------------------------------------------------------------------------
    # 3. HEYZINE PROVIDER
    # --------------------------------------------------------------------------
    async def _download_heyzine(self) -> str:
        self._report("analyzing", percent=5.0)
        connector = aiohttp.TCPConnector(ssl=self.ssl_context)
        async with aiohttp.ClientSession(connector=connector) as session:
            headers = {"User-Agent": USER_AGENT}
            async with session.get(self.url, headers=headers) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Heyzine returned HTTP {resp.status}")
                html = await resp.text(errors="ignore")

            title_m = re.search(r'<title>(.*?)</title>', html)
            title = title_m.group(1) if title_m else "Heyzine Flipbook"
            title = sanitize_filename(title)
            final_pdf_path = os.path.join(self.output_dir, f"{title}.pdf")

            # Look for direct PDF loaded by heyzine
            pdf_m = re.search(r'heyzine\.load\([\'"](https?://[^\'"]+\.pdf)', html)
            if not pdf_m:
                pdf_m = re.search(r'[\'"](https?://[^\'"]+\.pdf)[\'"]', html)

            if pdf_m:
                pdf_url = pdf_m.group(1)
                self._report("downloading", percent=20.0)
                async with session.get(pdf_url, headers=headers) as dl_resp:
                    if dl_resp.status == 200:
                        content = await dl_resp.read()
                        with open(final_pdf_path, "wb") as f:
                            f.write(content)
                        self._report("completed", percent=100.0)
                        return final_pdf_path

        # If direct PDF wasn't exposed, fall back to headless CDP
        return await self._download_cdp_flipbook()

    # --------------------------------------------------------------------------
    # 4. CDP HEADLESS PROVIDER (FlipHTML5, AnyFlip, PubHTML5, Calaméo, Yumpu)
    # --------------------------------------------------------------------------
    async def _download_cdp_flipbook(self) -> str:
        self._report("analyzing", percent=5.0)
        browser_bin = find_browser_binary()
        if not browser_bin:
            raise RuntimeError("Chromium/Brave headless browser not found. Required for protected flipbook extraction.")

        user_data_dir = tempfile.mkdtemp(prefix="copita_brave_")
        port = find_free_port()

        # Build canonical online reader URL
        target_url = self.url
        if "fliphtml5.com" in target_url and "online.fliphtml5.com" not in target_url:
            path_parts = [p for p in urllib.parse.urlparse(target_url).path.split('/') if p]
            if len(path_parts) >= 2:
                target_url = f"https://online.fliphtml5.com/{path_parts[0]}/{path_parts[1]}/"
        elif "anyflip.com" in target_url and "online.anyflip.com" not in target_url:
            path_parts = [p for p in urllib.parse.urlparse(target_url).path.split('/') if p]
            if len(path_parts) >= 2:
                target_url = f"https://online.anyflip.com/{path_parts[0]}/{path_parts[1]}/"
        elif "pubhtml5.com" in target_url and "online.pubhtml5.com" not in target_url:
            path_parts = [p for p in urllib.parse.urlparse(target_url).path.split('/') if p]
            if len(path_parts) >= 2:
                target_url = f"https://online.pubhtml5.com/{path_parts[0]}/{path_parts[1]}/"

        cmd = [
            browser_bin,
            "--headless=new",
            "--disable-gpu",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank"
        ]

        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        extracted_data = None
        captured_images: List[str] = []

        try:
            await asyncio.sleep(1.5)
            connector = aiohttp.TCPConnector(ssl=self.ssl_context)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.put(f"http://127.0.0.1:{port}/json/new") as r:
                    tab_info = await r.json()
                    ws_url = tab_info["webSocketDebuggerUrl"]

                async with session.ws_connect(ws_url) as ws:
                    # Enable Network & Page
                    await ws.send_json({"id": 1, "method": "Network.enable"})
                    await ws.send_json({"id": 2, "method": "Page.navigate", "params": {"url": target_url}})

                    expr = """(() => {
                        let pages = window.fliphtml5_pages || window.anyflip_pages || (window.htmlConfig && window.htmlConfig.pages) || [];
                        let base = (window.bookConfig && window.bookConfig.bookBaseURL) || (window.bookConfig && window.bookConfig.baseURL) || (window.location.origin + window.location.pathname.replace(/\\/mobile\\/.*|\\/index\\.html|\\/$/, '') + '/');
                        if (!base.endsWith('/')) base += '/';
                        let title = document.title || (window.bookConfig && window.bookConfig.bookTitle) || 'Flipbook';
                        let urls = [];
                        for (let p of pages) {
                            let raw = p.n || p.l || p.t;
                            if (Array.isArray(raw)) raw = raw[0];
                            if (raw) {
                                let fname = raw.split('?')[0].split('/').pop();
                                urls.push(base + 'files/large/' + fname);
                            }
                        }
                        return { title: title, urls: urls, count: urls.length };
                    })()"""

                    # Poll for up to 10 seconds for window pages array
                    start_t = time.time()
                    while time.time() - start_t < 10.0:
                        if self._cancel_requested:
                            raise asyncio.CancelledError()
                        await asyncio.sleep(0.5)
                        await ws.send_json({"id": 10, "method": "Runtime.evaluate", "params": {"expression": expr, "returnByValue": True}})

                        try:
                            while True:
                                msg = await asyncio.wait_for(ws.receive(), timeout=0.8)
                                data = json.loads(msg.data)
                                if data.get("method") == "Network.requestWillBeSent":
                                    req_u = data["params"]["request"]["url"]
                                    if any(ext in req_u.lower() for ext in [".webp", ".jpg", ".jpeg", ".png"]):
                                        if "files/large" in req_u or "files/page" in req_u:
                                            if req_u not in captured_images:
                                                captured_images.append(req_u)
                                elif data.get("id") == 10:
                                    val = data.get("result", {}).get("result", {}).get("value") or {}
                                    if val.get("count", 0) > 0:
                                        extracted_data = val
                                        break
                        except asyncio.TimeoutError:
                            pass

                        if extracted_data:
                            break

        finally:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except Exception:
                proc.kill()
            shutil.rmtree(user_data_dir, ignore_errors=True)

        if not extracted_data and not captured_images:
            raise RuntimeError("Could not discover page assets via CDP.")

        # Determine title and page URLs
        title = extracted_data.get("title") if extracted_data else "Flipbook Document"
        title = sanitize_filename(title)
        final_pdf_path = os.path.join(self.output_dir, f"{title}.pdf")

        page_urls = extracted_data.get("urls", []) if extracted_data else captured_images
        self._report("downloading", percent=15.0, total=len(page_urls) * 400000, threads=16)

        # Step 5: Concurrently download all page WebP/JPEG images
        temp_dir = tempfile.mkdtemp(prefix="copita_cdp_pages_")
        try:
            connector = aiohttp.TCPConnector(ssl=self.ssl_context)
            async with aiohttp.ClientSession(connector=connector) as dl_session:
                page_files = await self._download_direct_images(
                    dl_session, page_urls, temp_dir, referer=target_url
                )
                if not page_files:
                    raise RuntimeError("Failed to download page images for flipbook.")

                # Step 6: Compile to PDF
                self._report("compiling", percent=90.0)
                await self._compile_images_to_pdf(page_files, final_pdf_path)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        self._report("completed", percent=100.0)
        return final_pdf_path

    # --------------------------------------------------------------------------
    # COMMON HELPERS
    # --------------------------------------------------------------------------
    async def _download_candidate_images(
        self,
        session: aiohttp.ClientSession,
        candidate_lists: List[List[str]],
        temp_dir: str
    ) -> List[str]:
        """Download each page using fallback candidates in priority order."""
        sem = asyncio.Semaphore(16)
        downloaded_bytes = 0
        completed_pages = 0
        total_pages = len(candidate_lists)
        start_time = time.time()
        file_map: Dict[int, str] = {}
        headers = {"User-Agent": USER_AGENT}

        async def fetch_page(idx: int, candidates: List[str]):
            nonlocal downloaded_bytes, completed_pages
            async with sem:
                if self._cancel_requested:
                    return
                for u in candidates:
                    try:
                        async with session.get(u, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                            if resp.status == 200:
                                content = await resp.read()
                                if len(content) > 500:
                                    ext = ".webp" if ".webp" in u.lower() else ".jpg"
                                    p_path = os.path.join(temp_dir, f"page_{idx:04d}{ext}")
                                    with open(p_path, "wb") as f:
                                        f.write(content)
                                    file_map[idx] = p_path
                                    downloaded_bytes += len(content)
                                    completed_pages += 1
                                    elapsed = max(0.1, time.time() - start_time)
                                    pct = 15.0 + (completed_pages / total_pages) * 70.0
                                    self._report(
                                        "downloading",
                                        percent=pct,
                                        downloaded=downloaded_bytes,
                                        total=total_pages * (downloaded_bytes // max(1, completed_pages)),
                                        speed=downloaded_bytes / elapsed,
                                        threads=16
                                    )
                                    return
                    except Exception:
                        continue

        await asyncio.gather(*(fetch_page(i, c) for i, c in enumerate(candidate_lists)))
        return [file_map[i] for i in sorted(file_map.keys())]

    async def _download_direct_images(
        self,
        session: aiohttp.ClientSession,
        urls: List[str],
        temp_dir: str,
        referer: Optional[str] = None
    ) -> List[str]:
        sem = asyncio.Semaphore(16)
        downloaded_bytes = 0
        completed_pages = 0
        total_pages = len(urls)
        start_time = time.time()
        file_map: Dict[int, str] = {}

        headers = {"User-Agent": USER_AGENT}
        if referer:
            headers["Referer"] = referer

        async def fetch_item(idx: int, img_url: str):
            nonlocal downloaded_bytes, completed_pages
            async with sem:
                if self._cancel_requested:
                    return
                ext = ".webp" if ".webp" in img_url.lower() else ".jpg"
                p_path = os.path.join(temp_dir, f"page_{idx:04d}{ext}")
                try:
                    async with session.get(img_url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                        if resp.status == 200:
                            content = await resp.read()
                            with open(p_path, "wb") as f:
                                f.write(content)
                            file_map[idx] = p_path
                            downloaded_bytes += len(content)
                            completed_pages += 1
                            elapsed = max(0.1, time.time() - start_time)
                            pct = 15.0 + (completed_pages / total_pages) * 75.0
                            self._report(
                                "downloading",
                                percent=pct,
                                downloaded=downloaded_bytes,
                                total=total_pages * (downloaded_bytes // max(1, completed_pages)),
                                speed=downloaded_bytes / elapsed,
                                threads=16
                            )
                except Exception as e:
                    print(f"[Flipbook] Error downloading {img_url}: {e}")

        await asyncio.gather(*(fetch_item(i, u) for i, u in enumerate(urls)))
        return [file_map[i] for i in sorted(file_map.keys())]

    async def _compile_images_to_pdf(self, page_files: List[str], output_pdf_path: str):
        """Compile list of image files into a single unified high-res PDF via Pillow."""
        loop = asyncio.get_running_loop()

        def _do_compile():
            images = []
            for p in page_files:
                try:
                    im = Image.open(p)
                    if im.mode in ("RGBA", "P"):
                        im = im.convert("RGB")
                    images.append(im)
                except Exception as e:
                    print(f"[Flipbook] Error opening image {p}: {e}")

            if not images:
                raise RuntimeError("No valid images available to assemble PDF.")

            images[0].save(
                output_pdf_path,
                save_all=True,
                append_images=images[1:],
                resolution=150.0,
                quality=95
            )

        await loop.run_in_executor(None, _do_compile)
        if not os.path.exists(output_pdf_path) or os.path.getsize(output_pdf_path) == 0:
            raise RuntimeError("Failed to create PDF document.")
