const COPITA_BASE = "http://127.0.0.1:8888";
const WS_URL = "ws://127.0.0.1:8888/ws";

// ===========================================================================
// C2 — persistent bridge to the desktop app
// ---------------------------------------------------------------------------
// One long-lived WebSocket to the backend's /ws channel. It is the liveness
// signal: while it's open the app is up, so queued sends flush; while it's
// down, sends are cached (chrome.storage.session survives the MV3 service
// worker sleeping) and the app is nudged awake via the copita:// scheme.
// The socket also streams task_updated events so the toolbar badge and the
// popup can show live download status without polling.
// ===========================================================================

let ws = null;
let bridgeUp = false;
let reconnectDelay = 1000;
let activeTasks = {};        // taskId -> { title, status, percent }
let outbox = [];             // [{ body, tabId }] queued while the app is down

const ACTIVE_STATUSES = new Set([
  "queued", "analyzing", "downloading", "merging", "compressing", "extracting",
]);

function isActive(status) {
  return ACTIVE_STATUSES.has(status);
}

function summarize(t) {
  return {
    id: t.id,
    title: t.title || t.url || "Download",
    status: t.status,
    percent: typeof t.percent === "number" ? Math.round(t.percent) : null,
  };
}

async function loadOutbox() {
  try {
    const s = await chrome.storage.session.get("outbox");
    outbox = Array.isArray(s.outbox) ? s.outbox : [];
  } catch (e) {
    outbox = [];
  }
}

async function saveOutbox() {
  try { await chrome.storage.session.set({ outbox }); } catch (e) {}
}

function connectBridge() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  try {
    ws = new WebSocket(WS_URL);
  } catch (e) {
    scheduleReconnect();
    return;
  }
  ws.onopen = () => {
    bridgeUp = true;
    reconnectDelay = 1000;
    flushOutbox();
  };
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }
    if (msg.event === "init" && Array.isArray(msg.tasks)) {
      activeTasks = {};
      for (const t of msg.tasks) if (isActive(t.status)) activeTasks[t.id] = summarize(t);
    } else if (msg.task && msg.task.id) {
      const t = msg.task;
      if (msg.event === "task_deleted" || !isActive(t.status)) delete activeTasks[t.id];
      else activeTasks[t.id] = summarize(t);
    } else {
      return;
    }
    updateBadge();
    notifyPopup();
  };
  ws.onerror = () => {};   // onclose always follows
  ws.onclose = () => {
    bridgeUp = false;
    ws = null;
    scheduleReconnect();
  };
}

function scheduleReconnect() {
  setTimeout(connectBridge, reconnectDelay);
  reconnectDelay = Math.min(Math.round(reconnectDelay * 1.7), 30000);
}

// MV3 service workers are killed after ~30s idle; an open socket no longer
// reliably prevents that. A short-period alarm both keeps the worker warm
// and re-checks the connection.
chrome.alarms.create("copita-bridge", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name !== "copita-bridge") return;
  if (!bridgeUp) {
    connectBridge();
  } else {
    try { ws.send("ping"); } catch (e) { bridgeUp = false; ws = null; connectBridge(); }
  }
});

chrome.runtime.onStartup.addListener(() => { loadOutbox().then(connectBridge); });
chrome.runtime.onInstalled.addListener(() => { loadOutbox().then(connectBridge); registerContextMenu(); });
loadOutbox().then(connectBridge);

// ---------------------------------------------------------------------------
// Send a download to Copita — immediately if the bridge is up, else queue it
// and launch the app.
// ---------------------------------------------------------------------------

async function postDownload(body) {
  const resp = await fetch(`${COPITA_BASE}/api/download`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

async function flushOutbox() {
  if (!outbox.length) return;
  const pending = outbox.slice();
  outbox = [];
  await saveOutbox();
  for (const item of pending) {
    try {
      await postDownload(item.body);
    } catch (e) {
      outbox.push(item);   // still failing — keep it
    }
  }
  await saveOutbox();
}

async function sendToCopita(url, filename, referrer, tabId, mediaHint, capturedHeaders, extra) {
  const body = { url };
  if (filename) body.filename = String(filename).split(/[\\/]/).pop();
  const headers = Object.assign({}, capturedHeaders || {});
  // The desktop's HTTP client supplies its own browser-consistent
  // User-Agent (curl_cffi impersonation) — a captured UA would risk
  // mismatching the TLS fingerprint. Referer / Origin / Cookie / Range are
  // what actually matter for replaying an authenticated request.
  delete headers["User-Agent"];
  delete headers["Accept"];
  delete headers["Accept-Language"];
  if (referrer && !headers.Referer) headers.Referer = referrer;
  if (Object.keys(headers).length) body.headers = headers;
  if (mediaHint) body.media_hint = mediaHint;
  if (extra && typeof extra === "object") {
    if (extra.clip_start != null) body.clip_start = extra.clip_start;
    if (extra.clip_end != null) body.clip_end = extra.clip_end;
    if (extra.merge && extra.merge.video && extra.merge.audio) body.merge = extra.merge;
  }

  try {
    const data = await postDownload(body);
    flashBadge("OK", "#2e7d32");
    return { status: "success", task: data.task };
  } catch (e) {
    if (e instanceof TypeError) {
      // No response at all — the backend isn't up. Queue and launch it.
      outbox.push({ body, tabId });
      await saveOutbox();
      const launched = await launchCopitaWithDownload(url, tabId);
      flashBadge(launched ? "···" : "ERR", launched ? "#e8590c" : "#c62828");
      return { status: launched ? "queued" : "error", queued: true,
               error: launched ? undefined : "Copita isn't running and couldn't be started" };
    }
    flashBadge("ERR", "#c62828");
    return { status: "error", error: String(e) };
  }
}

async function launchCopitaWithDownload(url, tabId) {
  const launchUrl = `copita://download?url=${encodeURIComponent(url)}`;
  if (tabId != null) {
    try {
      await chrome.tabs.update(tabId, { url: launchUrl });
      return true;
    } catch (e) {}
  }
  try {
    const tab = await chrome.tabs.create({ url: launchUrl, active: false });
    setTimeout(() => chrome.tabs.remove(tab.id).catch(() => {}), 1500);
    return true;
  } catch (e) {
    return false;
  }
}

// ---------------------------------------------------------------------------
// Toolbar badge — live active-download count, with brief send-result flashes.
// ---------------------------------------------------------------------------

let flashTimer = null;

function updateBadge() {
  if (flashTimer) return;   // a flash is showing; it'll restore the count itself
  const n = Object.keys(activeTasks).length;
  chrome.action.setBadgeBackgroundColor({ color: "#e8590c" });
  chrome.action.setBadgeText({ text: n ? String(n) : "" });
}

function flashBadge(text, color) {
  if (flashTimer) clearTimeout(flashTimer);
  chrome.action.setBadgeBackgroundColor({ color });
  chrome.action.setBadgeText({ text });
  flashTimer = setTimeout(() => { flashTimer = null; updateBadge(); }, 1800);
}

function notifyPopup() {
  chrome.runtime.sendMessage({ type: "copita-tasks-update", tasks: Object.values(activeTasks) })
    .catch(() => {});   // no popup open
}

// ===========================================================================
// C1 — capture every downloadable resource the page fetches, with the exact
// request headers the browser used (Referer / Origin / Cookie / UA / Range).
// The desktop can then replay that request instead of guessing.
// ===========================================================================

const tabResources = {};          // tabId -> [{ url, ctype, filename, size, headers, ts }]
const pendingRequestHeaders = {};  // requestId -> { Referer, Origin, Cookie, ... }

const DOWNLOADABLE_CTYPES = [
  "application/pdf", "application/zip", "application/x-rar", "application/vnd.rar",
  "application/x-7z-compressed", "application/x-tar", "application/gzip",
  "application/epub+zip", "application/octet-stream",
  "application/vnd.apple.mpegurl", "application/x-mpegurl", "application/dash+xml",
  "application/vnd.ms-", "application/msword",
  "application/vnd.openxmlformats-",
];
const MEDIA_EXT_RE = /\.(mp4|m4v|mkv|webm|mov|avi|flv|ts|m3u8|mpd|mp3|m4a|flac|wav|aac|ogg|opus|pdf|epub|zip|rar|7z|tar|gz|docx?|pptx?|xlsx?|apk|dmg|iso)(\?|#|$)/i;
const SKIP_CTYPES = ["text/html", "text/css", "text/javascript", "application/javascript",
  "application/json", "image/svg", "font/", "application/font"];

function headersToObject(list, wanted) {
  const out = {};
  for (const h of list || []) {
    const name = h.name.toLowerCase();
    if (wanted.has(name)) out[canonicalHeader(name)] = h.value;
  }
  return out;
}

function canonicalHeader(lower) {
  return { referer: "Referer", origin: "Origin", cookie: "Cookie",
           "user-agent": "User-Agent", range: "Range",
           "accept": "Accept", "accept-language": "Accept-Language" }[lower] || lower;
}

const WANTED_REQ_HEADERS = new Set([
  "referer", "origin", "cookie", "user-agent", "accept", "accept-language",
]);

const MANIFEST_CTYPES = ["application/vnd.apple.mpegurl", "application/x-mpegurl",
  "application/mpegurl", "application/dash+xml"];
const MIN_MEDIA_BYTES = 400 * 1024;   // below this, an audio/video response is a
                                      // UI sound / sprite / ad ping, not content

function isDownloadable(ctype, cd, url, size) {
  const ct = (ctype || "").toLowerCase();
  if (SKIP_CTYPES.some((s) => ct.startsWith(s))) return false;
  if (/attachment/i.test(cd || "")) return true;

  // Streaming manifests: always keep, regardless of their (tiny) size.
  if (MANIFEST_CTYPES.some((s) => ct.startsWith(s)) || /\.(m3u8|mpd)(\?|#|$)/i.test(url)) {
    return true;
  }

  // Individual HLS/DASH media segments — hundreds per stream, useless on
  // their own. The manifest (kept above) is the download.
  if (ct.startsWith("video/mp2t") || /\.(ts|m4s)(\?|#|$)/i.test(url) ||
      /[?&]sq=\d/.test(url) || /segment\d|\bfrag(ment)?[-_]?\d|chunk-\d/i.test(url)) {
    return false;
  }

  if (ct.startsWith("audio/") || ct.startsWith("video/")) {
    // A real media file is large, OR is served without a Content-Length
    // (progressive stream) but has a media file extension in its URL.
    // Adaptive-stream range chunks (googlevideo etc.) have neither — skip
    // them; the site's own player / yt-dlp is the right path for those.
    if (size) return size >= MIN_MEDIA_BYTES;
    return MEDIA_EXT_RE.test(url);
  }

  if (DOWNLOADABLE_CTYPES.some((s) => ct.startsWith(s))) {
    if (ct.startsWith("application/octet-stream") && !MEDIA_EXT_RE.test(url) && !size) return false;
    return true;
  }
  if (ct.startsWith("image/")) return size ? size > 150 * 1024 : MEDIA_EXT_RE.test(url);
  return MEDIA_EXT_RE.test(url);
}

function filenameFromResponse(url, cd) {
  const m = /filename\*?=(?:UTF-8''|")?([^";]+)/i.exec(cd || "");
  if (m) return decodeURIComponent(m[1].replace(/"$/, ""));
  try {
    const p = new URL(url).pathname;
    const base = p.split("/").filter(Boolean).pop();
    if (base && base.includes(".")) return decodeURIComponent(base);
  } catch (e) {}
  return "";
}

try {
  chrome.webRequest.onSendHeaders.addListener(
    (details) => {
      if (details.tabId < 0) return;
      pendingRequestHeaders[details.requestId] =
        headersToObject(details.requestHeaders, WANTED_REQ_HEADERS);
    },
    { urls: ["<all_urls>"] },
    ["requestHeaders", "extraHeaders"],
  );

  chrome.webRequest.onHeadersReceived.addListener(
    (details) => {
      const reqHeaders = pendingRequestHeaders[details.requestId] || {};
      delete pendingRequestHeaders[details.requestId];
      if (details.tabId < 0) return;

      const resp = details.responseHeaders || [];
      let ctype = "", cd = "", size = 0;
      for (const h of resp) {
        const n = h.name.toLowerCase();
        if (n === "content-type") ctype = h.value;
        else if (n === "content-disposition") cd = h.value;
        else if (n === "content-length") size = parseInt(h.value, 10) || 0;
      }
      if (!isDownloadable(ctype, cd, details.url, size)) return;

      const list = tabResources[details.tabId] || (tabResources[details.tabId] = []);
      if (list.some((r) => r.url === details.url)) return;
      list.push({
        url: details.url,
        ctype: (ctype || "").split(";")[0].trim(),
        filename: filenameFromResponse(details.url, cd),
        size,
        headers: reqHeaders,
        ts: Date.now(),
      });
      if (list.length > 40) list.splice(0, list.length - 40);
    },
    { urls: ["<all_urls>"] },
    ["responseHeaders"],
  );

  // Don't let pendingRequestHeaders leak for requests that never reach
  // onHeadersReceived (cached, blocked, errored).
  const dropPending = (d) => { delete pendingRequestHeaders[d.requestId]; };
  chrome.webRequest.onCompleted.addListener(dropPending, { urls: ["<all_urls>"] });
  chrome.webRequest.onErrorOccurred.addListener(dropPending, { urls: ["<all_urls>"] });
} catch (e) {
  // webRequest unavailable (permission missing) — the overlay button and
  // download interception still work without page-resource capture.
}

// Clear a tab's captured resources when it navigates away or closes.
chrome.webNavigation && chrome.webNavigation.onCommitted.addListener((details) => {
  if (details.frameId === 0) delete tabResources[details.tabId];
});
chrome.tabs.onRemoved.addListener((tabId) => { delete tabResources[tabId]; });

// ===========================================================================
// Download interception (unchanged behaviour) + blob adoption
// ===========================================================================

chrome.downloads.onCreated.addListener(async (item) => {
  if (!item.url) return;
  const { interceptEnabled } = await chrome.storage.local.get("interceptEnabled");
  if (interceptEnabled !== true) return;  // opt-in, off by default
  if (item.url.startsWith("blob:") || item.url.startsWith("data:")) return;

  try {
    await chrome.downloads.cancel(item.id);
    await chrome.downloads.erase({ id: item.id });
  } catch (e) {}
  const [activeTab] = await chrome.tabs.query({ active: true, currentWindow: true }).catch(() => []);
  // If we captured this exact URL's request headers on the page, forward them.
  const captured = findCapturedHeaders(activeTab && activeTab.id, item.url);
  await sendToCopita(item.url, item.filename, item.referrer, activeTab && activeTab.id, undefined, captured);
});

function findCapturedHeaders(tabId, url) {
  const list = tabResources[tabId] || [];
  const hit = list.find((r) => r.url === url);
  return hit ? hit.headers : null;
}

chrome.downloads.onChanged.addListener(async (delta) => {
  if (!delta.state || delta.state.current !== "complete") return;
  const { interceptEnabled } = await chrome.storage.local.get("interceptEnabled");
  if (interceptEnabled !== true) return;  // opt-in, off by default
  const [item] = await chrome.downloads.search({ id: delta.id });
  if (!item || !item.url || !/^(blob|data):/.test(item.url)) return;
  if (!item.filename || item.exists === false) return;
  if (item.fileSize && item.fileSize < 1024) return;
  try {
    await fetch(`${COPITA_BASE}/api/import-file`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: item.filename, source_url: item.referrer || "" }),
    });
    flashBadge("OK", "#2e7d32");
  } catch (e) {}
});

// ===========================================================================
// Messages from the popup / the on-page overlay button
// ===========================================================================

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg) return;
  const tabId = sender.tab ? sender.tab.id : undefined;

  if (msg.type === "copita-download") {
    const captured = msg.url ? findCapturedHeaders(tabId, msg.url) : null;
    sendToCopita(msg.url, msg.filename, sender.tab ? sender.tab.url : undefined,
                 tabId, msg.mediaHint, captured,
                 { clip_start: msg.clip_start, clip_end: msg.clip_end, merge: msg.merge })
      .then(sendResponse);
    return true;
  }

  if (msg.type === "copita-tasks-request") {
    sendResponse({ tasks: Object.values(activeTasks), bridgeUp });
    return false;
  }

  if (msg.type === "copita-resources-request") {
    chrome.tabs.query({ active: true, currentWindow: true }).then(([tab]) => {
      const list = (tabResources[tab && tab.id] || []).slice().reverse();
      sendResponse({ resources: list });
    });
    return true;
  }

  // Cloud-folder listing (Google Drive) — so the popup can show a checklist
  // and the user picks which files to send, instead of "download everything".
  if (msg.type === "copita-folder-contents") {
    fetch(`${COPITA_BASE}/api/folder-contents`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: msg.url }),
    })
      .then((r) => r.json())
      .then((d) => sendResponse(d))
      .catch((e) => sendResponse({ supported: false, items: [], error: String(e) }));
    return true;
  }
});

// ===========================================================================
// Native right-click "Download with Copita"
// ===========================================================================

const CONTEXT_MENU_ID = "copita-download-with-copita";
function registerContextMenu() {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: CONTEXT_MENU_ID,
      title: "Download with Copita",
      contexts: ["image", "video", "audio", "link"],
    });
  });
}
chrome.runtime.onInstalled.addListener(registerContextMenu);
chrome.runtime.onStartup.addListener(registerContextMenu);

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId !== CONTEXT_MENU_ID) return;
  let url = info.srcUrl || info.linkUrl;
  if (!url || url.startsWith("blob:") || url.startsWith("data:")) {
    url = info.pageUrl || (tab && tab.url);
  }
  if (!url) return;
  const captured = findCapturedHeaders(tab && tab.id, url);
  sendToCopita(url, undefined, tab ? tab.url : undefined, tab ? tab.id : undefined, undefined, captured);
});
