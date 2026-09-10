# Copita extension — manual test matrix

No automated tests (plain JS, no harness). Run this whole matrix after any
change to `background.js`, `content.js`, or `popup.js`.

## UI (v1.3.0 redesign)

Popup and on-page overlay were rebuilt: light/dark-aware popup with a
system design scale, live task cards with **Pause / Resume / Cancel**
controls, resource cards with file-type glyphs, a real toggle switch, and
an "Open app" button (`copita://open`). The overlay button now lives in a
**shadow root** so page CSS can't restyle or hide it.

| # | Steps | Expected |
|---|-------|----------|
| U1 | Open the popup in OS light mode, then dark mode. | Colours adapt; text stays legible; accent stays orange. |
| U2 | With a download running, open the popup. | Card shows filename, glyph, `Downloading · N% · X/Y · speed · ETA`, a moving bar. |
| U3 | Click **Pause** on a chunk/HTTP download, then **Resume**. | Status flips to Paused then back; bar stops/moves. (Pause only shows for engines that support it.) |
| U4 | Click **Cancel** on any active task. | Task disappears from the list within ~1 s. |
| U5 | Scanning / finishing task. | Bar is an indeterminate sweep, status reads "Scanning" / "Finishing". |
| U6 | Overlay button on a page with aggressive CSS (e.g. `* { all: revert }` sites, fullscreen players). | Pill still renders correctly — not squashed, recoloured, or hidden. |
| U7 | Popup footer → **Open app**. | Copita's window comes to the front (app already running). |
| U8 | Open a **Google Drive folder** page, then the popup. | An "In this folder" checklist replaces "On this page" — every file + subfolder, with size. Select all + "Download N" work; each pick goes to Copita as its own task (a subfolder downloads recursively). |
| U9 | A Drive folder shared only privately (not link-shared). | Checklist is empty / section hidden — the public scrape can't see it; falls back to the folder URL + Download button. |

**Setup:** Copita backend running on `127.0.0.1:8888`. `chrome://extensions`
→ Developer mode → Load unpacked → `copita-extension/`. After editing any
file, click the extension's ↻ reload button (unpacked extensions do NOT
auto-reload). Open the service-worker console from `chrome://extensions` →
"service worker" link for logs.

## C2 — persistent bridge

| # | Steps | Expected |
|---|-------|----------|
| 1 | Backend up. Open the popup. | Green pulsing dot, "Connected". |
| 2 | Stop the backend. Wait ~30 s. Open the popup. | Red dot, "App not running". |
| 3 | Backend down. Popup → paste a direct file URL → Download. | "Queued — Copita is starting…". Copita launches (menu-bar, no Dock). Within a few seconds the download appears in Copita and runs. |
| 4 | Start a large download in Copita. Look at the toolbar icon. | Badge shows the active-download count; updates as downloads finish. |
| 5 | With a download running, open the popup. | "Downloading" section lists it with a live bar and Pause/Cancel. |
| 6 | Kill the backend mid-download, then relaunch it. | Extension reconnects within ~30 s (badge/popup recover). Queued sends (if any) flush. |

## C1 — page resource capture

| # | Steps | Expected |
|---|-------|----------|
| 7 | Open a page with a plain `<video src=".mp4">` or a direct MP4 link. Open the popup. | "On this page" lists the .mp4 with its type/size. Clicking it → "Sent to Copita ✓", download starts. |
| 8 | Open an HLS site (a stream that loads a `.m3u8`). Open the popup. | The `.m3u8` manifest is listed. |
| 9 | Open a page you're **logged into** that streams media (e.g. a members video). Grab a resource from the popup. | Download succeeds server-side using the captured Cookie/Referer (would 401/403 without). |
| 10 | Open a normal content-heavy page (news site). Open the popup. | "On this page" is NOT flooded with tracking pixels / JS / CSS — only real media/files/large images. |
| 11 | Navigate the same tab to a different page. | The previous page's resources are cleared from the popup. |
| 12 | Right-click a video/image/file link → "Download with Copita". | Uses captured headers if the URL was seen on the page; downloads. |

## C3 — real media identification (MSE probe)

| # | Steps | Expected |
|---|-------|----------|
| 13 | Open an **HLS site** (not YouTube — e.g. a news video, a sports clip). Wait for it to start playing. Hover the video → **⬇ Copita**. | Copita downloads the actual stream (the `.m3u8` the probe caught), not the page's share image. |
| 14 | Open an **Instagram / Twitter video**. ⬇ Copita. | Sends the per-post page URL to yt-dlp (these are known platforms). |
| 15 | Open a site that streams **separate video + audio (DASH)**. ⬇ Copita. | Backend downloads both tracks and muxes them (`downloading → merging → completed`). |
| 16 | Open the service-worker console. Play a video, then check `chrome://extensions` → inspect the page → console for probe signals. | `postMessage` signals fire (`mse_url`, `mse_mime`, `request`, `hls`). |

## C3b — clip / time-range

| # | Steps | Expected |
|---|-------|----------|
| 17 | On a YouTube video, hover → **✂ Clip** → type `0:05` start, `0:12` end → **Download clip**. | Copita downloads a ~7-second clip, not the whole video. Check the file duration. |
| 18 | ✂ Clip → click **now** next to "start" while the video plays at 1:30. | The start field fills with `1:30`. |
| 19 | Clip with end ≤ start. | "✗ Check the times" — nothing sent. |

## Regression — existing behaviour still works

| # | Steps | Expected |
|---|-------|----------|
| 13 | Click a direct download link (a `.zip`, a "Save As" target). | Browser's own download is cancelled; Copita takes it. |
| 14 | Telegram Web (`web.telegram.org`) → download a non-restricted media file with its own button. | The finished blob file is adopted into Copita's library (`/api/import-file`). |
| 15 | YouTube video page → the floating "⬇ Copita" button. | Sends the `watch?v=` URL; Copita downloads via yt-dlp. |
| 16 | TikTok For You feed → scroll to a video → "⬇ Copita". | Sends the reconstructed per-video URL (not `tiktok.com/`). |
| 17 | SoundCloud track page → "⬇ Copita" (fixed corner button). | Sends the page URL; Copita downloads the track. |
| 18 | Toggle "Route browser downloads through Copita" off → click a download link. | Browser downloads it normally (no interception). |

## Sites to keep in rotation

YouTube · TikTok · Instagram · a plain MP4 host · an HLS demo
(e.g. `bitmovin`/`test-streams`) · a login-walled site you have an account
on · `web.telegram.org` · SoundCloud · a large-image gallery (pixabay/pexels).
