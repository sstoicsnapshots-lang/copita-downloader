"""
Deep Webpage Media Sniffer for Copita.
Discovers hidden video, audio, HLS/DASH streams, and downloadable assets
across arbitrary websites, embedded players, and single-page apps.
"""

import re
import ssl
import json
import time
import asyncio
import certifi
import aiohttp
from urllib.parse import urljoin, urlparse
from typing import List, Dict, Any, Optional

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def _image_res_score(url: str) -> int:
    """Rough 'how big is this image' score from dimension hints in the URL
    (…_1280.png, ?w=1920, /2048/…, 800x600) so the highest-resolution variant
    of the same picture is preferred over a thumbnail. Only counts numbers in
    positions that actually denote a size, not e.g. a date in the path."""
    hints = []
    hints += re.findall(r'[_\-/](\d{2,5})\.(?:jpe?g|png|webp|gif|bmp)', url, re.I)
    hints += re.findall(r'[?&](?:w|width|h|height|size|s)=(\d{2,5})', url, re.I)
    hints += re.findall(r'(?:^|[/_\-])(\d{3,5})x(\d{3,5})(?:[/_\-.]|$)', url)
    flat = []
    for h in hints:
        flat.extend(h if isinstance(h, tuple) else [h])
    plausible = [int(n) for n in flat if 80 <= int(n) <= 8000]
    return max(plausible) if plausible else 0

class MediaSniffer:
    def __init__(self, headers: Optional[Dict[str, str]] = None, progress_callback: Optional[Any] = None):
        self.headers = headers or {}
        self.ssl_context = ssl.create_default_context(cafile=certifi.where())
        # Deep scanning can take 30+ seconds once it falls back to spawning a
        # real headless browser (see _sniff_browser_cdp) — without any
        # in-between status, that whole stretch looks identical to the app
        # being frozen. This lets callers surface what phase it's actually in.
        self._progress_callback = progress_callback
        # Filled in by _sniff_browser_cdp once it has a rendered page — the
        # real <title> / og:title behind a captcha wall the plain fetch only
        # saw the challenge page of. Used by the "promised file behind a
        # gate" guard so it works even when the first fetch never got the
        # real HTML.
        self._cdp_page_text = ""

    def _notify(self, message: str):
        if self._progress_callback:
            try:
                self._progress_callback({"status": "analyzing", "message": message})
            except Exception:
                pass

    async def sniff_url(self, page_url: str) -> Dict[str, Any]:
        """Deep scan a webpage URL for all downloadable media."""
        req_headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            **self.headers
        }

        self._notify("Fetching page…")
        connector = aiohttp.TCPConnector(ssl=self.ssl_context)
        async with aiohttp.ClientSession(connector=connector) as session:
            html = None
            fetch_error = None
            # The first fetch goes through the browser-fingerprinted client
            # (curl_cffi) — a plain aiohttp handshake is what most
            # "site blocked a direct request" 403s actually reject.
            try:
                from copita.core.http_client import HttpClient
                async with HttpClient(headers=self.headers, timeout=20.0) as fp_client:
                    async with await fp_client.get(page_url, allow_redirects=True) as resp:
                        if resp.status == 200:
                            html = (await resp.read()).decode("utf-8", errors="ignore")
                        else:
                            fetch_error = f"HTTP {resp.status}"
            except Exception as e:
                fetch_error = str(e)

            if html is None:
                # The page blocked a plain request (Cloudflare / bot check /
                # 403 — very common on image sites like pixabay). A real
                # headless browser often still renders it fine, so fall
                # straight through to the CDP path rather than giving up.
                self._notify("The site blocked a direct request — opening a headless browser instead…")
                cdp_media: List[Dict[str, Any]] = []

                def _cdp_add(url, kind, label, quality="Unknown"):
                    clean = url.strip()
                    if clean.startswith('//'):
                        clean = 'https:' + clean
                    if clean not in {m["url"] for m in cdp_media}:
                        cdp_media.append({"url": clean, "type": kind, "label": label,
                                          "quality": quality, "page_url": page_url})

                try:
                    await asyncio.wait_for(self._sniff_browser_cdp(page_url, _cdp_add), timeout=30)
                except Exception:
                    pass
                if cdp_media:
                    return {"page_url": page_url, "title": urlparse(page_url).netloc,
                            "media_count": len(cdp_media), "media": cdp_media, "cookies": ""}
                return {"page_url": page_url, "title": "", "media": [],
                        "error": fetch_error,
                        "note": f"Couldn't reach this page directly ({fetch_error}) and "
                                f"nothing downloadable was found when opening it in a browser."}

            title_m = re.search(r'<title[^>]*>(.*?)</title>', html, re.I | re.DOTALL)
            page_title = title_m.group(1).strip() if title_m else urlparse(page_url).netloc

            found_media: List[Dict[str, Any]] = []
            seen_urls = set()

            def add_media(url: str, kind: str, label: str, quality: str = "Unknown"):
                clean = url.strip().replace(r'\/', '/')
                if clean.startswith('//'):
                    clean = 'https:' + clean
                elif clean.startswith('/'):
                    clean = urljoin(page_url, clean)
                elif not clean.startswith('http'):
                    clean = urljoin(page_url, clean)

                if clean not in seen_urls and not clean.endswith('.js') and not clean.endswith('.css'):
                    seen_urls.add(clean)
                    found_media.append({
                        "url": clean,
                        "type": kind,
                        "label": label,
                        "quality": quality,
                        "page_url": page_url
                    })

            # 1. HTML5 Video & Audio Elements
            for tag in re.finditer(r'<(video|audio)[^>]*>', html, re.I):
                tag_html = tag.group(0)
                src_m = re.search(r'src=[\"\']([^\"\']+)[\"\']', tag_html, re.I)
                if src_m:
                    kind = "video" if "video" in tag.group(1).lower() else "audio"
                    add_media(src_m.group(1), kind, f"HTML5 {kind.title()} Element")

            for src in re.finditer(r'<source[^>]+src=[\"\']([^\"\']+)[\"\'][^>]*>', html, re.I):
                src_html = src.group(0)
                media_url = src.group(1)
                kind = "video"
                if "audio" in src_html.lower():
                    kind = "audio"
                add_media(media_url, kind, f"HTML5 Source ({kind})")

            # 2. OpenGraph & Twitter Cards
            for og in re.finditer(r'<meta\s+property=[\"\'](og:video|og:video:url|og:audio|og:audio:url)[\"\']\s+content=[\"\']([^\"\']+)[\"\']', html, re.I):
                prop, content = og.group(1).lower(), og.group(2)
                kind = "audio" if "audio" in prop else "video"
                add_media(content, kind, f"OpenGraph {kind.title()}")

            # 2b. Images — the page's own primary image, the way a browser's
            # "Save image as…" sees it. Covers single-image pages (pixabay,
            # reddit i.redd.it posts, most CMS "photo" pages) that have no
            # video/audio at all and previously fell through as "no media".
            image_candidates: List[tuple] = []
            for m in re.finditer(
                r'<meta[^>]+(?:property|name)=["\'](og:image(?::url|:secure_url)?|twitter:image(?::src)?)["\'][^>]+content=["\']([^"\']+)["\']',
                html, re.I,
            ):
                image_candidates.append((m.group(2), "OpenGraph/Twitter image"))
            for m in re.finditer(
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](og:image(?::url|:secure_url)?|twitter:image(?::src)?)["\']',
                html, re.I,
            ):
                image_candidates.append((m.group(1), "OpenGraph/Twitter image"))
            for m in re.finditer(r'<link[^>]+rel=["\']image_src["\'][^>]+href=["\']([^"\']+)["\']', html, re.I):
                image_candidates.append((m.group(1), "Linked page image"))
            # JSON-LD "image": "<url>" or "image": ["<url>", ...] or {"url": "<url>"}
            for m in re.finditer(r'"(?:image|contentUrl|thumbnailUrl)"\s*:\s*"(https?://[^"]+\.(?:jpe?g|png|webp|gif)[^"]*)"', html, re.I):
                image_candidates.append((m.group(1), "Structured-data image"))

            # Highest-resolution variant of the same picture first.
            image_candidates.sort(key=lambda t: _image_res_score(t[0]), reverse=True)
            for img_url, img_label in image_candidates:
                add_media(img_url, "image", img_label, quality="Original")

            # 3. Wistia Embeds
            for wistia in re.finditer(r'(?:wistia\.com/medias/|fast\.wistia\.net/embed/iframe/)([a-zA-Z0-9]+)', html):
                w_id = wistia.group(1)
                try:
                    w_api = f"https://fast.wistia.net/embed/medias/{w_id}.json"
                    async with session.get(w_api, headers=req_headers, timeout=aiohttp.ClientTimeout(total=5)) as w_resp:
                        if w_resp.status == 200:
                            w_data = await w_resp.json()
                            assets = w_data.get("media", {}).get("assets", [])
                            for asset in assets:
                                a_url = asset.get("url")
                                if a_url:
                                    res = f"{asset.get('width', 0)}x{asset.get('height', 0)}"
                                    add_media(a_url, "video", f"Wistia Video ({asset.get('display_name', '')})", quality=res)
                except Exception:
                    pass

            # 4. Vimeo Embeds
            for vimeo in re.finditer(r'player\.vimeo\.com/video/(\d+)', html):
                v_id = vimeo.group(1)
                try:
                    v_config_url = f"https://player.vimeo.com/video/{v_id}/config"
                    async with session.get(v_config_url, headers=req_headers, timeout=aiohttp.ClientTimeout(total=5)) as v_resp:
                        if v_resp.status == 200:
                            v_data = await v_resp.json()
                            # HLS
                            hls_url = v_data.get("request", {}).get("files", {}).get("hls", {}).get("cdns", {})
                            for cdn_info in hls_url.values():
                                if cdn_info.get("url"):
                                    add_media(cdn_info["url"], "hls", f"Vimeo HLS Stream ({v_id})", quality="Adaptive")
                            # Progressive MP4
                            progressive = v_data.get("request", {}).get("files", {}).get("progressive", [])
                            for prog in progressive:
                                if prog.get("url"):
                                    add_media(prog["url"], "video", f"Vimeo MP4 ({prog.get('quality', '')})", quality=prog.get("quality", ""))
                except Exception:
                    pass

            # 5. Regex for Direct Streams (.m3u8, .mpd, .mp4, .webm, .mp3, .pdf)
            stream_regex = r'https?://[^\s\"\'<>]+\.(?:m3u8|mpd|mp4|webm|mp3|flac|aac|m4a|pdf)(?:\?[^\s\"\'<>]*)?'
            for match in re.finditer(stream_regex, html):
                stream_url = match.group(0)
                ext = stream_url.split('?')[0].split('.')[-1].lower()
                kind = "hls" if ext == "m3u8" else ("dash" if ext == "mpd" else ("audio" if ext in ("mp3", "flac", "aac", "m4a") else ("document" if ext == "pdf" else "video")))
                add_media(stream_url, kind, f"Discovered {ext.upper()} Resource")

            # 6. Cloudflare Stream / JWPlayer / VideoJS config searches
            for cf in re.finditer(r'videodelivery\.net/([a-zA-Z0-9]+)/manifest/video\.m3u8', html):
                add_media(f"https://videodelivery.net/{cf.group(1)}/manifest/video.m3u8", "hls", "Cloudflare Stream HLS", quality="Adaptive")

            # 7. Flipsnack Embedded Interactive Videos
            if "flipsnack.com" in page_url:
                try:
                    import base64, urllib.parse, gzip
                    m_hash = re.search(r'[?&]hash=([a-zA-Z0-9%]+)', html)
                    raw_hash = urllib.parse.unquote(m_hash.group(1)) if m_hash else None
                    if not raw_hash:
                        m_acc = re.search(r'accountId["\']?\s*:\s*["\']([a-zA-Z0-9]+)["\']', html)
                        m_flp = re.search(r'flipbookHash["\']?\s*:\s*["\']([a-zA-Z0-9]+)["\']', html)
                        if m_acc and m_flp:
                            raw_hash = base64.b64encode(f"{m_acc.group(1)}+{m_flp.group(1)}".encode()).decode()
                    if raw_hash:
                        dec = base64.b64decode(raw_hash).decode('utf-8')
                        acc_id, flp_hsh = dec.split('+', 1)
                        auth_u = f"https://content-private.flipsnack.com/authorization?hash={raw_hash}&domain=www.flipsnack.com"
                        async with session.get(auth_u, headers=req_headers, timeout=aiohttp.ClientTimeout(total=5)) as a_r:
                            if a_r.status == 200:
                                a_d = await a_r.json()
                                s_sig = a_d.get("signature", {}).get(flp_hsh)
                                if s_sig:
                                    d_u = f"https://d3u72tnj701eui.cloudfront.net/{acc_id}/collections/{flp_hsh}/data.json?{s_sig}"
                                    async with session.get(d_u, headers=req_headers, timeout=aiohttp.ClientTimeout(total=8)) as d_r:
                                        d_bytes = await d_r.read()
                                        try: d_bytes = gzip.decompress(d_bytes)
                                        except: pass
                                        c_data = json.loads(d_bytes.decode('utf-8'))
                                        p_order = c_data.get("pages", {}).get("order", [])
                                        p_dict = c_data.get("pages", {}).get("data", {})
                                        for p_num, pid in enumerate(p_order, 1):
                                            p_obj = p_dict.get(pid, {})
                                            for el in p_obj.get("elements", []):
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
                                                        add_media(v_url, "video", f"Flipsnack Page {p_num} Embedded Video", quality="1080p")
                except Exception:
                    pass

            # 8. Spotify Tracks & Previews
            if "open.spotify.com" in page_url:
                try:
                    from copita.core.spotify_downloader import SpotifyDownloader
                    sp = SpotifyDownloader()
                    loop = asyncio.get_running_loop()
                    s_info = await loop.run_in_executor(None, sp.get_track_info, page_url)
                    if s_info:
                        page_title = f"{s_info['title']} - {s_info['artist']} (Spotify)"
                        if s_info.get("preview_url"):
                            add_media(s_info["preview_url"], "audio", f"{s_info['title']} (Official 320k Preview)", quality="320kbps")
                        if s_info.get("cover_url"):
                            add_media(s_info["cover_url"], "image", f"{s_info['title']} Official Cover Art (640x640)", quality="640x640")
                except Exception:
                    pass

            # 9. Cloud Storage & File Hosters (MediaFire, Gofile, PixelDrain, Mega, Ufile, KrakenFiles, Buzzheavier, Catbox, Sendcm, 1Fichier, Dropbox, GDrive)
            if any(host in page_url for host in ("mediafire.com", "gofile.io", "pixeldrain.com", "mega.nz", "ufile.io", "uploadfiles.io", "krakenfiles.com", "buzzheavier.com", "bzzhr.to", "workupload.com", "catbox.moe", "send.cm", "1fichier.com", "dropbox.com", "drive.google.com", "docs.google.com", "drive.usercontent.google.com")):
                try:
                    from copita.core.filehoster_downloader import FileHosterDownloader
                    f_dl = FileHosterDownloader(page_url)
                    f_info = await f_dl.resolve()
                    if f_info:
                        fname = f_info.get("filename") or "File"
                        page_title = f"{f_info.get('provider', 'File').title()}: {fname}"
                        if f_info.get("children"):
                            for child in f_info["children"]:
                                if not child.get("is_folder") and child.get("url"):
                                    sz_label = f"{child['size'] / (1024 * 1024):.1f}MB" if child.get("size") else "Direct"
                                    add_media(child["url"], child.get("category", "file"), child["name"], quality=sz_label)
                        elif f_info.get("direct_url"):
                            add_media(f_info["direct_url"], "file", f"Direct Download: {fname}", quality="Original")
                except Exception:
                    pass

            # 10. Generic Document/Ebook Links (any site — not tied to a
            # specific host). Many free-file/ebook sites expose the real
            # download as a plain <a href> with no file extension (content
            # negotiated via a query param like ?fmt=pdf), which none of the
            # patterns above catch.
            for url, label in self._extract_generic_document_links(html, page_url):
                ext_m = re.search(r'\.(pdf|epub|mobi|azw3|djvu|fb2|cbz|cbr|docx?|rtf)(?:\?|$)', url, re.I)
                add_media(url, "document" if ext_m else "file", label)

            # Probe media sizes & headers. Pulled out to a helper because it
            # needs to run twice: once on whatever the static HTML scan
            # found, and again on whatever the CDP fallback below adds —
            # see that fallback's trigger condition for why.
            async def probe_candidates(candidates):
                # Probe every candidate CONCURRENTLY. Sequentially, 25 assets
                # each on a 5 s HEAD timeout is a ~2-minute worst case — long
                # enough for the task watchdog to kill the whole download
                # while it still says "verifying…". In parallel the slow ones
                # overlap, and a hard 20 s cap bounds the phase regardless.
                async def probe_one(item):
                    await _probe_one_impl(item)
                    return item

                try:
                    results = await asyncio.wait_for(
                        asyncio.gather(*(probe_one(dict(c)) for c in candidates[:25])),
                        timeout=20.0,
                    )
                except asyncio.TimeoutError:
                    # Keep whatever finished; the rest fall through unprobed.
                    results = candidates[:25]
                return [
                    it for it in results
                    if not (it.get("type") in ("document", "file")
                            and "text/html" in (it.get("content_type") or ""))
                ]

            async def _probe_one_impl(item):
                    try:
                        async with session.head(item["url"], headers=req_headers, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=5)) as h_resp:
                            cl = h_resp.headers.get("Content-Length")
                            item["size"] = int(cl) if cl and cl.isdigit() else None
                            item["status"] = h_resp.status
                            item["content_type"] = h_resp.headers.get("Content-Type", "")
                    except Exception:
                        item["size"] = None
                        item["status"] = None

                    # Generic one-hop landing-page follow: many free-file/ebook
                    # sites route "download" links through an intermediate ad
                    # page before the real file. If a generic (not strongly
                    # confirmed) candidate resolved to HTML instead of a real
                    # file, follow it once and re-run the same generic scan on
                    # what it returns — a common pattern across many such sites,
                    # not specific to any one of them.
                    if item.get("type") in ("document", "file") and "text/html" in (item.get("content_type") or ""):
                        try:
                            async with session.get(item["url"], headers=req_headers, timeout=aiohttp.ClientTimeout(total=8)) as g_resp:
                                landing_html = await g_resp.text(errors="ignore")
                            nested = self._extract_generic_document_links(landing_html, item["url"])
                            nested = [n for n in nested if n[0] != item["url"]]
                            # Prefer a link with a real, confirmed file extension.
                            nested.sort(key=lambda n: 0 if re.search(self._DOC_EXT_PATTERN, n[0], re.I) else 1)
                            if nested:
                                real_url, real_label = nested[0]
                                async with session.head(real_url, headers=req_headers, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=5)) as r_resp:
                                    item["url"] = real_url
                                    item["label"] = f"{item['label']} → {real_label}"
                                    cl = r_resp.headers.get("Content-Length")
                                    item["size"] = int(cl) if cl and cl.isdigit() else None
                                    item["status"] = r_resp.status
                                    item["content_type"] = r_resp.headers.get("Content-Type", "")
                        except Exception:
                            pass

                    # NB: a "document"/"file" candidate that STILL probes as
                    # HTML after the one-hop follow is a nav link, not a real
                    # file — it's dropped by the filter in probe_candidates()
                    # after this returns (can't `continue` out of a coroutine).

            probed_media = await probe_candidates(found_media)

            # 11. Fallback: Headless Browser CDP Network Interception.
            # Triggering this on `not probed_media` (post-probe) rather than
            # `not found_media` (pre-probe) matters a lot in practice: a
            # real-world page's nav/footer often has a handful of links
            # whose path merely contains "/download/" (breadcrumbs, a
            # "Downloads" menu item) — the static scan picks those up as
            # candidates, so `found_media` is non-empty, but every one of
            # them gets discarded above for actually being HTML, not a
            # file. Gating on the pre-probe count meant that common case
            # skipped the headless-browser fallback entirely and reported
            # "no media found" even though a real download button existed
            # and would only render via JavaScript — caught live testing a
            # batch of real software-release pages, where this was the
            # dominant failure mode (dozens of legitimate download pages,
            # not exotic edge cases).
            saw_websocket_stream = False
            if not probed_media:
                self._notify("No direct media in the page's HTML — opening a headless browser to watch what it actually loads (up to ~10s)…")
                try:
                    saw_websocket_stream = await asyncio.wait_for(
                        self._sniff_browser_cdp(page_url, add_media), timeout=30)
                except (asyncio.TimeoutError, Exception):
                    saw_websocket_stream = False
                # add_media() (passed in above) appends straight into the
                # same found_media list, so re-probing it picks up whatever
                # CDP just found — re-checking the original candidates too
                # is a bounded, harmless bit of redundant work, not a
                # correctness issue.
                probed_media = await probe_candidates(found_media)

            # Rank the confirmed candidates so the caller's `media[0]` is the
            # thing the user actually pasted the page for. A real audio/video
            # stream outranks the page's OpenGraph thumbnail every time — an
            # audiobook page has both, and picking the 41 KB cover image over
            # the 14 MB .mp3 is the classic "it downloaded but it's wrong"
            # bug. Stable sort keeps discovery order within a tier.
            _TYPE_RANK = {"hls": 0, "dash": 0, "video": 0, "audio": 0,
                          "document": 2, "image": 3}

            def _rank(item):
                r = _TYPE_RANK.get(item.get("type"), 1)
                # A tiny "media" file (a UI blip, a 1x1 tracker, a redirect
                # stub) is almost never the real download — push it below
                # a real one of the same type.
                sz = item.get("size") or 0
                small = 1 if (r == 0 and 0 < sz < 100_000) else 0
                return (r, small)

            probed_media = sorted(probed_media, key=_rank)

            # "The page promises a file but all we found is its artwork."
            # A download page for `Some Book.epub` that's behind a captcha /
            # ad-gate the scan couldn't pass leaves us with nothing but the
            # page's og:image / cover thumbnail — and downloading a 40 KB PNG
            # named after an ebook is the worst kind of silent wrong result.
            # If the page's own title/description names a concrete file with
            # a real media/document/archive extension and every candidate we
            # have is just an image, that's a gate we lost to, not the file.
            # (A genuine image page — pixabay, a reddit i.redd.it post — names
            # no such file, so this doesn't touch it.)
            promised = re.search(
                r'([\w()\[\]. ,+\-\'’&!#]{1,120}?\.(?:epub|pdf|mobi|azw3?|djvu|'
                r'fb2|cbz|cbr|zip|rar|7z|tar\.\w+|tgz|iso|dmg|exe|msi|pkg|apk|'
                r'mp4|mkv|avi|mov|webm|m4v|mp3|flac|m4a|m4b|wav|aac|ogg))\b',
                f"{page_title}\n{self._cdp_page_text}\n{html[:8000]}", re.I,
            )
            if (promised and probed_media
                    and all(m.get("type") == "image" for m in probed_media)):
                # Trim a leading "Download " / "Get " verb the title often
                # prefixes the filename with.
                fname = re.sub(r'^(?:download|get|free download)\s+', '',
                               promised.group(1).strip(), flags=re.I)
                return {
                    "page_url": page_url,
                    "title": page_title,
                    "media_count": 0,
                    "media": [],
                    "cookies": "",
                    "note": (
                        f"This is a download page for “{fname}”, but the "
                        f"actual file is behind a captcha or ad-gate that Copita "
                        f"couldn't get through on its own. Open the page in your "
                        f"browser and click download with the Copita extension "
                        f"active — it'll catch the real file."
                    ),
                }

            self._notify(f"Found {len(probed_media)} candidate(s) — verifying…" if probed_media else "No downloadable media found.")

            # Some sites (Cloudflare-fronted gates especially) mint a
            # download token tied to the session's cookies established while
            # browsing the page — a fresh, cookie-less request to that same
            # URL gets rejected. Carrying these forward lets the actual
            # download reuse the session that discovered the link, which is
            # universally useful (not specific to any one site).
            cookie_header = "; ".join(f"{c.key}={c.value}" for c in session.cookie_jar) if session.cookie_jar else ""

            result = {
                "page_url": page_url,
                "title": page_title,
                "media_count": len(probed_media),
                "media": probed_media,
                "cookies": cookie_header
            }
            if not probed_media and saw_websocket_stream:
                # A real, specific reason beats the generic "no media found"
                # — this page streams over a live WebSocket connection,
                # which has no re-fetchable URL to hand to a downloader
                # engine, rather than genuinely having no media at all.
                result["note"] = (
                    "This page streams media over a live WebSocket connection, "
                    "not a re-fetchable URL — direct download of WebSocket-"
                    "delivered streams isn't supported."
                )
            return result

    _DOC_EXT_PATTERN = r'\.(pdf|epub|mobi|azw3|djvu|fb2|cbz|cbr|docx?|rtf)(?:\?[^"\'<>\s]*)?(?:["\'\s]|$)'
    # Real software-release pages (kernel.org, SourceForge project pages,
    # etc.) link straight to installer/archive files just as often as
    # ebook sites link to documents — the generic scanner only recognized
    # the latter, missing the exact kind of link a page like kernel.org
    # puts directly in its static HTML with no JS involved at all.
    _ARCHIVE_EXT_PATTERN = r'\.(zip|7z|rar|tar\.gz|tar\.xz|tar\.bz2|tgz|txz|gz|xz|bz2|exe|msi|dmg|pkg|deb|rpm|appimage)(?:\?[^"\'<>\s]*)?(?:["\'\s]|$)'
    # SourceForge (and occasionally others) end a real download link in a
    # bare "/download" with no trailing slash and nothing after it
    # (".../qbittorrent_5.2.3_x64_setup.exe/download") — the original
    # pattern required a slash on *both* sides of "download" and silently
    # missed this extremely common convention entirely.
    _DOWNLOAD_INTENT_PATTERN = r'(?:[?&](?:fmt|format|dl|download)=[a-z0-9]+|/download/|/dl/|[?&]attachment|/download(?:[?#]|$))'
    _SKIP_EXT_PATTERN = r'\.(js|css|png|jpe?g|svg|gif|ico|webp|woff2?|ttf|json)(?:\?|$)'

    @classmethod
    def _extract_generic_document_links(cls, html: str, page_url: str) -> List[tuple]:
        """Finds <a href> links that look like a document/ebook/file download
        on ANY site — by real file extension, or by common "download intent"
        query patterns (?fmt=pdf, /download/, ?dl=1) used by sites that
        content-negotiate the format rather than exposing it in the URL
        path. Deliberately not tied to any specific host."""
        results = []
        seen = set()
        for a in re.finditer(r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.I | re.S):
            href, inner = a.group(1), a.group(2)
            if re.search(cls._SKIP_EXT_PATTERN, href, re.I):
                continue
            has_doc_ext = re.search(cls._DOC_EXT_PATTERN, href, re.I)
            has_archive_ext = re.search(cls._ARCHIVE_EXT_PATTERN, href, re.I)
            has_intent = re.search(cls._DOWNLOAD_INTENT_PATTERN, href, re.I)
            if not (has_doc_ext or has_archive_ext or has_intent):
                continue
            clean = urljoin(page_url, href.strip())
            if clean in seen:
                continue
            seen.add(clean)
            text = re.sub(r'<[^>]+>', ' ', inner).strip()
            ext = has_doc_ext.group(1).upper() if has_doc_ext else (has_archive_ext.group(1).upper() if has_archive_ext else None)
            if has_doc_ext:
                label = f"Ebook/Document Link (.{ext.lower()})"
            elif has_archive_ext:
                label = f"Archive/Installer Link (.{ext.lower()})"
            else:
                label = f"Download Link: {text[:40]}" if text else "Possible Download Link"
            results.append((clean, label))
        return results

    async def _sniff_browser_cdp(self, page_url: str, add_media_fn: Any) -> bool:
        """Uses headless Chrome/Brave CDP to capture dynamically loaded media
        streams. Returns True if a live WebSocket-delivered stream was
        observed (informational — such streams can't be turned into a
        downloadable candidate, but the caller can at least report why)."""
        try:
            from copita.core.flipbook_downloader import find_browser_binary, find_free_port
            browser_bin = find_browser_binary()
            if not browser_bin:
                return False
            saw_ws_stream = False

            port = find_free_port()
            import tempfile, subprocess
            user_data = tempfile.mkdtemp(prefix="copita_sniff_")
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
            try:
                await asyncio.sleep(1.2)
                async with aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=10)) as session:
                    async with session.put(f"http://127.0.0.1:{port}/json/new") as r:
                        tab = await r.json()
                        ws_url = tab["webSocketDebuggerUrl"]

                    async with session.ws_connect(ws_url, timeout=10.0) as ws:
                        await ws.send_json({"id": 1, "method": "Network.enable"})
                        await ws.send_json({"id": 2, "method": "Page.navigate", "params": {"url": page_url}})

                        # MIME types that mark a response as real media/manifest
                        # even when the URL itself has no recognizable extension
                        # — very common on tokenized/signed CDN URLs
                        # (?token=..., a hashed path, no trailing .mp4/.m3u8 at
                        # all). Matching on Content-Type instead of just the
                        # URL string catches those; matching by extension alone
                        # (the old behavior) silently missed every one of them.
                        _MIME_MEDIA = {
                            "application/vnd.apple.mpegurl": ("hls", "Discovered HLS Stream (by Content-Type)"),
                            "application/x-mpegurl": ("hls", "Discovered HLS Stream (by Content-Type)"),
                            "application/dash+xml": ("dash", "Discovered DASH Manifest (by Content-Type)"),
                            "video/mp4": ("video", "Discovered Video Stream (by Content-Type)"),
                            "video/webm": ("video", "Discovered Video Stream (by Content-Type)"),
                            "audio/mp4": ("audio", "Discovered Audio Stream (by Content-Type)"),
                            "audio/mpeg": ("audio", "Discovered Audio Stream (by Content-Type)"),
                            "audio/aac": ("audio", "Discovered Audio Stream (by Content-Type)"),
                        }

                        start_t = time.time()
                        while True:
                            remaining = 7.0 - (time.time() - start_t)
                            if remaining <= 0:
                                break
                            try:
                                msg = await asyncio.wait_for(ws.receive_json(), timeout=min(1.5, remaining))
                            except asyncio.TimeoutError:
                                # No message in this slice, but the 7s window
                                # isn't up yet — a slow-starting page (loads,
                                # then buffers video a beat later) still gets
                                # its later requests observed instead of the
                                # scan ending on the first quiet moment.
                                continue

                            method = msg.get("method")
                            if method == "Network.requestWillBeSent":
                                req = msg.get("params", {}).get("request", {})
                                u = req.get("url", "")
                                if ".m3u8" in u:
                                    quality = "1080p" if "/1080/" in u else ("720p" if "/720/" in u else ("480p" if "/480/" in u else ("360p" if "/360/" in u else "Adaptive")))
                                    add_media_fn(u, "hls", f"Decrypted HLS Stream ({quality})", quality=quality)
                                elif ".mpd" in u:
                                    add_media_fn(u, "dash", "Discovered DASH Manifest", quality="Adaptive")
                                elif any(u.split('?')[0].endswith(ext) for ext in [".mp4", ".webm", ".mkv"]):
                                    add_media_fn(u, "video", "Discovered Video Stream", quality="HD")
                                elif any(u.split('?')[0].endswith(ext) for ext in [".mp3", ".m4a", ".aac"]):
                                    add_media_fn(u, "audio", "Discovered Audio Stream", quality="Original")
                            elif method == "Network.responseReceived":
                                resp = msg.get("params", {}).get("response", {})
                                u = resp.get("url", "")
                                mime = (resp.get("mimeType") or "").lower().split(";")[0].strip()
                                match = _MIME_MEDIA.get(mime)
                                if match and u:
                                    kind, label = match
                                    add_media_fn(u, kind, label, quality="Adaptive" if kind in ("hls", "dash") else "Original")
                            elif method == "Network.webSocketCreated" and not saw_ws_stream:
                                # Can't turn a WebSocket connection into a
                                # re-fetchable download URL — but silently
                                # reporting "no media found" here would be a
                                # worse failure than an honest one naming what
                                # was actually seen.
                                saw_ws_stream = True

                        # Passive network sniffing only ever catches
                        # resources the page requests on its own (an
                        # autoplaying video, an XHR call) — it can't see a
                        # plain "click here to download" link, since a
                        # link's href is never actually requested unless
                        # something clicks it. That's exactly how GitHub
                        # Releases (and many other sites) work: the asset
                        # list is rendered into the DOM by JavaScript after
                        # load, but nothing about it fires a network
                        # request on its own. Querying the rendered DOM
                        # directly for real download-looking hrefs catches
                        # this whole category instead of relying on the
                        # page to volunteer a request for it.
                        try:
                            eval_id = 9999
                            await ws.send_json({
                                "id": eval_id,
                                "method": "Runtime.evaluate",
                                "params": {
                                    "expression": "Array.from(document.querySelectorAll('a[href]')).map(a => a.href).join('\\n')",
                                    "returnByValue": True
                                }
                            })
                            dom_deadline = time.time() + 3.0
                            while time.time() < dom_deadline:
                                try:
                                    msg = await asyncio.wait_for(ws.receive_json(), timeout=1.5)
                                except asyncio.TimeoutError:
                                    break
                                if msg.get("id") == eval_id:
                                    value = msg.get("result", {}).get("result", {}).get("value") or ""
                                    for href in value.split("\n"):
                                        href = href.strip()
                                        ext_m = re.search(
                                            r'\.(pdf|epub|mobi|docx?|zip|7z|rar|tar\.gz|tar\.xz|tar\.bz2|tgz|txz|exe|msi|dmg|pkg|deb|rpm|appimage)(?:\?|$)',
                                            href, re.I
                                        )
                                        if href and ext_m:
                                            add_media_fn(href, "file", f"Discovered Download Link (.{ext_m.group(1).lower()})", quality="Unknown")
                                    break

                            # The page's primary image, as the rendered DOM
                            # sees it: og:image meta first (that's what a
                            # browser's share preview / right-click uses),
                            # then the single biggest <img> actually on
                            # screen. Skips the icon/avatar/sprite noise that
                            # a blanket "every image response" network filter
                            # would pull in.
                            img_eval_id = 9998
                            await ws.send_json({
                                "id": img_eval_id,
                                "method": "Runtime.evaluate",
                                "params": {
                                    "expression": (
                                        "(() => {"
                                        "  const og = document.querySelector('meta[property=\"og:image\"],meta[name=\"twitter:image\"]');"
                                        "  let biggest = '', area = 0;"
                                        "  for (const im of document.images) {"
                                        "    const a = (im.naturalWidth||0) * (im.naturalHeight||0);"
                                        "    if (a > area && im.currentSrc) { area = a; biggest = im.currentSrc; }"
                                        "  }"
                                        "  const ot = document.querySelector('meta[property=\"og:title\"]');"
                                        "  const od = document.querySelector('meta[property=\"og:description\"],meta[name=\"description\"]');"
                                        "  return JSON.stringify({og: og ? og.content : '', big: area > 40000 ? biggest : '',"
                                        "    txt: (document.title||'') + ' ' + (ot?ot.content:'') + ' ' + (od?od.content:'')});"
                                        "})()"
                                    ),
                                    "returnByValue": True,
                                }
                            })
                            img_deadline = time.time() + 3.0
                            while time.time() < img_deadline:
                                try:
                                    msg = await asyncio.wait_for(ws.receive_json(), timeout=1.5)
                                except asyncio.TimeoutError:
                                    break
                                if msg.get("id") == img_eval_id:
                                    try:
                                        payload = json.loads(msg.get("result", {}).get("result", {}).get("value") or "{}")
                                    except Exception:
                                        payload = {}
                                    if payload.get("txt"):
                                        self._cdp_page_text = payload["txt"]
                                    imgs = []
                                    if payload.get("og"):
                                        imgs.append((payload["og"], "Page image (og:image)"))
                                    if payload.get("big") and payload["big"] != payload.get("og"):
                                        imgs.append((payload["big"], "Largest image on the page"))
                                    imgs.sort(key=lambda t: _image_res_score(t[0]), reverse=True)
                                    for iu, il in imgs:
                                        add_media_fn(iu, "image", il, quality="Original")
                                    break
                        except Exception:
                            pass
            finally:
                proc.kill()
        except Exception:
            return False
        return saw_ws_stream

