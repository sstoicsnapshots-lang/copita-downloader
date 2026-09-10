/*
 * Copita — MSE / network attribution probe.
 *
 * Runs in the page's MAIN world at document_start so it can patch the real
 * DOM APIs before any player uses them. It captures NOTHING (no buffers, no
 * bodies) — it only reports, via window.postMessage, which network requests
 * and which MediaSource activity happened, so the content script
 * (ISOLATED world) can work out the real media URL feeding a <video>.
 *
 * Approach adapted from Ghost-Downloader-3's mse-probe (which credits
 * cat-catch, GPL-3.0).
 */
(function () {
  if (window.__copitaProbeInstalled) return;
  window.__copitaProbeInstalled = true;

  const KEY = "__copitaMediaSignal";
  function emit(sig) {
    try { window.postMessage(Object.assign({ [KEY]: true }, sig), "*"); } catch (e) {}
  }

  // --- MediaSource: blob URL <-> MediaSource, and buffer mime types --------
  const msIds = new WeakMap();
  let msCounter = 0;
  function msId(ms) {
    let id = msIds.get(ms);
    if (!id) { id = "ms-" + (++msCounter); msIds.set(ms, id); }
    return id;
  }

  // Each patch is independent — a page that froze one API must not stop the
  // others from installing.
  try {
    if (typeof URL !== "undefined" && typeof URL.createObjectURL === "function" &&
        typeof MediaSource !== "undefined") {
      const orig = URL.createObjectURL.bind(URL);
      URL.createObjectURL = function (obj) {
        const url = orig(obj);
        try {
          if (obj instanceof MediaSource) emit({ kind: "mse_url", id: msId(obj), objectUrl: url });
        } catch (e) {}
        return url;
      };
    }
  } catch (e) {}

  try {
    if (typeof MediaSource !== "undefined" && MediaSource.prototype.addSourceBuffer) {
      const origAdd = MediaSource.prototype.addSourceBuffer;
      MediaSource.prototype.addSourceBuffer = function (mime) {
        const sb = origAdd.call(this, mime);
        const id = msId(this);
        emit({ kind: "mse_mime", id, mime: String(mime || "") });
        try {
          const origAppend = sb.appendBuffer;
          sb.appendBuffer = function (data) {
            emit({ kind: "mse_append", id, mime: String(mime || "") });
            return origAppend.call(this, data);
          };
        } catch (e) {}
        return sb;
      };
    }
  } catch (e) {}

  // --- fetch / XHR: report completed requests + sniff HLS manifests --------
  function looksLikeManifestText(t) {
    return typeof t === "string" && t.slice(0, 7) === "#EXTM3U";
  }

  function reportResponse(url, contentType) {
    if (!url || /^data:|^blob:/.test(url)) return;
    emit({ kind: "request", url, contentType: contentType || "" });
  }

  try {
   if (typeof fetch === "function") {
    const origFetch = fetch;
    window.fetch = function (input, init) {
      let url = "";
      try {
        url = typeof input === "string" ? input
            : (input && input.url) ? input.url : String(input || "");
      } catch (e) {}
      const p = origFetch(input, init);
      p.then((resp) => {
        try {
          const rurl = (resp && resp.url) || url;
          const ct = resp && resp.headers && resp.headers.get ? resp.headers.get("content-type") : "";
          reportResponse(rurl, ct);
          const clen = parseInt((ct && resp.headers.get("content-length")) || "0", 10);
          if (clen && clen < 2_000_000 && resp.body && resp.clone) {
            const rdr = resp.clone().body.getReader();
            rdr.read().then(({ value }) => {
              rdr.cancel();
              if (!value || value[0] !== 0x23) return;
              const txt = new TextDecoder().decode(value.slice(0, 400));
              if (looksLikeManifestText(txt)) {
                emit({ kind: "hls", url: rurl, isMaster: txt.indexOf("#EXT-X-STREAM-INF") !== -1 });
              }
            }).catch(() => {});
          }
        } catch (e) {}
      }).catch(() => {});
      return p;
    };
   }
  } catch (e) {}

  try {
    const origOpen = XMLHttpRequest.prototype.open;
    const origSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function (m, u) {
      this.__copitaUrl = String(u);
      return origOpen.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function (body) {
      const xhr = this;
      xhr.addEventListener("loadend", function () {
        const rurl = xhr.responseURL || xhr.__copitaUrl || "";
        try {
          reportResponse(rurl, xhr.getResponseHeader ? xhr.getResponseHeader("content-type") : "");
        } catch (e) {}
        try {
          if ((xhr.responseType === "" || xhr.responseType === "text") &&
              looksLikeManifestText(xhr.responseText)) {
            emit({ kind: "hls", url: rurl,
                   isMaster: xhr.responseText.indexOf("#EXT-X-STREAM-INF") !== -1 });
          }
        } catch (e) {}
      });
      return origSend.call(this, body);
    };
  } catch (e) {}
})();
