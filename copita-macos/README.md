# Copita Downloader for macOS

A native macOS desktop companion for Copita Downloader, featuring:
- **macOS Menu Bar Item**: Quick status icon in the top menu bar to paste and download links, open the downloads folder, or bring the window to the front.
- **Native macOS Window**: Resizable dark slate window styled with native traffic lights and dense UI layout.
- **Drag-and-Drop Catching**: Drag any media URL or link directly from your browser (Chrome, Safari, Brave) into the window to download immediately.
- **Automatic Backend Supervision**: Detects if the Python backend is running on `127.0.0.1:8888`, and automatically launches it if needed.
- **System Notifications**: Native macOS alert banners when downloads finish.
- **Apple Silicon Native**: Compiled natively for Apple Silicon arm64 (146 KB binary).

---

## Build Instructions

To build `Copita.app` from source:

```bash
cd copita-macos
./build.sh
```

## Running the App

```bash
open copita-macos/Copita.app
```

Or drag `Copita.app` to your `/Applications` folder.
