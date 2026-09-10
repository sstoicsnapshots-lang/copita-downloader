"""
Command-line interface for Copita Universal Downloader.
"""

import os
import sys
import re
import argparse
import asyncio
import subprocess
import webbrowser
import urllib.parse
from copita.core.classifier import classify_url
from copita.core.sniffer import MediaSniffer
from copita.core.chunk_downloader import ChunkDownloader
from copita.core.hls_downloader import TurboHlsDownloader
from copita.core.ytdlp_wrapper import YtDlpWrapper
from copita.core.scribd_downloader import ScribdDownloader
from copita.core.flipbook_downloader import FlipbookDownloader
from copita.core.deepzoom_downloader import DeepZoomDownloader
from copita.core.sketchfab_downloader import SketchfabDownloader

def format_bytes(b: float) -> str:
    for unit in ['B', 'KB', 'MB', 'GB']:
        if b < 1024.0:
            return f"{b:.1f} {unit}"
        b /= 1024.0
    return f"{b:.1f} TB"

async def async_download(url: str, output_dir: str, threads: int, audio_only: bool, format_id: str):
    os.makedirs(output_dir, exist_ok=True)
    classification = classify_url(url)
    engine = classification.get("engine", "turbo_chunk")
    print(f"\n🚀 [Copita] Target: {url}")
    print(f"📦 Classification: {classification.get('name')} | Engine: {engine} | Threads: {threads}\n")

    def progress_callback(d):
        speed_str = f"{format_bytes(d.get('speed', 0))}/s" if d.get('speed') else "---"
        pct = d.get('percent', 0.0) or 0.0
        status = d.get('status', 'downloading')
        eta = f"{int(d['eta'])}s" if d.get('eta') else "---"
        downloaded = format_bytes(d.get('downloaded', 0))
        total = format_bytes(d['total']) if d.get('total') else "Unknown"
        sys.stdout.write(f"\r  [{status.upper()}] {pct:5.1f}% | {downloaded} / {total} | Speed: {speed_str} | ETA: {eta}  ")
        sys.stdout.flush()

    if engine == "turbo_hls":
        out_file = os.path.join(output_dir, "hls_stream.mp4")
        dl = TurboHlsDownloader(url, out_file, concurrency=threads, progress_callback=progress_callback)
        res = await dl.start()
    elif engine == "scribd":
        dl = ScribdDownloader(url, output_dir, progress_callback=progress_callback)
        res = await dl.download()
    elif engine == "flipbook":
        dl = FlipbookDownloader(url, output_dir, progress_callback=progress_callback, provider=classification.get("provider"))
        res = await dl.download()
    elif engine == "deep_zoom":
        dl = DeepZoomDownloader(url, output_dir, progress_callback=progress_callback)
        res = await dl.download()
    elif engine == "sketchfab_3d":
        dl = SketchfabDownloader(url, output_dir, progress_callback=progress_callback)
        res = await dl.download()
    elif engine == "spotify":
        from copita.core.spotify_downloader import SpotifyDownloader
        dl = SpotifyDownloader(download_dir=output_dir)
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(None, dl.download_track, url, progress_callback)
    elif engine == "file_hoster":
        from copita.core.filehoster_downloader import FileHosterDownloader
        dl = FileHosterDownloader(url, output_dir=output_dir, num_connections=threads, progress_callback=progress_callback)
        res = await dl.download()
    elif engine == "torrent":
        dn_match = re.search(r'dn=([^&]+)', url)
        raw_name = dn_match.group(1) if dn_match else 'Torrent'
        name = urllib.parse.unquote_plus(raw_name)
        out_file = os.path.join(output_dir, f"{name}.magnet")
        with open(out_file, "w") as f:
            f.write(url)
        print(f"\n🧲 Magnet URI detected: {name}")
        print(f"   Saved reference to: {out_file}")
        if sys.platform == 'darwin':
            try:
                subprocess.run(['open', url], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                print("   Dispatched to system torrent handler.")
            except Exception:
                pass
        res = out_file
    elif engine == "ytdlp" or classification.get("type") == "media_platform":
        wrapper = YtDlpWrapper()
        loop = asyncio.get_running_loop()
        out_tmpl = os.path.join(output_dir, "%(title).80B.%(ext)s")
        res = await loop.run_in_executor(
            None,
            lambda: wrapper.download(
                url, out_tmpl, format_id=format_id, audio_only=audio_only, progress_callback=progress_callback
            )
        )
    else:
        filename = classification.get("name") or "download.bin"
        out_file = os.path.join(output_dir, filename)
        dl = ChunkDownloader(url, out_file, num_connections=threads, progress_callback=progress_callback)
        res = await dl.start()

    print(f"\n\n✅ Download complete: {res}\n")

async def async_sniff(url: str):
    print(f"\n🔍 [Copita Deep Sniffer] Scanning: {url} ...")
    sniffer = MediaSniffer()
    res = await sniffer.sniff_url(url)
    print(f"📄 Page Title: {res.get('title')}")
    print(f"🎬 Media Streams Found: {len(res.get('media', []))}\n")
    for i, m in enumerate(res.get('media', []), 1):
        sz = format_bytes(m['size']) if m.get('size') else 'Adaptive/Stream'
        print(f"  [{i:2d}] {m.get('type').upper():6s} | {m.get('label'):30s} | {sz:>12s} | {m.get('url')[:80]}...")
    print()

def main():
    parser = argparse.ArgumentParser(prog="copita", description="Copita Universal Downloader")
    subparsers = parser.add_subparsers(dest="command")

    # download
    dl_parser = subparsers.add_parser("download", help="Download from any URL")
    dl_parser.add_argument("url", help="Target URL")
    dl_parser.add_argument("-o", "--output", default="./downloads", help="Output directory")
    dl_parser.add_argument("-t", "--threads", type=int, default=16, help="Worker threads")
    dl_parser.add_argument("-a", "--audio-only", action="store_true", help="Extract audio only (MP3)")
    dl_parser.add_argument("-f", "--format", default=None, help="Format ID")

    # sniff
    sniff_parser = subparsers.add_parser("sniff", help="Deep sniff any webpage for hidden streams")
    sniff_parser.add_argument("url", help="Webpage URL to inspect")

    # test
    subparsers.add_parser("test", help="Run the hard things test suite")

    # serve
    serve_parser = subparsers.add_parser("serve", help="Launch the Web UI server")
    serve_parser.add_argument("-p", "--port", type=int, default=8888, help="Port to listen on")
    serve_parser.add_argument("--open", action="store_true", help="Open browser on launch")

    args = parser.parse_args()

    if args.command == "download":
        asyncio.run(async_download(args.url, args.output, args.threads, args.audio_only, args.format))
    elif args.command == "sniff":
        asyncio.run(async_sniff(args.url))
    elif args.command == "test":
        from tests.test_hard_downloads import run_suite
        run_suite()
    elif args.command == "serve" or args.command is None:
        import uvicorn
        port = getattr(args, "port", 8888) if args.command == "serve" else 8888
        if getattr(args, "open", False):
            webbrowser.open(f"http://127.0.0.1:{port}")
        uvicorn.run("copita.api.server:app", host="127.0.0.1", port=port, reload=False)

if __name__ == "__main__":
    main()
