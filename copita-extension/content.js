(function () {
  const HOST_ID = "copita-overlay-host";
  let activeMedia = null;          // the <video>/<audio> the overlay is on
  let boundAt = 0;                 // ts the active video's src became a blob

  // ===== attribution: signals from probe.js (MAIN world) ==================
  const captured = [];            // { url, contentType, ts, isMaster }
  const mseMimes = new Set();     // codecs strings seen on SourceBuffers
  let sawMseUrl = false;

  const MEDIA_CT_RE = /^(video|audio)\//i;
  const MANIFEST_RE = /\.(m3u8|mpd)(\?|#|$)/i;
  const SEGMENT_RE = /\.(ts|m4s)(\?|#|$)|[?&]sq=\d|[?&](range|bytestart)=/i;

  function remember(url, contentType, isMaster) {
    if (!url || /^blob:|^data:/.test(url)) return;
    const ct = (contentType || "").toLowerCase();
    const isManifest = MANIFEST_RE.test(url) || /mpegurl|dash\+xml/.test(ct);
    if (!isManifest && !MEDIA_CT_RE.test(ct) && !/\.(mp4|m4v|webm|mov|m4a|mp3|aac|flac|ogg|wav)(\?|#|$)/i.test(url)) return;
    const existing = captured.find((c) => c.url === url);
    if (existing) { existing.ts = Date.now(); if (isMaster) existing.isMaster = true; return; }
    captured.push({ url, contentType: ct, ts: Date.now(), isMaster: !!isMaster || (isManifest && /stream-?inf/i.test(ct)) });
    if (captured.length > 80) captured.splice(0, captured.length - 80);
  }

  window.addEventListener("message", (ev) => {
    if (ev.source !== window) return;
    const d = ev.data;
    if (!d || d.__copitaMediaSignal !== true) return;
    if (d.kind === "request") remember(d.url, d.contentType, false);
    else if (d.kind === "hls") remember(d.url, "application/vnd.apple.mpegurl", d.isMaster);
    else if (d.kind === "mse_url") sawMseUrl = true;
    else if (d.kind === "mse_mime" || d.kind === "mse_append") { if (d.mime) mseMimes.add(d.mime.toLowerCase()); }
  }, false);

  // ===== which URL should we actually download? ==========================
  const YTDLP_HOSTS = /(^|\.)(youtube\.com|youtu\.be|music\.youtube\.com|tiktok\.com|bilibili\.com|twitch\.tv|vimeo\.com|dailymotion\.com|twitter\.com|x\.com|instagram\.com|facebook\.com|reddit\.com|soundcloud\.com|nebula\.tv|patreon\.com)$/i;

  function host() { return location.hostname; }

  function fresh(list) {
    return list.filter((c) => c.ts >= boundAt - 1500 || MANIFEST_RE.test(c.url));
  }
  function newest(list) {
    return list.slice().sort((a, b) => b.ts - a.ts)[0];
  }

  // Returns { url, mediaHint, merge } or { pageUrl } or null.
  function resolveDownload() {
    const el = activeMedia;

    const src = el && (el.currentSrc || el.src) || "";
    if (/^https?:\/\//i.test(src) && !MANIFEST_RE.test(src) && !/^blob:/.test(src)) {
      return { url: src, mediaHint: el.tagName === "AUDIO" ? "audio" : "video" };
    }

    if (YTDLP_HOSTS.test(host())) {
      const pageUrl = perVideoPageUrl();
      return pageUrl ? { pageUrl } : null;
    }

    const pool = fresh(captured);

    const master = pool.find((c) => c.isMaster) ||
                   pool.find((c) => MANIFEST_RE.test(c.url) && /\.m3u8/i.test(c.url));
    if (master) return { url: master.url, mediaHint: "video" };
    const anyManifest = pool.find((c) => MANIFEST_RE.test(c.url));
    if (anyManifest) return { url: anyManifest.url, mediaHint: "video" };

    const isDash = mseMimes.size >= 2 ||
      (pool.some((c) => /^video\//.test(c.contentType)) && pool.some((c) => /^audio\//.test(c.contentType)));
    const nonSeg = pool.filter((c) => !SEGMENT_RE.test(c.url));
    if (isDash) {
      const v = newest(nonSeg.filter((c) => /^video\//.test(c.contentType) || /\.(mp4|m4v|webm)(\?|#|$)/i.test(c.url)));
      const a = newest(nonSeg.filter((c) => /^audio\//.test(c.contentType) || /\.(m4a|aac|mp3|ogg)(\?|#|$)/i.test(c.url)));
      if (v && a) return { merge: { video: stripRange(v.url), audio: stripRange(a.url) }, mediaHint: "video" };
      if (v) return { url: stripRange(v.url), mediaHint: "video" };
    }

    const one = newest(nonSeg);
    if (one) return { url: stripRange(one.url), mediaHint: /^audio\//.test(one.contentType) ? "audio" : "video" };

    return { pageUrl: perVideoPageUrl() || location.href };
  }

  function stripRange(url) {
    try {
      const u = new URL(url);
      ["bytestart", "byteend", "range", "_nc_rmd"].forEach((k) => u.searchParams.delete(k));
      return u.toString();
    } catch (e) { return url; }
  }

  // ----- per-video page URL for feed/preview SPAs ------------------------
  function perVideoPageUrl() {
    const h = host();
    if (/(^|\.)tiktok\.com$/.test(h)) return resolveTikTok() || (isPerVideoUrl() ? location.href : null);
    if (/(^|\.)youtube\.com$/.test(h) || h === "youtu.be") return resolveYouTube() || (isPerVideoUrl() ? location.href : null);
    return location.href;
  }
  function isPerVideoUrl() {
    return /\/video\/\d+|\/watch\?v=|\/shorts\/|\/reel\/|\/p\//.test(location.href);
  }
  function resolveTikTok() {
    if (!activeMedia) return null;
    let el = activeMedia, aweme = null;
    for (let i = 0; i < 6 && el; i++) {
      const m = el.id && el.id.match(/^xgwrapper-\d+-(\d+)$/);
      if (m) { aweme = m[1]; break; }
      el = el.parentElement;
    }
    if (!aweme) return null;
    let section = activeMedia;
    for (let i = 0; i < 15 && section; i++) {
      if (section.tagName === "SECTION" && section.getAttribute("data-e2e") === "feed-video") break;
      section = section.parentElement;
    }
    const a = section && section.querySelector('a[href^="/@"]');
    if (!a) return null;
    return `https://www.tiktok.com${a.getAttribute("href").split("?")[0].replace(/\/$/, "")}/video/${aweme}`;
  }
  function resolveYouTube() {
    if (/\/watch\?v=|\/shorts\//.test(location.href)) return location.href;
    let el = activeMedia;
    for (let i = 0; i < 12 && el; i++) {
      const a = el.querySelector && el.querySelector('a[href*="/watch?v="], a[href^="/shorts/"]');
      if (a) return new URL(a.getAttribute("href"), location.origin).href;
      el = el.parentElement;
    }
    return null;
  }

  // ===== time helpers for the clip UI ===================================
  function parseTime(s) {
    s = String(s || "").trim();
    if (!s) return null;
    if (/^\d+(\.\d+)?$/.test(s)) return parseFloat(s);
    const parts = s.split(":").map(Number);
    if (parts.some(isNaN)) return null;
    return parts.reduce((acc, n) => acc * 60 + n, 0);
  }
  function fmtTime(sec) {
    sec = Math.max(0, Math.floor(sec || 0));
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    return (h ? `${h}:${String(m).padStart(2, "0")}` : `${m}`) + `:${String(s).padStart(2, "0")}`;
  }

  // ===== send to background ============================================
  function send(payload, btn, okText) {
    btn.disabled = true;
    const label = btn.dataset.label || btn.textContent;
    btn.dataset.label = label;
    btn.textContent = "Sending…";
    chrome.runtime.sendMessage(Object.assign({ type: "copita-download" }, payload), (resp) => {
      const ok = resp && (resp.status === "success" || resp.status === "queued");
      btn.textContent = ok ? (okText || "Sent") : "Failed";
      btn.classList.toggle("ok", ok);
      btn.classList.toggle("bad", !ok);
      setTimeout(() => {
        btn.textContent = label;
        btn.disabled = false;
        btn.classList.remove("ok", "bad");
      }, 2400);
    });
  }

  function doDownload(btn, clip) {
    const r = resolveDownload();
    if (!r) { flash(btn, "No media found"); return; }
    const payload = {};
    if (r.pageUrl) payload.url = r.pageUrl;
    else if (r.merge) { payload.merge = r.merge; payload.url = r.merge.video; }
    else payload.url = r.url;
    if (r.mediaHint) payload.mediaHint = r.mediaHint;
    if (clip) { payload.clip_start = clip.start; payload.clip_end = clip.end; }
    send(payload, btn, clip ? "Clip sent" : "Sent");
  }

  function flash(btn, text) {
    const label = btn.dataset.label || btn.textContent;
    btn.dataset.label = label;
    btn.textContent = text;
    btn.classList.add("bad");
    setTimeout(() => { btn.textContent = label; btn.classList.remove("bad"); }, 2200);
  }

  // ===== overlay UI (isolated in a shadow root) =========================
  const OVERLAY_CSS = `
    :host { all: initial; }
    * { box-sizing: border-box; margin: 0; }
    .wrap {
      position: fixed;
      z-index: 2147483647;
      display: none;
      flex-direction: column;
      align-items: flex-start;
      gap: 7px;
      font: 500 12px/1.3 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    }
    .wrap.show { display: flex; }
    .pill {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      padding: 7px 12px 7px 10px;
      border-radius: 999px;
      background: rgba(24, 24, 27, 0.92);
      color: #fff;
      border: 1px solid rgba(255, 255, 255, 0.12);
      box-shadow: 0 4px 16px rgba(0, 0, 0, 0.35), 0 1px 3px rgba(0, 0, 0, 0.3);
      backdrop-filter: blur(8px) saturate(1.4);
      -webkit-backdrop-filter: blur(8px) saturate(1.4);
      cursor: pointer;
      user-select: none;
      transition: transform .1s ease, background .12s;
    }
    .pill:hover { background: rgba(24, 24, 27, 0.98); transform: translateY(-1px); }
    .pill:active { transform: translateY(0); }
    .pill .glyph {
      width: 18px; height: 18px; flex: none;
      border-radius: 50%;
      background: #ea580c;
      display: grid; place-items: center;
    }
    .pill .glyph svg { width: 11px; height: 11px; fill: #fff; display: block; }
    .pill button {
      all: unset;
      font: inherit;
      font-weight: 600;
      cursor: pointer;
      white-space: nowrap;
    }
    .pill .sep { width: 1px; height: 13px; background: rgba(255,255,255,.18); flex: none; }
    .pill button.ok { color: #4ade80; }
    .pill button.bad { color: #f87171; }
    .clip-toggle {
      opacity: .78;
      display: inline-flex; align-items: center; gap: 4px;
    }
    .clip-toggle:hover { opacity: 1; }
    .panel {
      display: none;
      flex-direction: column;
      gap: 8px;
      padding: 11px;
      border-radius: 12px;
      background: rgba(24, 24, 27, 0.96);
      border: 1px solid rgba(255, 255, 255, 0.12);
      box-shadow: 0 8px 28px rgba(0, 0, 0, 0.45);
      backdrop-filter: blur(10px);
      -webkit-backdrop-filter: blur(10px);
      color: #e4e4e7;
    }
    .panel.show { display: flex; }
    .panel .label { font-size: 10.5px; text-transform: uppercase; letter-spacing: .05em; color: #a1a1aa; font-weight: 700; }
    .row { display: flex; align-items: center; gap: 5px; }
    .row input {
      all: unset;
      width: 58px;
      padding: 5px 7px;
      font: 500 12px -apple-system, sans-serif;
      color: #fff;
      background: rgba(255,255,255,.08);
      border: 1px solid rgba(255,255,255,.14);
      border-radius: 6px;
      text-align: center;
    }
    .row input:focus { border-color: #ea580c; background: rgba(255,255,255,.12); }
    .row .arrow { color: #a1a1aa; font-size: 11px; }
    .row .now {
      all: unset;
      cursor: pointer;
      font-size: 10px; font-weight: 700;
      color: #fb923c;
      padding: 4px 6px;
      border-radius: 5px;
      background: rgba(251,146,60,.12);
    }
    .row .now:hover { background: rgba(251,146,60,.22); }
    .panel .go {
      all: unset;
      cursor: pointer;
      text-align: center;
      font-weight: 650;
      color: #fff;
      background: #ea580c;
      padding: 7px;
      border-radius: 8px;
    }
    .panel .go:hover { background: #c2410c; }
    .panel .go.ok { background: #16a34a; }
    .panel .go.bad { background: #dc2626; }
  `;

  let shadow = null;
  let wrapEl = null;

  const DOWN_SVG = '<svg viewBox="0 0 24 24"><path d="M12 3v10.2l3.6-3.6L17 11l-5 5-5-5 1.4-1.4L11 13.2V3zM5 19h14v2H5z"/></svg>';

  function buildBar() {
    if (wrapEl) return wrapEl;
    const hostEl = document.createElement("div");
    hostEl.id = HOST_ID;
    shadow = hostEl.attachShadow({ mode: "open" });
    shadow.innerHTML = `
      <style>${OVERLAY_CSS}</style>
      <div class="wrap">
        <div class="pill">
          <span class="glyph">${DOWN_SVG}</span>
          <button data-act="dl">Copita</button>
          <span class="sep" data-vid></span>
          <button class="clip-toggle" data-act="clip" data-vid>✂ Clip</button>
        </div>
        <div class="panel">
          <span class="label">Clip a section</span>
          <div class="row">
            <input data-f="start" placeholder="0:00">
            <button class="now" data-f="start">now</button>
            <span class="arrow">→</span>
            <input data-f="end" placeholder="end">
            <button class="now" data-f="end">now</button>
          </div>
          <button class="go" data-act="clipdl">Download clip</button>
        </div>
      </div>`;
    (document.body || document.documentElement).appendChild(hostEl);
    wrapEl = shadow.querySelector(".wrap");
    const panel = shadow.querySelector(".panel");

    wrapEl.addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      e.preventDefault(); e.stopPropagation();
      const act = b.dataset.act;
      if (act === "dl") doDownload(b, null);
      else if (act === "clip") panel.classList.toggle("show");
      else if (b.dataset.f) {
        const inp = shadow.querySelector(`input[data-f="${b.dataset.f}"]`);
        inp.value = fmtTime(activeMedia ? activeMedia.currentTime : 0);
      } else if (act === "clipdl") {
        const sv = shadow.querySelector('input[data-f="start"]').value;
        const ev = shadow.querySelector('input[data-f="end"]').value;
        let start = parseTime(sv);
        let end = parseTime(ev);
        // "9:18" → "10" almost always means 10:00, not 10 seconds.
        if (start != null && end != null && end <= start && !/[:.]/.test(ev.trim())) {
          end = end * 60;
        }
        if (start == null || end == null || end <= start) { flash(b, "Check the times"); return; }
        doDownload(b, { start, end });
      }
    });
    return wrapEl;
  }

  function setVideoOnly(isVideo) {
    if (!shadow) return;
    shadow.querySelectorAll("[data-vid]").forEach((n) => { n.style.display = isVideo ? "" : "none"; });
    if (!isVideo) shadow.querySelector(".panel").classList.remove("show");
  }

  function positionBar(rect) {
    buildBar();
    wrapEl.classList.add("show");
    wrapEl.style.top = `${Math.max(8, rect.top + 12)}px`;
    wrapEl.style.left = `${Math.max(8, rect.left + 12)}px`;
    wrapEl.style.bottom = "";
    wrapEl.style.right = "";
    setVideoOnly(activeMedia && activeMedia.tagName === "VIDEO");
  }
  function positionFixed() {
    buildBar();
    wrapEl.classList.add("show");
    wrapEl.style.top = "";
    wrapEl.style.left = "";
    wrapEl.style.bottom = "16px";
    wrapEl.style.right = "16px";
    setVideoOnly(activeMedia && activeMedia.tagName === "VIDEO");
  }
  function hideBar() {
    if (wrapEl) wrapEl.classList.remove("show");
  }

  // ===== active-media scan (viewport + playing wins) ===================
  const AUDIO_PLATFORM = [/(^|\.)soundcloud\.com$/];
  function scan() {
    const els = Array.from(document.querySelectorAll("video, audio"));
    let best = null, bestScore = -1, bestRect = null;
    for (const m of els) {
      const isAudio = m.tagName === "AUDIO";
      const rect = m.getBoundingClientRect();
      if (isAudio) {
        if (m.paused) continue;
      } else {
        if (rect.width < 120 || rect.height < 80) continue;
        if (rect.bottom <= 0 || rect.right <= 0 || rect.top >= innerHeight || rect.left >= innerWidth) continue;
      }
      const playing = !m.paused && !m.ended ? 1e9 : 0;
      const score = playing + (m.readyState || 0) * 1e6 + (isAudio ? 0 : rect.width * rect.height);
      if (score > bestScore) { best = m; bestScore = score; bestRect = rect; }
    }

    if (!best) {
      if (AUDIO_PLATFORM.some((rx) => rx.test(host())) && location.pathname.length > 1) {
        activeMedia = null; positionFixed(); return;
      }
      hideBar();
      activeMedia = null;
      return;
    }

    if (best !== activeMedia) {
      activeMedia = best;
      boundAt = /^blob:/.test(best.currentSrc || best.src || "") ? Date.now() : 0;
    }
    positionBar(bestRect);
  }

  let scanQueued = false;
  function queueScan() {
    if (scanQueued) return;
    scanQueued = true;
    requestAnimationFrame(() => { scanQueued = false; scan(); });
  }

  const obs = new MutationObserver((records) => {
    for (const r of records) {
      if (r.target && r.target.id === HOST_ID) continue;
      queueScan();
      return;
    }
  });
  obs.observe(document.documentElement, { childList: true, subtree: true });
  addEventListener("scroll", queueScan, true);
  addEventListener("resize", queueScan);
  setInterval(scan, 1500);
  scan();
})();
