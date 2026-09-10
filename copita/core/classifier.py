"""
URL and Input Classifier for Copita.
Detects content type, protocol, and the best download engine to use.
"""

import os
import re
from urllib.parse import urlparse, unquote_plus, parse_qs
from typing import Dict, Any

class LinkType:
    MAGNET = "magnet"
    DIRECT_FILE = "direct_file"
    HLS_STREAM = "hls_stream"
    DASH_STREAM = "dash_stream"
    SCRIBD_DOC = "scribd_doc"
    FLIPBOOK = "flipbook"
    DEEP_ZOOM = "deep_zoom"
    MODEL_3D = "model_3d"
    SPOTIFY = "spotify"
    FILE_HOSTER = "file_hoster"
    MANGA = "manga"
    VIDEO_EMBED = "video_embed"
    MEDIA_PLATFORM = "media_platform"
    GENERIC_WEBPAGE = "generic_webpage"

FILE_EXTENSIONS = {
    # Video
    'mp4', 'mkv', 'webm', 'avi', 'mov', 'flv', 'wmv', 'm4v', 'ts',
    # Audio
    'mp3', 'm4a', 'flac', 'wav', 'aac', 'ogg', 'opus', 'wma',
    # Documents
    'pdf', 'epub', 'mobi', 'docx', 'doc', 'pptx', 'xlsx', 'txt', 'rtf',
    # Archives / Images / Binaries
    'zip', 'tar', 'gz', 'bz2', 'xz', '7z', 'rar', 'iso', 'dmg', 'pkg',
    'jpg', 'jpeg', 'png', 'gif', 'webp', 'svg', 'bin', 'exe'
}

MANGA_PATTERNS = [
    (r'(?:mangaplus\.shueisha\.co\.jp)', 'mangaplus', 'MangaPlus by Shueisha'),
    (r'(?:globalcomix\.com)', 'globalcomix', 'GlobalComix'),
    (r'(?:comic-walker\.com)', 'kadocomi', 'KADOCOMI / Comic-Walker'),
    (r'(?:webtoons\.com)', 'webtoons', 'WEBTOON Comic'),
    (r'(?:mangadex\.org)', 'mangadex', 'MangaDex'),
    (r'(?:tapas\.io)', 'tapas', 'Tapas Webcomic'),
    (r'(?:namicomi\.com)', 'namicomi', 'NamiComi'),
    (r'(?:mangaplaza\.com)', 'mangaplaza', 'MangaPlaza'),
    (r'(?:inkr\.com)', 'inkr', 'INKR Comics'),
    (r'(?:hiveworkscomics\.com)', 'hiveworks', 'Hiveworks Comics'),
    (r'(?:voyce\.me)', 'voyce', 'VoyceMe'),
    (r'(?:comix\.to)', 'comix', 'Comix'),
]

FLIPBOOK_PATTERNS = [
    (r'(?:issuu\.com)', 'issuu', 'Issuu Publication'),
    (r'(?:flipsnack\.com)', 'flipsnack', 'Flipsnack Flipbook'),
    (r'(?:fliphtml5\.com)', 'fliphtml5', 'FlipHTML5 Book'),
    (r'(?:anyflip\.com)', 'anyflip', 'AnyFlip Book'),
    (r'(?:calameo\.com)', 'calameo', 'Calaméo Publication'),
    (r'(?:yumpu\.com)', 'yumpu', 'YUMPU Document'),
    (r'(?:heyzine\.com)', 'heyzine', 'Heyzine Flipbook'),
    (r'(?:pubhtml5\.com)', 'pubhtml5', 'PubHTML5 Book'),
]

FILEHOSTER_PATTERNS = [
    (r'(?:mediafire\.com)', 'mediafire', 'MediaFire File'),
    (r'(?:mega\.nz|mega\.co\.nz)', 'mega', 'MEGA Cloud File'),
    (r'(?:gofile\.io)', 'gofile', 'Gofile Direct File'),
    (r'(?:pixeldrain\.com)', 'pixeldrain', 'PixelDrain File'),
    (r'(?:ufile\.io|uploadfiles\.io)', 'ufile', 'Ufile File'),
    (r'(?:krakenfiles\.com)', 'krakenfiles', 'KrakenFiles File'),
    (r'(?:buzzheavier\.com|bzzhr\.to)', 'buzzheavier', 'Buzzheavier File'),
    (r'(?:workupload\.com)', 'workupload', 'Workupload File'),
    (r'(?:catbox\.moe|files\.catbox\.moe|litterbox\.catbox\.moe)', 'catbox', 'Catbox File'),
    (r'(?:send\.cm)', 'sendcm', 'Send.cm File'),
    (r'(?:1fichier\.com)', 'fichier', '1Fichier File'),
    (r'(?:dropbox\.com)', 'dropbox', 'Dropbox File'),
    (r'(?:drive\.google\.com|docs\.google\.com|drive\.usercontent\.google\.com)', 'gdrive', 'Google Drive File'),
]


PLATFORM_PATTERNS = [
    (r'(?:youtube\.com|youtu\.be)', 'YouTube'),
    (r'(?:tiktok\.com)', 'TikTok'),
    (r'(?:twitter\.com|x\.com)', 'Twitter / X'),
    (r'(?:facebook\.com|fb\.watch)', 'Facebook'),
    (r'(?:instagram\.com)', 'Instagram'),
    (r'(?:reddit\.com|redd\.it)', 'Reddit'),
    (r'(?:bilibili\.com)', 'Bilibili'),
    (r'(?:twitch\.tv)', 'Twitch'),
    (r'(?:soundcloud\.com)', 'SoundCloud'),
    (r'(?:vimeo\.com)', 'Vimeo'),
    (r'(?:loom\.com)', 'Loom'),
    (r'(?:dailymotion\.com)', 'Dailymotion'),
    (r'(?:bandcamp\.com)', 'Bandcamp'),
    (r'(?:pinterest\.com|pin\.it)', 'Pinterest'),
]

VIDEO_EMBED_PATTERNS = [
    (r'(?:viduki\.net)', 'viduki', 'Viduki Movie/TV Stream'),
    (r'(?:vidsrc|vidplay|2embed|superembed)', 'video_embed', 'Embedded Video Stream'),
]


def classify_url(url: str) -> Dict[str, Any]:
    url = url.strip()
    if url.startswith("local://"):
        fname = url[8:]
        ext = fname.split('.')[-1].lower() if '.' in fname else ''
        cat = "document" if ext == "pdf" else ("audio" if ext in ("mp3", "m4a", "wav") else ("video" if ext in ("mp4", "mkv") else ("image" if ext in ("jpg", "jpeg", "png", "webp") else "file")))
        return {
            "type": LinkType.DIRECT_FILE,
            "engine": "local",
            "name": fname,
            "url": url,
            "category": cat
        }

    if url.startswith("magnet:?"):
        name_match = re.search(r'dn=([^&]+)', url)
        name = unquote_plus(name_match.group(1)) if name_match else "Magnet Torrent"
        return {
            "type": LinkType.MAGNET,
            "engine": "torrent",
            "name": name,
            "url": url,
            "category": "torrent"
        }

    if url.split('?')[0].lower().endswith('.torrent'):
        fname = os.path.basename(urlparse(url).path) or "download.torrent"
        return {
            "type": LinkType.MAGNET,
            "engine": "torrent",
            "name": fname,
            "url": url,
            "category": "torrent"
        }

    try:
        parsed = urlparse(url)
        netloc = parsed.netloc.lower()
        path = parsed.path.lower()
    except Exception:
        netloc = ""
        path = url.lower()
        parsed = None

    # Telegram: media in chats/channels is delivered over MTProto (chunked,
    # encrypted, assembled in the client) — a t.me / web.telegram.org link
    # has no fetchable media URL, only a thumbnail in its OpenGraph tags.
    # Scraping that just saves a 16KB channel avatar and calls it done, so
    # bail out with a clear pointer to the extension flow instead.
    if netloc in ("t.me", "telegram.me", "telegram.dog") or netloc.endswith(".t.me") \
            or "web.telegram.org" in netloc or netloc in ("webk.telegram.org", "webz.telegram.org"):
        return {
            "type": LinkType.GENERIC_WEBPAGE,
            "engine": "telegram_link",
            "name": "Telegram Link",
            "url": url,
            "category": "webpage",
        }

    # Reddit's fullscreen image viewer wraps a direct CDN image URL as a
    # query param: reddit.com/media?url=<url-encoded i.redd.it/... .jpg>.
    # yt-dlp (where a bare reddit.com URL is otherwise routed) has no image
    # extractor and errors out on this shape — unwrap it to the real image.
    if parsed and 'reddit.com' in netloc and path.rstrip('/') == '/media':
        inner = parse_qs(parsed.query).get('url', [None])[0]
        if inner:
            return classify_url(unquote_plus(inner))

    # Scratch project — rebuild the .sb3 from the public API
    _scratch = re.search(r'scratch\.mit\.edu/projects/(\d+)', url)
    if _scratch:
        return {
            "type": LinkType.GENERIC_WEBPAGE,
            "engine": "scratch",
            "name": f"Scratch Project {_scratch.group(1)}",
            "url": url,
            "category": "file",
        }

    # Scribd document
    if 'scribd.com/document/' in url:
        doc_id_match = re.search(r'document/(\d+)', url)
        doc_id = doc_id_match.group(1) if doc_id_match else None
        return {
            "type": LinkType.SCRIBD_DOC,
            "engine": "scribd",
            "name": f"Scribd Document {doc_id}",
            "doc_id": doc_id,
            "url": url,
            "category": "document"
        }

    # Flipbook & Protected Publication Viewers
    for domain_regex, provider, display_name in FLIPBOOK_PATTERNS:
        if re.search(domain_regex, netloc):
            slug = [p for p in path.split('/') if p][-1] if path.strip('/') else provider
            clean_name = re.sub(r'[\-_]', ' ', slug).title()
            return {
                "type": LinkType.FLIPBOOK,
                "engine": "flipbook",
                "provider": provider,
                "name": f"{display_name}: {clean_name}" if slug != provider else display_name,
                "url": url,
                "category": "document"
            }

    # Google Arts & Culture Deep-Zoom
    if 'artsandculture.google.com' in netloc:
        slug = [p for p in path.split('/') if p][-1] if path.strip('/') else "Artwork"
        clean_name = re.sub(r'[\-_]', ' ', slug).title()
        return {
            "type": LinkType.DEEP_ZOOM,
            "engine": "deep_zoom",
            "provider": "google_arts",
            "name": f"Arts & Culture: {clean_name}",
            "url": url,
            "category": "image"
        }

    # Sketchfab 3D Models
    if 'sketchfab.com' in netloc:
        slug = [p for p in path.split('/') if p][-1] if path.strip('/') else "3D Model"
        clean_name = re.sub(r'[\-_]', ' ', slug).title()
        return {
            "type": LinkType.MODEL_3D,
            "engine": "sketchfab_3d",
            "provider": "sketchfab",
            "name": f"Sketchfab 3D: {clean_name}",
            "url": url,
            "category": "model_3d"
        }

    # Spotify: tracks, podcast episodes, albums, playlists
    if 'open.spotify.com' in netloc or 'spotify.link' in netloc:
        kind_m = re.search(r'/(track|episode|album|playlist|show|audiobook|chapter)/', path)
        kind = kind_m.group(1) if kind_m else None
        name = {
            "track": "Spotify Track",
            "episode": "Spotify Podcast Episode",
            "album": "Spotify Album",
            "playlist": "Spotify Playlist",
            "show": "Spotify Podcast",
            "audiobook": "Spotify Audiobook",
            "chapter": "Spotify Audiobook Chapter",
        }.get(kind, "Spotify Audio")
        return {
            "type": LinkType.SPOTIFY,
            "engine": "spotify",
            "provider": "spotify",
            "spotify_kind": kind,
            "name": name,
            "url": url,
            "category": "audio"
        }

    # Cloud Storage & File Hosters (MediaFire, Mega, Gofile, PixelDrain, Ufile)
    for domain_regex, provider, display_name in FILEHOSTER_PATTERNS:
        if re.search(domain_regex, netloc):
            if provider == "gdrive":
                # A bare drive.google.com URL (the account's Drive home,
                # "My Drive", search results, etc.) has no file/folder ID
                # at all — matching it here anyway produced a task titled
                # generically "Google Drive File" that was doomed to fail
                # with a raw "Could not extract Google Drive file ID"
                # error. Only treat it as a real Drive link when an actual
                # ID pattern is present; otherwise fall through so it's
                # handled as a generic webpage instead of a fake file task.
                query = parsed.query if parsed else ""
                has_drive_id = bool(re.search(r'/file/d/|/folders/|/d/[\w-]{10,}|[?&]id=', path + "?" + query))
                if not has_drive_id:
                    continue
            fn_candidate = [p for p in path.split('/') if p and '.' in p and p != 'file']
            name = fn_candidate[0] if fn_candidate else display_name
            cat = "archive" if any(ext in name.lower() for ext in (".zip", ".rar", ".7z", ".tar", ".gz")) else "file"
            if provider == "gdrive":
                if "folder" in path:
                    name = "Google Drive Folder"
                    cat = "archive"
                elif "document" in path:
                    name = "Google Docs Document"
                    cat = "document"
                elif "spreadsheets" in path:
                    name = "Google Sheets Spreadsheet"
                    cat = "document"
                elif "presentation" in path:
                    name = "Google Slides Presentation"
                    cat = "document"
            return {
                "type": LinkType.FILE_HOSTER,
                "engine": "file_hoster",
                "provider": provider,
                "name": name,
                "url": url,
                "category": cat
            }

    # Manga & Webcomics (MANGA Plus, GlobalComix, KADOCOMI, WEBTOON, MangaDex, Tapas, etc.)
    for domain_regex, provider, display_name in MANGA_PATTERNS:
        if re.search(domain_regex, netloc):
            slug = [p for p in path.split('/') if p][-1] if path.strip('/') else provider
            clean_name = re.sub(r'[\-_]', ' ', slug).title()
            if provider == "comix":
                ch_m = re.search(r'/title/([^/]+)/(\d+)-chapter-([0-9\.]+)', path)
                if ch_m:
                    manga_slug, _, ch_num = ch_m.groups()
                    clean_title = re.sub(r'^[a-z0-9]+-', '', manga_slug).replace('-', ' ').title()
                    clean_name = f"{clean_title} - Chapter {ch_num}"
                else:
                    t_m = re.search(r'/title/([^/]+)', path)
                    if t_m:
                        clean_title = re.sub(r'^[a-z0-9]+-', '', t_m.group(1)).replace('-', ' ').title()
                        clean_name = clean_title
            return {
                "type": LinkType.MANGA,
                "engine": "manga",
                "provider": provider,
                "name": f"{display_name}: {clean_name}" if slug != provider else display_name,
                "url": url,
                "category": "document"
            }

    # Protected Video & Movie Embed Streams (Viduki, VidSrc, 2Embed, etc.)
    for domain_regex, provider, display_name in VIDEO_EMBED_PATTERNS:
        if re.search(domain_regex, netloc):
            m_id = re.search(r'/movie/(\d+)', path) or re.search(r'/tv/(\d+)', path)
            name = f"Movie/TV Stream ({m_id.group(1)})" if m_id else display_name
            return {
                "type": LinkType.VIDEO_EMBED,
                "engine": "video_embed",
                "provider": provider,
                "name": name,
                "url": url,
                "category": "video"
            }

    # HLS / M3U8. A loose "hls"/"m3u8" substring match also fires on player
    # demo pages that merely reference a manifest in a query param (e.g.
    # bitmovin.com/demos/test-stream/?manifest=<url-encoded .m3u8>) — fetching
    # the demo page itself as if it were the raw playlist gets rejected by
    # the site (it's an HTML page, not a stream). Only match a real manifest
    # URL (path itself ends in .m3u8), and when a query param's decoded
    # value is itself a .m3u8 URL, download THAT instead of the page.
    embedded_manifest = None
    if parsed and parsed.query:
        for values in parse_qs(parsed.query).values():
            for v in values:
                if v.split('?')[0].split('#')[0].lower().endswith('.m3u8'):
                    embedded_manifest = v
                    break
            if embedded_manifest:
                break

    if path.endswith('.m3u8') or embedded_manifest:
        real_url = embedded_manifest or url
        real_path = urlparse(real_url).path if embedded_manifest else path
        return {
            "type": LinkType.HLS_STREAM,
            "engine": "turbo_hls",
            "name": real_path.split('/')[-1] or "HLS Stream",
            "url": real_url,
            "category": "video"
        }

    # DASH / MPD — yt-dlp's generic extractor downloads DASH manifests
    # (every representation, all fragments, merged + remuxed) far more
    # robustly than a hand-rolled parser would.
    if path.endswith('.mpd') or '.mpd?' in path or '.mpd#' in path:
        return {
            "type": LinkType.DASH_STREAM,
            "engine": "ytdlp",
            "name": (path.split('/')[-1].split('.mpd')[0] or "DASH") + " (DASH Stream)",
            "url": url,
            "category": "video"
        }

    # Direct File by Extension
    ext = path.split('.')[-1] if '.' in path else ''
    if ext in FILE_EXTENSIONS:
        cat = "video" if ext in {'mp4', 'mkv', 'webm', 'mov', 'avi'} else (
              "audio" if ext in {'mp3', 'm4a', 'flac', 'wav', 'opus'} else (
              "document" if ext in {'pdf', 'epub', 'docx', 'txt'} else (
              "image" if ext in {'jpg', 'jpeg', 'png', 'gif', 'webp', 'svg'} else "file"
        )))
        filename = path.split('/')[-1]
        return {
            "type": LinkType.DIRECT_FILE,
            "engine": "turbo_chunk",
            "name": filename,
            "extension": ext,
            "url": url,
            "category": cat
        }

    # Known Media Platforms
    for pattern, platform_name in PLATFORM_PATTERNS:
        if re.search(pattern, netloc):
            return {
                "type": LinkType.MEDIA_PLATFORM,
                "engine": "ytdlp",
                "platform": platform_name,
                "name": f"{platform_name} Media",
                "url": url,
                "category": "video"
            }

    # Generic webpage: Can be analyzed with deep sniffer
    return {
        "type": LinkType.GENERIC_WEBPAGE,
        "engine": "sniffer_or_ytdlp",
        "name": netloc or "Webpage",
        "url": url,
        "category": "webpage"
    }
