const BASE = "http://127.0.0.1:8888";

const ACTIVE = ["queued", "analyzing", "downloading", "merging", "compressing", "extracting", "paused"];
const INDETERMINATE = ["analyzing", "merging", "compressing", "extracting"];

document.addEventListener("DOMContentLoaded", async () => {
  const el = (id) => document.getElementById(id);
  const urlInput = el("url");
  const hint = el("hint");
  const intercept = el("intercept");
  const tasksEl = el("tasks");
  const resEl = el("resources");
  const bridge = el("bridge");
  const bridgeText = el("bridge-text");
  const tasksCount = el("tasks-count");
  const resCount = el("res-count");

  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab && tab.url && /^https?:/.test(tab.url)) urlInput.value = tab.url;

  // ---- settings ----------------------------------------------------------
  const stored = await chrome.storage.local.get("interceptEnabled");
  // Off by default — opt-in. When on, the browser's own downloads get
  // handed to Copita instead; users who haven't asked for that find it
  // surprising ("my download vanished").
  intercept.checked = stored.interceptEnabled === true;
  intercept.addEventListener("change", () => {
    chrome.storage.local.set({ interceptEnabled: intercept.checked });
  });

  el("open-app").addEventListener("click", () => {
    chrome.tabs.create({ url: "copita://open", active: false })
      .then((t) => setTimeout(() => chrome.tabs.remove(t.id).catch(() => {}), 1200))
      .catch(() => {});
  });

  // ---- send ------------------------------------------------------------
  function setHint(text, kind) {
    hint.textContent = text || "";
    hint.className = "hint" + (kind ? " " + kind : "");
  }

  function sendUrl(url) {
    if (!url) return;
    setHint("Sending…");
    chrome.runtime.sendMessage({ type: "copita-download", url }, (resp) => {
      const ok = resp && (resp.status === "success" || resp.status === "queued");
      if (ok) {
        setHint(resp.status === "queued" ? "Queued — Copita is starting…" : "Sent to Copita", "ok");
        setTimeout(pullTasks, 400);
      } else {
        setHint("Failed: " + (resp ? resp.error || "no response" : "no response"), "err");
      }
    });
  }

  el("send").addEventListener("click", () => sendUrl(urlInput.value.trim()));
  urlInput.addEventListener("keydown", (e) => { if (e.key === "Enter") sendUrl(urlInput.value.trim()); });

  // ---- helpers -------------------------------------------------------
  function fmtSize(n) {
    if (!n) return "";
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return `${n.toFixed(n < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
  }
  function fmtEta(s) {
    if (s == null || s <= 0 || !isFinite(s)) return "";
    if (s < 60) return `${Math.round(s)}s left`;
    if (s < 3600) return `${Math.round(s / 60)}m left`;
    return `${(s / 3600).toFixed(1)}h left`;
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]
    ));
  }
  // Clean line icons (stroke = currentColor, so they follow the theme).
  const ICON = {
    folder: '<path d="M3 6.5A1.5 1.5 0 0 1 4.5 5h4l2 2.2h7A1.5 1.5 0 0 1 19 8.7v8.8A1.5 1.5 0 0 1 17.5 19h-13A1.5 1.5 0 0 1 3 17.5Z"/>',
    video: '<rect x="3" y="5" width="16" height="12" rx="2"/><path d="m10 9 4 3-4 3Z" fill="currentColor" stroke="none"/>',
    audio: '<path d="M9 17V6l9-2v11"/><circle cx="6.5" cy="17" r="2.5"/><circle cx="15.5" cy="15" r="2.5"/>',
    doc: '<path d="M7 3h6l5 5v11a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Z"/><path d="M13 3v5h5M9 13h6M9 16h6"/>',
    image: '<rect x="3" y="4" width="16" height="14" rx="2"/><circle cx="8" cy="9" r="1.6" fill="currentColor" stroke="none"/><path d="m4 16 4.5-4.5L12 15l3-3 4 4"/>',
    archive: '<rect x="3" y="4" width="16" height="15" rx="2"/><path d="M3 9h16M11 4v5M9.5 12h3M9.5 14.5h3"/>',
    book: '<path d="M5 4h9a2 2 0 0 1 2 2v13H7a2 2 0 0 1-2-2Z"/><path d="M16 6h2a1 1 0 0 1 1 1v12H7"/>',
    disc: '<circle cx="11" cy="11" r="8"/><circle cx="11" cy="11" r="2.5"/>',
    file: '<path d="M7 3h6l5 5v11a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Z"/><path d="M13 3v5h5"/>',
  };
  function svg(key) {
    return `<svg viewBox="0 0 22 22" fill="none" stroke="currentColor" stroke-width="1.6" `
      + `stroke-linecap="round" stroke-linejoin="round">${ICON[key] || ICON.file}</svg>`;
  }
  function glyph(name, ctype, isFolder) {
    if (isFolder) return svg("folder");
    const s = (name + " " + (ctype || "")).toLowerCase();
    if (/\.(mp4|m4v|mkv|webm|mov|avi|flv|ts|m3u8|mpd)\b|video\/|mpegurl|dash/.test(s)) return svg("video");
    if (/\.(mp3|m4a|flac|wav|aac|ogg|opus)\b|audio\//.test(s)) return svg("audio");
    if (/\.(pdf|docx?|pptx?|xlsx?|rtf|txt)\b|pdf|officedocument|msword/.test(s)) return svg("doc");
    if (/\.(zip|rar|7z|tar|gz)\b|zip|rar|compress/.test(s)) return svg("archive");
    if (/\.(png|jpe?g|gif|webp|svg|bmp)\b|image\//.test(s)) return svg("image");
    if (/\.(epub|cbz|cbr)\b/.test(s)) return svg("book");
    if (/\.(apk|dmg|iso|exe)\b/.test(s)) return svg("disc");
    return svg("file");
  }
  function statusLine(t) {
    const st = t.status;
    if (st === "downloading") {
      const parts = [];
      if (t.percent != null) parts.push(`${Math.round(t.percent)}%`);
      if (t.total) parts.push(`${fmtSize(t.downloaded)} / ${fmtSize(t.total)}`);
      else if (t.downloaded) parts.push(fmtSize(t.downloaded));
      const tail = [];
      if (t.speed) tail.push(fmtSize(t.speed) + "/s");
      const eta = fmtEta(t.eta);
      if (eta) tail.push(eta);
      return `<span class="status-word">Downloading</span> · ${parts.join(" · ")}${tail.length ? " · " + tail.join(" · ") : ""}`;
    }
    const word = {
      queued: "Queued", analyzing: "Scanning", paused: "Paused",
      merging: "Finishing", compressing: "Packaging", extracting: "Extracting",
      completed: "Done", failed: "Failed",
    }[st] || st;
    if (st === "completed" && t.total) return `<span class="status-word">Done</span> · ${fmtSize(t.total)}`;
    if (st === "failed" && t.error) return `<span class="status-word">Failed</span> · ${esc(t.error).slice(0, 80)}`;
    return `<span class="status-word">${word}</span>`;
  }

  function taskCtl(id, action) {
    return () => {
      const url = action === "delete"
        ? `${BASE}/api/tasks/${id}`
        : `${BASE}/api/tasks/${id}/${action}`;
      fetch(url, { method: action === "delete" ? "DELETE" : "POST" })
        .then(() => setTimeout(pullTasks, 250))
        .catch(() => {});
    };
  }

  function renderTasks(list) {
    const shown = (list || []).filter((t) => ACTIVE.includes(t.status));
    tasksCount.hidden = shown.length === 0;
    tasksCount.textContent = shown.length;
    if (!shown.length) {
      tasksEl.innerHTML = '<div class="empty">Nothing downloading right now.</div>';
      return;
    }
    tasksEl.innerHTML = "";
    for (const t of shown) {
      const card = document.createElement("div");
      card.className = "task";
      const pct = typeof t.percent === "number" ? t.percent : 0;
      const indet = INDETERMINATE.includes(t.status) || (t.status === "downloading" && pct <= 0);
      const cls = t.status === "failed" ? "failed" : t.status === "completed" ? "done" : "";
      card.innerHTML =
        `<div class="task-top">` +
          `<div class="task-glyph">${glyph(t.title || t.url || "", t.category)}</div>` +
          `<div style="flex:1;min-width:0">` +
            `<div class="task-name">${esc(t.title || t.url || "Download")}</div>` +
            `<div class="task-sub ${cls}">${statusLine(t)}</div>` +
          `</div>` +
        `</div>` +
        `<div class="bar ${indet ? "indet" : ""}"><i style="width:${indet ? 35 : Math.max(2, pct)}%"></i></div>` +
        `<div class="task-actions"></div>`;
      const actions = card.querySelector(".task-actions");

      const mk = (label, fn, extra) => {
        const b = document.createElement("button");
        b.textContent = label;
        if (extra) b.className = extra;
        b.addEventListener("click", fn);
        actions.appendChild(b);
        return b;
      };
      if (t.status === "downloading" && t.can_pause) mk("Pause", taskCtl(t.id, "pause"));
      if (t.status === "paused") mk("Resume", taskCtl(t.id, "resume"), "go");
      mk("Cancel", taskCtl(t.id, "cancel"), "danger");
      tasksEl.appendChild(card);
    }
  }

  function renderResources(list) {
    const items = (list || []).slice(0, 20);
    resCount.hidden = items.length === 0;
    resCount.textContent = items.length;
    if (!items.length) {
      resEl.innerHTML = '<div class="empty">No media or files spotted yet.</div>';
      return;
    }
    resEl.innerHTML = "";
    for (const r of items) {
      const label = r.filename || (() => {
        try { return decodeURIComponent(new URL(r.url).pathname.split("/").filter(Boolean).pop() || r.url); }
        catch (e) { return r.url; }
      })();
      const meta = [r.ctype, r.size ? fmtSize(r.size) : ""].filter(Boolean).join(" · ");
      const div = document.createElement("div");
      div.className = "res";
      div.innerHTML =
        `<div class="res-glyph">${glyph(label, r.ctype)}</div>` +
        `<div class="res-body">` +
          `<div class="res-name">${esc(label)}</div>` +
          `<div class="res-meta">${esc(meta || "file")}</div>` +
        `</div>` +
        `<span class="res-send">Send →</span>`;
      div.addEventListener("click", () => {
        div.style.opacity = ".5";
        chrome.runtime.sendMessage({ type: "copita-download", url: r.url, filename: r.filename }, (resp) => {
          const ok = resp && (resp.status === "success" || resp.status === "queued");
          setHint(ok ? "Sent to Copita" : "Failed to send", ok ? "ok" : "err");
          div.style.opacity = "";
          if (ok) setTimeout(pullTasks, 400);
        });
      });
      resEl.appendChild(div);
    }
  }

  function setBridge(up) {
    bridge.className = "bridge " + (up ? "up" : "down");
    bridgeText.textContent = up ? "Connected" : "App not running";
  }

  // ---- live updates -------------------------------------------------
  chrome.runtime.onMessage.addListener((msg) => {
    if (msg && msg.type === "copita-tasks-update") pullTasks();
  });

  chrome.runtime.sendMessage({ type: "copita-resources-request" }, (resp) => {
    if (resp) renderResources(resp.resources);
  });

  // ---- cloud folder checklist (Google Drive) -----------------------
  const folderSec = el("folder-sec");
  const folderItemsEl = el("folder-items");
  const folderCount = el("folder-count");
  const folderTitle = el("folder-title");
  const selectAll = el("folder-select-all");
  const folderDownloadBtn = el("folder-download");
  let folderItems = [];
  const picked = new Set();

  function isCloudFolder(u) {
    return /drive\.google\.com\/(drive\/)?(u\/\d+\/)?folders\//.test(u || "");
  }
  function refreshFolderBtn() {
    folderDownloadBtn.textContent = `Download ${picked.size}`;
    folderDownloadBtn.disabled = picked.size === 0;
    selectAll.checked = folderItems.length > 0 && picked.size === folderItems.length;
  }
  function renderFolder(data) {
    folderItems = data.items || [];
    picked.clear();
    if (!folderItems.length) { folderSec.hidden = true; return; }
    folderSec.hidden = false;
    resEl.parentElement.hidden = true;   // hide the (useless-for-Drive) "On this page"
    folderTitle.textContent = data.folder_name
      ? (data.folder_name.length > 34 ? data.folder_name.slice(0, 33) + "…" : data.folder_name)
      : "In this folder";
    folderCount.hidden = false;
    folderCount.textContent = folderItems.length;
    folderItemsEl.innerHTML = "";
    folderItems.forEach((it, i) => {
      const row = document.createElement("label");
      row.className = "fitem";
      const g = glyph(it.name, "", it.is_folder);
      const meta = it.is_folder ? "folder" : (it.size ? fmtSize(it.size) : "file");
      row.innerHTML =
        `<input type="checkbox" data-i="${i}">` +
        `<span class="fglyph">${g}</span>` +
        `<span class="fbody"><span class="fname">${esc(it.name)}</span>` +
        `<span class="fmeta">${esc(meta)}</span></span>`;
      row.querySelector("input").addEventListener("change", (e) => {
        if (e.target.checked) picked.add(i); else picked.delete(i);
        refreshFolderBtn();
      });
      folderItemsEl.appendChild(row);
    });
    refreshFolderBtn();
  }

  selectAll.addEventListener("change", () => {
    picked.clear();
    if (selectAll.checked) folderItems.forEach((_, i) => picked.add(i));
    folderItemsEl.querySelectorAll("input[type=checkbox]").forEach((cb) => {
      cb.checked = selectAll.checked;
    });
    refreshFolderBtn();
  });

  folderDownloadBtn.addEventListener("click", () => {
    const chosen = [...picked].map((i) => folderItems[i]).filter(Boolean);
    if (!chosen.length) return;
    folderDownloadBtn.disabled = true;
    folderDownloadBtn.textContent = "Sending…";
    let done = 0;
    chosen.forEach((it) => {
      chrome.runtime.sendMessage({ type: "copita-download", url: it.url }, () => {
        if (++done === chosen.length) {
          setHint(`Sent ${chosen.length} to Copita`, "ok");
          folderDownloadBtn.textContent = `Download ${picked.size}`;
          folderDownloadBtn.disabled = picked.size === 0;
          setTimeout(pullTasks, 500);
        }
      });
    });
  });

  if (tab && isCloudFolder(tab.url)) {
    chrome.runtime.sendMessage({ type: "copita-folder-contents", url: tab.url }, (data) => {
      if (data && data.supported && (data.items || []).length) renderFolder(data);
    });
  }

  // The MV3 worker's task cache is lost when it sleeps — the backend is the
  // source of truth. Poll it, and merge live per-task fields the WS pushes.
  function pullTasks() {
    fetch(`${BASE}/api/tasks`)
      .then((r) => r.json())
      .then((d) => { setBridge(true); renderTasks(d.tasks || []); })
      .catch(() => { setBridge(false); renderTasks([]); });
  }
  pullTasks();
  const poll = setInterval(pullTasks, 1200);
  window.addEventListener("unload", () => clearInterval(poll));
});
