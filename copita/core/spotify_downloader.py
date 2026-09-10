"""
Spotify metadata-matched audio downloader for Copita.

Full Spotify streaming audio is protected with Widevine DRM on Spotify's
servers and is deliberately NOT circumvented here. Instead this downloader
uses the metadata-matching approach:

1. Read authentic metadata (title, artist / show, duration, cover art, and
   Spotify's own public preview) from the open embed API — no login.
2. Find the same recording on a public source (YouTube / SoundCloud),
   duration- and title-matched.
3. Download that audio and burn the real Spotify title/artist + cover art
   in with ffmpeg.

Handles individual tracks and podcast episodes. Albums and playlists are
expanded into one task per track upstream in TaskManager (see
`get_collection`); this module is only asked to fetch a single item.
"""

import os
import re
import ssl
import json
import logging
import difflib
import tempfile
import subprocess
import urllib.request
from typing import Dict, Any, Optional, Callable, List

logger = logging.getLogger("copita.spotify")

# open.spotify.com/track/ID, open.spotify.com/intl-de/episode/ID,
# spotify:playlist:ID, ...
_SPOTIFY_URL_RE = re.compile(
    r'(?:open\.spotify\.com/(?:intl-[a-z]+/)?|spotify:)'
    r'(track|episode|album|playlist|show|audiobook|chapter)[/:]([a-zA-Z0-9]+)'
)


def parse_spotify_url(url: str):
    """Returns (kind, id) or (None, None). kind is one of
    track / episode / album / playlist / show / audiobook / chapter."""
    m = _SPOTIFY_URL_RE.search(url or "")
    return (m.group(1), m.group(2)) if m else (None, None)


def _similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()


def _parse_hhmmss(text: str) -> int:
    """'1:02:03' / '12:34' / '743' -> seconds. 0 if unparseable."""
    text = (text or "").strip()
    if text.isdigit():
        return int(text)
    parts = text.split(":")
    try:
        parts = [int(p) for p in parts]
    except ValueError:
        return 0
    secs = 0
    for p in parts:
        secs = secs * 60 + p
    return secs


class SpotifyDownloader:
    """Extracts and downloads Spotify tracks and podcast episodes with real
    metadata and cover art via public metadata-matched sources."""

    def __init__(self, download_dir: str = "downloads"):
        self.download_dir = download_dir
        os.makedirs(self.download_dir, exist_ok=True)
        self._ssl_ctx = ssl.create_default_context()
        self._ssl_ctx.check_hostname = False
        self._ssl_ctx.verify_mode = ssl.CERT_NONE

    # ------------------------------------------------------------------ embed

    def _fetch_embed_entity(self, kind: str, item_id: str) -> Optional[Dict[str, Any]]:
        embed_url = f"https://open.spotify.com/embed/{kind}/{item_id}"
        req = urllib.request.Request(embed_url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        try:
            with urllib.request.urlopen(req, context=self._ssl_ctx, timeout=15) as resp:
                html = resp.read().decode("utf-8", errors="ignore")
        except Exception as e:
            logger.error(f"Failed to fetch Spotify embed page: {e}")
            return None

        m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html)
        if not m:
            return None
        try:
            data = json.loads(m.group(1))
            return data["props"]["pageProps"]["state"]["data"]["entity"]
        except Exception as e:
            logger.error(f"Failed to parse Spotify embed JSON: {e}")
            return None

    def get_item_info(self, url: str) -> Optional[Dict[str, Any]]:
        """Metadata for a single track or podcast episode."""
        kind, item_id = parse_spotify_url(url)
        if kind not in ("track", "episode"):
            return None
        entity = self._fetch_embed_entity(kind, item_id)
        if not entity:
            return None

        title = entity.get("title") or entity.get("name") or (
            "Spotify Episode" if kind == "episode" else "Spotify Track")

        if kind == "episode":
            # For an episode the "artist" slot is the show name.
            artist = entity.get("subtitle") or "Podcast"
            artists_list = [artist]
        else:
            artists_list = [a["name"] for a in entity.get("artists", []) if a.get("name")]
            artist = ", ".join(artists_list) if artists_list else "Unknown Artist"

        images = entity.get("visualIdentity", {}).get("image", [])
        cover_url = images[-1]["url"] if images else None
        preview_url = (entity.get("audioPreview") or {}).get("url")

        return {
            "id": item_id,
            "kind": kind,
            "title": title,
            "artist": artist,
            "artists": artists_list,
            "duration": (entity.get("duration") or 0) / 1000,
            "cover_url": cover_url,
            "preview_url": preview_url,
            "spotify_url": f"https://open.spotify.com/{kind}/{item_id}",
        }

    # Back-compat alias — older callers (sniffer) still import this name.
    def get_track_info(self, url: str) -> Optional[Dict[str, Any]]:
        return self.get_item_info(url)

    def get_collection(self, url: str) -> Optional[Dict[str, Any]]:
        """Track list for an album or playlist, from the public embed.
        Note: the embed truncates very large playlists (~50 tracks)."""
        kind, item_id = parse_spotify_url(url)
        if kind not in ("album", "playlist"):
            return None
        entity = self._fetch_embed_entity(kind, item_id)
        if not entity:
            return None
        tracks: List[Dict[str, Any]] = []
        for t in entity.get("trackList", []):
            uri = t.get("uri") or ""
            tid = uri.split(":")[-1] if uri.startswith("spotify:track:") else None
            if not tid:
                continue
            tracks.append({
                "id": tid,
                "title": t.get("title") or "",
                "artist": t.get("subtitle") or "",
                "duration": (t.get("duration") or 0) / 1000,
            })
        if not tracks:
            return None
        return {
            "kind": kind,
            "title": entity.get("title") or entity.get("name") or "Spotify Collection",
            "tracks": tracks,
        }

    # --------------------------------------------------------------- matching

    def _search_provider(self, prefix: str, query: str, target_dur: float,
                         want_channel: str = "", tolerance: float = 0.15,
                         abs_tolerance: float = 18.0) -> Optional[str]:
        """Return the best public URL for `query` from a yt-dlp search prefix
        (ytsearchN: / scsearchN:), duration- and name-matched."""
        try:
            p = subprocess.run(
                ["yt-dlp", "--no-warnings", "--dump-json", "--flat-playlist",
                 f"{prefix}8:{query}"],
                capture_output=True, text=True, timeout=25,
            )
        except Exception as e:
            logger.warning(f"{prefix} search error: {e}")
            return None

        best_url, best_score = None, -1.0
        for line in p.stdout.strip().split("\n"):
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            c_dur = d.get("duration") or 0
            cand_url = d.get("webpage_url") or d.get("url")
            if not cand_url:
                continue
            if cand_url.isalnum() and prefix == "ytsearch":
                cand_url = f"https://www.youtube.com/watch?v={cand_url}"

            dur_ok = True
            if target_dur > 0 and c_dur > 0:
                dur_ok = (abs(c_dur - target_dur) <= abs_tolerance or
                          abs(c_dur - target_dur) / target_dur <= tolerance)

            score = _similar(d.get("title", ""), query)
            channel = (d.get("channel") or d.get("uploader") or "").lower().strip()
            wc = (want_channel or "").lower().strip()
            channel_match = bool(wc and channel and (
                channel in wc or wc in channel or _similar(channel, wc) > 0.6))
            if channel_match:
                score += 0.6
            if dur_ok:
                score += 1.0

            if score > best_score:
                best_score, best_url = score, cand_url

        # Require a plausible match — a duration hit, or a strong title +
        # same-channel signal — not just "the first search result".
        return best_url if best_score >= 1.0 else None

    # ---------------------------------------------------------- podcast feeds

    def _http_get(self, url: str, timeout: int = 25) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, context=self._ssl_ctx, timeout=timeout) as r:
            return r.read()

    def _find_podcast_feed(self, show_name: str) -> Optional[str]:
        """The show's public RSS feed URL, via Apple's free iTunes Search API
        (no key, no auth). Podcast RSS feeds are the open distribution format
        every podcast client uses — this is not a Spotify request."""
        try:
            import urllib.parse
            q = urllib.parse.quote(show_name)
            data = json.loads(self._http_get(
                f"https://itunes.apple.com/search?media=podcast&limit=10&term={q}"))
        except Exception as e:
            logger.warning(f"iTunes podcast lookup failed: {e}")
            return None
        best, best_score = None, 0.0
        for r in data.get("results", []):
            if not r.get("feedUrl"):
                continue
            s = _similar(r.get("collectionName", ""), show_name)
            if s > best_score:
                best_score, best = s, r["feedUrl"]
        return best if best_score >= 0.55 else None

    def _resolve_podcast_episode(self, show_name: str, episode_title: str,
                                 duration: float) -> Optional[str]:
        """Direct audio URL for the episode, from the show's public RSS feed.
        None if the show has no findable public feed or no episode matches."""
        feed_url = self._find_podcast_feed(show_name)
        if not feed_url:
            return None
        try:
            xml = self._http_get(feed_url, timeout=30).decode("utf-8", "ignore")
        except Exception as e:
            logger.warning(f"Podcast feed fetch failed: {e}")
            return None

        best, best_score = None, 0.0
        for item in re.findall(r"<item[\s>].*?</item>", xml, re.S):
            tm = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", item, re.S)
            em = re.search(r'<enclosure\b[^>]*\burl="([^"]+)"', item)
            if not tm or not em:
                continue
            s = _similar(tm.group(1).strip(), episode_title)
            dm = re.search(r"<itunes:duration>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</itunes:duration>", item)
            if dm and duration:
                secs = _parse_hhmmss(dm.group(1).strip())
                if secs and abs(secs - duration) <= max(90, duration * 0.2):
                    s += 0.2
            if s > best_score:
                best_score, best = s, em.group(1)
        return best if best_score >= 0.6 else None

    def _download_direct(self, url: str, dest: str, report, base: float, span: float):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, context=self._ssl_ctx, timeout=30) as r, \
                open(dest, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            while True:
                chunk = r.read(262144)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if total:
                    report("downloading", base + span * got / total,
                           msg=f"Downloading episode: {got * 100 // total}%")

    # -------------------------------------------------------------- download

    def download_track(self, url: str,
                       progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None) -> str:
        """Download a single Spotify track or podcast episode."""
        def report(status: str, percent: float, speed: str = "", msg: str = ""):
            if progress_cb:
                progress_cb({"status": status, "percent": percent, "speed": speed, "message": msg})

        kind, _ = parse_spotify_url(url)
        if kind in ("album", "playlist", "show"):
            raise ValueError(
                "This is a Spotify collection — it should be added as individual "
                "tracks. Try pasting the link again."
            )
        if kind in ("audiobook", "chapter"):
            raise ValueError(
                "Spotify audiobooks are DRM-protected and premium-only — Copita "
                "can't download them. For a public-domain audiobook, try its "
                "LibriVox / archive.org page instead."
            )

        report("extracting", 10, msg="Reading Spotify metadata & cover art…")
        info = self.get_item_info(url)
        if not info:
            raise ValueError(
                "Could not read this Spotify link. Open it in your browser to "
                "confirm it still works, then retry."
            )

        is_episode = info["kind"] == "episode"
        title = info["title"]
        artist = info["artist"]
        cover_url = info["cover_url"]
        preview_url = info["preview_url"]
        target_dur = info.get("duration", 0)

        clean_title = re.sub(r'[\\/*?:"<>|]', "", f"{artist} - {title}").strip()
        final_filename = f"{clean_title}.mp3"
        final_path = os.path.join(self.download_dir, final_filename)

        with tempfile.TemporaryDirectory() as temp_dir:
            cover_file = os.path.join(temp_dir, "cover.jpg")
            raw_audio = os.path.join(temp_dir, "raw_audio.mp3")

            if cover_url:
                try:
                    c_req = urllib.request.Request(cover_url, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(c_req, context=self._ssl_ctx, timeout=10) as cr, \
                            open(cover_file, "wb") as cf:
                        cf.write(cr.read())
                except Exception as e:
                    logger.warning(f"Could not download cover art: {e}")
                    cover_file = None
            else:
                cover_file = None

            audio_downloaded = False

            # --- Podcast episode: get the REAL episode from the show's public
            # RSS feed. No YouTube "closest guess" — for a podcast/audiobook a
            # wrong match is worse than an honest failure (a music track is a
            # single well-defined recording; a podcast episode isn't).
            if is_episode:
                report("downloading", 15, msg="Finding the podcast's public feed…")
                enclosure = self._resolve_podcast_episode(artist, title, target_dur)
                if not enclosure:
                    raise RuntimeError(
                        "Couldn't find this show's public podcast feed to download "
                        "the real episode — it may be a Spotify-exclusive show. "
                        "Copita won't substitute a different recording for it."
                    )
                report("downloading", 25, msg="Downloading the episode…")
                try:
                    self._download_direct(enclosure, raw_audio, report, base=25.0, span=55.0)
                    audio_downloaded = os.path.getsize(raw_audio) > 0
                except Exception as e:
                    raise RuntimeError(f"The podcast episode failed to download: {e}") from e
                best_audio_url = None
                is_youtube = False

            # --- Music track: metadata-match to a public recording.
            if not is_episode:
                search_query = f"{artist} {title}"
                tol, abs_tol = 0.15, 18.0
                report("downloading", 25, msg=f"Finding audio for “{search_query}”…")
                best_audio_url = self._search_provider(
                    "ytsearch", search_query, target_dur,
                    tolerance=tol, abs_tolerance=abs_tol)
                is_youtube = best_audio_url is not None
                if not best_audio_url:
                    best_audio_url = self._search_provider(
                        "scsearch", search_query, target_dur,
                        tolerance=tol, abs_tolerance=abs_tol)

            if not is_episode and best_audio_url:
                report("downloading", 35,
                       msg=f"Downloading audio from {'YouTube' if is_youtube else 'SoundCloud'}…")
                out_tmpl = os.path.join(temp_dir, "audio.%(ext)s")
                dl_cmd = ["yt-dlp", "--no-warnings"]
                if is_youtube:
                    dl_cmd += ["--extractor-args", "youtube:player_client=android"]
                dl_cmd += ["-f", "ba/b", "-x", "--audio-format", "mp3",
                           "--audio-quality", "0", "-o", out_tmpl, best_audio_url]
                try:
                    proc = subprocess.Popen(dl_cmd, stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, text=True)
                    for line in iter(proc.stdout.readline, ""):
                        m_prog = re.search(r"\[download\]\s+([\d\.]+)%\s+of\s+([^\s]+)\s+at\s+([^\s]+)", line)
                        if m_prog:
                            sub_pct = float(m_prog.group(1))
                            sp_speed = m_prog.group(3)
                            report("downloading", 35.0 + sub_pct * 0.45, speed=sp_speed,
                                   msg=f"Downloading audio: {sub_pct:.0f}% ({sp_speed})")
                    proc.wait(timeout=600)
                    if proc.returncode == 0:
                        for f in os.listdir(temp_dir):
                            if f.startswith("audio") and f.endswith(".mp3"):
                                raw_audio = os.path.join(temp_dir, f)
                                audio_downloaded = True
                                break
                except Exception as e:
                    logger.warning(f"Stream download error: {e}")

            # Preview fallback — music tracks only. Spotify's preview is a
            # ~30-90s clip: an acceptable stand-in for a short song we
            # couldn't match, never for a podcast episode.
            if not is_episode and not audio_downloaded and preview_url and (
                    target_dur <= 0 or target_dur <= 120):
                report("downloading", 60, msg="Using Spotify's official preview clip…")
                try:
                    p_req = urllib.request.Request(preview_url, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(p_req, context=self._ssl_ctx, timeout=15) as pr, \
                            open(raw_audio, "wb") as af:
                        af.write(pr.read())
                    audio_downloaded = True
                except Exception as e:
                    logger.warning(f"Preview fallback error: {e}")

            if not audio_downloaded or not os.path.exists(raw_audio):
                raise RuntimeError(
                    "Couldn't find a matching audio source for this track on "
                    "YouTube or SoundCloud."
                )

            report("processing", 85, msg="Embedding title, artist and cover art…")
            if cover_file and os.path.exists(cover_file):
                ffmpeg_cmd = [
                    "ffmpeg", "-y", "-i", raw_audio, "-i", cover_file,
                    "-map", "0:a", "-map", "1:v", "-c:a", "copy", "-c:v", "mjpeg",
                    "-id3v2_version", "3",
                    "-metadata", f"title={title}", "-metadata", f"artist={artist}",
                    "-metadata:s:v", 'title="Album cover"',
                    "-metadata:s:v", 'comment="Cover (front)"',
                    final_path,
                ]
            else:
                ffmpeg_cmd = [
                    "ffmpeg", "-y", "-i", raw_audio, "-c:a", "copy",
                    "-id3v2_version", "3",
                    "-metadata", f"title={title}", "-metadata", f"artist={artist}",
                    final_path,
                ]

            f_res = subprocess.run(ffmpeg_cmd, capture_output=True, text=True)
            if f_res.returncode != 0:
                import shutil
                shutil.copy2(raw_audio, final_path)

            report("completed", 100, msg=f"Saved: {final_filename}")
            return final_path
