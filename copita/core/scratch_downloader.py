"""
Scratch project downloader.

A Scratch project isn't a file on a server — it's a `project.json` plus a
pile of asset files (costumes, sounds), referenced by content hash. The
`.sb3` everyone downloads is just a ZIP of all of that. This rebuilds it
from Scratch's public API:

  api.scratch.mit.edu/projects/<id>          -> metadata + a project_token
  projects.scratch.mit.edu/<id>?token=<tok>  -> project.json
  assets.scratch.mit.edu/internalapi/asset/<md5ext>/get/  -> each asset
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import zipfile
from typing import Any, Callable, Dict, List, Optional

from copita.core.http_client import HttpClient

_META = "https://api.scratch.mit.edu/projects/{pid}"
_PROJECT = "https://projects.scratch.mit.edu/{pid}"
_ASSET = "https://assets.scratch.mit.edu/internalapi/asset/{md5ext}/get/"
_ASSET_CDN = "https://cdn.assets.scratch.mit.edu/internalapi/asset/{md5ext}/get/"


def project_id(url: str) -> Optional[str]:
    m = re.search(r"scratch\.mit\.edu/projects/(\d+)", url)
    return m.group(1) if m else None


def _safe_name(s: str) -> str:
    s = re.sub(r'[\\/*?:"<>|\x00-\x1f]+', "", s or "").strip(" .")
    return (s or "scratch_project")[:120]


class ScratchDownloader:
    def __init__(self, url: str, output_dir: str,
                 progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
                 concurrency: int = 12):
        self.url = url
        self.output_dir = output_dir
        self.progress_callback = progress_callback
        self.concurrency = max(2, concurrency)
        self.is_cancelled = False

    def cancel(self):
        self.is_cancelled = True

    def _report(self, status: str, percent: float, msg: str = "",
                downloaded: int = 0, total: int = 0):
        if self.progress_callback:
            self.progress_callback({"status": status, "percent": percent,
                                    "message": msg, "downloaded": downloaded,
                                    "total": total})

    async def download(self) -> str:
        pid = project_id(self.url)
        if not pid:
            raise ValueError("That doesn't look like a Scratch project link "
                             "(expected scratch.mit.edu/projects/<number>).")

        os.makedirs(self.output_dir, exist_ok=True)
        self._report("analyzing", 0.0, "Reading the Scratch project…")

        async with HttpClient(timeout=25.0) as client:
            # 1. metadata → title + token (token is required for most projects)
            title, token = f"scratch_{pid}", None
            try:
                async with await client.get(_META.format(pid=pid)) as r:
                    if r.status == 200:
                        meta = json.loads(await r.read())
                        title = meta.get("title") or title
                        token = meta.get("project_token")
                    elif r.status in (404, 403):
                        raise ValueError("This Scratch project is private or doesn't exist.")
            except ValueError:
                raise
            except Exception:
                pass

            # 2. project.json (try with token, then a couple of fallbacks that
            #    work for older / unshared-but-public projects)
            proj_bytes = None
            candidates = []
            if token:
                candidates.append(f"{_PROJECT.format(pid=pid)}?token={token}")
            candidates += [_PROJECT.format(pid=pid),
                           f"https://projects.scratch.mit.edu/internalapi/project/{pid}/get/"]
            for u in candidates:
                try:
                    async with await client.get(u) as r:
                        if r.status == 200:
                            body = await r.read()
                            json.loads(body)  # validate
                            proj_bytes = body
                            break
                except Exception:
                    continue
            if proj_bytes is None:
                raise RuntimeError("Couldn't fetch this project's data from Scratch.")

            project = json.loads(proj_bytes)

            # 3. every unique asset referenced by any sprite/stage
            assets: List[str] = []
            seen = set()
            for target in project.get("targets", []):
                for item in list(target.get("costumes", [])) + list(target.get("sounds", [])):
                    md5ext = item.get("md5ext") or (
                        f"{item.get('assetId')}.{item.get('dataFormat')}"
                        if item.get("assetId") and item.get("dataFormat") else None)
                    if md5ext and md5ext not in seen:
                        seen.add(md5ext)
                        assets.append(md5ext)

            total = len(assets)
            self._report("downloading", 2.0, f"Downloading {total} assets…", 0, total)

            fetched: Dict[str, bytes] = {}
            sem = asyncio.Semaphore(self.concurrency)
            done = 0

            async def grab(md5ext: str):
                nonlocal done
                if self.is_cancelled:
                    return
                async with sem:
                    for tmpl in (_ASSET, _ASSET_CDN):
                        try:
                            async with await client.get(tmpl.format(md5ext=md5ext)) as r:
                                if r.status == 200:
                                    fetched[md5ext] = await r.read()
                                    break
                        except Exception:
                            continue
                done += 1
                self._report("downloading", 2.0 + (done / max(1, total)) * 92.0,
                             f"Downloading assets… {done}/{total}", done, total)

            await asyncio.gather(*(grab(a) for a in assets))
            if self.is_cancelled:
                raise asyncio.CancelledError("Download cancelled")

            missing = [a for a in assets if a not in fetched]
            if missing and len(missing) > total * 0.1:
                raise RuntimeError(
                    f"Only got {len(fetched)}/{total} of the project's assets — "
                    f"Scratch may be rate-limiting. Try again."
                )

            # 4. zip it into an .sb3 (project.json at the root, assets flat).
            # Building a multi-MB zip is CPU/IO work — off the event loop.
            self._report("compressing", 95.0, "Packaging .sb3…")
            out_path = os.path.join(self.output_dir, f"{_safe_name(title)}.sb3")

            def _build_zip():
                with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
                    z.writestr("project.json", proj_bytes)
                    for md5ext, data in fetched.items():
                        z.writestr(md5ext, data)

            await asyncio.get_running_loop().run_in_executor(None, _build_zip)

        self._report("completed", 100.0, f"Saved {os.path.basename(out_path)}",
                     os.path.getsize(out_path), os.path.getsize(out_path))
        return out_path
