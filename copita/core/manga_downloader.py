"""
Universal Manga and Web Comic Downloader for Copita.
Engineered for protected browser-based readers with disabled downloads:
1. MANGA Plus by Shueisha (jumpg-webapi Protobuf protocol, dynamic session tokens, XOR image descrambler)
2. GlobalComix (readV3 API, X-Gc-Client auth, signed reader JWT cookies, reader-cdn WebP pages)
3. KADOCOMI / Comic-Walker (contents/viewer API, CloudFront signed URLs, XOR DRM hash descrambler)
4. WEBTOON (Naver webtoon vertical strip loader)
5. MangaDex (REST API, MangaDex@Home cluster)
6. Tapas (series episode loader and token-protected strip pages)

Compiles decrypted pages into high-resolution multi-page PDFs or CBZ/ZIP archives.
Zero .bin fallback.
"""

import os
import re
import io
import uuid
import time
import zipfile
import asyncio
from typing import Dict, Any, Optional, List, Callable
from urllib.parse import urlparse, parse_qs

import httpx
from PIL import Image

def decode_protobuf(data: bytes, pos: int = 0, end: Optional[int] = None) -> List[tuple]:
    """
    Pure-Python lightweight protobuf wire-format parser.
    Returns a list of (field_number, wire_type, value).
    """
    if end is None:
        end = len(data)
    items = []
    while pos < end:
        tag = 0
        shift = 0
        while pos < end:
            b = data[pos]
            pos += 1
            tag |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7
        wire = tag & 7
        num = tag >> 3

        if wire == 0:  # Varint
            val = 0
            shift = 0
            while pos < end:
                b = data[pos]
                pos += 1
                val |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            items.append((num, wire, val))
        elif wire == 2:  # Length-delimited
            length = 0
            shift = 0
            while pos < end:
                b = data[pos]
                pos += 1
                length |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            chunk = data[pos:pos + length]
            pos += length
            items.append((num, wire, chunk))
        elif wire == 1:  # 64-bit
            items.append((num, wire, data[pos:pos + 8]))
            pos += 8
        elif wire == 5:  # 32-bit
            items.append((num, wire, data[pos:pos + 4]))
            pos += 4
        else:
            break
    return items


def descramble_xor(encrypted_bytes: bytes, hex_key: str) -> bytes:
    """
    Decrypts XOR-scrambled image bytes used by MangaPlus and KADOCOMI.
    Formula: decrypted[i] = encrypted[i] ^ key_bytes[i % key_len]
    """
    if not hex_key:
        return encrypted_bytes
    key_bytes = bytes.fromhex(hex_key)
    klen = len(key_bytes)
    buf = bytearray(encrypted_bytes)
    for i in range(len(buf)):
        buf[i] ^= key_bytes[i % klen]
    return bytes(buf)


class MangaDownloader:
    def __init__(
        self,
        url: str,
        output_dir: str,
        concurrency: int = 8,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        options: Optional[Dict[str, Any]] = None
    ):
        self.url = url.strip()
        self.output_dir = os.path.abspath(output_dir)
        os.makedirs(self.output_dir, exist_ok=True)
        self.concurrency = max(1, concurrency)
        self.progress_callback = progress_callback
        self.options = options or {}
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    def _notify(self, data: Dict[str, Any]):
        if self.progress_callback:
            try:
                self.progress_callback(data)
            except Exception:
                pass

    async def download(self) -> str:
        parsed = urlparse(self.url)
        netloc = parsed.netloc.lower()

        if "mangaplus.shueisha.co.jp" in netloc:
            return await self._download_mangaplus()
        elif "globalcomix.com" in netloc:
            return await self._download_globalcomix()
        elif "comic-walker.com" in netloc:
            return await self._download_kadocomi()
        elif "webtoons.com" in netloc:
            return await self._download_webtoons()
        elif "mangadex.org" in netloc:
            return await self._download_mangadex()
        elif "tapas.io" in netloc:
            return await self._download_tapas()
        elif "namicomi.com" in netloc:
            return await self._download_namicomi()
        elif "voyce.me" in netloc:
            return await self._download_voyce()
        elif "comix.to" in netloc:
            return await self._download_comix()
        else:
            return await self._download_generic_manga()

    # ==========================================
    # 1. MANGA PLUS BY SHUEISHA
    # ==========================================
    async def _download_mangaplus(self) -> str:
        parsed = urlparse(self.url)
        path = parsed.path.rstrip('/')

        if "/viewer/" in path:
            ch_match = re.search(r'/viewer/(\d+)', path)
            if not ch_match:
                raise ValueError("Could not extract chapter ID from viewer URL")
            chapter_id = int(ch_match.group(1))
            return await self._download_mangaplus_chapter(chapter_id)
        elif "/titles/" in path:
            t_match = re.search(r'/titles/(\d+)', path)
            if not t_match:
                raise ValueError("Could not extract title ID from titles URL")
            title_id = int(t_match.group(1))

            opt_chapter = self.options.get("chapter_id")
            if opt_chapter:
                return await self._download_mangaplus_chapter(int(opt_chapter))

            return await self._download_mangaplus_title(title_id)
        else:
            raise ValueError(f"Unrecognized MangaPlus URL format: {self.url}")

    async def _download_mangaplus_title(self, title_id: int) -> str:
        self._notify({"status": "analyzing", "percent": 5.0, "message": "Fetching MangaPlus title..."})
        session_token = str(uuid.uuid4())

        async with httpx.AsyncClient(verify=False, timeout=20.0) as client:
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "SESSION-TOKEN": session_token,
                "Origin": "https://mangaplus.shueisha.co.jp",
                "Referer": "https://mangaplus.shueisha.co.jp/",
            }
            api_url = f"https://jumpg-webapi.tokyo-cdn.com/api/title_detailV3?title_id={title_id}"
            resp = await client.get(api_url, headers=headers)
            if resp.status_code != 200:
                raise ValueError(f"MangaPlus API returned status {resp.status_code}")

            raw_proto = resp.content

        title_name = f"Manga_{title_id}"
        chapters = []

        for n1, w1, v1 in decode_protobuf(raw_proto):
            if n1 == 1:
                for n2, w2, v2 in decode_protobuf(v1):
                    if n2 == 8:
                        for n3, w3, v3 in decode_protobuf(v2):
                            if n3 == 1:
                                for n_t, w_t, v_t in decode_protobuf(v3):
                                    if n_t == 2 and w_t == 2:
                                        title_name = v_t.decode('utf-8', errors='ignore').strip()
                            elif n3 == 28:
                                for n4, w4, v4 in decode_protobuf(v3):
                                    if w4 == 2 and n4 in (2, 4):  # First and Latest free readable chapters
                                        ch = {}
                                        for n5, w5, v5 in decode_protobuf(v4):
                                            if w5 == 0 and n5 == 2:
                                                ch['chapter_id'] = v5
                                            elif w5 == 2 and n5 == 3:
                                                ch['number'] = v5.decode('utf-8', errors='ignore').strip()
                                            elif w5 == 2 and n5 == 4:
                                                ch['title'] = v5.decode('utf-8', errors='ignore').strip()
                                        if 'chapter_id' in ch and ch not in chapters:
                                            chapters.append(ch)

        if not chapters:
            raise ValueError(f"No readable chapters found for title '{title_name}' (ID: {title_id})")

        safe_title = re.sub(r'[\/*?:"<>|]', '_', title_name).strip()
        title_subfolder = os.path.join(self.output_dir, safe_title)
        os.makedirs(title_subfolder, exist_ok=True)

        total_chapters = len(chapters)
        self._notify({
            "status": "downloading",
            "percent": 10.0,
            "message": f"Found {total_chapters} readable chapters for '{title_name}'"
        })

        chapter_pdf_paths = []
        for idx, ch in enumerate(chapters):
            if self.cancelled:
                raise asyncio.CancelledError()

            ch_id = ch['chapter_id']
            ch_num = ch.get('number', f"#{idx+1}")
            ch_title = ch.get('title', '')
            label = f"{ch_num} - {ch_title}" if ch_title else ch_num

            pct_start = 10.0 + (idx / total_chapters) * 80.0
            pct_end = 10.0 + ((idx + 1) / total_chapters) * 80.0

            def _ch_progress(sub_data):
                sub_pct = sub_data.get("percent", 0.0)
                overall = pct_start + (sub_pct / 100.0) * (pct_end - pct_start)
                self._notify({
                    "status": "downloading",
                    "percent": overall,
                    "message": f"Downloading chapter {idx+1}/{total_chapters}: {label}"
                })

            try:
                pdf_path = await self._download_mangaplus_chapter(
                    chapter_id=ch_id,
                    target_dir=title_subfolder,
                    sub_progress=_ch_progress
                )
                chapter_pdf_paths.append(pdf_path)
            except Exception as ch_err:
                # Chapter may require mobile app ticket or subscription
                continue

        zip_filename = f"{safe_title}.zip"
        zip_path = os.path.join(self.output_dir, zip_filename)
        self._notify({"status": "merging", "percent": 95.0, "message": "Creating collection archive..."})

        with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
            for c_path in chapter_pdf_paths:
                zf.write(c_path, arcname=os.path.basename(c_path))

        self._notify({
            "status": "completed",
            "percent": 100.0,
            "downloaded": os.path.getsize(zip_path),
            "total": os.path.getsize(zip_path),
            "message": f"Complete collection saved ({total_chapters} chapters)"
        })
        return zip_path

    async def _download_mangaplus_chapter(
        self,
        chapter_id: int,
        target_dir: Optional[str] = None,
        sub_progress: Optional[Callable[[Dict[str, Any]], None]] = None
    ) -> str:
        out_dir = target_dir or self.output_dir
        session_token = str(uuid.uuid4())

        async with httpx.AsyncClient(verify=False, timeout=20.0) as client:
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "SESSION-TOKEN": session_token,
                "Origin": "https://mangaplus.shueisha.co.jp",
                "Referer": "https://mangaplus.shueisha.co.jp/",
            }
            api_url = f"https://jumpg-webapi.tokyo-cdn.com/api/manga_viewer_v3?chapter_id={chapter_id}&split=yes&img_quality=high"
            resp = await client.get(api_url, headers=headers)
            if resp.status_code != 200:
                raise ValueError(f"MangaPlus viewer API returned status {resp.status_code}")

            raw_proto = resp.content

        vw_token = None
        title_name = "Manga"
        chapter_number = ""
        pages = []

        for n1, w1, v1 in decode_protobuf(raw_proto):
            if n1 == 1:
                for n2, w2, v2 in decode_protobuf(v1):
                    if n2 == 10:
                        for n3, w3, v3 in decode_protobuf(v2):
                            if n3 == 19 and w3 == 2:
                                vw_token = v3.decode('utf-8', errors='ignore')
                            elif n3 == 5 and w3 == 2:
                                title_name = v3.decode('utf-8', errors='ignore').strip()
                            elif n3 == 6 and w3 == 2:
                                chapter_number = v3.decode('utf-8', errors='ignore').strip()
                            elif n3 == 1 and w3 == 2:
                                for n4, w4, v4 in decode_protobuf(v3):
                                    if n4 == 1 and w4 == 2:
                                        p_info = {}
                                        for n5, w5, v5 in decode_protobuf(v4):
                                            if n5 == 1 and w5 == 2:
                                                p_info['url'] = v5.decode('utf-8', errors='ignore')
                                            elif n5 == 5 and w5 == 2:
                                                p_info['key'] = v5.decode('utf-8', errors='ignore')
                                        if 'url' in p_info:
                                            pages.append(p_info)

        if not pages:
            raise ValueError(f"No pages found for chapter {chapter_id}")

        safe_title = re.sub(r'[\/*?:"<>|]', '_', title_name).strip()
        safe_chapter = re.sub(r'[\/*?:"<>|]', '_', chapter_number or f"Chapter_{chapter_id}").strip()
        pdf_name = f"{safe_title} - {safe_chapter}.pdf"
        pdf_path = os.path.join(out_dir, pdf_name)

        total_pages = len(pages)
        downloaded_pages: List[Optional[Image.Image]] = [None] * total_pages
        t_start = time.time()

        img_headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Origin": "https://mangaplus.shueisha.co.jp",
            "Referer": "https://mangaplus.shueisha.co.jp/",
        }
        if vw_token:
            img_headers["Plus-Vw-Token"] = vw_token

        def _fetch_page(index: int, page_data: Dict[str, Any]) -> Image.Image:
            page_url = page_data['url']
            key_hex = page_data.get('key')
            with httpx.Client(verify=False, timeout=30.0) as client:
                r = client.get(page_url, headers=img_headers)
                if r.status_code != 200:
                    raise ValueError(f"Failed to fetch page {index+1}: HTTP {r.status_code}")
                raw_bytes = r.content

            if key_hex:
                decrypted = descramble_xor(raw_bytes, key_hex)
            else:
                decrypted = raw_bytes

            img = Image.open(io.BytesIO(decrypted))
            return img.convert("RGB")

        loop = asyncio.get_running_loop()
        completed_count = 0

        async def _download_worker(idx: int, p_data: Dict[str, Any]):
            nonlocal completed_count
            if self.cancelled:
                return
            img = await loop.run_in_executor(None, lambda: _fetch_page(idx, p_data))
            downloaded_pages[idx] = img
            completed_count += 1
            pct = (completed_count / total_pages) * 90.0
            elapsed = max(0.1, time.time() - t_start)
            speed = (completed_count * 250_000) / elapsed

            progress_data = {
                "status": "downloading",
                "percent": pct,
                "downloaded": completed_count * 250_000,
                "total": total_pages * 250_000,
                "speed": speed,
                "message": f"Downloading page {completed_count}/{total_pages}"
            }
            if sub_progress:
                sub_progress(progress_data)
            else:
                self._notify(progress_data)

        sem = asyncio.Semaphore(self.concurrency)
        async def _bounded_worker(idx: int, p_data: Dict[str, Any]):
            async with sem:
                await _download_worker(idx, p_data)

        tasks = [_bounded_worker(i, p) for i, p in enumerate(pages)]
        await asyncio.gather(*tasks)

        if sub_progress:
            sub_progress({"status": "merging", "percent": 95.0, "message": "Compiling PDF..."})
        else:
            self._notify({"status": "merging", "percent": 95.0, "message": "Compiling PDF..."})

        valid_images = [img for img in downloaded_pages if img is not None]
        if not valid_images:
            raise ValueError(f"No pages successfully downloaded for chapter {chapter_id}")

        def _save_pdf():
            first_page = valid_images[0]
            rest_pages = valid_images[1:]
            first_page.save(pdf_path, save_all=True, append_images=rest_pages, resolution=100.0)

        await loop.run_in_executor(None, _save_pdf)
        pdf_size = os.path.getsize(pdf_path)

        final_data = {
            "status": "completed",
            "percent": 100.0,
            "downloaded": pdf_size,
            "total": pdf_size,
            "message": f"Saved {pdf_name} ({total_pages} pages)"
        }
        if sub_progress:
            sub_progress(final_data)
        else:
            self._notify(final_data)

        return pdf_path

    # ==========================================
    # 2. GLOBALCOMIX
    # ==========================================
    async def _download_globalcomix(self) -> str:
        parsed = urlparse(self.url)
        path = parsed.path.rstrip('/')

        # Extract release UUID
        release_id_match = re.search(r'([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})', path)
        if not release_id_match:
            # Maybe comic overview page /c/{slug}
            async with httpx.AsyncClient(verify=False, timeout=15.0) as client:
                r = await client.get(self.url, headers={"User-Agent": "Mozilla/5.0"})
                # Find first reader link
                r_matches = re.findall(r'/r/([0-9a-fA-F\-]{36})', r.text)
                if not r_matches:
                    r_matches = re.findall(r'/read/([0-9a-fA-F\-]{36})', r.text)
                if r_matches:
                    release_id = r_matches[0]
                else:
                    raise ValueError(f"Could not find readable release on GlobalComix page: {self.url}")
        else:
            release_id = release_id_match.group(1)

        self._notify({"status": "analyzing", "percent": 5.0, "message": "Fetching GlobalComix release..."})
        api_headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "X-Gc-Client": "gck_b4d492261ec541eda44ce41de79da424",
            "X-Gc-Identmode": "cookie",
            "Origin": "https://globalcomix.com",
            "Referer": "https://globalcomix.com/",
        }

        async with httpx.AsyncClient(verify=False, timeout=20.0) as client:
            read_url = f"https://api.globalcomix.com/v1/readV3/{release_id}?readerV=2"
            api_resp = await client.get(read_url, headers=api_headers)
            if api_resp.status_code != 200:
                raise ValueError(f"GlobalComix API returned status {api_resp.status_code}")

            data = api_resp.json()
            results = data.get("payload", {}).get("results", {})
            title = results.get("title") or results.get("comic_name") or f"GlobalComix_{release_id}"
            cdn_access = results.get("reader_cdn_access", {})
            base_url = cdn_access.get("base_url", "https://reader-cdn.globalcomix.com").rstrip('/')
            rel_key = cdn_access.get("release_key", release_id)
            page_count = cdn_access.get("page_count") or results.get("page_count", 0)

            # Build image list using the reader CDN session cookies
            cookies = client.cookies

        if page_count <= 0:
            raise ValueError("No pages available for this GlobalComix release")

        safe_title = re.sub(r'[\/*?:"<>|]', '_', title).strip()
        pdf_name = f"{safe_title}.pdf"
        pdf_path = os.path.join(self.output_dir, pdf_name)

        downloaded_images: List[Optional[Image.Image]] = [None] * page_count
        loop = asyncio.get_running_loop()
        t_start = time.time()

        def _fetch_gc_page(order: int) -> Image.Image:
            page_url = f"{base_url}/r/{rel_key}/p/{order}/desktop.webp"
            with httpx.Client(verify=False, timeout=30.0, cookies=cookies) as cl:
                res = cl.get(page_url, headers={"Referer": "https://globalcomix.com/"})
                if res.status_code != 200:
                    # Try thumbnail as fallback
                    thumb_url = f"{base_url}/r/{rel_key}/p/{order}/thumbnail.webp"
                    res = cl.get(thumb_url, headers={"Referer": "https://globalcomix.com/"})
                res.raise_for_status()
                return Image.open(io.BytesIO(res.content)).convert("RGB")

        sem = asyncio.Semaphore(self.concurrency)
        completed = 0

        async def _gc_worker(idx: int):
            nonlocal completed
            if self.cancelled:
                return
            order = idx + 1
            img = await loop.run_in_executor(None, lambda: _fetch_gc_page(order))
            downloaded_images[idx] = img
            completed += 1
            self._notify({
                "status": "downloading",
                "percent": (completed / page_count) * 90.0,
                "message": f"Downloading GlobalComix page {completed}/{page_count}"
            })

        await asyncio.gather(*[_gc_worker(i) for i in range(page_count)])

        self._notify({"status": "merging", "percent": 95.0, "message": "Compiling PDF..."})
        valid = [im for im in downloaded_images if im is not None]
        if not valid:
            raise ValueError("Failed to download pages from GlobalComix")

        await loop.run_in_executor(None, lambda: valid[0].save(pdf_path, save_all=True, append_images=valid[1:], resolution=100.0))
        self._notify({"status": "completed", "percent": 100.0, "message": f"Saved {pdf_name} ({page_count} pages)"})
        return pdf_path

    # ==========================================
    # 3. KADOCOMI / COMIC-WALKER
    # ==========================================
    async def _download_kadocomi(self) -> str:
        parsed = urlparse(self.url)
        path = parsed.path.rstrip('/')

        # 1. Extract episode ID or workCode
        episode_id = None
        work_title = "KADOCOMI_Comic"
        episode_title = "Episode"

        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Referer": "https://comic-walker.com/",
        }

        async with httpx.AsyncClient(verify=False, timeout=20.0, headers=headers) as client:
            resp = await client.get(self.url)
            html = resp.text

            # Check for __NEXT_DATA__
            next_match = re.search(r'id=\"__NEXT_DATA__\"[^>]*>(.*?)</script>', html, re.DOTALL)
            if next_match:
                import json
                try:
                    ndata = json.loads(next_match.group(1))
                    queries = ndata.get('props', {}).get('pageProps', {}).get('dehydratedState', {}).get('queries', [])
                    for q in queries:
                        qk = q.get('queryKey', [])
                        qd = q.get('state', {}).get('data', {})
                        if '/api/contents/details/work' in qk:
                            work_title = qd.get('work', {}).get('title') or work_title
                            if not episode_id and 'firstEpisodes' in qd:
                                eps = qd['firstEpisodes'].get('result', [])
                                if eps:
                                    episode_id = eps[0].get('id')
                                    episode_title = eps[0].get('title', episode_title)
                        elif '/api/contents/details/episode' in qk:
                            ep_obj = qd.get('episode', {})
                            if ep_obj.get('id'):
                                episode_id = ep_obj['id']
                                episode_title = ep_obj.get('title', episode_title)
                except Exception:
                    pass

            if not episode_id:
                # Direct regex search for episode UUID
                ep_match = re.search(r'([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})', html)
                if ep_match:
                    episode_id = ep_match.group(1)

            if not episode_id:
                raise ValueError(f"Could not find active episode for KADOCOMI URL: {self.url}")

            # 2. Call viewer API
            self._notify({"status": "analyzing", "percent": 10.0, "message": "Fetching KADOCOMI viewer..."})
            viewer_url = f"https://comic-walker.com/api/contents/viewer?episodeId={episode_id}&imageSizeType=width%3A1284"
            v_resp = await client.get(viewer_url)
            if v_resp.status_code != 200:
                raise ValueError(f"KADOCOMI viewer API returned status {v_resp.status_code}")

            v_data = v_resp.json()
            manuscripts = v_data.get("manuscripts", [])

        if not manuscripts:
            raise ValueError(f"No pages found for KADOCOMI episode: {episode_id}")

        safe_work = re.sub(r'[\/*?:"<>|]', '_', work_title).strip()
        safe_ep = re.sub(r'[\/*?:"<>|]', '_', episode_title).strip()
        pdf_name = f"{safe_work} - {safe_ep}.pdf"
        pdf_path = os.path.join(self.output_dir, pdf_name)

        total_pages = len(manuscripts)
        downloaded_images: List[Optional[Image.Image]] = [None] * total_pages
        loop = asyncio.get_running_loop()

        def _fetch_kado_page(m: Dict[str, Any]) -> Image.Image:
            img_url = m['drmImageUrl']
            drm_hash = m.get('drmHash')
            with httpx.Client(verify=False, timeout=30.0, headers=headers) as cl:
                r = cl.get(img_url)
                r.raise_for_status()
                raw = r.content

            if drm_hash and m.get('drmMode') == 'xor':
                raw = descramble_xor(raw, drm_hash)

            return Image.open(io.BytesIO(raw)).convert("RGB")

        sem = asyncio.Semaphore(self.concurrency)
        completed = 0

        async def _kado_worker(idx: int, m_item: Dict[str, Any]):
            nonlocal completed
            if self.cancelled:
                return
            img = await loop.run_in_executor(None, lambda: _fetch_kado_page(m_item))
            downloaded_images[idx] = img
            completed += 1
            self._notify({
                "status": "downloading",
                "percent": (completed / total_pages) * 90.0,
                "message": f"Downloading KADOCOMI page {completed}/{total_pages}"
            })

        await asyncio.gather(*[_kado_worker(i, m) for i, m in enumerate(manuscripts)])

        self._notify({"status": "merging", "percent": 95.0, "message": "Compiling PDF..."})
        valid = [im for im in downloaded_images if im is not None]
        if not valid:
            raise ValueError("Failed to download pages from KADOCOMI")

        await loop.run_in_executor(None, lambda: valid[0].save(pdf_path, save_all=True, append_images=valid[1:], resolution=100.0))
        self._notify({"status": "completed", "percent": 100.0, "message": f"Saved {pdf_name} ({total_pages} pages)"})
        return pdf_path

    # ==========================================
    # 4. WEBTOONS
    # ==========================================
    async def _download_webtoons(self) -> str:
        self._notify({"status": "analyzing", "percent": 10.0, "message": "Extracting Webtoon strip panels..."})
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Referer": "https://www.webtoons.com/"
        }
        async with httpx.AsyncClient(verify=False, timeout=20.0, headers=headers) as client:
            resp = await client.get(self.url)
            html = resp.text

        title_match = re.search(r'<h1 class="subj_episode"[^>]*>([^<]+)</h1>', html)
        ch_title = title_match.group(1).strip() if title_match else "Webtoon_Episode"
        safe_title = re.sub(r'[\/*?:"<>|]', '_', ch_title).strip()

        img_urls = re.findall(r'class="_images"[^>]*data-url="([^"]+)"', html)
        if not img_urls:
            img_urls = re.findall(r'data-url="(https://webtoon-phinf\.pstatic\.net/[^"]+)"', html)

        if not img_urls:
            raise ValueError("No comic strip images found on Webtoons page")

        pdf_path = os.path.join(self.output_dir, f"{safe_title}.pdf")
        total = len(img_urls)
        downloaded_images: List[Optional[Image.Image]] = [None] * total
        loop = asyncio.get_running_loop()

        def _fetch_strip(u: str) -> Image.Image:
            with httpx.Client(verify=False, timeout=30.0, headers=headers) as cl:
                r = cl.get(u)
                r.raise_for_status()
                return Image.open(io.BytesIO(r.content)).convert("RGB")

        sem = asyncio.Semaphore(self.concurrency)
        completed = 0
        async def _worker(i: int, u: str):
            nonlocal completed
            async with sem:
                img = await loop.run_in_executor(None, lambda: _fetch_strip(u))
                downloaded_images[i] = img
                completed += 1
                self._notify({
                    "status": "downloading",
                    "percent": (completed / total) * 90.0,
                    "message": f"Downloading panel {completed}/{total}"
                })

        await asyncio.gather(*[_worker(i, u) for i, u in enumerate(img_urls)])

        valid = [im for im in downloaded_images if im is not None]
        await loop.run_in_executor(None, lambda: valid[0].save(pdf_path, save_all=True, append_images=valid[1:], resolution=100.0))
        self._notify({"status": "completed", "percent": 100.0, "message": f"Saved {safe_title}.pdf"})
        return pdf_path

    # ==========================================
    # 5. MANGADEX
    # ==========================================
    async def _download_mangadex(self) -> str:
        parsed = urlparse(self.url)
        path = parsed.path.rstrip('/')

        chapter_match = re.search(r'/chapter/([0-9a-fA-F\-]+)', path)
        if chapter_match:
            chapter_id = chapter_match.group(1)
            return await self._download_mangadex_chapter(chapter_id)

        title_match = re.search(r'/title/([0-9a-fA-F\-]+)', path)
        if title_match:
            manga_id = title_match.group(1)
            return await self._download_mangadex_title(manga_id)

        raise ValueError(f"Unrecognized MangaDex URL format: {self.url}")

    async def _download_mangadex_title(self, manga_id: str) -> str:
        self._notify({"status": "analyzing", "percent": 5.0, "message": "Fetching MangaDex title..."})
        async with httpx.AsyncClient(verify=False, timeout=15.0) as client:
            r = await client.get(f"https://api.mangadex.org/manga/{manga_id}/feed?translatedLanguage[]=en&order[chapter]=desc&limit=1")
            r_data = r.json()
            data = r_data.get("data", [])
            if not data:
                raise ValueError("No English chapters found for this MangaDex title")
            latest_chapter_id = data[0]["id"]
            return await self._download_mangadex_chapter(latest_chapter_id)

    async def _download_mangadex_chapter(self, chapter_id: str) -> str:
        self._notify({"status": "analyzing", "percent": 10.0, "message": "Fetching MangaDex chapter..."})
        async with httpx.AsyncClient(verify=False, timeout=15.0) as client:
            ch_r = await client.get(f"https://api.mangadex.org/chapter/{chapter_id}")
            ch_data = ch_r.json().get("data", {}).get("attributes", {})
            ch_num = ch_data.get("chapter", "1")
            ch_title = ch_data.get("title", "")

            srv_r = await client.get(f"https://api.mangadex.org/at-home/server/{chapter_id}")
            srv_data = srv_r.json()
            base_url = srv_data.get("baseUrl")
            ch_hash = srv_data.get("chapter", {}).get("hash")
            filenames = srv_data.get("chapter", {}).get("data", [])

        if not filenames or not base_url:
            raise ValueError(f"No pages found for MangaDex chapter {chapter_id}")

        safe_name = f"MangaDex_Ch_{ch_num}"
        if ch_title:
            safe_name += f"_{re.sub(r'[\/*?:\u003c\u003e|]', '_', ch_title)}"
        pdf_path = os.path.join(self.output_dir, f"{safe_name}.pdf")

        total = len(filenames)
        downloaded_images: List[Optional[Image.Image]] = [None] * total
        loop = asyncio.get_running_loop()

        def _fetch_page(fn: str) -> Image.Image:
            img_url = f"{base_url}/data/{ch_hash}/{fn}"
            with httpx.Client(verify=False, timeout=30.0) as cl:
                res = cl.get(img_url)
                res.raise_for_status()
                return Image.open(io.BytesIO(res.content)).convert("RGB")

        sem = asyncio.Semaphore(self.concurrency)
        completed = 0
        async def _worker(i: int, fn: str):
            nonlocal completed
            async with sem:
                img = await loop.run_in_executor(None, lambda: _fetch_page(fn))
                downloaded_images[i] = img
                completed += 1
                self._notify({
                    "status": "downloading",
                    "percent": (completed / total) * 90.0,
                    "message": f"Downloading page {completed}/{total}"
                })

        await asyncio.gather(*[_worker(i, fn) for i, fn in enumerate(filenames)])

        valid = [im for im in downloaded_images if im is not None]
        await loop.run_in_executor(None, lambda: valid[0].save(pdf_path, save_all=True, append_images=valid[1:], resolution=100.0))
        self._notify({"status": "completed", "percent": 100.0, "message": f"Saved {safe_name}.pdf"})
        return pdf_path

    # ==========================================
    # 6. TAPAS
    # ==========================================
    async def _download_tapas(self) -> str:
        parsed = urlparse(self.url)
        path = parsed.path.rstrip('/')

        ep_match = re.search(r'/episode/(\d+)', path)
        if ep_match:
            ep_id = ep_match.group(1)
            return await self._download_tapas_episode(ep_id)

        series_match = re.search(r'/series/([^\/]+)', path)
        if series_match:
            series_slug = series_match.group(1)
            # Fetch first episode of series
            async with httpx.AsyncClient(verify=False, timeout=15.0) as client:
                r = await client.get(f"https://tapas.io/series/{series_slug}/episodes?page=1&sort=OLDEST")
                data = r.json()
                episodes = data.get("data", {}).get("episodes", [])
                if not episodes:
                    raise ValueError(f"No readable episodes found for Tapas series {series_slug}")
                first_ep_id = str(episodes[0]["id"])
                return await self._download_tapas_episode(first_ep_id)

        raise ValueError(f"Unrecognized Tapas URL: {self.url}")

    async def _download_tapas_episode(self, episode_id: str) -> str:
        self._notify({"status": "analyzing", "percent": 10.0, "message": "Fetching Tapas episode..."})
        ep_url = f"https://tapas.io/episode/{episode_id}"
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

        async with httpx.AsyncClient(verify=False, timeout=20.0, headers=headers) as client:
            resp = await client.get(ep_url)
            html = resp.text

        title_m = re.search(r'<h1 class="viewer__header-title"[^>]*>([^<]+)</h1>', html)
        if not title_m:
            title_m = re.search(r'<title>([^<]+)</title>', html)
        ep_title = title_m.group(1).strip() if title_m else f"Tapas_{episode_id}"
        safe_title = re.sub(r'[\/*?:"<>|]', '_', ep_title).strip()

        img_urls = re.findall(r'class=\"art-image\"[^>]*data-src=\"([^\"]+)\"', html)
        if not img_urls:
            img_urls = re.findall(r'data-src=\"(https://[^\"]+)\"', html)

        # unescape html entities in URLs
        img_urls = [u.replace('&amp;', '&') for u in img_urls]
        if not img_urls:
            raise ValueError(f"No pages found for Tapas episode {episode_id}")

        pdf_path = os.path.join(self.output_dir, f"{safe_title}.pdf")
        total = len(img_urls)
        downloaded_images: List[Optional[Image.Image]] = [None] * total
        loop = asyncio.get_running_loop()

        def _fetch_tapas_img(u: str) -> Image.Image:
            with httpx.Client(verify=False, timeout=30.0, headers=headers) as cl:
                r = cl.get(u)
                r.raise_for_status()
                return Image.open(io.BytesIO(r.content)).convert("RGB")

        sem = asyncio.Semaphore(self.concurrency)
        completed = 0
        async def _tapas_worker(i: int, u: str):
            nonlocal completed
            async with sem:
                img = await loop.run_in_executor(None, lambda: _fetch_tapas_img(u))
                downloaded_images[i] = img
                completed += 1
                self._notify({
                    "status": "downloading",
                    "percent": (completed / total) * 90.0,
                    "message": f"Downloading Tapas panel {completed}/{total}"
                })

        await asyncio.gather(*[_tapas_worker(i, u) for i, u in enumerate(img_urls)])

        valid = [im for im in downloaded_images if im is not None]
        await loop.run_in_executor(None, lambda: valid[0].save(pdf_path, save_all=True, append_images=valid[1:], resolution=100.0))
        self._notify({"status": "completed", "percent": 100.0, "message": f"Saved {safe_title}.pdf"})
        return pdf_path

    # ==========================================
    # 7. NAMICOMI
    # ==========================================
    async def _download_namicomi(self) -> str:
        parsed = urlparse(self.url)
        path = parsed.path.rstrip('/')

        # Chapter URL: /chapter/{id} or /en/chapter/{id}
        ch_match = re.search(r'/chapter/([a-zA-Z0-9_\-]+)', path)
        if ch_match:
            ch_id = ch_match.group(1)
            return await self._download_namicomi_chapter(ch_id)

        # Title URL: /title/{id} or /title/{id}/{slug} or /org/{org}/title/{id}
        t_match = re.search(r'/title/([a-zA-Z0-9_\-]+)', path)
        if t_match:
            title_id = t_match.group(1)
            opt_ch = self.options.get("chapter_id")
            if opt_ch:
                return await self._download_namicomi_chapter(str(opt_ch))
            return await self._download_namicomi_title(title_id)

        raise ValueError(f"Unrecognized NamiComi URL format: {self.url}")

    async def _download_namicomi_title(self, title_id: str) -> str:
        self._notify({"status": "analyzing", "percent": 5.0, "message": "Fetching NamiComi title chapters..."})
        async with httpx.AsyncClient(verify=False, timeout=15.0) as client:
            r = await client.get(f"https://api.namicomi.com/title/{title_id}/chapters")
            if r.status_code != 200:
                raise ValueError(f"NamiComi API returned status {r.status_code}")
            data = r.json().get("data", {}).get("attributes", {})
            chapter_groups = data.get("list", [])

        chapters = []
        for g in chapter_groups:
            for ch in g.get("chapters", []):
                if ch.get("id") and ch not in chapters:
                    chapters.append(ch)

        if not chapters:
            raise ValueError(f"No chapters found for NamiComi title {title_id}")

        manga_name = f"NamiComi_{title_id}"
        try:
            async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
                tr = await client.get(f"https://api.namicomi.com/title/{title_id}")
                if tr.status_code == 200:
                    t_dict = tr.json().get("data", {}).get("attributes", {}).get("title", {})
                    manga_name = t_dict.get("en") or list(t_dict.values())[0]
        except Exception:
            pass

        safe_name = re.sub(r'[\/*?:"<>|]', '_', manga_name).strip()
        title_subfolder = os.path.join(self.output_dir, safe_name)
        os.makedirs(title_subfolder, exist_ok=True)

        total_chapters = len(chapters)
        self._notify({
            "status": "downloading",
            "percent": 10.0,
            "message": f"Found {total_chapters} chapters for '{manga_name}'"
        })

        chapter_pdf_paths = []
        for idx, ch in enumerate(chapters):
            if self.cancelled:
                raise asyncio.CancelledError()

            ch_id = ch['id']
            ch_num = ch.get('chapter', f"#{idx+1}")
            pct_start = 10.0 + (idx / total_chapters) * 80.0
            pct_end = 10.0 + ((idx + 1) / total_chapters) * 80.0

            def _ch_progress(sub_data):
                sub_pct = sub_data.get("percent", 0.0)
                overall = pct_start + (sub_pct / 100.0) * (pct_end - pct_start)
                self._notify({
                    "status": "downloading",
                    "percent": overall,
                    "message": f"Downloading chapter {idx+1}/{total_chapters}: Ch.{ch_num}"
                })

            try:
                pdf_path = await self._download_namicomi_chapter(
                    chapter_id=ch_id,
                    target_dir=title_subfolder,
                    sub_progress=_ch_progress
                )
                chapter_pdf_paths.append(pdf_path)
            except Exception as ch_err:
                print(f"Warning: Failed to download NamiComi chapter {ch_id}: {ch_err}")

        if not chapter_pdf_paths:
            raise ValueError(f"Failed to download any chapters for NamiComi title {title_id}")

        self._notify({"status": "compressing", "percent": 95.0, "message": "Packaging title archive..."})
        zip_path = os.path.join(self.output_dir, f"{safe_name}.zip")
        loop = asyncio.get_running_loop()

        def _make_zip():
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for p in chapter_pdf_paths:
                    zf.write(p, arcname=os.path.basename(p))

        await loop.run_in_executor(None, _make_zip)
        self._notify({"status": "completed", "percent": 100.0, "message": f"Saved {safe_name}.zip ({len(chapter_pdf_paths)} chapters)"})
        return zip_path

    async def _download_namicomi_chapter(
        self,
        chapter_id: str,
        target_dir: Optional[str] = None,
        sub_progress: Optional[Callable[[Dict[str, Any]], None]] = None
    ) -> str:
        dest_dir = target_dir or self.output_dir
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

        async with httpx.AsyncClient(verify=False, timeout=15.0, headers=headers) as client:
            ch_resp = await client.get(f"https://api.namicomi.com/chapter/{chapter_id}?includes[]=title")
            ch_data = ch_resp.json().get("data", {}) if ch_resp.status_code == 200 else {}
            attr = ch_data.get("attributes", {})
            ch_num = attr.get("chapter", "1")
            ch_name = attr.get("name", "")

            manga_title = "NamiComi"
            for rel in ch_data.get("relationships", []):
                if rel.get("type") == "title":
                    t_dict = rel.get("attributes", {}).get("title", {})
                    manga_title = t_dict.get("en") or (list(t_dict.values())[0] if t_dict else manga_title)
                    break

            img_resp = await client.get(f"https://api.namicomi.com/images/chapter/{chapter_id}?newQualities=true")
            if img_resp.status_code != 200:
                raise ValueError(f"NamiComi image API returned status {img_resp.status_code}")
            img_data = img_resp.json().get("data", {})
            base_url = img_data.get("baseUrl")
            ch_hash = img_data.get("hash")
            page_list = img_data.get("high") or img_data.get("source", [])

        if not page_list or not base_url or not ch_hash:
            raise ValueError(f"No page images found for NamiComi chapter {chapter_id}")

        img_urls = [f"{base_url}/chapter/{chapter_id}/{ch_hash}/high/{p['filename']}" for p in page_list]

        safe_manga = re.sub(r'[\/*?:"<>|]', '_', manga_title).strip()
        safe_ch = re.sub(r'[\/*?:"<>|]', '_', f"Ch.{ch_num}").strip()
        if ch_name:
            safe_ch += f" - {re.sub(r'[\/*?:\"<>|]', '_', ch_name)[:40].strip()}"
        pdf_name = f"{safe_manga} - {safe_ch}.pdf"
        pdf_path = os.path.join(dest_dir, pdf_name)

        total_pages = len(img_urls)
        downloaded_images: List[Optional[Image.Image]] = [None] * total_pages
        loop = asyncio.get_running_loop()

        def _fetch_img(u: str) -> Image.Image:
            with httpx.Client(verify=False, timeout=30.0, headers=headers) as cl:
                r = cl.get(u)
                r.raise_for_status()
                return Image.open(io.BytesIO(r.content)).convert("RGB")

        sem = asyncio.Semaphore(self.concurrency)
        completed = 0

        async def _worker(idx: int, u: str):
            nonlocal completed
            if self.cancelled:
                return
            async with sem:
                img = await loop.run_in_executor(None, lambda: _fetch_img(u))
                downloaded_images[idx] = img
                completed += 1
                prog = {
                    "status": "downloading",
                    "percent": (completed / total_pages) * 90.0,
                    "message": f"Downloading NamiComi page {completed}/{total_pages}"
                }
                if sub_progress:
                    sub_progress(prog)
                else:
                    self._notify(prog)

        await asyncio.gather(*[_worker(i, u) for i, u in enumerate(img_urls)])
        valid = [im for im in downloaded_images if im is not None]
        if not valid:
            raise ValueError(f"Failed to download pages for NamiComi chapter {chapter_id}")

        if sub_progress:
            sub_progress({"status": "merging", "percent": 95.0, "message": "Compiling PDF..."})
        else:
            self._notify({"status": "merging", "percent": 95.0, "message": "Compiling PDF..."})

        await loop.run_in_executor(None, lambda: valid[0].save(pdf_path, save_all=True, append_images=valid[1:], resolution=100.0))

        if not sub_progress:
            self._notify({"status": "completed", "percent": 100.0, "message": f"Saved {pdf_name} ({total_pages} pages)"})
        return pdf_path

    # ==========================================
    # 8. VOYCEME
    # ==========================================
    async def _download_voyce(self) -> str:
        parsed = urlparse(self.url)
        path = parsed.path.rstrip('/')

        # Check for chapter URL: /series/{slug}/{chapter_id}
        ch_match = re.search(r'/series/([a-zA-Z0-9_\-]+)/(\d+)', path)
        if ch_match:
            slug = ch_match.group(1)
            ch_id = int(ch_match.group(2))
            return await self._download_voyce_chapter(slug, ch_id)

        # Check for series URL: /series/{slug}
        series_match = re.search(r'/series/([a-zA-Z0-9_\-]+)', path)
        if series_match:
            slug = series_match.group(1)
            opt_ch = self.options.get("chapter_id")
            if opt_ch:
                return await self._download_voyce_chapter(slug, int(opt_ch))
            return await self._download_voyce_series(slug)

        raise ValueError(f"Unrecognized VoyceMe URL format: {self.url}")

    async def _download_voyce_series(self, slug: str) -> str:
        self._notify({"status": "analyzing", "percent": 5.0, "message": f"Fetching VoyceMe series '{slug}'..."})
        query = """
        query ChaptersBySeriesSlug {
          voyce_chapters(where: {publish: {_eq: 1}, is_deleted: {_eq: false}, series: {slug: {_eq: "%s"}}}, order_by: {id: asc}) {
            id
            title
            order
          }
        }
        """ % slug

        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
        async with httpx.AsyncClient(verify=False, timeout=15.0, headers=headers) as client:
            r = await client.post("https://graphql.voyce.me/v1/graphql/", json={"query": query})
            if r.status_code != 200:
                raise ValueError(f"VoyceMe GraphQL API returned status {r.status_code}")
            chapters = r.json().get("data", {}).get("voyce_chapters", [])

        if not chapters:
            raise ValueError(f"No readable chapters found for VoyceMe series '{slug}'")

        safe_slug = re.sub(r'[\/*?:"<>|]', '_', slug).title()
        series_subfolder = os.path.join(self.output_dir, safe_slug)
        os.makedirs(series_subfolder, exist_ok=True)

        total_chapters = len(chapters)
        self._notify({
            "status": "downloading",
            "percent": 10.0,
            "message": f"Found {total_chapters} episodes for VoyceMe '{safe_slug}'"
        })

        chapter_pdf_paths = []
        for idx, ch in enumerate(chapters):
            if self.cancelled:
                raise asyncio.CancelledError()

            ch_id = ch['id']
            ch_title = ch.get('title', f"Episode {idx+1}")
            pct_start = 10.0 + (idx / total_chapters) * 80.0
            pct_end = 10.0 + ((idx + 1) / total_chapters) * 80.0

            def _ch_progress(sub_data):
                sub_pct = sub_data.get("percent", 0.0)
                overall = pct_start + (sub_pct / 100.0) * (pct_end - pct_start)
                self._notify({
                    "status": "downloading",
                    "percent": overall,
                    "message": f"Downloading episode {idx+1}/{total_chapters}: {ch_title}"
                })

            try:
                pdf_path = await self._download_voyce_chapter(
                    slug=slug,
                    chapter_id=ch_id,
                    target_dir=series_subfolder,
                    sub_progress=_ch_progress
                )
                chapter_pdf_paths.append(pdf_path)
            except Exception as ch_err:
                print(f"Warning: Failed to download VoyceMe episode {ch_id}: {ch_err}")

        if not chapter_pdf_paths:
            raise ValueError(f"Failed to download any episodes for VoyceMe series '{slug}'")

        self._notify({"status": "compressing", "percent": 95.0, "message": "Packaging series archive..."})
        zip_path = os.path.join(self.output_dir, f"{safe_slug}.zip")
        loop = asyncio.get_running_loop()

        def _make_zip():
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for p in chapter_pdf_paths:
                    zf.write(p, arcname=os.path.basename(p))

        await loop.run_in_executor(None, _make_zip)
        self._notify({"status": "completed", "percent": 100.0, "message": f"Saved {safe_slug}.zip ({len(chapter_pdf_paths)} episodes)"})
        return zip_path

    async def _download_voyce_chapter(
        self,
        slug: str,
        chapter_id: int,
        target_dir: Optional[str] = None,
        sub_progress: Optional[Callable[[Dict[str, Any]], None]] = None
    ) -> str:
        dest_dir = target_dir or self.output_dir
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

        query = """
        query ChapterImagesById {
          voyce_chapter_images(where: {chapter: {id: {_eq: %d}}}, order_by: [{sort_order: asc}, {id: asc}]) {
            id
            image
            chapter {
              title
              order
            }
          }
        }
        """ % chapter_id

        async with httpx.AsyncClient(verify=False, timeout=15.0, headers=headers) as client:
            r = await client.post("https://graphql.voyce.me/v1/graphql/", json={"query": query})
            if r.status_code != 200:
                raise ValueError(f"VoyceMe GraphQL API returned status {r.status_code}")
            panel_items = r.json().get("data", {}).get("voyce_chapter_images", [])

        if not panel_items:
            raise ValueError(f"No panels found for VoyceMe chapter {chapter_id}")

        ch_meta = panel_items[0].get("chapter") or {}
        ch_title = ch_meta.get("title") or f"Chapter_{chapter_id}"
        safe_series = re.sub(r'[\/*?:"<>|]', '_', slug).title()
        safe_ch = re.sub(r'[\/*?:"<>|]', '_', ch_title)[:50].strip()
        pdf_name = f"{safe_series} - {safe_ch}.pdf"
        pdf_path = os.path.join(dest_dir, pdf_name)

        total_panels = len(panel_items)
        downloaded_images: List[Optional[Image.Image]] = [None] * total_panels
        loop = asyncio.get_running_loop()

        def _fetch_panel(img_path: str) -> Image.Image:
            url = f"https://dlkfxmdtxtzpb.cloudfront.net/{img_path}"
            with httpx.Client(verify=False, timeout=30.0, headers=headers) as cl:
                res = cl.get(url)
                res.raise_for_status()
                return Image.open(io.BytesIO(res.content)).convert("RGB")

        sem = asyncio.Semaphore(self.concurrency)
        completed = 0

        async def _worker(idx: int, p_item: Dict[str, Any]):
            nonlocal completed
            if self.cancelled:
                return
            async with sem:
                img_path = p_item["image"]
                img = await loop.run_in_executor(None, lambda: _fetch_panel(img_path))
                downloaded_images[idx] = img
                completed += 1
                prog = {
                    "status": "downloading",
                    "percent": (completed / total_panels) * 90.0,
                    "message": f"Downloading VoyceMe panel {completed}/{total_panels}"
                }
                if sub_progress:
                    sub_progress(prog)
                else:
                    self._notify(prog)

        await asyncio.gather(*[_worker(i, p) for i, p in enumerate(panel_items)])
        valid = [im for im in downloaded_images if im is not None]
        if not valid:
            raise ValueError(f"Failed to download panels for VoyceMe chapter {chapter_id}")

        if sub_progress:
            sub_progress({"status": "merging", "percent": 95.0, "message": "Compiling PDF..."})
        else:
            self._notify({"status": "merging", "percent": 95.0, "message": "Compiling PDF..."})

        await loop.run_in_executor(None, lambda: valid[0].save(pdf_path, save_all=True, append_images=valid[1:], resolution=100.0))

        if not sub_progress:
            self._notify({"status": "completed", "percent": 100.0, "message": f"Saved {pdf_name} ({total_panels} panels)"})
        return pdf_path

    # ==========================================
    # 9. GENERIC MANGA / COMIC FALLBACK
    # ==========================================
    async def _download_generic_manga(self) -> str:
        self._notify({"status": "analyzing", "percent": 10.0, "message": "Analyzing webcomic page..."})
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
        async with httpx.AsyncClient(verify=False, timeout=20.0, headers=headers) as client:
            resp = await client.get(self.url)
            html = resp.text

        title_match = re.search(r'<title>([^<]+)</title>', html)
        doc_title = title_match.group(1).strip() if title_match else "Comic_Document"
        safe_title = re.sub(r'[\/*?:"<>|]', '_', doc_title)[:80].strip()

        # Find high-res image candidates
        img_urls = re.findall(r'(?:src|data-src|data-url)=\"([^\"]+\.(?:webp|jpg|jpeg|png)(?:\?[^\"]*)?)\"', html, re.IGNORECASE)
        # Filter out common icons, avatars, banners
        filtered_imgs = [
            u for u in img_urls
            if not any(b in u.lower() for b in ['logo', 'icon', 'avatar', 'banner', 'button', 'badge', 'tracking', 'pixel', 'advert'])
            and (u.startswith('http://') or u.startswith('https://'))
        ]

        if not filtered_imgs:
            raise ValueError(f"Could not extract comic images from {self.url}")

        pdf_path = os.path.join(self.output_dir, f"{safe_title}.pdf")
        total = len(filtered_imgs)
        downloaded_images: List[Optional[Image.Image]] = [None] * total
        loop = asyncio.get_running_loop()

        def _fetch_img(u: str) -> Image.Image:
            with httpx.Client(verify=False, timeout=30.0, headers=headers) as cl:
                r = cl.get(u)
                r.raise_for_status()
                return Image.open(io.BytesIO(r.content)).convert("RGB")

        sem = asyncio.Semaphore(self.concurrency)
        completed = 0
        async def _worker(i: int, u: str):
            nonlocal completed
            async with sem:
                try:
                    img = await loop.run_in_executor(None, lambda: _fetch_img(u))
                    downloaded_images[i] = img
                except Exception:
                    pass
                completed += 1
                self._notify({
                    "status": "downloading",
                    "percent": (completed / total) * 90.0,
                    "message": f"Downloading panel {completed}/{total}"
                })

        await asyncio.gather(*[_worker(i, u) for i, u in enumerate(filtered_imgs)])
        valid = [im for im in downloaded_images if im is not None]
        if not valid:
            raise ValueError("No comic images could be downloaded")

        await loop.run_in_executor(None, lambda: valid[0].save(pdf_path, save_all=True, append_images=valid[1:], resolution=100.0))
        self._notify({"status": "completed", "percent": 100.0, "message": f"Saved {safe_title}.pdf"})
        return pdf_path

    # ==========================================
    # 9. COMIX.TO
    # ==========================================
    # comix.to renders chapters through an encrypted API (/api/v1/chapters/{id})
    # that only the site's own obfuscated JS can decrypt. A stealth headless
    # browser is used to run that JS itself and a JSON.parse hook captures the
    # decrypted payload as soon as the page decodes it. The page-image CDN
    # (e.g. wowpic2.store) needs no token, only a comix.to Referer header.
    _COMIX_STEALTH_INIT_SCRIPT = """
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        window.chrome = { runtime: {} };
    """

    @staticmethod
    def _new_comix_stealth_page(context_manager):
        browser = context_manager.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800}
        )
        return browser, context

    def _extract_comix_chapter_payload(self, chapter_url: str) -> Optional[Dict[str, Any]]:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser, context = self._new_comix_stealth_page(p)
            try:
                context.add_init_script(self._COMIX_STEALTH_INIT_SCRIPT + """
                    window.__chapterPayload = null;
                    const origParse = JSON.parse;
                    JSON.parse = function(str, ...rest) {
                        const res = origParse.call(this, str, ...rest);
                        if (res && res.result && res.result.pages) {
                            window.__chapterPayload = res.result;
                        }
                        return res;
                    };
                """)
                page = context.new_page()
                page.goto(chapter_url, timeout=30000)
                payload = None
                for _ in range(30):
                    payload = page.evaluate("() => window.__chapterPayload")
                    if payload:
                        break
                    time.sleep(0.5)
                return payload
            finally:
                browser.close()

    # comix.to's chapter list only renders 20 items per page (a title like One
    # Piece has 1000+ chapters across ~237 pages, each listing 3-4 scanlation
    # groups per chapter number). We page through the site's own "Next page"
    # control so its JS mints a fresh anti-scraping token each time, dedupe by
    # chapter number, and stop at a safety cap so one click can't trigger an
    # hours-long, multi-gigabyte download of an entire long-running series.
    _COMIX_MAX_CHAPTERS_DEFAULT = 300
    _COMIX_MAX_LIST_PAGES = 60
    _COMIX_MAX_LIST_SECONDS = 240

    def _extract_comix_chapters_list(self, title_url: str) -> List[Dict[str, Any]]:
        from playwright.sync_api import sync_playwright
        max_chapters = self.options.get("max_chapters", self._COMIX_MAX_CHAPTERS_DEFAULT)
        deadline = time.time() + self._COMIX_MAX_LIST_SECONDS

        with sync_playwright() as p:
            browser, context = self._new_comix_stealth_page(p)
            try:
                context.add_init_script(self._COMIX_STEALTH_INIT_SCRIPT + """
                    window.__chaptersList = [];
                    const origParse = JSON.parse;
                    JSON.parse = function(str, ...rest) {
                        const res = origParse.call(this, str, ...rest);
                        if (res && res.result && res.result.items && Array.isArray(res.result.items)) {
                            if (res.result.items.length > 0 && res.result.items[0].number !== undefined) {
                                window.__chaptersList = res.result.items;
                            }
                        }
                        return res;
                    };
                """)
                page = context.new_page()
                page.goto(title_url, timeout=30000)

                seen_numbers = set()
                chapters: List[Dict[str, Any]] = []

                for page_idx in range(self._COMIX_MAX_LIST_PAGES):
                    batch: List[Dict[str, Any]] = []
                    for _ in range(30):
                        batch = page.evaluate("() => window.__chaptersList")
                        if batch:
                            break
                        time.sleep(0.5)
                    for item in batch or []:
                        number = item.get("number")
                        if number is None or number in seen_numbers:
                            continue
                        seen_numbers.add(number)
                        chapters.append(item)

                    if len(chapters) >= max_chapters or time.time() > deadline:
                        break

                    page.evaluate("() => { window.__chaptersList = []; }")
                    next_btn = page.query_selector('button.npager__nav[aria-label="Next page"]')
                    if not next_btn or "is-disabled" in (next_btn.get_attribute("class") or ""):
                        break
                    try:
                        next_btn.click(force=True, timeout=5000)
                    except Exception:
                        break
                    time.sleep(1.2)

                return chapters[:max_chapters]
            finally:
                browser.close()

    async def _download_comix(self) -> str:
        parsed = urlparse(self.url)
        path = parsed.path.rstrip('/')
        if re.search(r'/title/[^/]+/\d+-chapter-[0-9.]+', path):
            return await self._download_comix_chapter(self.url)
        elif '/title/' in path:
            return await self._download_comix_title(self.url)
        else:
            raise ValueError(f"Unrecognized Comix URL format: {self.url}")

    async def _download_comix_chapter(
        self,
        chapter_url: str,
        target_dir: Optional[str] = None,
        sub_progress: Optional[Callable[[Dict[str, Any]], None]] = None
    ) -> str:
        notify = sub_progress or self._notify
        dest_dir = target_dir or self.output_dir
        os.makedirs(dest_dir, exist_ok=True)

        notify({"status": "analyzing", "percent": 5.0, "message": "Launching stealth browser for Comix..."})
        loop = asyncio.get_running_loop()
        payload = await loop.run_in_executor(None, self._extract_comix_chapter_payload, chapter_url)
        if not payload:
            raise ValueError("Could not extract chapter data from Comix. The site's protection may have changed.")

        pages_data = payload.get("pages") or {}
        base_url = pages_data.get("baseUrl", "")
        items = pages_data.get("items") or []
        if not items:
            raise ValueError("No pages found for this Comix chapter.")

        title_slug_match = re.search(r'/title/([^/]+)', urlparse(chapter_url).path)
        title_part = re.sub(r'^[a-z0-9]+-', '', title_slug_match.group(1)).replace('-', ' ').title() \
            if title_slug_match else "Comix"
        ch_number = payload.get("number", "0")
        ch_name = payload.get("name") or ""
        label = f"{title_part} - Chapter {ch_number}"
        if ch_name:
            label += f" - {ch_name}"
        safe_name = re.sub(r'[\/*?:"<>|]', '_', label).strip()

        total = len(items)
        urls = [f"{base_url}{it.get('url')}" for it in items]
        downloaded: List[Optional[bytes]] = [None] * total
        sem = asyncio.Semaphore(self.concurrency)
        completed = 0

        async def _fetch_page(idx: int, page_url: str):
            nonlocal completed
            if self.cancelled:
                return
            async with sem:
                async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
                    r = await client.get(page_url, headers={"Referer": "https://comix.to/"})
                    r.raise_for_status()
                    downloaded[idx] = r.content
            completed += 1
            notify({
                "status": "downloading",
                "percent": (completed / total) * 90.0,
                "message": f"Downloading page {completed}/{total}"
            })

        await asyncio.gather(*[_fetch_page(i, u) for i, u in enumerate(urls)])

        if self.cancelled:
            raise RuntimeError("Download cancelled")
        if any(d is None for d in downloaded):
            raise ValueError("Some Comix pages failed to download.")

        cbz_path = os.path.join(dest_dir, f"{safe_name}.cbz")

        def _make_cbz():
            with zipfile.ZipFile(cbz_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for i, data in enumerate(downloaded):
                    zf.writestr(f"page_{i + 1:03d}.webp", data)

        await loop.run_in_executor(None, _make_cbz)
        notify({"status": "completed", "percent": 100.0, "message": f"Saved {safe_name}.cbz ({total} pages)"})
        return cbz_path

    async def _download_comix_title(self, title_url: str) -> str:
        self._notify({"status": "analyzing", "percent": 5.0, "message": "Fetching Comix title chapter list..."})
        loop = asyncio.get_running_loop()
        chapters = await loop.run_in_executor(None, self._extract_comix_chapters_list, title_url)
        if not chapters:
            raise ValueError("Could not find any chapters for this Comix title.")

        title_slug_match = re.search(r'/title/([^/]+)', urlparse(title_url).path)
        safe_title = re.sub(r'^[a-z0-9]+-', '', title_slug_match.group(1)).replace('-', ' ').title() \
            if title_slug_match else "Comix_Title"
        safe_title = re.sub(r'[\/*?:"<>|]', '_', safe_title).strip()
        title_subfolder = os.path.join(self.output_dir, safe_title)
        os.makedirs(title_subfolder, exist_ok=True)

        total_chapters = len(chapters)
        chapter_paths = []
        for idx, ch in enumerate(chapters):
            if self.cancelled:
                break
            ch_url = ch.get("url")
            if not ch_url:
                continue
            if not ch_url.startswith("http"):
                ch_url = f"https://comix.to{ch_url}" if ch_url.startswith('/') else f"https://comix.to/{ch_url}"

            pct_start = 5.0 + (idx / total_chapters) * 85.0
            pct_end = 5.0 + ((idx + 1) / total_chapters) * 85.0

            def _ch_progress(sub_data, pct_start=pct_start, pct_end=pct_end, idx=idx):
                sub_pct = sub_data.get("percent", 0.0)
                overall = pct_start + (sub_pct / 100.0) * (pct_end - pct_start)
                self._notify({
                    "status": "downloading",
                    "percent": overall,
                    "message": f"Downloading chapter {idx + 1}/{total_chapters}"
                })

            try:
                cbz_path = await self._download_comix_chapter(ch_url, target_dir=title_subfolder, sub_progress=_ch_progress)
                chapter_paths.append(cbz_path)
            except Exception as ch_err:
                print(f"Warning: Failed to download Comix chapter {ch.get('number')}: {ch_err}")

        if not chapter_paths:
            raise ValueError(f"Failed to download any chapters for Comix title {safe_title}")

        self._notify({"status": "compressing", "percent": 95.0, "message": "Packaging title archive..."})
        zip_path = os.path.join(self.output_dir, f"{safe_title}.zip")

        def _make_zip():
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for p in chapter_paths:
                    zf.write(p, arcname=os.path.basename(p))

        await loop.run_in_executor(None, _make_zip)
        self._notify({"status": "completed", "percent": 100.0, "message": f"Saved {safe_title}.zip ({len(chapter_paths)} chapters)"})
        return zip_path

    @staticmethod
    def inspect(url: str) -> Dict[str, Any]:
        """
        Inspects manga URL and returns metadata (title, chapters, thumbnail).
        """
        parsed = urlparse(url)
        netloc = parsed.netloc.lower()

        if "mangaplus.shueisha.co.jp" in netloc:
            session_token = str(uuid.uuid4())
            headers = {
                "User-Agent": "Mozilla/5.0",
                "SESSION-TOKEN": session_token,
                "Origin": "https://mangaplus.shueisha.co.jp",
                "Referer": "https://mangaplus.shueisha.co.jp/",
            }
            if "/titles/" in parsed.path:
                t_match = re.search(r'/titles/(\d+)', parsed.path)
                if t_match:
                    t_id = t_match.group(1)
                    api_url = f"https://jumpg-webapi.tokyo-cdn.com/api/title_detailV3?title_id={t_id}"
                    with httpx.Client(verify=False, timeout=10.0) as client:
                        resp = client.get(api_url, headers=headers)
                        if resp.status_code == 200:
                            title_name = "Manga"
                            chapters = []
                            for n1, w1, v1 in decode_protobuf(resp.content):
                                if n1 == 1:
                                    for n2, w2, v2 in decode_protobuf(v1):
                                        if n2 == 8:
                                            for n3, w3, v3 in decode_protobuf(v2):
                                                if n3 == 1:
                                                    for n_t, w_t, v_t in decode_protobuf(v3):
                                                        if n_t == 2 and w_t == 2:
                                                            title_name = v_t.decode('utf-8', errors='ignore').strip()
                                                elif n3 == 28:
                                                    for n4, w4, v4 in decode_protobuf(v3):
                                                        if w4 == 2 and n4 in (2, 4):  # First and Latest free readable chapters
                                                            ch = {}
                                                            for n5, w5, v5 in decode_protobuf(v4):
                                                                if w5 == 0 and n5 == 2:
                                                                    ch['chapter_id'] = v5
                                                                elif w5 == 2 and n5 == 3:
                                                                    ch['number'] = v5.decode('utf-8', errors='ignore').strip()
                                                                elif w5 == 2 and n5 == 4:
                                                                    ch['title'] = v5.decode('utf-8', errors='ignore').strip()
                                                            if 'chapter_id' in ch and ch not in chapters:
                                                                chapters.append(ch)
                            return {
                                "title": title_name,
                                "type": "manga_title",
                                "chapters": chapters,
                                "total_chapters": len(chapters)
                            }
            elif "/viewer/" in parsed.path:
                v_match = re.search(r'/viewer/(\d+)', parsed.path)
                if v_match:
                    v_id = v_match.group(1)
                    api_url = f"https://jumpg-webapi.tokyo-cdn.com/api/manga_viewer_v3?chapter_id={v_id}&split=yes&img_quality=high"
                    with httpx.Client(verify=False, timeout=10.0) as client:
                        resp = client.get(api_url, headers=headers)
                        if resp.status_code == 200:
                            title_name = "Manga"
                            chapter_name = f"Chapter {v_id}"
                            for n1, w1, v1 in decode_protobuf(resp.content):
                                if n1 == 1:
                                    for n2, w2, v2 in decode_protobuf(v1):
                                        if n2 == 10:
                                            for n3, w3, v3 in decode_protobuf(v2):
                                                if n3 == 5 and w3 == 2:
                                                    title_name = v3.decode('utf-8', errors='ignore').strip()
                                                elif n3 == 6 and w3 == 2:
                                                    chapter_name = v3.decode('utf-8', errors='ignore').strip()
                            return {
                                "title": f"{title_name} - {chapter_name}",
                                "type": "manga_chapter"
                            }

        elif "namicomi.com" in netloc:
            ch_match = re.search(r'/chapter/([a-zA-Z0-9_\-]+)', parsed.path)
            if ch_match:
                ch_id = ch_match.group(1)
                try:
                    with httpx.Client(verify=False, timeout=10.0) as client:
                        r = client.get(f"https://api.namicomi.com/chapter/{ch_id}?includes[]=title")
                        if r.status_code == 200:
                            d = r.json().get("data", {})
                            attr = d.get("attributes", {})
                            ch_name = attr.get("name", "")
                            ch_num = attr.get("chapter", "1")
                            title_name = "NamiComi"
                            for rel in d.get("relationships", []):
                                if rel.get("type") == "title":
                                    t_dict = rel.get("attributes", {}).get("title", {})
                                    title_name = t_dict.get("en") or (list(t_dict.values())[0] if t_dict else title_name)
                            label = f"{title_name} - Ch.{ch_num}"
                            if ch_name:
                                label += f": {ch_name}"
                            return {"title": label, "type": "manga_chapter"}
                except Exception:
                    pass

            t_match = re.search(r'/title/([a-zA-Z0-9_\-]+)', parsed.path)
            if t_match:
                t_id = t_match.group(1)
                try:
                    with httpx.Client(verify=False, timeout=10.0) as client:
                        r = client.get(f"https://api.namicomi.com/title/{t_id}/chapters")
                        if r.status_code == 200:
                            data = r.json().get("data", {}).get("attributes", {})
                            chapter_groups = data.get("list", [])
                            chapters = []
                            for g in chapter_groups:
                                for ch in g.get("chapters", []):
                                    chapters.append({
                                        "chapter_id": ch.get("id"),
                                        "number": ch.get("chapter"),
                                        "title": ch.get("name", "")
                                    })
                            return {
                                "title": f"NamiComi Title {t_id}",
                                "type": "manga_title",
                                "chapters": chapters,
                                "total_chapters": len(chapters)
                            }
                except Exception:
                    pass

        elif "voyce.me" in netloc:
            ch_match = re.search(r'/series/([a-zA-Z0-9_\-]+)/(\d+)', parsed.path)
            if ch_match:
                slug, ch_id = ch_match.group(1), int(ch_match.group(2))
                return {"title": f"VoyceMe: {slug.title()} - Episode {ch_id}", "type": "manga_chapter"}

            series_match = re.search(r'/series/([a-zA-Z0-9_\-]+)', parsed.path)
            if series_match:
                slug = series_match.group(1)
                query = """
                query ChaptersBySeriesSlug {
                  voyce_chapters(where: {publish: {_eq: 1}, is_deleted: {_eq: false}, series: {slug: {_eq: "%s"}}}, order_by: {id: asc}) {
                    id
                    title
                    order
                  }
                }
                """ % slug
                try:
                    with httpx.Client(verify=False, timeout=10.0) as client:
                        r = client.post("https://graphql.voyce.me/v1/graphql/", json={"query": query})
                        if r.status_code == 200:
                            ch_list = r.json().get("data", {}).get("voyce_chapters", [])
                            chapters = [{"chapter_id": c["id"], "number": f"#{c.get('order', i)+1}", "title": c.get("title", "")} for i, c in enumerate(ch_list)]
                            return {
                                "title": f"VoyceMe: {slug.title()}",
                                "type": "manga_title",
                                "chapters": chapters,
                                "total_chapters": len(chapters)
                            }
                except Exception:
                    pass

        elif "comix.to" in netloc:
            ch_match = re.search(r'/title/([^/]+)/(\d+)-chapter-([0-9.]+)', parsed.path)
            if ch_match:
                manga_slug, _, ch_num = ch_match.groups()
                clean_title = re.sub(r'^[a-z0-9]+-', '', manga_slug).replace('-', ' ').title()
                return {"title": f"Comix: {clean_title} - Chapter {ch_num}", "type": "manga_chapter"}
            t_match = re.search(r'/title/([^/]+)', parsed.path)
            if t_match:
                clean_title = re.sub(r'^[a-z0-9]+-', '', t_match.group(1)).replace('-', ' ').title()
                return {"title": f"Comix: {clean_title}", "type": "manga_title"}

        return {"title": "Manga Publication", "type": "manga"}
