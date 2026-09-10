"""
FastAPI Server for Copita Downloader.
Handles REST endpoints, WebSocket live updates, media streaming, and serves the web frontend.
"""

import os
import re
import sys
import shutil
import asyncio
import subprocess
import mimetypes
from typing import Dict, Any, Optional
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from copita.core.classifier import classify_url, LinkType
from copita.core.task_manager import TaskManager
from copita.core.sniffer import MediaSniffer
from copita.core.ytdlp_wrapper import YtDlpWrapper
from copita.core.manga_downloader import MangaDownloader
from copita.core.requirements_checker import RequirementsManager

app = FastAPI(title="Copita Downloader API", version="2.0.0")

# Setup directories
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE_ROOT = os.path.dirname(BASE_DIR)
# Deliberately NOT derived from WORKSPACE_ROOT (unlike STATIC_DIR below).
# WORKSPACE_ROOT reflects wherever this specific copy of the `copita`
# package happens to be running from -- fine for the web app's own static
# assets (a dev/self-hosted deployment concern), but for a macOS app
# installed from a DMG, that's the app bundle's own Contents/Resources.
# Saving user downloads *inside the .app bundle* is broken on every level:
# fragile permissions, wiped on reinstall/update, and nobody would ever
# find their files there in Finder. Every test this session ran the app
# straight from the dev project folder, where the old
# WORKSPACE_ROOT-derived path happened to coincidentally resolve to the
# real project's downloads/ folder -- masking this until actually tracing
# through what a real /Applications install would do. A fixed, standard,
# user-visible location under ~/Downloads is correct regardless of where the
# app binary lives.
# COPITA_DOWNLOAD_DIR lets a test / a packaging script point the backend at
# a throwaway location instead of the user's real Downloads folder.
DOWNLOAD_DIR = os.environ.get("COPITA_DOWNLOAD_DIR") or \
    os.path.join(os.path.expanduser("~"), "Downloads", "Copita Downloads")
# The web frontend is its own self-contained sibling directory (copita-web/),
# not nested inside this backend package — copita/ is the shared engine both
# the web app and the native macOS app talk to, and it has no business
# bundling one specific frontend's assets (the macOS app's build previously
# pulled in this whole directory including multi-MB web images it never
# needed, just because they happened to live inside copita/).
STATIC_DIR = os.path.join(WORKSPACE_ROOT, "copita-web")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)

manager = TaskManager(download_dir=DOWNLOAD_DIR)
sniffer = MediaSniffer()
requirements_manager = RequirementsManager()

# WebSocket connections registry
active_websockets: list[WebSocket] = []
main_loop: Optional[asyncio.AbstractEventLoop] = None

# The desktop app's WKWebView persists its HTTP cache across relaunches. The
# HTML shell has no cache-busting query string of its own (only the CSS/JS it
# references do), so without this a rebuilt app keeps serving a stale,
# already-cached index.html forever and every UI fix silently fails to show.
@app.middleware("http")
async def no_cache_for_html_shell(request: Request, call_next):
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.endswith(".html"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response

@app.on_event("startup")
async def startup_event():
    global main_loop
    main_loop = asyncio.get_running_loop()
    # Keep yt-dlp current — YouTube/etc. break it weekly. Background thread,
    # never blocks startup.
    try:
        from copita.core.autoupdate import start_periodic
        start_periodic()
    except Exception:
        pass

@app.on_event("shutdown")
async def shutdown_event():
    # Belt-and-suspenders alongside the throttled saves on every broadcast:
    # guarantees the latest state is flushed even if the app quits inside
    # that throttle window (e.g. right after a task was just created).
    manager._save_state_now()

def ws_broadcaster(data: Dict[str, Any]):
    async def _send():
        for ws in list(active_websockets):
            try:
                await ws.send_json(data)
            except Exception:
                if ws in active_websockets:
                    active_websockets.remove(ws)

    global main_loop
    try:
        current_loop = asyncio.get_running_loop()
        current_loop.create_task(_send())
    except RuntimeError:
        if main_loop and main_loop.is_running():
            main_loop.call_soon_threadsafe(lambda: asyncio.create_task(_send()))

manager.register_listener(ws_broadcaster)
# ws_broadcaster just forwards whatever dict it's given to every open socket,
# so the same one works for requirement-install progress too — no need for a
# second broadcast channel.
requirements_manager.register_listener(ws_broadcaster)
try:
    from copita.core import autoupdate as _autoupdate
    _autoupdate.register_listener(ws_broadcaster)
except Exception:
    pass

class AnalyzeRequest(BaseModel):
    url: str

class DownloadRequest(BaseModel):
    url: str
    threads: Optional[int] = 16
    format_id: Optional[str] = None
    audio_only: Optional[bool] = False
    audio_format: Optional[str] = "mp3"
    video_quality: str = "best"
    filename: Optional[str] = None
    headers: Optional[Dict[str, str]] = None
    # "audio" / "video" — set by the browser extension from the element the
    # user clicked, so a page whose media can't be extracted fails cleanly
    # instead of falling back to downloading the page's share image.
    media_hint: Optional[str] = None
    # Download only a time window of the video (seconds). yt-dlp fetches
    # just the needed bytes and ffmpeg trims to exact boundaries.
    clip_start: Optional[float] = None
    clip_end: Optional[float] = None
    # Separate DASH video + audio track URLs the extension resolved — the
    # backend downloads both and ffmpeg-muxes them into one file.
    merge: Optional[Dict[str, str]] = None

class SniffRequest(BaseModel):
    url: str

@app.get("/api/health")
async def health_check():
    return {
        "status": "healthy",
        "engine": "multi-threaded-chunk-turbo",
        "active_tasks": sum(t.status in ('analyzing', 'downloading', 'merging', 'queued', 'compressing') for t in manager.tasks.values())
    }

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_websockets.append(websocket)
    # Send initial state
    await websocket.send_json({"event": "init", "tasks": manager.get_all_tasks()})
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        if websocket in active_websockets:
            active_websockets.remove(websocket)

@app.post("/api/analyze")
async def analyze_url(req: AnalyzeRequest):
    url = req.url.strip()
    classification = classify_url(url)
    
    # If media platform, get full formats and title via yt-dlp
    if classification.get("type") in (LinkType.MEDIA_PLATFORM, LinkType.GENERIC_WEBPAGE):
        try:
            loop = asyncio.get_running_loop()
            wrapper = YtDlpWrapper(cookies_browser=manager.cookies(url))
            info = await loop.run_in_executor(None, lambda: wrapper.inspect_url(url))
            return {
                "classification": classification,
                "info": info
            }
        except Exception:
            # Fallback to basic classification
            pass

    # If manga platform, get manga title and chapter list
    if classification.get("type") == LinkType.MANGA:
        try:
            loop = asyncio.get_running_loop()
            manga_info = await loop.run_in_executor(None, lambda: MangaDownloader.inspect(url))
            return {
                "classification": classification,
                "info": {
                    "title": manga_info.get("title", classification.get("name")),
                    "chapters": manga_info.get("chapters", []),
                    "total_chapters": manga_info.get("total_chapters", 0),
                    "formats": []
                }
            }
        except Exception:
            pass

    # If video embed platform (e.g. viduki.net), get movie metadata and stream formats
    if classification.get("engine") == "video_embed":
        try:
            from copita.core.video_embed_downloader import VideoEmbedDownloader
            loop = asyncio.get_running_loop()
            embed_info = await VideoEmbedDownloader.inspect(url)
            return {
                "classification": classification,
                "info": {
                    "title": embed_info.get("title", classification.get("name")),
                    "overview": embed_info.get("overview", ""),
                    "master_url": embed_info.get("master_url", ""),
                    "formats": embed_info.get("formats", [])
                }
            }
        except Exception:
            pass

    return {

        "classification": classification,
        "info": {
            "title": classification.get("name", "Direct Link"),
            "formats": []
        }
    }

@app.post("/api/sniff")
async def sniff_page(req: SniffRequest):
    results = await sniffer.sniff_url(req.url.strip())
    return results


@app.post("/api/folder-contents")
async def folder_contents(req: SniffRequest):
    """List the files inside a cloud folder (Google Drive for now) WITHOUT
    downloading anything — so the browser extension can show a checklist and
    the user picks what they actually want. `items[].url` is a normal
    per-file link that can be handed straight to /api/download."""
    url = req.url.strip()
    try:
        from copita.core.filehoster_downloader import FileHosterDownloader
        fh = FileHosterDownloader(url, output_dir=DOWNLOAD_DIR)
        folder_id = FileHosterDownloader.extract_gdrive_id(url)
        is_drive_folder = "drive.google.com" in url and ("/folders/" in url or "/drive/" in url)
        if not (folder_id and is_drive_folder):
            return {"supported": False, "items": []}
        folder_name, children = await fh._list_gdrive_folder_items(folder_id)
        items = []
        for c in children:
            items.append({
                "name": c["name"],
                "size": c.get("size"),
                "category": "folder" if c.get("is_folder") else c.get("category", "file"),
                "is_folder": bool(c.get("is_folder")),
                "url": (f"https://drive.google.com/drive/folders/{c['id']}"
                        if c.get("is_folder")
                        else f"https://drive.google.com/file/d/{c['id']}/view"),
            })
        return {"supported": True, "provider": "gdrive",
                "folder_name": folder_name, "items": items}
    except Exception as e:
        return {"supported": False, "items": [], "error": str(e)}

@app.post("/api/download")
async def start_download(req: DownloadRequest):
    url = req.url.strip()
    options = {
        "threads": req.threads or 16,
        "format_id": req.format_id,
        "audio_only": req.audio_only,
        "audio_format": req.audio_format or "mp3",
        "video_quality": req.video_quality,
        "filename": req.filename,
        "headers": req.headers or {},
        "media_hint": req.media_hint,
        "clip_start": req.clip_start,
        "clip_end": req.clip_end,
        "merge": req.merge,
    }
    task = manager.add_task(url, options)
    return {"status": "success", "task": task.to_dict()}

class ImportFileRequest(BaseModel):
    path: str
    source_url: Optional[str] = None

@app.post("/api/import-file")
async def import_file(req: ImportFileRequest):
    """Adopt a file the browser already downloaded (a blob: download the
    extension can't forward as a URL — Telegram Web media, etc.) into
    Copita's library."""
    try:
        # Copy, don't move — the browser already saved this file where the
        # user expects it; relocating it out from under the browser makes it
        # show as "Deleted" in the downloads list. Copita keeps its own copy.
        task = manager.import_local_file(req.path, req.source_url, move=False)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"status": "error", "error": str(e)})
    return {"status": "success", "task": task.to_dict()}

@app.get("/api/tasks")
async def list_tasks():
    return {"tasks": manager.get_all_tasks()}

@app.post("/api/tasks/{task_id}/pause")
async def pause_task(task_id: str):
    manager.pause_task(task_id)
    return {"status": "paused"}

@app.post("/api/tasks/{task_id}/retry")
async def retry_task(task_id: str):
    if task_id not in manager.tasks:
        raise HTTPException(status_code=404, detail='Download not found')
    try:
        task = manager.retry_task(task_id)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error))
    return {"status": "success", "task": task.to_dict()}

@app.post("/api/tasks/{task_id}/resume")
async def resume_task(task_id: str):
    manager.resume_task(task_id)
    return {"status": "resumed"}

@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: str):
    manager.cancel_task(task_id)
    return {"status": "cancelled"}

@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str, delete_file: bool = Query(False)):
    manager.delete_task(task_id, delete_file=delete_file)
    return {"status": "deleted"}

@app.post("/api/open-directory")
async def open_download_directory():
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    if sys.platform == "darwin":
        subprocess.run(["open", DOWNLOAD_DIR])
        return {"status": "opened", "path": DOWNLOAD_DIR}
    return {"status": "unsupported_platform"}

@app.post("/api/queue/pause-all")
async def pause_all_tasks():
    count = 0
    for task_id, task in list(manager.tasks.items()):
        if task.status == 'downloading' and hasattr(task.worker_instance, 'pause'):
            manager.pause_task(task_id)
            count += 1
    return {"status": "success", "paused": count}

@app.post("/api/queue/resume-all")
async def resume_all_tasks():
    count = 0
    for task_id, task in list(manager.tasks.items()):
        if task.status == "paused":
            manager.resume_task(task_id)
            count += 1
    return {"status": "success", "resumed": count}

@app.delete("/api/tasks-completed")
async def clear_completed_tasks():
    count = 0
    for task_id, task in list(manager.tasks.items()):
        if task.status == "completed":
            manager.delete_task(task_id, delete_file=False)
            count += 1
    return {"status": "success", "cleared": count}

@app.post("/api/tasks/{task_id}/open")
async def open_file(task_id: str, reveal: bool = Query(False)):
    task = manager.tasks.get(task_id)
    if not task or not task.output_path or not os.path.exists(task.output_path):
        raise HTTPException(status_code=404, detail="File not found on disk")

    if sys.platform == "darwin":
        cmd = ["open", "-R", task.output_path] if reveal else ["open", task.output_path]
        subprocess.run(cmd)
        return {"status": "opened"}
    return {"status": "unsupported_platform"}

@app.get("/api/stream/{task_id}")
@app.head("/api/stream/{task_id}")
async def stream_media(task_id: str):
    task = manager.tasks.get(task_id)
    if not task or not task.output_path or not os.path.exists(task.output_path):
        raise HTTPException(status_code=404, detail="Media file not found")
    media_type, _ = mimetypes.guess_type(task.output_path)
    return FileResponse(
        task.output_path,
        media_type=media_type or "application/octet-stream",
        content_disposition_type="inline"
    )

@app.get("/api/system")
async def get_system_info():
    total, used, free = shutil.disk_usage(DOWNLOAD_DIR)
    
    # Check yt-dlp version
    ytdlp_ver = "Unknown"
    try:
        import yt_dlp
        ytdlp_ver = yt_dlp.version.__version__
    except Exception:
        pass

    # Check ffmpeg version
    ffmpeg_ver = "Unknown"
    try:
        res = subprocess.run(["ffmpeg", "-version"], stdout=subprocess.PIPE, text=True)
        ffmpeg_ver = res.stdout.splitlines()[0]
    except Exception:
        pass

    return {
        "app_name": "Copita Downloader",
        "version": "2.0.0",
        "download_dir": DOWNLOAD_DIR,
        "disk_free_gb": round(free / (1024**3), 2),
        "disk_total_gb": round(total / (1024**3), 2),
        "ffmpeg": ffmpeg_ver,
        "ytdlp": ytdlp_ver,
        "default_threads": manager.default_threads,
        "cookies_browser": manager.cookies_browser,
        "cookies_browser_resolved": manager.cookies(),
        "max_concurrent_downloads": manager.max_concurrent_downloads,
        "max_speed_kbps": manager.max_speed_kbps,
        "proxy_url": manager.proxy_url,
        "verify_ssl": manager.verify_ssl,
    }

@app.post("/api/settings")
async def update_settings(data: Dict[str, Any]):
    if "threads" in data:
        manager.default_threads = int(data["threads"])
    if "cookies_browser" in data:
        new_browser = data["cookies_browser"]
        new_browser = "" if new_browser is None else str(new_browser)
        if new_browser != manager.cookies_browser:
            try:
                from copita.core.browser_cookies import clear_cache
                clear_cache()
            except Exception:
                pass
        manager.cookies_browser = new_browser
    if "max_concurrent" in data:
        await manager.set_max_concurrent(int(data["max_concurrent"]))
    if "max_speed_kbps" in data:
        try:
            manager.max_speed_kbps = max(0, int(data["max_speed_kbps"]))
        except (TypeError, ValueError):
            pass
    if "proxy_url" in data:
        pu = (data["proxy_url"] or "").strip()
        if pu and not re.match(r"^(https?|socks[45]h?)://", pu, re.I):
            return {"status": "error",
                    "error": "Proxy must start with http://, https://, socks5:// or socks4://"}
        manager.proxy_url = pu
    if "verify_ssl" in data:
        manager.verify_ssl = bool(data["verify_ssl"])
    manager.apply_http_config()
    manager.save_settings()
    return {"status": "success", "settings": {
        "threads": manager.default_threads,
        "cookies_browser": manager.cookies_browser,
        "max_concurrent": manager.max_concurrent_downloads,
        "max_speed_kbps": manager.max_speed_kbps,
        "proxy_url": manager.proxy_url,
        "verify_ssl": manager.verify_ssl,
    }}

@app.get("/api/ytdlp-status")
async def ytdlp_status():
    from copita.core.autoupdate import status
    return status()

@app.post("/api/ytdlp-update")
async def ytdlp_update():
    """Force a yt-dlp version check + update now."""
    from copita.core.autoupdate import maybe_check
    maybe_check(force=True)
    return {"status": "checking"}

@app.get("/api/requirements")
async def get_requirements():
    # Re-checks real system state every time rather than trusting a cache —
    # if the user installed something in a terminal themselves, this must
    # reflect that immediately rather than still offering to install it.
    await requirements_manager.refresh_all_async()
    return {"requirements": requirements_manager.get_all()}

@app.post("/api/requirements/{req_id}/install")
async def install_requirement(req_id: str):
    try:
        requirements_manager.validate_install(req_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))
    requirements_manager.start_install(req_id)
    return {"status": "started"}

@app.post("/api/run-tests")
async def execute_tests():
    """Run the 7 hard download tests and return output."""
    cmd = [sys.executable, os.path.join(os.path.dirname(BASE_DIR), "tests", "test_hard_downloads.py")]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT
    )
    stdout, _ = await proc.communicate()
    return {
        "exit_code": proc.returncode,
        "output": stdout.decode("utf-8", errors="ignore")
    }

# Mount static frontend
if os.path.exists(STATIC_DIR):
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("copita.api.server:app", host="127.0.0.1", port=8888, reload=False)

