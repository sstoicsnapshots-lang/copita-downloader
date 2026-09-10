"""
Universal File-Hoster & Cloud Storage Downloader for Copita.
Resolves and accelerates downloads from MediaFire, Mega.nz, Gofile, PixelDrain, and Ufile.
Bypasses landing pages, ad walls, and prevents saving HTML pages as '.bin' files.
"""

import os
import re
import ssl
import json
import time
import struct
import base64
import certifi
import asyncio
import hashlib
import subprocess
import aiohttp
import urllib.request
import urllib.parse
from typing import Dict, Any, Optional, Callable
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from copita.core.chunk_downloader import ChunkDownloader
from copita.core.speed_gate import GLOBAL as _speed_gate


USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

class FileHosterDownloader:
    """Detects, resolves, and downloads files from major file hosting providers."""

    def __init__(
        self,
        url: str,
        output_dir: str = "downloads",
        num_connections: int = 8,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None
    ):
        self.url = url.strip()
        self.output_dir = output_dir
        self.num_connections = min(num_connections or 8, 8)
        self.progress_callback = progress_callback
        os.makedirs(self.output_dir, exist_ok=True)
        self._ssl_ctx = ssl.create_default_context(cafile=certifi.where())

    def _report(self, status: str, percent: float, speed: Optional[float] = None, msg: str = "", downloaded: int = 0, total: int = 0):
        if self.progress_callback:
            self.progress_callback({
                "status": status,
                "percent": percent,
                "speed": speed,
                "message": msg,
                "downloaded": downloaded,
                "total": total
            })

    async def resolve(self) -> Dict[str, Any]:
        """Resolves direct download URL, filename, and engine type."""
        netloc = urllib.parse.urlparse(self.url).netloc.lower()

        if "mediafire.com" in netloc:
            return await self._resolve_mediafire()
        elif "gofile.io" in netloc:
            return await self._resolve_gofile()
        elif "pixeldrain.com" in netloc:
            return await self._resolve_pixeldrain()
        elif "mega.nz" in netloc or "mega.co.nz" in netloc:
            return await self._resolve_mega()
        elif "ufile.io" in netloc or "uploadfiles.io" in netloc:
            return await self._resolve_ufile()
        elif "krakenfiles.com" in netloc:
            return await self._resolve_krakenfiles()
        elif "buzzheavier.com" in netloc or "bzzhr.to" in netloc:
            return await self._resolve_buzzheavier()
        elif "workupload.com" in netloc:
            return await self._resolve_workupload()
        elif "catbox.moe" in netloc:
            return await self._resolve_catbox()
        elif "send.cm" in netloc:
            return await self._resolve_sendcm()
        elif "1fichier.com" in netloc:
            return await self._resolve_fichier()
        elif "dropbox.com" in netloc:
            return await self._resolve_dropbox()
        elif any(d in netloc for d in ("drive.google.com", "docs.google.com", "drive.usercontent.google.com")):
            return await self._resolve_gdrive()
        else:
            raise ValueError(f"Unsupported file hoster: {netloc}")

    async def download(self) -> str:
        """Resolves the direct stream and downloads the file with progress tracking."""
        self._report("extracting", 10.0, msg="Resolving direct download link from file hoster...")
        res = await self.resolve()

        provider = res.get("provider")
        if provider == "mega":
            return await self._download_mega(res)
        if provider == "mega_folder":
            return await self._download_mega_folder(res)

        if res.get("is_folder") and res.get("children"):
            return await self._download_folder(res)

        direct_url = res["direct_url"]
        filename = res.get("filename") or "download.zip"
        final_path = os.path.join(self.output_dir, filename)

        self._report("downloading", 20.0, msg=f"Starting multi-threaded download: {filename}...")

        def _chunk_cb(data: Dict[str, Any]):
            if self.progress_callback:
                self.progress_callback(data)

        dl = ChunkDownloader(
            url=direct_url,
            output_path=final_path,
            num_connections=self.num_connections,
            headers=res.get("headers", {}),
            progress_callback=_chunk_cb
        )
        return await dl.start()

    # A folder this deep or this large is far more likely to be a runaway/
    # malformed listing than a real download target, so crawling stops there
    # rather than hammering Drive indefinitely or filling the disk unbounded.
    _GDRIVE_MAX_FILES = 500
    _GDRIVE_MAX_DEPTH = 8

    async def _crawl_gdrive_folder(self, top_children: list) -> tuple:
        """Recursively walks every subfolder (previously only the top level's
        files were downloaded — subfolders were silently discarded) and
        returns a flat list of {url, name, category, rel_dir} download jobs
        plus how many subfolders were skipped for exceeding the depth cap."""
        jobs = []
        skipped_subfolders = 0

        async def _crawl(items, rel_dir, depth):
            nonlocal skipped_subfolders
            for item in items:
                if len(jobs) >= self._GDRIVE_MAX_FILES:
                    return
                if item.get("is_folder"):
                    if depth >= self._GDRIVE_MAX_DEPTH:
                        skipped_subfolders += 1
                        continue
                    sub_name = re.sub(r'[\\/*?:"<>|]', '_', item.get("name") or item.get("id") or "folder")
                    _, sub_items = await self._list_gdrive_folder_items(item["id"])
                    await _crawl(sub_items, rel_dir + [sub_name], depth + 1)
                else:
                    jobs.append({
                        "url": item["url"],
                        "name": item.get("name") or f"file_{item.get('id')}.bin",
                        "rel_dir": rel_dir,
                        "size": item.get("size"),
                    })

        await _crawl(top_children, [], 0)
        return jobs, skipped_subfolders

    async def _download_folder(self, res: Dict[str, Any]) -> str:
        folder_name = res.get("folder_name") or f"folder_{res.get('folder_id', 'download')}"
        target_dir = os.path.join(self.output_dir, folder_name)
        os.makedirs(target_dir, exist_ok=True)

        self._report("analyzing", 5.0, msg=f"Scanning folder '{folder_name}' (including subfolders)...")
        jobs, skipped_subfolders = await self._crawl_gdrive_folder(res.get("children", []))
        total_files = len(jobs)
        if total_files == 0:
            return target_dir

        # Drive's folder listing already carries each file's size (no extra
        # request needed), so the total is known immediately — show it now
        # rather than waiting for every file to individually report its own
        # size mid-download (which with a small download semaphore and many
        # files could take most of the transfer to finally add up).
        known_total = sum(job.get("size") or 0 for job in jobs)
        self._report("downloading", 10.0, total=known_total,
                     msg=f"Downloading {total_files} files from folder '{folder_name}'...")
        # Each job is itself a multi-connection ChunkDownloader, so 4 files at
        # once meant up to 16 concurrent sockets plus a progress broadcast
        # every ~0.5s from each — enough to make the UI (Cancel/Retry clicks)
        # feel sluggish on a large folder. 2 keeps good throughput without that.
        sem = asyncio.Semaphore(2)

        progress = [{'percent': 0, 'speed': 0, 'downloaded': 0, 'total': job.get('size') or 0} for job in jobs]
        failures = []
        used_paths = set()

        def _report_children():
            pct = 10 + sum(item['percent'] for item in progress) / total_files * .85
            total = sum(item['total'] for item in progress)
            self._report('downloading', pct,
                         speed=sum(item['speed'] for item in progress),
                         downloaded=sum(item['downloaded'] for item in progress), total=total)

        def _unique_path(rel_dir, fname):
            cdir = os.path.join(target_dir, *rel_dir)
            os.makedirs(cdir, exist_ok=True)
            base, ext = os.path.splitext(fname)
            candidate = os.path.join(cdir, fname)
            n = 2
            while candidate in used_paths:
                candidate = os.path.join(cdir, f"{base} ({n}){ext}")
                n += 1
            used_paths.add(candidate)
            return candidate

        async def _download_child(job: Dict[str, Any], idx: int):
            async with sem:
                cpath = _unique_path(job["rel_dir"], job["name"])
                cfname = os.path.basename(cpath)
                # Retrying a partially-completed folder shouldn't re-fetch
                # files that already finished successfully.
                expected_size = job.get("size")
                if expected_size and os.path.isfile(cpath) and os.path.getsize(cpath) == expected_size:
                    progress[idx].update(percent=100, downloaded=expected_size, total=expected_size, speed=0)
                    _report_children()
                    return
                def _child_progress(data):
                    item = progress[idx]
                    for key in ('percent', 'speed', 'downloaded', 'total'):
                        if data.get(key) is not None:
                            item[key] = data[key]
                    if data.get('status') == 'completed':
                        item['percent'], item['speed'] = 100, 0
                    _report_children()
                cdl = ChunkDownloader(
                    url=job["url"], output_path=cpath, num_connections=4,
                    headers=res.get("headers", {}), progress_callback=_child_progress
                )
                try:
                    await cdl.start()
                    size = os.path.getsize(cpath)
                    progress[idx].update(percent=100, downloaded=size, total=size, speed=0)
                except Exception as error:
                    failures.append((cfname, str(error)))
                    progress[idx]['speed'] = 0
                _report_children()

        await asyncio.gather(*[_download_child(job, i) for i, job in enumerate(jobs)])
        if failures:
            raise ValueError(f'{len(failures)} of {total_files} files could not be downloaded. Completed files are kept in {folder_name}. First error: {failures[0][1]}')
        size = sum(item['downloaded'] for item in progress)
        note = f" ({skipped_subfolders} deeply nested subfolders skipped)" if skipped_subfolders else ""
        self._report('finished', 100, speed=0, downloaded=size, total=size,
                     msg=f'Completed downloading {total_files} files to {folder_name}{note}.')
        return target_dir

    # -------------------------------------------------------------------------
    # 1. MediaFire Resolver
    # -------------------------------------------------------------------------
    async def _resolve_mediafire(self) -> Dict[str, Any]:
        headers = {"User-Agent": USER_AGENT}
        connector = aiohttp.TCPConnector(ssl=self._ssl_ctx)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(self.url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                html = await resp.text(errors="ignore")

        # Extract direct link from downloadButton
        m = re.search(r'aria-label=[\"\']Download file[\"\']\s+href=[\"\']([^\"\']+)[\"\']', html)
        if not m:
            m = re.search(r'id=[\"\']downloadButton[\"\'][^>]+href=[\"\']([^\"\']+)[\"\']', html)
        if not m:
            m = re.search(r'href=[\"\']((?:https?:)?//download\d+\.mediafire\.com/[^\"\']+)[\"\']', html)

        if not m:
            raise RuntimeError("Could not locate direct download link on MediaFire page.")

        direct_url = m.group(1)
        if direct_url.startswith("//"):
            direct_url = "https:" + direct_url

        # Extract clean filename
        fn_match = re.search(r'/([a-zA-Z0-9_\-\.\%]+\.[a-zA-Z0-9]{2,5})(?:\?|$)', direct_url)
        filename = urllib.parse.unquote(fn_match.group(1)) if fn_match else "mediafire_download.zip"

        return {
            "provider": "mediafire",
            "direct_url": direct_url,
            "filename": filename,
            "headers": {"User-Agent": USER_AGENT, "Referer": self.url}
        }

    @staticmethod
    def _get_gofile_wt(account_token: str, ua: str, lang: str = "en-US") -> str:
        """Derives Gofile's required X-Website-Token via wt.obf.js or SHA256 formula."""
        # Try dynamic evaluation via Node.js first
        try:
            node_script = f'''
            const ua = {json.dumps(ua)};
            const lang = {json.dumps(lang)};
            globalThis.navigator = {{ userAgent: ua, language: lang }};
            fetch('https://gofile.io/js/wt.obf.js')
              .then(r => r.text())
              .then(code => {{
                globalThis.window = globalThis;
                eval(code + '\\n; globalThis.generateWT = generateWT;');
                console.log(globalThis.generateWT({json.dumps(account_token)}));
              }}).catch(e => {{
                process.exit(1);
              }});
            '''
            res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True, timeout=3)
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
        except Exception:
            pass

        # Fallback to current SHA256 signing formula
        ts_chunk = str(int(time.time() // 14400))
        secret = "12af056dacea0b"
        to_hash = f"{ua}::{lang}::{account_token}::{ts_chunk}::{secret}"
        return hashlib.sha256(to_hash.encode()).hexdigest()

    async def _resolve_gofile(self) -> Dict[str, Any]:
        m = re.search(r'gofile\.io/d/([a-zA-Z0-9_-]+)', self.url)
        if not m:
            raise ValueError("Invalid Gofile URL format. Expected gofile.io/d/{contentId}")
        content_id = m.group(1)

        connector = aiohttp.TCPConnector(ssl=self._ssl_ctx)
        async with aiohttp.ClientSession(connector=connector) as session:
            # 1. Get Guest Token
            token = None
            try:
                async with session.post("https://api.gofile.io/accounts", headers={"User-Agent": USER_AGENT}, timeout=aiohttp.ClientTimeout(total=8)) as acc_r:
                    if acc_r.status == 200:
                        acc_d = await acc_r.json()
                        token = acc_d.get("data", {}).get("token")
            except Exception:
                pass

            if not token:
                token = "guestToken"

            # Off the event loop: this shells out to Node.js (up to a 3s
            # timeout), which would otherwise freeze every other concurrent
            # task on the app's single event loop for that stretch.
            loop = asyncio.get_running_loop()
            wt = await loop.run_in_executor(None, self._get_gofile_wt, token, USER_AGENT, "en-US")

            req_headers = {
                "User-Agent": USER_AGENT,
                "Authorization": f"Bearer {token}",
                "X-Website-Token": wt,
                "X-BL": "en-US",
                "Origin": "https://gofile.io",
                "Referer": "https://gofile.io/"
            }

            # 2. Fetch Content Metadata with retry on 429
            content_url = f"https://api.gofile.io/contents/{content_id}"
            c_data = None
            for attempt in range(2):
                try:
                    async with session.get(content_url, headers=req_headers, timeout=aiohttp.ClientTimeout(total=10)) as c_r:
                        if c_r.status == 429:
                            if attempt == 0:
                                await asyncio.sleep(2.0)
                                continue
                            raise RuntimeError("⚠️ Gofile has temporarily rate-limited guest requests from this IP. Please wait a moment and retry.")
                        elif c_r.status == 401:
                            raise RuntimeError("⚠️ Gofile API returned 401 Unauthorized. The link may be private or password-protected.")
                        elif c_r.status == 404:
                            raise RuntimeError(f"⚠️ Gofile folder/file '{content_id}' was not found (404).")
                        elif c_r.status != 200:
                            raise RuntimeError(f"⚠️ Gofile API returned status {c_r.status}")
                        c_data = await c_r.json()
                        break
                except (asyncio.TimeoutError, aiohttp.ClientConnectorError):
                    raise RuntimeError("⚠️ Connection to Gofile API timed out. Gofile may be experiencing downtime or temporary firewalling.")

            if not c_data or c_data.get("status") != "ok":
                err_msg = (c_data.get("status") if c_data else "Unknown error")
                raise RuntimeError(f"Gofile error: {err_msg}")

            children = c_data.get("data", {}).get("children", {})
            if not children:
                if c_data.get("data", {}).get("link"):
                    node = c_data["data"]
                    return {
                        "provider": "gofile",
                        "direct_url": node["link"],
                        "filename": node.get("name") or f"gofile_{content_id}.zip",
                        "headers": {"User-Agent": USER_AGENT, "Cookie": f"accountToken={token}"}
                    }
                raise RuntimeError("No downloadable files found in this Gofile container.")

            # Pick the primary file
            first_child = list(children.values())[0]
            direct_url = first_child.get("link")
            filename = first_child.get("name") or f"gofile_{content_id}.zip"

            dl_headers = {"User-Agent": USER_AGENT}
            if token:
                dl_headers["Cookie"] = f"accountToken={token}"

            return {
                "provider": "gofile",
                "direct_url": direct_url,
                "filename": filename,
                "headers": dl_headers
            }


    # -------------------------------------------------------------------------
    # 3. PixelDrain Resolver
    # -------------------------------------------------------------------------
    async def _resolve_pixeldrain(self) -> Dict[str, Any]:
        m = re.search(r'pixeldrain\.com/u/([a-zA-Z0-9_\-]+)', self.url)
        if not m:
            raise ValueError("Invalid PixelDrain URL format. Expected pixeldrain.com/u/{id}")
        file_id = m.group(1)

        filename = f"pixeldrain_{file_id}.zip"
        connector = aiohttp.TCPConnector(ssl=self._ssl_ctx)
        async with aiohttp.ClientSession(connector=connector) as session:
            try:
                info_url = f"https://pixeldrain.com/api/file/{file_id}/info"
                async with session.get(info_url, headers={"User-Agent": USER_AGENT}) as ir:
                    if ir.status == 200:
                        idat = await ir.json()
                        filename = idat.get("name") or filename
            except Exception:
                pass

        direct_url = f"https://pixeldrain.com/api/file/{file_id}?download"
        return {
            "provider": "pixeldrain",
            "direct_url": direct_url,
            "filename": filename,
            "headers": {"User-Agent": USER_AGENT}
        }

    # -------------------------------------------------------------------------
    # 4. Ufile.io Resolver
    # -------------------------------------------------------------------------
    async def _resolve_ufile(self) -> Dict[str, Any]:
        headers = {"User-Agent": USER_AGENT}
        connector = aiohttp.TCPConnector(ssl=self._ssl_ctx)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(self.url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                html = await resp.text(errors="ignore")

        # Ufile puts download link in button or script
        m = re.search(r'href=[\"\']((?:https?:)?//[^\"]+ufile\.io/[^\"]*download[^\"]*)[\"\']', html, re.I)
        if not m:
            m = re.search(r'data-url=[\"\']([^\"\']+)[\"\']', html)
        if not m:
            m = re.search(r'location\.href\s*=\s*[\"\']([^\"\']+)[\"\']', html)

        direct_url = m.group(1) if m else self.url
        fn_match = re.search(r'/([a-zA-Z0-9_\-\.\%]+\.[a-zA-Z0-9]{2,5})', direct_url)
        filename = fn_match.group(1) if fn_match else "ufile_download.zip"

        return {
            "provider": "ufile",
            "direct_url": direct_url,
            "filename": filename,
            "headers": {"User-Agent": USER_AGENT, "Referer": self.url}
        }

    # -------------------------------------------------------------------------
    # 5. KrakenFiles Resolver
    # -------------------------------------------------------------------------
    async def _resolve_krakenfiles(self) -> Dict[str, Any]:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://krakenfiles.com/"
        }
        connector = aiohttp.TCPConnector(ssl=self._ssl_ctx)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(self.url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 404:
                    raise RuntimeError("KrakenFiles file not found (404).")
                html = await resp.text(errors="ignore")

        # 1. Parse token
        m_tok = re.search(r'id=[\"\']dl-token[\"\'][^>]+value=[\"\']([^\"\']+)[\"\']', html)
        if not m_tok:
            m_tok = re.search(r'name=[\"\']token[\"\'][^>]+value=[\"\']([^\"\']+)[\"\']', html)
        if not m_tok:
            m_tok = re.search(r'value=[\"\']([a-zA-Z0-9_-]{20,})[\"\'][^>]+name=[\"\']token[\"\']', html)
        token = m_tok.group(1) if m_tok else ""

        # 2. Parse file hash
        m_hash = re.search(r'data-file-hash=[\"\']([^\"\']+)[\"\']', html)
        if not m_hash:
            m_hash = re.search(r'action=[\"\']/download/([^\"\']+)[\"\']', html)
        if not m_hash:
            m_url = re.search(r'/view/([a-zA-Z0-9]+)', self.url)
            if m_url:
                m_hash = m_url

        if not m_hash:
            raise RuntimeError("Could not find KrakenFiles download hash on page.")

        dl_hash = m_hash.group(1)

        # Parse clean filename
        fn_match = re.search(r'class=[\"\']coin-name[\"\'][^>]*>([^<]+)<', html)
        if not fn_match:
            fn_match = re.search(r'<title>([^<]+)</title>', html)
        filename = fn_match.group(1).strip() if fn_match else f"kraken_{dl_hash}.zip"
        filename = filename.replace(" - KrakenFiles", "").strip()

        # 3. Post to /download/{dl_hash}
        dl_endpoint = f"https://krakenfiles.com/download/{dl_hash}"
        post_headers = {
            "User-Agent": USER_AGENT,
            "Referer": self.url,
            "Origin": "https://krakenfiles.com",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded"
        }
        post_data = {"token": token}

        direct_url = None
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=self._ssl_ctx)) as session:
            try:
                async with session.post(dl_endpoint, headers=post_headers, data=post_data, timeout=aiohttp.ClientTimeout(total=15)) as post_resp:
                    if post_resp.status == 200:
                        try:
                            res_json = await post_resp.json()
                            direct_url = res_json.get("url")
                        except Exception:
                            pass
                    elif post_resp.status in (301, 302, 303, 307, 308):
                        direct_url = post_resp.headers.get("Location")
            except Exception:
                pass

        if not direct_url:
            m_direct = re.search(r'href=[\"\'](https?://[^\"]+krakenfiles\.com/get/[^\"]+)[\"\']', html)
            if m_direct:
                direct_url = m_direct.group(1)
            else:
                raise RuntimeError("Could not resolve KrakenFiles direct download URL.")

        return {
            "provider": "krakenfiles",
            "direct_url": direct_url,
            "filename": filename,
            "headers": {"User-Agent": USER_AGENT, "Referer": self.url}
        }

    # -------------------------------------------------------------------------
    # 6. Buzzheavier Resolver
    # -------------------------------------------------------------------------
    async def _resolve_buzzheavier(self) -> Dict[str, Any]:
        parsed = urllib.parse.urlparse(self.url)
        clean_path = parsed.path.rstrip('/')
        if not clean_path.endswith("/download"):
            download_endpoint = f"https://{parsed.netloc}{clean_path}/download"
        else:
            download_endpoint = f"https://{parsed.netloc}{clean_path}"

        buzz_headers = {
            "User-Agent": USER_AGENT,
            "Referer": self.url,
            "Hx-Request": "true",
            "Hx-Current-Url": self.url,
            "Priority": "u=1, i"
        }

        redirect_url = None
        connector = aiohttp.TCPConnector(ssl=self._ssl_ctx)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(download_endpoint, headers=buzz_headers, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 403:
                    raise RuntimeError("⚠️ Buzzheavier is protected by Cloudflare challenge. Please download directly in your browser.")
                redirect_url = resp.headers.get("Hx-Redirect") or resp.headers.get("Location")
                if not redirect_url and resp.status == 200:
                    text = await resp.text(errors="ignore")
                    m = re.search(r'href=[\"\'](https?://[^\"]+)[\"\']', text)
                    if m:
                        redirect_url = m.group(1)

        if not redirect_url:
            raise RuntimeError("Could not resolve Buzzheavier download redirect.")

        fn_match = re.search(r'/([^/?#]+\.[a-zA-Z0-9]{2,5})(?:[?#]|$)', redirect_url)
        filename = urllib.parse.unquote(fn_match.group(1)) if fn_match else f"buzzheavier_{clean_path.split('/')[-1]}.zip"

        return {
            "provider": "buzzheavier",
            "direct_url": redirect_url,
            "filename": filename,
            "headers": {"User-Agent": USER_AGENT, "Referer": self.url}
        }

    # -------------------------------------------------------------------------
    # 6b. Workupload Resolver
    #
    # workupload.com gates every download behind a SHA-256 proof-of-work JS
    # "Are you a human?" check (/puzzle + /captcha), a few seconds of dwell
    # time on the /file/<id> page, and an ad-reward click that finally 302s
    # to an f<N>.workupload.com/download/<id> URL bound to the session
    # cookies. It also IP-throttles anything that hammers it. This does the
    # whole flow with a real browser TLS fingerprint (curl_cffi); if any
    # step is blocked it fails with a message that points at the extension,
    # which drives a real browser and always works.
    # -------------------------------------------------------------------------
    async def _resolve_workupload(self) -> Dict[str, Any]:
        try:
            from curl_cffi.requests import AsyncSession
        except Exception:
            raise RuntimeError(
                "workupload needs the fingerprinted HTTP client — install "
                "requirements from Settings, or use the Copita browser extension."
            )

        m = re.search(r'/(?:file|start|f)/([A-Za-z0-9]+)', urllib.parse.urlparse(self.url).path)
        if not m:
            raise RuntimeError("Couldn't find the workupload file id in that link.")
        fid = m.group(1)
        base = "https://workupload.com"
        gate_msg = (
            "workupload put this download behind a captcha / ad-gate Copita "
            "couldn't pass automatically (it also blocks repeated attempts). "
            "Open the page in your browser and click download with the Copita "
            "extension active — it'll catch the file."
        )

        async with AsyncSession(impersonate="chrome", timeout=25) as s:
            async def _get(path, **kw):
                return await s.get(base + path if path.startswith("/") else path, **kw)

            filename = ""

            def _title_filename(html: str) -> str:
                # The /file/<id> page <title> is the bare filename ("Book.epub").
                tm = re.search(r'<title>\s*([^<]+?)\s*</title>', html, re.I)
                if tm and "workupload" not in tm.group(1).lower() and "." in tm.group(1):
                    return re.sub(r'[\\/*?:"<>|]', '_', tm.group(1).strip())
                return ""

            try:
                r = await _get(f"/file/{fid}")
                filename = _title_filename(r.text)

                # Solve the proof-of-work human check if present.
                probe = await _get(f"/start/{fid}")
                if "Are you a human" in probe.text or "captcha" in probe.text.lower():
                    pz = (await _get("/puzzle", headers={"X-Requested-With": "XMLHttpRequest"})).json()["data"]
                    find, rng, seed = pz["find"], int(pz["range"]), pz["puzzle"]
                    sols = [str(i) for i in range(rng)
                            if hashlib.sha256(f"{seed}{i}".encode()).hexdigest() in find]
                    await s.post(f"{base}/captcha", data={"captcha": " ".join(sols) + " "},
                                 headers={"X-Requested-With": "XMLHttpRequest"})

                # A few seconds of dwell on /file/<id> — the server won't
                # release the download server before that. (Also our chance
                # to read the real filename, if the first hit was the
                # captcha page.)
                dwell = await _get(f"/file/{fid}")
                filename = filename or _title_filename(dwell.text)
                await asyncio.sleep(5.5)
                await _get(f"/start/{fid}", headers={"Referer": f"{base}/file/{fid}"})

                # The /start page's JS calls this for the real CDN URL
                # (f<N>.workupload.com/download/<id>, bound to the session).
                api = await _get(f"/api/file/getDownloadServer/{fid}",
                                 headers={"X-Requested-With": "XMLHttpRequest",
                                          "Referer": f"{base}/start/{fid}"})
                direct = None
                try:
                    direct = api.json().get("data", {}).get("url")
                except Exception:
                    pass
                if not direct:
                    raise RuntimeError(gate_msg)

                cookie_hdr = "; ".join(f"{c.name}={c.value}" for c in s.cookies.jar)
                if not filename:
                    # The CDN response names the file in Content-Disposition.
                    try:
                        hr = await s.get(direct, headers={"Referer": f"{base}/start/{fid}"},
                                         allow_redirects=True, stream=True)
                        cd = hr.headers.get("content-disposition") or ""
                        await hr.aclose()
                        cdm = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', cd, re.I)
                        if cdm:
                            filename = re.sub(r'[\\/*?:"<>|]', '_',
                                              urllib.parse.unquote(cdm.group(1)).strip())
                    except Exception:
                        pass
                if not filename:
                    fn = re.search(r'/([^/?#]+\.[a-zA-Z0-9]{2,5})(?:[?#]|$)', direct)
                    filename = urllib.parse.unquote(fn.group(1)) if fn else f"workupload_{fid}"

                return {
                    "provider": "workupload",
                    "direct_url": direct,
                    "filename": filename,
                    "headers": {
                        "User-Agent": USER_AGENT,
                        "Referer": f"{base}/start/{fid}",
                        "Cookie": cookie_hdr,
                    },
                }
            except RuntimeError:
                raise
            except Exception:
                raise RuntimeError(gate_msg)

    # -------------------------------------------------------------------------
    # 7. Catbox & Litterbox Resolver
    # -------------------------------------------------------------------------
    async def _resolve_catbox(self) -> Dict[str, Any]:
        parsed = urllib.parse.urlparse(self.url)
        if "files.catbox.moe" in parsed.netloc or "litterbox.catbox.moe" in parsed.netloc:
            filename = os.path.basename(parsed.path) or "catbox_download.bin"
            return {
                "provider": "catbox",
                "direct_url": self.url,
                "filename": filename,
                "headers": {"User-Agent": USER_AGENT}
            }

        connector = aiohttp.TCPConnector(ssl=self._ssl_ctx)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(self.url, headers={"User-Agent": USER_AGENT}, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                html = await resp.text(errors="ignore")

        m = re.search(r'href=[\"\'](https?://files\.catbox\.moe/[^\"\']+)[\"\']', html)
        if not m:
            m = re.search(r'src=[\"\'](https?://files\.catbox\.moe/[^\"\']+)[\"\']', html)

        if not m:
            raise RuntimeError("No media items found in Catbox container.")

        direct_url = m.group(1)
        filename = os.path.basename(urllib.parse.urlparse(direct_url).path)
        return {
            "provider": "catbox",
            "direct_url": direct_url,
            "filename": filename,
            "headers": {"User-Agent": USER_AGENT, "Referer": self.url}
        }

    # -------------------------------------------------------------------------
    # 8. Send.cm Resolver
    # -------------------------------------------------------------------------
    async def _resolve_sendcm(self) -> Dict[str, Any]:
        headers = {"User-Agent": USER_AGENT}
        connector = aiohttp.TCPConnector(ssl=self._ssl_ctx)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(self.url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                html = await resp.text(errors="ignore")

        post_data = {}
        for m in re.finditer(r'<input\s+type=[\"\']hidden[\"\']\s+name=[\"\']([^\"\']+)[\"\']\s+value=[\"\']([^\"\']*)[\"\']', html):
            post_data[m.group(1)] = m.group(2)

        m_link = re.search(r'id=[\"\']direct_link[\"\'][^>]+href=[\"\']([^\"\']+)[\"\']', html)
        if not m_link:
            m_link = re.search(r'href=[\"\'](https?://[^\"]+send\.cm/d/[^\"]+)[\"\']', html)

        if m_link:
            direct_url = m_link.group(1)
        elif post_data:
            async with session.post(self.url, headers={"User-Agent": USER_AGENT, "Referer": self.url}, data=post_data, allow_redirects=False) as presp:
                direct_url = presp.headers.get("Location")
                if not direct_url:
                    phtml = await presp.text(errors="ignore")
                    m2 = re.search(r'href=[\"\'](https?://[^\"]+send\.cm/d/[^\"]+)[\"\']', phtml)
                    if m2:
                        direct_url = m2.group(1)
        else:
            direct_url = None

        if not direct_url:
            raise RuntimeError("Could not resolve Send.cm direct download link.")

        fn_match = re.search(r'/([^/?#]+\.[a-zA-Z0-9]{2,5})(?:[?#]|$)', direct_url)
        filename = urllib.parse.unquote(fn_match.group(1)) if fn_match else "sendcm_download.zip"

        return {
            "provider": "sendcm",
            "direct_url": direct_url,
            "filename": filename,
            "headers": {"User-Agent": USER_AGENT, "Referer": self.url}
        }

    # -------------------------------------------------------------------------
    # 9. 1Fichier Resolver
    # -------------------------------------------------------------------------
    async def _resolve_fichier(self) -> Dict[str, Any]:
        headers = {"User-Agent": USER_AGENT}
        connector = aiohttp.TCPConnector(ssl=self._ssl_ctx)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.post(self.url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 404:
                    raise RuntimeError("1Fichier file not found (404).")
                html = await resp.text(errors="ignore")

        m = re.search(r'href=[\"\'](https?://[^\"]+1fichier\.com/[^\"]+)[\"\']\s+class=[\"\']ok btn-general btn-orange[\"\']', html)
        if not m:
            m = re.search(r'class=[\"\']ok btn-general btn-orange[\"\'][^>]+href=[\"\']([^\"\']+)[\"\']', html)
        if not m:
            if "you must wait" in html.lower():
                raise RuntimeError("1Fichier error: Free download limit reached. Please wait before retrying.")
            raise RuntimeError("Could not locate 1Fichier direct download link.")

        direct_url = m.group(1)
        fn_match = re.search(r'/([^/?#]+\.[a-zA-Z0-9]{2,5})(?:[?#]|$)', direct_url)
        filename = urllib.parse.unquote(fn_match.group(1)) if fn_match else "1fichier_download.zip"

        return {
            "provider": "fichier",
            "direct_url": direct_url,
            "filename": filename,
            "headers": {"User-Agent": USER_AGENT, "Referer": self.url}
        }

    # -------------------------------------------------------------------------
    # 10. Dropbox Resolver
    # -------------------------------------------------------------------------
    async def _resolve_dropbox(self) -> Dict[str, Any]:
        direct_url = self.url
        if "dl=0" in direct_url:
            direct_url = direct_url.replace("dl=0", "dl=1")
        elif "?" not in direct_url:
            direct_url = direct_url + "?dl=1"
        else:
            direct_url = direct_url + "&dl=1"

        parsed = urllib.parse.urlparse(self.url)
        filename = os.path.basename(parsed.path) or "dropbox_file.zip"
        if not re.search(r'\.[a-zA-Z0-9]{2,5}$', filename):
            filename = f"{filename}.zip"

        return {
            "provider": "dropbox",
            "direct_url": direct_url,
            "filename": filename,
            "headers": {"User-Agent": USER_AGENT}
        }

    # -------------------------------------------------------------------------
    # 11. Google Drive Resolver
    # -------------------------------------------------------------------------
    @staticmethod
    def extract_gdrive_id(url: str) -> Optional[str]:
        """Extracts Google Drive file or folder ID from any valid Google URL or ID string."""
        if not url:
            return None
        cleaned_url = url.strip().strip('"\'<>')

        # 1. Standard /d/{id} paths (covers /file/d/, /file/u/0/d/, /document/d/, /spreadsheets/d/, /d/)
        m = re.search(r'/d/([a-zA-Z0-9_-]{15,})', cleaned_url)
        if not m:
            m = re.search(r'/d/([a-zA-Z0-9_-]+)', cleaned_url)
        if m:
            return m.group(1)

        # 2. Folder paths (covers /drive/folders/{id}, /drive/u/0/folders/{id}, /folders/{id})
        m = re.search(r'/(?:folders|folder)(?:/u/\d+)?/([a-zA-Z0-9_-]+)', cleaned_url)
        if m:
            return m.group(1)

        # 3. Query parameters (id=, docid=, folderId=, fileId=)
        m = re.search(r'[?&](?:id|docid|folderId|fileId)=([a-zA-Z0-9_-]+)', cleaned_url)
        if m:
            return m.group(1)

        # 4. Raw file ID (25 to 60 alphanumeric chars)
        if re.match(r'^[a-zA-Z0-9_-]{25,60}$', cleaned_url):
            return cleaned_url

        return None

    @staticmethod
    def _extract_filename_from_cd(cd_header: str) -> Optional[str]:
        """Extracts sanitized filename from Content-Disposition header."""
        if not cd_header:
            return None
        m_utf8 = re.search(r"filename\*\s*=\s*(?:UTF-8|utf-8)''([^;]+)", cd_header)
        if m_utf8:
            return urllib.parse.unquote(m_utf8.group(1)).strip().strip('"\'')
        m_fn = re.search(r'filename\s*=\s*(?:"([^"]+)"|([^;\s]+))', cd_header)
        if m_fn:
            val = m_fn.group(1) or m_fn.group(2)
            return urllib.parse.unquote(val).strip().strip('"\'')
        return None

    async def _list_gdrive_folder_items(self, folder_id: str) -> tuple:
        """Lists the immediate children (files and subfolders) of one Google
        Drive folder. Kept separate from _resolve_gdrive so it can also be
        called per-subfolder while recursively crawling a folder tree."""
        folder_name = f"gdrive_folder_{folder_id}"
        children = []
        folder_url = f"https://drive.google.com/drive/folders/{folder_id}"
        try:
            connector = aiohttp.TCPConnector(ssl=self._ssl_ctx)
            headers = {"User-Agent": USER_AGENT}
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(folder_url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        html = await resp.text(errors="ignore")
                        title_m = re.search(r'<title>([^<]+?)(?:\s*[-–]\s*Google Drive)?</title>', html, re.I)
                        if title_m and title_m.group(1).strip() and "Google Drive" not in title_m.group(1):
                            folder_name = re.sub(r'[\\/*?:"<>|]', '_', title_m.group(1).strip())

                        m_ivd = list(re.compile(r"window\['_DRIVE_ivd'\]\s*=\s*'((?:[^'\\]|\\.)*)'").finditer(html))
                        if m_ivd:
                            raw_str = m_ivd[0].group(1)
                            decoded_str = raw_str.encode("utf-8").decode("unicode_escape")
                            parsed_data = json.loads(decoded_str)
                            items = parsed_data[0] if parsed_data and parsed_data[0] else []
                            for item in items:
                                if len(item) > 3 and item[0] and item[2]:
                                    c_id = item[0]
                                    c_name = item[2]
                                    c_mime = item[3] or ""
                                    c_size = item[13] if len(item) > 13 and isinstance(item[13], int) else None
                                    is_sub = (c_mime == "application/vnd.google-apps.folder")
                                    c_url = f"https://drive.usercontent.google.com/download?id={c_id}&export=download&confirm=t" if not is_sub else f"https://drive.google.com/drive/folders/{c_id}"
                                    c_cat = "video" if "video" in c_mime else ("audio" if "audio" in c_mime else ("document" if "pdf" in c_mime else "file"))
                                    children.append({
                                        "id": c_id,
                                        "name": c_name,
                                        "mime": c_mime,
                                        "size": c_size,
                                        "is_folder": is_sub,
                                        "url": c_url,
                                        "category": c_cat
                                    })
        except Exception:
            pass
        return folder_name, children

    async def _resolve_gdrive(self) -> Dict[str, Any]:
        file_id = self.extract_gdrive_id(self.url)
        if not file_id:
            # Attempt to follow HTTP redirects in case of shortened/vanity URL
            try:
                connector = aiohttp.TCPConnector(ssl=self._ssl_ctx)
                async with aiohttp.ClientSession(connector=connector) as session:
                    async with session.head(self.url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        file_id = self.extract_gdrive_id(str(resp.url))
            except Exception:
                pass

        if not file_id:
            raise ValueError(f"Could not extract Google Drive file ID from URL: {self.url}")

        # Check for security resourcekey query parameter
        rk_match = re.search(r'[?&]resourcekey=([a-zA-Z0-9_-]+)', self.url)
        resource_key = rk_match.group(1) if rk_match else None

        # Handle Google Docs / Spreadsheets / Presentations exports
        if "docs.google.com/document" in self.url:
            return {
                "provider": "gdrive",
                "direct_url": f"https://docs.google.com/document/d/{file_id}/export?format=pdf",
                "filename": f"google_doc_{file_id}.pdf",
                "headers": {"User-Agent": USER_AGENT}
            }
        elif "docs.google.com/spreadsheets" in self.url:
            return {
                "provider": "gdrive",
                "direct_url": f"https://docs.google.com/spreadsheets/d/{file_id}/export?format=xlsx",
                "filename": f"google_sheet_{file_id}.xlsx",
                "headers": {"User-Agent": USER_AGENT}
            }
        elif "docs.google.com/presentation" in self.url:
            return {
                "provider": "gdrive",
                "direct_url": f"https://docs.google.com/presentation/d/{file_id}/export/pdf",
                "filename": f"google_slides_{file_id}.pdf",
                "headers": {"User-Agent": USER_AGENT}
            }

        # Check if URL is a Google Drive folder
        is_folder = bool(re.search(r'/(?:folders|folder)(?:/u/\d+)?/', self.url) or "folderId=" in self.url)
        if is_folder:
            folder_name, children = await self._list_gdrive_folder_items(file_id)

            return {
                "provider": "gdrive",
                "is_folder": True,
                "folder_id": file_id,
                "filename": f"{folder_name}.zip",
                "folder_name": folder_name,
                "children": children,
                "direct_url": f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t",
                "headers": {"User-Agent": USER_AGENT}
            }

        # Single file direct download resolution
        direct_url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"
        if resource_key:
            direct_url += f"&resourcekey={resource_key}"
        filename = f"gdrive_{file_id}.zip"
        headers = {"User-Agent": USER_AGENT}

        # Probe Google Drive to inspect Content-Disposition and bypass virus scan warning
        try:
            connector = aiohttp.TCPConnector(ssl=self._ssl_ctx)
            cookie_jar = aiohttp.CookieJar()
            async with aiohttp.ClientSession(connector=connector, cookie_jar=cookie_jar) as session:
                probe_url = f"https://drive.google.com/uc?export=download&id={file_id}&confirm=t"
                if resource_key:
                    probe_url += f"&resourcekey={resource_key}"
                async with session.get(probe_url, headers=headers, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    cd = resp.headers.get("Content-Disposition", "")
                    if cd:
                        fn = self._extract_filename_from_cd(cd)
                        if fn:
                            filename = fn
                            direct_url = str(resp.url)

                    content_type = resp.headers.get("Content-Type", "")
                    if resp.status == 200 and "text/html" in content_type:
                        html = await resp.text(errors="ignore")
                        # Extract title
                        title_m = re.search(r'<title>([^<]+?)(?:\s*[-–]\s*Google Drive)?</title>', html, re.I)
                        if title_m and title_m.group(1).strip():
                            cand = title_m.group(1).strip()
                            if "Google Drive" not in cand and "error" not in cand.lower() and "not found" not in cand.lower() and "warning" not in cand.lower():
                                filename = cand

                        # Check for form action (modern virus scan bypass)
                        form_action_m = re.search(r'<form\s+id=[\"\']download-form[\"\'][^>]+action=[\"\']([^\"\']+)[\"\']', html, re.I)
                        if not form_action_m:
                            form_action_m = re.search(r'<form[^>]+action=[\"\'](https://drive\.usercontent\.google\.com/download[^\"\']*)[\"\']', html, re.I)
                        if form_action_m:
                            action = form_action_m.group(1)
                            inputs = re.findall(r'<input\s+[^>]*name=[\"\']([^\"\']+)[\"\'][^>]*value=[\"\']([^\"\']*)[\"\']', html, re.I)
                            params = {k: v for k, v in inputs}
                            params.setdefault("id", file_id)
                            params.setdefault("export", "download")
                            params.setdefault("confirm", "t")
                            if resource_key and "resourcekey" not in params:
                                params["resourcekey"] = resource_key
                            direct_url = f"{action}?{urllib.parse.urlencode(params)}"
                        else:
                            # Check uc-download-link
                            link_m = re.search(r'id=[\"\']uc-download-link[\"\'][^>]+href=[\"\']([^\"\']+)[\"\']', html, re.I)
                            if not link_m:
                                link_m = re.search(r'href=[\"\'](/uc\?export=download[^\"\']+)[\"\']', html, re.I)
                            if link_m:
                                href = link_m.group(1).replace("&amp;", "&")
                                direct_url = f"https://drive.google.com{href}" if href.startswith("/") else href
                            else:
                                confirm_m = re.search(r'confirm=([0-9a-zA-Z_-]+)', html)
                                if confirm_m:
                                    direct_url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm={confirm_m.group(1)}"
                                    if resource_key:
                                        direct_url += f"&resourcekey={resource_key}"

                        cookies = [f"{c.key}={c.value}" for c in cookie_jar]
                        if cookies:
                            headers["Cookie"] = "; ".join(cookies)
        except Exception:
            pass

        return {
            "provider": "gdrive",
            "direct_url": direct_url,
            "filename": filename,
            "headers": headers
        }

    # -------------------------------------------------------------------------
    # 12. Mega.nz Native Decrypting Engine
    # -------------------------------------------------------------------------
    async def _resolve_mega(self) -> Dict[str, Any]:
        # Formats:
        # https://mega.nz/file/{file_id}#{file_key}
        # https://mega.nz/#!{file_id}!{file_key}
        # https://mega.nz/folder/{folder_id}#{folder_key}
        # https://mega.nz/#F!{folder_id}!{folder_key}
        folder_m = re.search(r'mega\.(?:nz|co\.nz)/folder/([a-zA-Z0-9_-]+)#([a-zA-Z0-9_-]+)', self.url)
        if not folder_m:
            folder_m = re.search(r'mega\.(?:nz|co\.nz)/#F!([a-zA-Z0-9_-]+)!([a-zA-Z0-9_-]+)', self.url)
        if folder_m:
            return await self._resolve_mega_folder(folder_m.group(1), folder_m.group(2))

        file_id = None
        file_key = None

        m1 = re.search(r'mega\.(?:nz|co\.nz)/file/([a-zA-Z0-9_-]+)#([a-zA-Z0-9_-]+)', self.url)
        if m1:
            file_id, file_key = m1.group(1), m1.group(2)
        else:
            m2 = re.search(r'mega\.(?:nz|co\.nz)/#!([a-zA-Z0-9_-]+)!([a-zA-Z0-9_-]+)', self.url)
            if m2:
                file_id, file_key = m2.group(1), m2.group(2)

        if not file_id or not file_key:
            raise ValueError("Invalid MEGA URL. Expected https://mega.nz/file/{id}#{key}")

        # Decode 128-bit AES Key & 64-bit IV
        k = self._mega_base64_to_a32(file_key)
        if len(k) < 8:
            raise ValueError("Invalid MEGA file key length.")

        aes_key = self._mega_a32_to_str((k[0] ^ k[4], k[1] ^ k[5], k[2] ^ k[6], k[3] ^ k[7]))
        iv = self._mega_a32_to_str((k[4], k[5], 0, 0))

        # Query Mega API
        api_payload = [{"a": "g", "g": 1, "ssl": 2, "p": file_id}]
        connector = aiohttp.TCPConnector(ssl=self._ssl_ctx)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.post(
                "https://g.api.mega.co.nz/cs?id=1",
                data=json.dumps(api_payload),
                headers={"Content-Type": "application/json"}
            ) as r:
                res = await r.json()

        if isinstance(res, list) and len(res) > 0 and isinstance(res[0], dict):
            file_info = res[0]
        elif isinstance(res, list) and len(res) > 0 and isinstance(res[0], int):
            raise RuntimeError(f"MEGA API error code: {res[0]} (File might be deleted or unavailable).")
        else:
            raise RuntimeError("Unexpected response from MEGA API.")

        download_url = file_info.get("g")
        file_size = file_info.get("s", 0)
        attr_b64 = file_info.get("at", "")

        # Decrypt Attributes for Filename
        filename = f"mega_{file_id}.bin"
        if attr_b64:
            try:
                attr_bytes = self._mega_base64_urldecode(attr_b64)
                # AES-128-CBC with zero IV
                zero_iv = b"\x00" * 16
                cipher = Cipher(algorithms.AES(aes_key), modes.CBC(zero_iv))
                decryptor = cipher.decryptor()
                dec_attrs = decryptor.update(attr_bytes) + decryptor.finalize()
                dec_attrs = dec_attrs.rstrip(b"\x00")
                if dec_attrs.startswith(b"MEGA"):
                    attr_json = json.loads(dec_attrs[4:].decode("utf-8", errors="ignore"))
                    filename = attr_json.get("n", filename)
            except Exception:
                pass

        return {
            "provider": "mega",
            "file_id": file_id,
            "aes_key": aes_key,
            "iv": iv,
            "download_url": download_url,
            "file_size": file_size,
            "filename": filename
        }

    @staticmethod
    def _decrypt_mega_attr_name(attr_b64: str, aes_key: bytes, fallback: str) -> str:
        try:
            attr_bytes = FileHosterDownloader._mega_base64_urldecode(attr_b64)
            decryptor = Cipher(algorithms.AES(aes_key), modes.CBC(b"\x00" * 16)).decryptor()
            dec_attrs = (decryptor.update(attr_bytes) + decryptor.finalize()).rstrip(b"\x00")
            if dec_attrs.startswith(b"MEGA"):
                return json.loads(dec_attrs[4:].decode("utf-8", errors="ignore")).get("n", fallback)
        except Exception:
            pass
        return fallback

    async def _resolve_mega_folder(self, folder_id: str, folder_key_b64: str) -> Dict[str, Any]:
        """Lists every file in a shared MEGA folder, at every nesting depth
        (the 'r':1 listing request returns the whole subtree, not just the
        top level), and reconstructs each file's real subfolder path so
        identically-named files from different subfolders don't collide when
        written to disk.

        Folder nodes carry a simple 16-byte AES key wrapped once with the
        folder key. File nodes carry a 32-byte key wrapped the same way, then
        XORed into a content key/IV — this is the same derivation as a
        single-file link's key, just wrapped instead of given directly."""
        folder_key_a32 = self._mega_base64_to_a32(folder_key_b64)
        if len(folder_key_a32) < 4:
            raise ValueError("Invalid MEGA folder key.")
        folder_key_bytes = self._mega_a32_to_str(tuple(folder_key_a32[:4]))

        api_payload = [{"a": "f", "c": 1, "ca": 1, "r": 1}]
        connector = aiohttp.TCPConnector(ssl=self._ssl_ctx)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.post(
                f"https://g.api.mega.co.nz/cs?id=1&n={folder_id}",
                data=json.dumps(api_payload),
                headers={"Content-Type": "application/json"}
            ) as r:
                res = await r.json()

        if isinstance(res, list) and len(res) > 0 and isinstance(res[0], int):
            raise RuntimeError(f"MEGA API error code: {res[0]} (Folder might be deleted, private, or unavailable).")
        if not isinstance(res, list) or not res or not isinstance(res[0], dict) or "f" not in res[0]:
            raise RuntimeError("Unexpected response from MEGA API when listing folder.")

        nodes = res[0]["f"]
        folder_names: Dict[str, str] = {}
        folder_parents: Dict[str, Optional[str]] = {}
        file_nodes = []

        for node in nodes:
            handle = node.get("h")
            key_field = node.get("k", "")
            key_part = key_field.split(":")[-1] if ":" in key_field else key_field
            if node.get("t") == 1:
                try:
                    encrypted_key_bytes = self._mega_base64_urldecode(key_part)
                    encrypted_key_bytes = encrypted_key_bytes[:len(encrypted_key_bytes) - len(encrypted_key_bytes) % 16]
                    decryptor = Cipher(algorithms.AES(folder_key_bytes), modes.ECB()).decryptor()
                    folder_aes_key = (decryptor.update(encrypted_key_bytes) + decryptor.finalize())[:16]
                    name = self._decrypt_mega_attr_name(node.get("a", ""), folder_aes_key, handle)
                except Exception:
                    name = handle
                folder_names[handle] = name
                folder_parents[handle] = node.get("p")
            elif node.get("t") == 0:
                file_nodes.append(node)

        # The shared root is whichever folder's parent wasn't itself returned
        # (MEGA scopes the listing to the shared subtree, so the true root's
        # parent handle is outside that subtree and never appears as a node).
        root_handle = next((h for h in folder_names if folder_parents.get(h) not in folder_names), None)
        folder_name = folder_names.get(root_handle, f"MEGA_{folder_id}") if root_handle else f"MEGA_{folder_id}"

        def _relative_dir(parent_handle: Optional[str]) -> list:
            parts = []
            cur = parent_handle
            seen = set()
            while cur and cur != root_handle and cur in folder_names and cur not in seen:
                seen.add(cur)
                parts.append(folder_names[cur])
                cur = folder_parents.get(cur)
            parts.reverse()
            return parts

        files = []
        for node in file_nodes:
            handle = node.get("h")
            key_field = node.get("k", "")
            key_part = key_field.split(":")[-1] if ":" in key_field else key_field
            try:
                encrypted_key_bytes = self._mega_base64_urldecode(key_part)
                encrypted_key_bytes = encrypted_key_bytes[:len(encrypted_key_bytes) - len(encrypted_key_bytes) % 16]
                decryptor = Cipher(algorithms.AES(folder_key_bytes), modes.ECB()).decryptor()
                decrypted_key_bytes = decryptor.update(encrypted_key_bytes) + decryptor.finalize()
                file_key_a32 = struct.unpack(f">{len(decrypted_key_bytes) // 4}I", decrypted_key_bytes)
            except Exception:
                continue
            if len(file_key_a32) < 8:
                continue

            aes_key = self._mega_a32_to_str((
                file_key_a32[0] ^ file_key_a32[4], file_key_a32[1] ^ file_key_a32[5],
                file_key_a32[2] ^ file_key_a32[6], file_key_a32[3] ^ file_key_a32[7]
            ))
            iv = self._mega_a32_to_str((file_key_a32[4], file_key_a32[5], 0, 0))
            filename = self._decrypt_mega_attr_name(node.get("a", ""), aes_key, f"mega_{handle}.bin")

            files.append({
                "handle": handle,
                "filename": filename,
                "aes_key": aes_key,
                "iv": iv,
                "size": node.get("s", 0),
                "rel_dir": _relative_dir(node.get("p")),
            })

        if not files:
            raise ValueError("No downloadable files found in this MEGA folder (it may be empty or contain only subfolders).")

        return {
            "provider": "mega_folder",
            "folder_id": folder_id,
            "folder_name": folder_name,
            "files": files,
        }

    async def _download_mega_folder(self, res: Dict[str, Any]) -> str:
        folder_id = res["folder_id"]
        folder_name = res.get("folder_name") or f"MEGA_{folder_id}"
        target_dir = os.path.join(self.output_dir, folder_name)
        os.makedirs(target_dir, exist_ok=True)

        # MEGA's anonymous-download quota is a fixed number of total bytes
        # for this IP, not per file. Downloading in whatever arbitrary order
        # the API listed nodes could burn the whole quota on one big video
        # and leave every smaller file (docs, images) unfinished. Smallest
        # first maximizes how many complete, usable files you get out of a
        # fixed byte budget before the throttle hits.
        files = sorted(res["files"], key=lambda f: f.get("size") or 0)

        total_files = len(files)
        known_total = sum(f.get("size") or 0 for f in files)
        self._report("downloading", 10.0, total=known_total,
                     msg=f"Downloading {total_files} files from MEGA folder...")
        # Sequential rather than concurrent: with a shared byte quota, running
        # several files at once just means several land unfinished the moment
        # the quota trips, instead of at most one.
        sem = asyncio.Semaphore(1)
        progress = [{"percent": 0.0, "speed": 0.0, "downloaded": 0, "total": f.get("size") or 0} for f in files]
        failures = []

        def _report_children():
            pct = 10 + sum(item["percent"] for item in progress) / total_files * .85
            total = sum(item["total"] for item in progress)
            self._report("downloading", pct,
                         speed=sum(item["speed"] for item in progress),
                         downloaded=sum(item["downloaded"] for item in progress), total=total)

        used_paths = set()

        def _unique_path(rel_dir, fname):
            safe_parts = [re.sub(r'[\\/*?:"<>|]', '_', p) for p in rel_dir]
            cdir = os.path.join(target_dir, *safe_parts)
            os.makedirs(cdir, exist_ok=True)
            base, ext = os.path.splitext(fname)
            candidate = os.path.join(cdir, fname)
            n = 2
            while candidate in used_paths:
                candidate = os.path.join(cdir, f"{base} ({n}){ext}")
                n += 1
            used_paths.add(candidate)
            return candidate

        # MEGA's anonymous-download bandwidth throttle applies to the whole
        # storage node, so once one file in this folder hits it, every other
        # file almost certainly will too. Without this, all remaining files
        # still get attempted at once (semaphore=3, each failing near-
        # instantly), which hammers MEGA harder and floods the UI with a
        # burst of near-simultaneous progress updates.
        throttled = {"hit": False}

        async def _download_one(file_info: Dict[str, Any], idx: int):
            async with sem:
                fpath = _unique_path(file_info.get("rel_dir") or [], file_info["filename"])
                fname = os.path.basename(fpath)
                # A retry after a partial folder download (e.g. hitting MEGA's
                # bandwidth throttle partway through) would otherwise
                # re-download every file from scratch, including ones that
                # already finished correctly. Skip anything already complete.
                expected_size = file_info.get("size")
                if expected_size and os.path.isfile(fpath) and os.path.getsize(fpath) == expected_size:
                    progress[idx].update(percent=100.0, speed=0.0, downloaded=expected_size, total=expected_size)
                    _report_children()
                    return
                if throttled["hit"]:
                    failures.append((fname, "Skipped — MEGA's bandwidth throttle is already active for this folder."))
                    progress[idx]["speed"] = 0.0
                    _report_children()
                    return
                try:
                    api_payload = [{"a": "g", "g": 1, "n": file_info["handle"]}]
                    connector = aiohttp.TCPConnector(ssl=self._ssl_ctx)
                    async with aiohttp.ClientSession(connector=connector) as session:
                        async with session.post(
                            f"https://g.api.mega.co.nz/cs?id=1&n={folder_id}",
                            data=json.dumps(api_payload),
                            headers={"Content-Type": "application/json"}
                        ) as r:
                            info = await r.json()

                        if isinstance(info, list) and info and isinstance(info[0], int):
                            raise RuntimeError(f"MEGA API error code: {info[0]}")
                        if not isinstance(info, list) or not info or not isinstance(info[0], dict):
                            raise RuntimeError("Unexpected response from MEGA API.")

                        file_data = info[0]
                        download_url = file_data.get("g")
                        file_size = file_data.get("s") or progress[idx]["total"]
                        progress[idx]["total"] = file_size
                        if not download_url:
                            raise RuntimeError("No download URL returned by MEGA for this file.")

                        cipher = Cipher(algorithms.AES(file_info["aes_key"]), modes.CTR(file_info["iv"]))
                        decryptor = cipher.decryptor()
                        downloaded = 0
                        last_time = time.time()
                        last_bytes = 0

                        async with session.get(download_url) as resp:
                            if resp.status == 509:
                                throttled["hit"] = True
                                raise RuntimeError(
                                    "MEGA has temporarily throttled this file (bandwidth quota "
                                    "for anonymous downloads). Try again in a few hours."
                                )
                            if resp.status != 200:
                                raise RuntimeError(f"MEGA CDN returned HTTP {resp.status}")
                            with open(fpath, "wb") as out_f:
                                async for chunk in resp.content.iter_chunked(64 * 1024):
                                    if not chunk:
                                        break
                                    out_f.write(decryptor.update(chunk))
                                    downloaded += len(chunk)
                                    await _speed_gate.throttle(len(chunk))
                                    now = time.time()
                                    if now - last_time >= 0.5:
                                        speed_bps = (downloaded - last_bytes) / (now - last_time)
                                        last_time, last_bytes = now, downloaded
                                        progress[idx].update(
                                            percent=(downloaded / file_size * 100) if file_size else 0.0,
                                            speed=speed_bps, downloaded=downloaded)
                                        _report_children()
                                out_f.write(decryptor.finalize())
                    progress[idx].update(percent=100.0, speed=0.0, downloaded=downloaded, total=downloaded)
                except Exception as error:
                    failures.append((fname, str(error)))
                    progress[idx]["speed"] = 0.0
                _report_children()

        await asyncio.gather(*[_download_one(f, i) for i, f in enumerate(files)])
        if failures:
            raise ValueError(
                f"{len(failures)} of {total_files} files could not be downloaded. "
                f"Completed files are kept in {folder_name}. First error: {failures[0][1]}"
            )
        size = sum(item["downloaded"] for item in progress)
        self._report("finished", 100, speed=0, downloaded=size, total=size,
                     msg=f"Completed downloading {total_files} files to {folder_name}.")
        return target_dir

    async def _download_mega(self, res: Dict[str, Any]) -> str:
        download_url = res["download_url"]
        file_size = res["file_size"]
        filename = res["filename"]
        aes_key = res["aes_key"]
        iv = res["iv"]
        final_path = os.path.join(self.output_dir, filename)

        self._report("downloading", 10.0, msg=f"Decrypting and downloading {filename}...", downloaded=0, total=file_size)

        cipher = Cipher(algorithms.AES(aes_key), modes.CTR(iv))
        decryptor = cipher.decryptor()

        connector = aiohttp.TCPConnector(ssl=self._ssl_ctx)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(download_url) as resp:
                if resp.status == 509:
                    raise RuntimeError(
                        "MEGA has temporarily throttled this file (its storage node hit its free "
                        "bandwidth quota for anonymous downloads). This isn't something Copita can "
                        "bypass — it typically resets in a few hours. Try again later, or open "
                        "the link in your browser and sign in to a MEGA account to download it now."
                    )
                if resp.status != 200:
                    raise RuntimeError(f"MEGA CDN returned HTTP {resp.status}")

                downloaded = 0
                last_time = time.time()
                last_bytes = 0
                speed_str = ""

                with open(final_path, "wb") as out_f:
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        if not chunk:
                            break
                        dec_chunk = decryptor.update(chunk)
                        out_f.write(dec_chunk)
                        downloaded += len(chunk)
                        await _speed_gate.throttle(len(chunk))

                        now = time.time()
                        if now - last_time >= 0.5:
                            speed_bps = (downloaded - last_bytes) / (now - last_time)
                            speed_str = f"{speed_bps / (1024*1024):.1f} MB/s"
                            last_time = now
                            last_bytes = downloaded
                            pct = (downloaded / file_size * 100) if file_size else 50.0
                            self._report("downloading", pct, speed=speed_bps, msg=f"Streaming: {pct:.1f}% ({speed_str})", downloaded=downloaded, total=file_size)

                    out_f.write(decryptor.finalize())

        self._report("completed", 100.0, msg=f"Saved: {filename}", downloaded=file_size, total=file_size)
        return final_path

    # MEGA Base64 & A32 helpers
    @staticmethod
    def _mega_base64_urldecode(data: str) -> bytes:
        data += "=" * ((4 - len(data) % 4) % 4)
        data = data.replace("-", "+").replace("_", "/")
        return base64.b64decode(data)

    @staticmethod
    def _mega_base64_to_a32(s: str):
        b = FileHosterDownloader._mega_base64_urldecode(s)
        return struct.unpack(f">{len(b) // 4}I", b[:len(b) - len(b) % 4])

    @staticmethod
    def _mega_a32_to_str(a):
        return struct.pack(f">{len(a)}I", *a)
