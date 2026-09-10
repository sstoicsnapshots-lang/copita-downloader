"""
High-Level yt-dlp wrapper for Copita.
Provides format inspection, audio extraction, browser cookie loading, and smooth progress tracking.
"""

import os
import asyncio
import signal
import subprocess
import threading
import time
from urllib.parse import urlparse
import yt_dlp
from typing import Dict, Any, Optional, Callable, List

# When YouTube's "not a bot" check has forced a cookie retry once, this
# network is being rate-limited — every following YouTube pull will need
# cookies too. Remember it (per process, ~10 min) so the rest of a batch
# doesn't each waste a cold cookie-free attempt first.
_YT_NEEDS_COOKIES_UNTIL = 0.0


class YtDlpWrapper:
    def __init__(self, cookies_browser: Optional[str] = None,
                 auth_browser: Optional[str] = None):
        self.cookies_browser = cookies_browser
        # The browser to fall back to *only* for an auth-required retry
        # (members-only / age-restricted / login wall). YouTube forces
        # `cookies_browser` to None so it isn't sent by default — but if the
        # user explicitly picked a browser in Preferences, that's where they're
        # signed in, so honour it here rather than guessing.
        self.auth_browser = auth_browser
        self._cancelled = threading.Event()
        self._proc_hint: Optional[str] = None

    @staticmethod
    def _proxy() -> Optional[str]:
        try:
            from copita.core.http_client import current_proxy
            return current_proxy()
        except Exception:
            return None

    def cancel(self):
        self._cancelled.set()
        # For live streams yt-dlp hands the whole transfer off to an
        # external ffmpeg process and blocks on Popen.wait() -- progress_hooks
        # (and therefore the _cancelled check inside _hook) never fire while
        # that's happening, so without this a "cancelled" live recording just
        # keeps running forever as an orphaned process. Killing by the
        # resolved output filename (unique per task) is the only handle we
        # have on it from outside yt-dlp's internals.
        if self._proc_hint:
            self._kill_matching_ffmpeg(self._proc_hint)

    @staticmethod
    def _kill_matching_ffmpeg(hint: str):
        try:
            result = subprocess.run(['pgrep', '-f', hint], capture_output=True, text=True)
            for pid in result.stdout.split():
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except (ValueError, ProcessLookupError, PermissionError):
                    pass
        except Exception:
            pass

    @staticmethod
    def _connection_error(error):
        message = str(error).lower()
        return any(term in message for term in (
            'timed out', 'timeout', 'connection reset', 'connection refused',
            'network is unreachable', 'name resolution', 'failed to resolve',
        ))

    @staticmethod
    def _transient_extract_error(error):
        """YouTube (and a few others) intermittently reject the first probe
        with a stale-session error that a plain re-run clears."""
        message = str(error).lower()
        return any(term in message for term in (
            'page needs to be reloaded', 'please try again', 'try again later',
            'unable to extract yt initial data', 'failed to extract any player response',
        ))

    # yt-dlp needs a small JS "challenge solver" to read YouTube's signed
    # stream URLs. Recent releases ship it as a fetch-on-demand component
    # rather than bundling it; without this yt-dlp warns and some videos
    # 403 on the format URLs. `ejs:github` lets it pull the official solver
    # straight from the yt-dlp project. Harmless on releases that don't need
    # it (they just ignore the option).
    _REMOTE_COMPONENTS = ['ejs:github']

    @staticmethod
    def _bot_check_error(error):
        """YouTube's IP-reputation challenge ("Sign in to confirm you're not a
        bot"). Cookies don't help — a logged-in session trips a *stricter*
        check ("page needs to be reloaded"). It's rate-limit-shaped: on a
        flagged IP some requests still get through, so a few spaced retries
        are worth it before giving up."""
        m = str(error).lower()
        return ("confirm you're not a bot" in m or "confirm you’re not a bot" in m
                or "sign in to confirm you" in m or "not a bot" in m)

    @staticmethod
    def _info_is_audio_only(info):
        """True when the extracted media has no video track anywhere — a music
        track / podcast, not a video whose audio we might want split out."""
        if not info or info.get('_type') in ('playlist', 'multi_video'):
            return False
        formats = info.get('formats')
        if formats:
            return all(f.get('vcodec') in (None, 'none') for f in formats)
        return info.get('vcodec') == 'none' and info.get('acodec') not in (None, 'none')

    @staticmethod
    def _auth_required_error(error):
        """The link genuinely needs a signed-in session (members-only,
        private, age-gated) — the one case where sending YouTube cookies is
        the right move rather than the wrong one."""
        message = str(error).lower()
        return any(term in message for term in (
            'members-only', 'members only', 'join this channel', 'private video',
            'sign in to confirm your age', 'age-restricted', 'login required',
            'this video is available to this channel',
            # Vimeo's stable yt-dlp extractor now refuses its `web` client
            # without an account — most ordinary public vimeo.com/<id> links
            # hit this. Retrying with the browser's cookies clears it when the
            # user has a Vimeo session.
            'only works when logged-in', 'only works when logged in',
            'unable to fetch new oauth tokens',
        ))

    def _extract_metadata(self, url, options, progress_callback=None):
        """Retry only connection failures, before any media files are written."""
        global _YT_NEEDS_COOKIES_UNTIL
        options = {**options, 'socket_timeout': 12, 'extractor_retries': 2,
                   'retries': 2, 'fragment_retries': 2, 'noplaylist': True,
                   'remote_components': self._REMOTE_COMPONENTS}
        host = (urlparse(url).hostname or '').lower()
        is_reddit = host == 'redd.it' or host.endswith('.redd.it') or host == 'reddit.com' or host.endswith('.reddit.com')
        is_youtube = 'youtube.com' in host or host == 'youtu.be'
        tried_with_cookies = 'cookiesfrombrowser' in options
        bot_check_retried = False

        # This network recently needed cookies for YouTube — skip the cold
        # cookie-free attempt that's just going to bounce.
        if (is_youtube and not tried_with_cookies
                and time.time() < _YT_NEEDS_COOKIES_UNTIL):
            from copita.core.browser_cookies import detect_default_browser
            br = self.cookies_browser or self.auth_browser or detect_default_browser()
            if br:
                options['cookiesfrombrowser'] = (br,)
                tried_with_cookies = True
        for attempt in range(3):
            if self._cancelled.is_set():
                raise RuntimeError('Download cancelled')
            try:
                with yt_dlp.YoutubeDL(options) as ydl:
                    info = ydl.extract_info(url, download=False)
                if not info:
                    raise ValueError('No downloadable media was found at this link.')
                return info, options
            except yt_dlp.utils.DownloadError as error:
                # A browser cookie store that can't be read (locked DB, the
                # user denied the keychain prompt) must not fail the whole
                # download — drop the cookies and try once more without them.
                if 'cookiesfrombrowser' in options and any(
                        s in str(error).lower() for s in
                        ('could not find', 'cookie', 'keyring', 'safe storage', 'permission')):
                    options.pop('cookiesfrombrowser', None)
                    continue
                # Cookies actively breaking a YouTube probe ("page needs to be
                # reloaded" from a logged-in session) — drop them and retry
                # immediately, no backoff.
                if 'cookiesfrombrowser' in options and self._transient_extract_error(error):
                    options.pop('cookiesfrombrowser', None)
                    continue
                # YouTube's "confirm you're not a bot" IP challenge. This is
                # exactly the case where the browser's own YouTube session
                # DOES get through — a signed-in request from an IP YouTube is
                # rate-limiting is trusted where a cold one isn't. Retry once
                # with cookies. (YouTube is left cookie-free by default because
                # on a *clean* IP cookies can trip a different check; here
                # they're the fix.)
                if self._bot_check_error(error) and not bot_check_retried:
                    bot_check_retried = True
                    if not tried_with_cookies:
                        from copita.core.browser_cookies import detect_default_browser
                        br = (self.cookies_browser or self.auth_browser
                              or detect_default_browser())
                        if br:
                            options['cookiesfrombrowser'] = (br,)
                            tried_with_cookies = True
                            if is_youtube:
                                _YT_NEEDS_COOKIES_UNTIL = time.time() + 600
                            if progress_callback:
                                progress_callback({'status': 'analyzing', 'speed': 0})
                            continue
                    # already had cookies (or no browser to pull from) — one
                    # quick cookie-free retry in case it's just transient.
                    time.sleep(0.5)
                    continue
                # The reverse: a link that actually needs a session. If we
                # haven't tried cookies yet, do so once now.
                if not tried_with_cookies and self._auth_required_error(error):
                    from copita.core.browser_cookies import detect_default_browser
                    br = self.cookies_browser or self.auth_browser or detect_default_browser()
                    if br:
                        options['cookiesfrombrowser'] = (br,)
                        tried_with_cookies = True
                        continue
                if attempt < 2 and self._transient_extract_error(error):
                    if progress_callback:
                        progress_callback({'status': 'analyzing', 'speed': 0})
                    # YouTube's "reload" flag clears after a short wait once
                    # its per-session token cycles. yt-dlp's own
                    # extractor_retries already burned a few seconds; keep our
                    # extra wait short so "Scanning…" doesn't drag.
                    time.sleep(1.5 + attempt * 1.5)
                    continue
                if attempt == 0 and self._connection_error(error):
                    # An IPv6 routing failure should not make an otherwise reachable
                    # site fail. Keep the fallback local to this download.
                    options['source_address'] = '0.0.0.0'
                    if progress_callback:
                        progress_callback({'status': 'analyzing', 'speed': 0})
                    continue
                if is_reddit:
                    if self._connection_error(error):
                        message = 'Reddit did not respond. Check that the post opens in your browser, then retry.'
                    elif any(word in str(error).lower() for word in ('403', '429', 'login', 'blocked')):
                        message = 'Reddit restricted this request. Open the post in your browser; if it requires sign-in, select that browser in Preferences, then retry.'
                    else:
                        raise
                    raise RuntimeError(message) from error
                # Vimeo's login wall, still failing after the cookie retry —
                # the user has no Vimeo session in the selected browser.
                if host.endswith('vimeo.com') and self._auth_required_error(error):
                    raise RuntimeError(
                        'Vimeo now requires a signed-in session for most videos. '
                        'Open the video in a browser where you are logged into '
                        'Vimeo, then pick that browser under Preferences → Cookies '
                        'and retry.'
                    ) from error
                # Instagram / Facebook: yt-dlp's "Unable to extract data" /
                # "login required" here is almost always the login wall or a
                # profile/handle URL instead of a specific post.
                emsg = str(error).lower()
                # YouTube age-restricted, still blocked after the cookie
                # retry. YouTube has fully closed the old tv_embedded /
                # player-client bypasses — an age-verified logged-in account
                # is the only route, and even that often fails from an
                # automated client. Say so instead of the raw yt-dlp line.
                if (is_youtube
                        and ('age-restricted' in emsg or 'confirm your age' in emsg
                             or 'inappropriate for some users' in emsg)):
                    raise RuntimeError(
                        "This YouTube video is age-restricted. Copita tried your "
                        "browser's YouTube session and YouTube still blocked it — "
                        "this needs an account with its birth date set (age-"
                        "verified). Sign into YouTube in your browser, confirm it "
                        "can play the video there, pick that browser under "
                        "Preferences → Cookies, then Retry."
                    ) from error
                # YouTube's "not a bot" challenge that outlasted the retries —
                # the IP is temporarily rate-limited (a burst of downloads, a
                # VPN, or a shared connection). It clears by itself; a signed-in
                # browser session does NOT help here (it trips a stricter
                # check). Say that plainly.
                if is_youtube and self._bot_check_error(error):
                    raise RuntimeError(
                        "YouTube is temporarily rate-limiting this network with a "
                        '"confirm you\'re not a bot" check. This clears on its own '
                        "in a little while — wait and press Retry. It usually "
                        "follows a burst of downloads, a VPN, or a shared "
                        "connection. (A signed-in browser session doesn't get "
                        "past this one.)"
                    ) from error
                if (('instagram.com' in host or 'facebook.com' in host or host == 'fb.watch')
                        and any(s in emsg for s in ('unable to extract', 'login required',
                                                    'requires login', 'empty media response',
                                                    'unsupported url', 'no video formats'))):
                    site = 'Instagram' if 'instagram' in host else 'Facebook'
                    raise RuntimeError(
                        f"{site} needs a signed-in session and a link to a specific "
                        f"post/reel/video (not a profile). Open the post, copy its "
                        f"URL, and pick the browser you're logged into {site} with "
                        f"under Preferences → Cookies."
                    ) from error
                raise

    def probe_entries(self, url: str) -> Dict[str, Any]:
        """Flat-probe a URL to detect a playlist / multi-item collection
        (a YouTube playlist, a SoundCloud set or profile, an archive.org item
        with several audio tracks, a Bandcamp album, ...).

        Returns {is_playlist, title, entries: [{url, title, id}], total}.
        `is_playlist` is only True when more than one downloadable entry is
        found, so a plain single-media URL — even one that carries a playlist
        context — is left to the normal single-item download path.
        """
        opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            # Don't resolve each entry's formats — just list them. One fast
            # network call regardless of how many items the playlist has.
            'extract_flat': 'in_playlist',
            'playlistend': 500,
            'socket_timeout': 12,
            'extractor_retries': 1,
            'ignoreerrors': 'only_download',
            'remote_components': self._REMOTE_COMPONENTS,
            # No player_client override — see inspect_url().
        }
        host = (urlparse(url).hostname or '').lower()
        is_youtube = 'youtube.com' in host or host == 'youtu.be'
        if self.cookies_browser:
            opts['cookiesfrombrowser'] = (self.cookies_browser,)
        elif is_youtube and time.time() < _YT_NEEDS_COOKIES_UNTIL:
            # This network is being bot-checked by YouTube — a flat playlist
            # probe needs the browser session too, or the playlist won't
            # expand (and the user gets just the first video).
            from copita.core.browser_cookies import detect_default_browser
            br = self.auth_browser or detect_default_browser()
            if br:
                opts['cookiesfrombrowser'] = (br,)
        if self._proxy():
            opts['proxy'] = self._proxy()
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception:
            # A bot-check on the first (cookie-free) YouTube probe — retry once
            # with the browser session before giving up on expansion.
            if is_youtube and 'cookiesfrombrowser' not in opts:
                from copita.core.browser_cookies import detect_default_browser
                br = self.auth_browser or detect_default_browser()
                if br:
                    opts['cookiesfrombrowser'] = (br,)
                    try:
                        with yt_dlp.YoutubeDL(opts) as ydl:
                            info = ydl.extract_info(url, download=False)
                    except Exception:
                        return {'is_playlist': False, 'entries': []}
                else:
                    return {'is_playlist': False, 'entries': []}
            else:
                return {'is_playlist': False, 'entries': []}

        if not info or info.get('_type') not in ('playlist', 'multi_video'):
            return {'is_playlist': False, 'entries': []}

        entries: List[Dict[str, Any]] = []
        for entry in (info.get('entries') or []):
            if not entry:
                continue
            entry_url = entry.get('url') or entry.get('webpage_url') or entry.get('original_url')
            if not entry_url:
                continue
            if entry_url.startswith('//'):
                entry_url = 'https:' + entry_url
            entries.append({
                'url': entry_url,
                'title': (entry.get('title') or '').strip(),
                'id': entry.get('id'),
            })

        return {
            'is_playlist': len(entries) > 1,
            'title': (info.get('title') or '').strip(),
            'entries': entries,
            'total': info.get('playlist_count') or len(entries),
        }

    def inspect_url(self, url: str) -> Dict[str, Any]:
        """Extract metadata, formats, and available qualities."""
        ydl_opts = {
            'quiet': True,
            'noprogress': True,
            'no_warnings': True,
            'skip_download': True,
            'extract_flat': False,
            'remote_components': self._REMOTE_COMPONENTS,
            # No player_client override — yt-dlp's own default client
            # selection now tracks YouTube's SABR rollout better than any
            # pinned list. A pinned ['android','web'] (which this used to
            # force) currently yields ~5 junk formats where the default
            # gives ~53. Revisit only if the default regresses.
        }
        if self.cookies_browser:
            ydl_opts['cookiesfrombrowser'] = (self.cookies_browser,)
        if self._proxy():
            ydl_opts['proxy'] = self._proxy()

        info, _ = self._extract_metadata(url, ydl_opts)
            
        formats_raw = info.get('formats', [])
        clean_formats: List[Dict[str, Any]] = []
        seen_res = set()

        for f in formats_raw:
            f_id = f.get('format_id')
            ext = f.get('ext')
            vcodec = f.get('vcodec', 'none')
            acodec = f.get('acodec', 'none')
            height = f.get('height')
            width = f.get('width')
            filesize = f.get('filesize') or f.get('filesize_approx')
            fps = f.get('fps')
            tbr = f.get('tbr')

            is_video = vcodec != 'none'
            is_audio = acodec != 'none' and vcodec == 'none'

            if is_video and height:
                res_key = f"{height}p"
                clean_formats.append({
                    "format_id": f_id,
                    "resolution": res_key,
                    "width": width,
                    "height": height,
                    "fps": fps,
                    "ext": ext,
                    "filesize": filesize,
                    "tbr": tbr,
                    "type": "video"
                })
            elif is_audio:
                abr = f.get('abr') or tbr
                clean_formats.append({
                    "format_id": f_id,
                    "resolution": f"Audio ({int(abr)} kbps)" if abr else "Audio",
                    "ext": ext,
                    "filesize": filesize,
                    "tbr": abr,
                    "type": "audio"
                })

        return {
            "id": info.get('id'),
            "title": info.get('title', 'Unknown Title'),
            "description": (info.get('description') or '')[:300],
            "uploader": info.get('uploader') or info.get('channel', 'Unknown'),
            "duration": info.get('duration'),
            "thumbnail": info.get('thumbnail'),
            "webpage_url": info.get('webpage_url', url),
            "formats": clean_formats
        }

    def download(
        self,
        url: str,
        output_template: str,
        format_id: Optional[str] = None,
        audio_only: bool = False,
        audio_format: str = "mp3",
        video_quality: str = "best",
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        clip_start: Optional[float] = None,
        clip_end: Optional[float] = None,
    ) -> str:
        """Download media using yt-dlp with live progress hook.

        clip_start / clip_end (seconds) restrict the download to that time
        window via yt-dlp's native download_ranges — only the needed bytes
        are fetched, then ffmpeg trims to exact keyframe-cut boundaries."""
        
        def _hook(d):
            if self._cancelled.is_set():
                raise RuntimeError('Download cancelled')
            if not progress_callback:
                return
            status = d.get('status')
            if status == 'downloading':
                total = d.get('total_bytes') or d.get('total_bytes_estimate')
                downloaded = d.get('downloaded_bytes', 0)
                speed = d.get('speed', 0)
                eta = d.get('eta')
                percent = (downloaded / total * 100) if total else 0.0
                progress_callback({
                    "status": "downloading",
                    "downloaded": downloaded,
                    "total": total,
                    "speed": speed or 0.0,
                    "eta": eta,
                    "percent": percent,
                    "threads": 4
                })
            elif status == 'finished':
                progress_callback({
                    "status": "merging",
                    "percent": 99.0,
                    "speed": 0.0,
                    "eta": 0.0,
                    "threads": 1
                })

        final_paths = []

        def _postprocessor_hook(data):
            if data.get('status') == 'finished':
                path = data.get('info_dict', {}).get('filepath')
                if path:
                    final_paths.append(path)

        ydl_opts = {
            'outtmpl': output_template,
            # NB: yt-dlp's `trim_file_name` trims the *entire path* to N bytes,
            # not just the basename — with a long download folder it mangled
            # the output into a truncated directory name. The output template
            # already bounds the title with `%(title).80B`, so this isn't
            # needed. (Removed 2026-09-08.)
            'progress_hooks': [_hook],
            'postprocessor_hooks': [_postprocessor_hook],
            'quiet': True,
            'no_warnings': True,
            'windowsfilenames': False,
            'remote_components': self._REMOTE_COMPONENTS,
            # No player_client override — see inspect_url().
        }

        if self.cookies_browser:
            ydl_opts['cookiesfrombrowser'] = (self.cookies_browser,)
        if self._proxy():
            ydl_opts['proxy'] = self._proxy()

        if clip_start is not None or clip_end is not None:
            start = max(0.0, float(clip_start or 0.0))
            end = float(clip_end) if clip_end is not None else None
            if end is not None and end <= start:
                raise ValueError("Clip end time must be after the start time.")
            try:
                from yt_dlp.utils import download_range_func
                ydl_opts['download_ranges'] = download_range_func(
                    None, [(start, end if end is not None else 1e9)])
            except Exception:
                # very old yt-dlp — fall back to a plain range spec string
                ydl_opts['download_ranges'] = lambda info, ydl: [
                    {'start_time': start, 'end_time': end if end is not None else info.get('duration') or 1e9}]
            # Exact cuts (re-encode the boundary GOPs) so the clip starts and
            # ends where the user asked, not at the nearest keyframe.
            ydl_opts['force_keyframes_at_cuts'] = True

        if audio_only:
            ydl_opts['format'] = 'bestaudio/best'
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': audio_format,
                'preferredquality': '320',
            }, {
                'key': 'FFmpegMetadata',
                'add_metadata': True
            }]
        elif format_id:
            ydl_opts['format'] = f"{format_id}+bestaudio/best"
            ydl_opts['merge_output_format'] = 'mp4'
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegVideoRemuxer',
                'preferedformat': 'mp4'
            }]
        else:
            # Default to <=1080p (nobody expects a multi-GB 4K download by
            # default) and strongly prefer H.264 video + AAC audio, which
            # every Mac plays natively. Higher-res VP9/AV1 mp4 streams (which
            # `[ext=mp4]` alone would happily pick) produce a file QuickTime
            # can't decode — "downloaded but won't open". Fall back through
            # them only if nothing compatible exists.
            h = video_quality if video_quality in ('480', '720', '1080') else '1080'
            ydl_opts['format'] = (
                f'bestvideo[vcodec^=avc1][height<={h}]+bestaudio[acodec^=mp4a]/'
                f'best[vcodec^=avc1][height<={h}]/'
                f'bestvideo[ext=mp4][height<={h}]+bestaudio[ext=m4a]/'
                f'bestvideo[height<={h}]+bestaudio/'
                f'best[height<={h}]/best'
            )
            ydl_opts['merge_output_format'] = 'mp4'
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegVideoRemuxer', 'preferedformat': 'mp4',
            }]

        info, ydl_opts = self._extract_metadata(url, ydl_opts, progress_callback)
        if self._cancelled.is_set():
            raise RuntimeError('Download cancelled')

        # The default (video) branch above forces an .mp4 container + video
        # remuxer. When the source has no video at all — a SoundCloud track,
        # a Bandcamp song, a plain audio URL — that turns an .m4a/.opus/.mp3
        # into a misleading ".mp4". Drop the video wrapping for audio-only
        # sources so the file keeps its real audio extension.
        if not audio_only and not format_id and self._info_is_audio_only(info):
            ydl_opts.pop('merge_output_format', None)
            ydl_opts['format'] = 'bestaudio/best'
            ydl_opts['postprocessors'] = [{'key': 'FFmpegMetadata', 'add_metadata': True}]
        is_live = bool(info.get('is_live') or info.get('live_status') == 'is_live')
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                self._proc_hint = ydl.prepare_filename(info)
            except Exception:
                self._proc_hint = None

            # A live broadcast is handed off entirely to an external ffmpeg
            # process (see FFmpegFD.real_download) which blocks until it
            # exits and never calls progress_hooks in between -- without
            # this, the UI would sit at 0%/"Scanning..." for the whole
            # recording no matter how much data actually comes down.
            stop_poll = threading.Event()
            poll_thread = None
            if is_live and self._proc_hint and progress_callback:
                partial_path = self._proc_hint + '.part'

                def _poll_live_progress():
                    start = time.time()
                    last_size = -1
                    while not stop_poll.wait(1.0):
                        try:
                            size = os.path.getsize(partial_path)
                        except OSError:
                            continue
                        if size != last_size:
                            last_size = size
                            elapsed = max(time.time() - start, 0.001)
                            progress_callback({
                                "status": "downloading",
                                "downloaded": size,
                                "total": None,
                                "speed": size / elapsed,
                                "eta": None,
                                "percent": 0.0,
                            })

                poll_thread = threading.Thread(target=_poll_live_progress, daemon=True)
                poll_thread.start()

            try:
                info = ydl.process_ie_result(info, download=True)
            finally:
                stop_poll.set()
                if poll_thread:
                    poll_thread.join(timeout=2)

            filename = final_paths[-1] if final_paths else info.get('filepath') or ydl.prepare_filename(info)
            if audio_only:
                base, _ = os.path.splitext(filename)
                filename = f"{base}.{audio_format}"

        if self._cancelled.is_set():
            # process_ie_result can return normally even after we killed the
            # ffmpeg subprocess mid-transfer (a live-stream cancel looks like
            # a clean finish to yt-dlp) -- don't report a partial recording
            # as a completed download, and don't leave the partial file behind.
            for path in (filename, filename + '.part'):
                try:
                    if path and os.path.isfile(path):
                        os.remove(path)
                except OSError:
                    pass
            raise RuntimeError('Download cancelled')

        if not os.path.isfile(filename):
            raise RuntimeError('The download finished without producing a media file. Please retry.')

        return filename
