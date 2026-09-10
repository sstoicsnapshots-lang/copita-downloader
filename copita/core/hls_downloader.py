"""
Turbo Parallel HLS/M3U8 Downloader for Copita.
Features:
- Master playlist parsing (multi-variant bitrate & split audio)
- Concurrent segment worker pool (16-32 connections)
- On-the-fly AES-128 decryption (with IV derivation)
- Byte-range segment extraction (#EXT-X-BYTERANGE)
- Preserves tokenized CDN query strings
- Seamless FFmpeg muxing to macOS QuickTime-ready MP4 (+faststart)
"""

import os
import re
import ssl
import time
import shutil
import asyncio
import certifi
import aiohttp
import subprocess
from urllib.parse import urljoin, urlparse
from typing import Optional, Callable, Dict, Any, List, Tuple
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend

from copita.core.speed_gate import GLOBAL as _speed_gate

class HlsSegment:
    def __init__(self, index: int, uri: str, duration: float, key_info: Optional[Dict[str, Any]] = None, byterange: Optional[Tuple[int, int]] = None):
        self.index = index
        self.uri = uri
        self.duration = duration
        self.key_info = key_info
        self.byterange = byterange  # (length, offset)

class TurboHlsDownloader:
    def __init__(
        self,
        m3u8_url: str,
        output_path: str,
        concurrency: int = 16,
        headers: Optional[Dict[str, str]] = None,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None
    ):
        self.m3u8_url = m3u8_url
        self.output_path = output_path
        self.concurrency = max(1, min(concurrency, 32))
        self.headers = headers or {}
        self.progress_callback = progress_callback
        
        self.ssl_context = ssl.create_default_context(cafile=certifi.where())
        self.key_cache: Dict[str, bytes] = {}
        self.is_cancelled = False
        self.is_paused = False
        self.downloaded_bytes = 0
        self.completed_segments = 0
        self.total_segments = 0
        self._start_time = 0.0
        self._last_speed_time = 0.0
        self._last_bytes = 0
        self.current_speed = 0.0
        # fMP4/CMAF playlists (#EXT-X-MAP) ship segments as raw moof/mdat
        # fragments with no moov of their own — they aren't independently
        # openable and can't be joined via ffmpeg's TS-oriented concat
        # demuxer the way classic .ts segments are.
        self.init_map_uri: Optional[str] = None
        self.init_map_byterange: Optional[Tuple[int, int]] = None

    async def _fetch_text(self, session: aiohttp.ClientSession, url: str) -> str:
        # Manifests are the thing sites bot-wall — fetch them with the real
        # browser TLS fingerprint (curl_cffi). Segments/keys reuse the CDN
        # session the manifest establishes and stay on aiohttp.
        try:
            from copita.core.http_client import HttpClient
            async with HttpClient(headers=self.headers, timeout=20.0) as fp:
                async with await fp.get(url, allow_redirects=True) as resp:
                    if resp.status == 200:
                        return (await resp.read()).decode("utf-8", errors="replace")
                    if resp.status not in (403, 429):
                        raise RuntimeError(f"Failed to fetch {url}, HTTP {resp.status}")
        except RuntimeError:
            raise
        except Exception:
            pass  # fall through to the plain path below
        req_headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            **self.headers
        }
        async with session.get(url, headers=req_headers) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Failed to fetch {url}, HTTP {resp.status}")
            return await resp.text()

    async def _fetch_key(self, session: aiohttp.ClientSession, key_url: str) -> bytes:
        if key_url in self.key_cache:
            return self.key_cache[key_url]
        req_headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            **self.headers
        }
        async with session.get(key_url, headers=req_headers) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Failed to fetch encryption key {key_url}, HTTP {resp.status}")
            key_data = await resp.read()
            self.key_cache[key_url] = key_data
            return key_data

    def parse_playlist(self, content: str, base_url: str) -> Tuple[List[HlsSegment], Optional[str]]:
        """Parse M3U8 content. Returns (segments, audio_playlist_url)."""
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        
        # Check if master playlist
        is_master = any("#EXT-X-STREAM-INF" in line for line in lines)
        if is_master:
            best_bandwidth = -1
            best_variant_url = None
            audio_url = None
            
            # Check for alternate audio
            for line in lines:
                if line.startswith("#EXT-X-MEDIA:TYPE=AUDIO"):
                    m_uri = re.search(r'URI="([^"]+)"', line)
                    if m_uri:
                        audio_url = urljoin(base_url, m_uri.group(1))

            # Pick highest bandwidth video
            for i, line in enumerate(lines):
                if line.startswith("#EXT-X-STREAM-INF"):
                    bw_match = re.search(r'BANDWIDTH=(\d+)', line)
                    bw = int(bw_match.group(1)) if bw_match else 0
                    if i + 1 < len(lines) and not lines[i+1].startswith('#'):
                        var_uri = lines[i+1]
                        if bw > best_bandwidth:
                            best_bandwidth = bw
                            best_variant_url = urljoin(base_url, var_uri)
                            
            if best_variant_url:
                return [], best_variant_url
        
        # Media playlist parsing
        segments: List[HlsSegment] = []
        current_key: Optional[Dict[str, Any]] = None
        current_byterange: Optional[Tuple[int, int]] = None
        current_offset = 0
        current_duration = 0.0
        seq_num = 0

        for line in lines:
            if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                seq_num = int(line.split(":")[-1])
            elif line.startswith("#EXT-X-KEY:"):
                # Parse METHOD, URI, IV
                method_m = re.search(r'METHOD=([^,]+)', line)
                method = method_m.group(1) if method_m else "NONE"
                if method == "AES-128":
                    uri_m = re.search(r'URI="([^"]+)"', line)
                    iv_m = re.search(r'IV=0x([0-9a-fA-F]+)', line)
                    key_uri = urljoin(base_url, uri_m.group(1)) if uri_m else None
                    iv_bytes = bytes.fromhex(iv_m.group(1).zfill(32)) if iv_m else None
                    current_key = {
                        "method": "AES-128",
                        "uri": key_uri,
                        "iv": iv_bytes
                    }
                else:
                    current_key = None
            elif line.startswith("#EXT-X-MAP:"):
                map_uri_m = re.search(r'URI="([^"]+)"', line)
                if map_uri_m:
                    self.init_map_uri = urljoin(base_url, map_uri_m.group(1))
                    map_br_m = re.search(r'BYTERANGE="([^"]+)"', line)
                    if map_br_m:
                        br_val = map_br_m.group(1)
                        if "@" in br_val:
                            length_str, offset_str = br_val.split("@")
                            self.init_map_byterange = (int(length_str), int(offset_str))
                        else:
                            self.init_map_byterange = (int(br_val), 0)
            elif line.startswith("#EXTINF:"):
                dur_m = re.search(r'#EXTINF:([\d.]+)', line)
                current_duration = float(dur_m.group(1)) if dur_m else 0.0
            elif line.startswith("#EXT-X-BYTERANGE:"):
                br_val = line.split(":")[-1]
                if "@" in br_val:
                    length_str, offset_str = br_val.split("@")
                    length = int(length_str)
                    offset = int(offset_str)
                    current_offset = offset + length
                else:
                    length = int(br_val)
                    offset = current_offset
                    current_offset += length
                current_byterange = (length, offset)
            elif not line.startswith("#"):
                seg_uri = urljoin(base_url, line)
                # If key has no explicit IV, derive from sequence number
                seg_key = None
                if current_key:
                    seg_key = dict(current_key)
                    if not seg_key.get("iv"):
                        seg_key["iv"] = (seq_num + len(segments)).to_bytes(16, byteorder="big")
                        
                segments.append(HlsSegment(
                    index=len(segments),
                    uri=seg_uri,
                    duration=current_duration,
                    key_info=seg_key,
                    byterange=current_byterange
                ))
                current_byterange = None

        return segments, None

    def decrypt_aes128(self, ciphertext: bytes, key: bytes, iv: bytes) -> bytes:
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
        decryptor = cipher.decryptor()
        decrypted = decryptor.update(ciphertext) + decryptor.finalize()
        # Remove PKCS7 padding
        pad_len = decrypted[-1]
        if 1 <= pad_len <= 16:
            decrypted = decrypted[:-pad_len]
        return decrypted

    def _looks_like_valid_segment(self, head: bytes) -> bool:
        """Sanity check on a segment's leading bytes. Guards against trusting
        a corrupted or error-response body as a real segment — seen in
        practice as a segment file that's mostly the UTF-8 replacement
        character (some path decoded raw binary as text at some point,
        upstream of this app or in a prior run), which ffmpeg then can't
        parse at all. A single 0x47 at offset 0 isn't enough: it's a plain
        ASCII byte ('G'), so it survives that exact corruption unchanged
        even though the rest of the packet didn't — real MPEG-TS repeats
        the 0x47 sync byte every fixed 188-byte packet, so checking that
        alignment actually catches it. For fMP4, check the leading box's
        fourcc is plausible instead."""
        if not head:
            return False
        if head[:1] in (b'<', b'{'):
            return False
        if self.init_map_uri:
            return len(head) >= 8 and head[4:8].isalpha()
        if len(head) < 189:
            return head[0] == 0x47
        sync_offsets = [o for o in (0, 188, 376) if o < len(head)]
        return all(head[o] == 0x47 for o in sync_offsets)

    async def _download_segment(
        self,
        session: aiohttp.ClientSession,
        segment: HlsSegment,
        sem: asyncio.Semaphore,
        seg_dir: str
    ):
        seg_file = os.path.join(seg_dir, f"seg_{segment.index:05d}.ts")
        if os.path.exists(seg_file) and os.path.getsize(seg_file) > 0:
            with open(seg_file, "rb") as f:
                head = f.read(512)
            if self._looks_like_valid_segment(head):
                self.completed_segments += 1
                self.downloaded_bytes += os.path.getsize(seg_file)
                return
            # A stale/corrupt leftover from a previous failed attempt —
            # trusting it would just reproduce the same ffmpeg failure on
            # every retry forever. Fall through and re-fetch it instead.

        async with sem:
            while self.is_paused:
                await asyncio.sleep(0.5)
                if self.is_cancelled:
                    return

            req_headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                **self.headers
            }
            if segment.byterange:
                length, offset = segment.byterange
                req_headers["Range"] = f"bytes={offset}-{offset + length - 1}"

            # Retry loop with backoff
            for attempt in range(5):
                try:
                    async with session.get(segment.uri, headers=req_headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                        if resp.status not in (200, 206):
                            raise RuntimeError(f"HTTP {resp.status}")
                        data = await resp.read()

                    # Handle AES-128 decryption
                    if segment.key_info and segment.key_info["method"] == "AES-128":
                        key_bytes = await self._fetch_key(session, segment.key_info["uri"])
                        data = self.decrypt_aes128(data, key_bytes, segment.key_info["iv"])

                    if not self._looks_like_valid_segment(data[:512]):
                        kind = "fMP4 fragment" if self.init_map_uri else "MPEG-TS packet"
                        raise RuntimeError(
                            f"response doesn't look like a valid {kind} "
                            f"(got {data[:16]!r}) — treating as a failed fetch"
                        )

                    with open(seg_file, "wb") as f:
                        f.write(data)

                    self.downloaded_bytes += len(data)
                    self.completed_segments += 1
                    self._update_progress()
                    await _speed_gate.throttle(len(data))
                    break
                except Exception as e:
                    if attempt == 4:
                        raise RuntimeError(f"Segment {segment.index} failed after 5 retries: {e}")
                    await asyncio.sleep(0.5 * (2 ** attempt))

    async def _fetch_init_segment(self, session: aiohttp.ClientSession) -> bytes:
        req_headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            **self.headers
        }
        if self.init_map_byterange:
            length, offset = self.init_map_byterange
            req_headers["Range"] = f"bytes={offset}-{offset + length - 1}"
        async with session.get(self.init_map_uri, headers=req_headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status not in (200, 206):
                raise RuntimeError(f"Failed to fetch fMP4 init segment {self.init_map_uri}, HTTP {resp.status}")
            return await resp.read()

    def _update_progress(self):
        now = time.time()
        time_diff = now - self._last_speed_time
        if time_diff >= 0.5:
            bytes_diff = self.downloaded_bytes - self._last_bytes
            self.current_speed = bytes_diff / time_diff if time_diff > 0 else 0.0
            self._last_speed_time = now
            self._last_bytes = self.downloaded_bytes

            pct = (self.completed_segments / self.total_segments * 100) if self.total_segments else 0.0
            eta = None
            if self.current_speed > 0 and self.total_segments:
                avg_seg_size = self.downloaded_bytes / max(1, self.completed_segments)
                rem_bytes = (self.total_segments - self.completed_segments) * avg_seg_size
                eta = rem_bytes / self.current_speed

            if self.progress_callback:
                self.progress_callback({
                    "downloaded": self.downloaded_bytes,
                    "total": None,
                    "completed_segments": self.completed_segments,
                    "total_segments": self.total_segments,
                    "speed": self.current_speed,
                    "eta": eta,
                    "threads": self.concurrency,
                    "percent": pct
                })

    async def start(self) -> str:
        self._start_time = time.time()
        self._last_speed_time = self._start_time
        self.downloaded_bytes = 0
        self.completed_segments = 0
        self.is_cancelled = False
        self.is_paused = False

        connector = aiohttp.TCPConnector(ssl=self.ssl_context, limit=self.concurrency + 4)
        async with aiohttp.ClientSession(connector=connector) as session:
            current_url = self.m3u8_url
            content = await self._fetch_text(session, current_url)
            segments, variant_or_audio = self.parse_playlist(content, current_url)

            # If master playlist, resolve to media playlist
            while variant_or_audio and not segments:
                current_url = variant_or_audio
                content = await self._fetch_text(session, current_url)
                segments, variant_or_audio = self.parse_playlist(content, current_url)

            if not segments:
                raise ValueError("No playable segments found in HLS playlist.")

            self.total_segments = len(segments)
            temp_dir = f"{self.output_path}.hls_tmp"
            os.makedirs(temp_dir, exist_ok=True)

            init_bytes = None
            if self.init_map_uri:
                init_bytes = await self._fetch_init_segment(session)

            sem = asyncio.Semaphore(self.concurrency)
            tasks = [self._download_segment(session, seg, sem, temp_dir) for seg in segments]
            await asyncio.gather(*tasks)

            if self.is_cancelled:
                raise asyncio.CancelledError("Download cancelled")

            # Remux to MP4 with FFmpeg
            if self.progress_callback:
                self.progress_callback({
                    "percent": 99.0,
                    "status": "merging",
                    "threads": 1,
                    "speed": 0.0
                })

            os.makedirs(os.path.dirname(os.path.abspath(self.output_path)), exist_ok=True)
            loop = asyncio.get_running_loop()

            if self.init_map_uri:
                # fMP4/CMAF: each segment is a headerless moof/mdat fragment
                # with no moov of its own — that's exactly the error this
                # branch exists to avoid ("could not find corresponding
                # track id/trex"): ffmpeg's concat demuxer requires every
                # listed file to be independently openable, which a bare
                # fragment isn't. CMAF is designed to be joined by raw byte
                # concatenation right after the init segment's ftyp+moov,
                # which already produces a valid, playable fragmented MP4
                # on its own — no multi-file concat demuxer involved.
                merged_path = os.path.join(temp_dir, "merged.mp4")
                with open(merged_path, "wb") as out_f:
                    out_f.write(init_bytes)
                    for seg in segments:
                        seg_file = os.path.join(temp_dir, f"seg_{seg.index:05d}.ts")
                        with open(seg_file, "rb") as in_f:
                            shutil.copyfileobj(in_f, out_f)

                cmd = [
                    "ffmpeg", "-y",
                    "-i", merged_path,
                    "-c", "copy",
                    "-movflags", "+faststart",
                    self.output_path
                ]
                proc = await loop.run_in_executor(
                    None, lambda: subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                )
                if proc.returncode != 0:
                    raise RuntimeError(f"FFmpeg remux failed: {proc.stderr.decode('utf-8', errors='ignore')}")
            else:
                concat_txt = os.path.join(temp_dir, "concat.txt")
                with open(concat_txt, "w") as f:
                    for seg in segments:
                        seg_file = f"seg_{seg.index:05d}.ts"
                        f.write(f"file '{seg_file}'\n")

                # FFmpeg concatenation into QuickTime-compatible MP4
                cmd = [
                    "ffmpeg", "-y",
                    "-f", "concat",
                    "-safe", "0",
                    "-i", concat_txt,
                    "-c", "copy",
                    "-movflags", "+faststart",
                    self.output_path
                ]

                # Run via executor, not directly on the event loop: ffmpeg on
                # a large/transcoded remux can take minutes, and a blocking
                # subprocess.run() here would freeze every other concurrent
                # download and the sniffer's own timeouts along with it.
                proc = await loop.run_in_executor(
                    None, lambda: subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                )
                if proc.returncode != 0:
                    # If copy fails (e.g. non-mp4 compatible codecs), transcode audio to aac
                    alt_cmd = [
                        "ffmpeg", "-y",
                        "-f", "concat",
                        "-safe", "0",
                        "-i", concat_txt,
                        "-c:v", "copy",
                        "-c:a", "aac",
                        "-movflags", "+faststart",
                        self.output_path
                    ]
                    proc2 = await loop.run_in_executor(
                        None, lambda: subprocess.run(alt_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    )
                    if proc2.returncode != 0:
                        raise RuntimeError(f"FFmpeg remux failed: {proc2.stderr.decode('utf-8', errors='ignore')}")

            # Clean temporary segments
            shutil.rmtree(temp_dir, ignore_errors=True)

            if self.progress_callback:
                self.progress_callback({
                    "downloaded": self.downloaded_bytes,
                    "total": self.downloaded_bytes,
                    "percent": 100.0,
                    "speed": 0.0,
                    "eta": 0.0,
                    "status": "completed"
                })

            return self.output_path

    def pause(self):
        self.is_paused = True

    def resume(self):
        self.is_paused = False

    def cancel(self):
        self.is_cancelled = True
