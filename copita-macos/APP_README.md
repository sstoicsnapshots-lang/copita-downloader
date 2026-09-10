# Copita Downloader

Copita is a native macOS download manager. Paste a link — or drag one
in, or send it straight from your browser — and Copita grabs it: video,
audio, images, documents, archives, torrents. Everything runs locally on
your Mac. Nothing you download ever passes through anyone else's server.

Copita is free and open source (GPL-3.0). Full source, and the Windows /
Linux versions, are at:
https://github.com/sstoicsnapshots-lang/copita-downloader

## Installing

Drag **Copita Downloader** onto the **Applications** shortcut in this
window.

## Opening it the first time

Copita isn't notarized by Apple, so the first launch is blocked. To
allow it (you only do this once):

1. Double-click **Copita Downloader** in Applications. macOS says it
   "cannot be opened" — click **Done**.
2. Open **System Settings → Privacy & Security**.
3. Scroll down to the **Security** section. You'll see a line like
   *"Copita Downloader was blocked to protect your Mac."* Click
   **Open Anyway**.
4. Confirm with Touch ID or your password, then click **Open** in the
   final dialog.

After that it opens normally like any other app.

*(On older macOS you can instead right-click the app → **Open** →
**Open**.)*

## First run

Open **Settings → Connection**. It checks the few components Copita's
download engine relies on and installs anything missing with one click.
Do this once and you're set.

## Getting a download started

Any of these work:

- **Paste a link** into the box at the top of the window and click
  **Download**.
- **Drag a link** from your browser straight onto the Copita window.
- **Browser extension** — install it from Settings → Integrations, then
  a "⬇ Copita" button appears on video/audio pages, and a native
  "Download with Copita" option shows up when you right-click images,
  video, audio, or file links while browsing.
- **Right-click anywhere** — select a link's text in Finder, Notes,
  Mail, or your browser, right-click it, and choose
  **Services → Download with Copita**.

## The menu bar icon

Copita lives in your menu bar (the small download-arrow icon near the
clock). Click it to see everything currently downloading, pause or
resume the whole queue, or jump straight to a finished file — without
needing the main window open at all. If you start a download from the
browser extension and Copita isn't already running, it launches quietly
in the background to catch it — no window popping up, no icon cluttering
your Dock, unless you ask to see it.

## What it can download

Video and streaming sites, cloud/file-hosting links, audio and podcast
platforms, manga/comic readers, torrents and magnet links, and plain
direct file links (zips, PDFs, images, and more). If you paste a page
rather than a direct file link, Copita will look at the page itself for
anything downloadable on it. The full reference list is in the app under
**Settings → Supported Sites**.

## Previewing what you've downloaded

Click **Play** or **Preview** on any finished download to watch, listen
to, or read it right inside Copita — no need to open another app first.

## Settings

Open **Settings** (⌘,) from the menu bar or the sidebar gear icon to
change:

- Where downloads are saved
- How many connections / simultaneous downloads to use
- Default video quality and audio format
- Your browser session (for sites that need you signed in, and for
  YouTube if it starts asking you to "confirm you're not a bot")

## Questions or something not working?

If a download fails, click **Retry** — most failures are the source
site being temporarily unavailable or rate-limiting your connection,
and clear up on their own. If the app itself won't respond, quit it
from the menu bar (**Quit Copita Downloader**) and reopen it.

Bugs and feature requests:
https://github.com/sstoicsnapshots-lang/copita-downloader/issues
