/**
 * Copita Downloader — Apple Native macOS Controller
 * Complete tab switching, native clipboard bridge, Finder folder opening,
 * queue pause/resume orchestration, and Apple Podcasts floating pill player.
 */

// Native macOS platform detection
const urlParams = new URLSearchParams(window.location.search);
if (urlParams.get('platform') === 'macos' || window.webkit?.messageHandlers?.copitaNative) {
  document.documentElement.classList.add('macos-native');
  if (document.body) document.body.classList.add('macos-native');
  window.addEventListener('DOMContentLoaded', () => {
    document.body.classList.add('macos-native');
  });

  // `-webkit-app-region: drag` on .canvas-topbar is unreliable on its own for
  // moving a WKWebView-backed window, so mousedown on the empty topbar area
  // (never on the search bar, buttons, or inputs) asks the native side to
  // perform the actual window drag.
  window.addEventListener('DOMContentLoaded', () => {
    const topbar = document.querySelector('.canvas-topbar');
    if (!topbar) return;
    topbar.addEventListener('mousedown', (e) => {
      if (e.button !== 0) return;
      if (e.target.closest('.tab-context-panel, button, input, select, a, textarea')) return;
      window.webkit?.messageHandlers?.copitaNative?.postMessage({ type: 'startWindowDrag' });
    });
  });
}

let socket = null;
let tasks = {};
// A task the user just removed, kept out of `tasks` even if a stray
// in-flight progress broadcast (e.g. from a torrent's background thread
// noticing cancellation a moment late) tries to re-add it before the
// server's own "task_deleted" confirmation arrives.
let pendingRemovals = new Set();
let currentFilter = 'all';
let searchQuery = '';
let activeInputForPaste = 'url-input';

// Badges & Common Elements
const pageTitle = document.getElementById('page-title');
const pageSubtitle = document.getElementById('page-subtitle');
const sidebarFilter = document.getElementById('sidebar-filter');
const tasksList = document.getElementById('tasks-list');
const emptyState = document.getElementById('empty-state');
const badgeAllCount = document.getElementById('badge-all-count');
const badgeActiveCount = document.getElementById('badge-active-count');
const badgeCompletedCount = document.getElementById('badge-completed-count');

// Floating Pill Elements
const pillTitle = document.getElementById('pill-title');
const pillSubtitle = document.getElementById('pill-subtitle');
const pillSpeedLabel = document.getElementById('pill-speed-label');
const pillProgressFill = document.getElementById('pill-progress-fill');
const pillPlayIcon = document.getElementById('pill-play-icon');
const pillPauseIcon = document.getElementById('pill-pause-icon');

// Modals
const playerModal = document.getElementById('player-modal');
const playerModalContent = document.getElementById('player-modal-content');
const playerTitle = document.getElementById('player-title');
const playerTypeBadge = document.getElementById('player-type-badge');
const playerFinderBtn = document.getElementById('player-finder-btn');
const playerClose = document.getElementById('player-close');
const videoPreview = document.getElementById('video-preview');
const audioContainer = document.getElementById('audio-container');
const audioPreview = document.getElementById('audio-preview');
const pdfPreview = document.getElementById('pdf-preview');
const imagePreview = document.getElementById('image-preview');

const settingsModal = document.getElementById('settings-modal');
const btnOpenSettings = document.getElementById('btn-open-settings');
const settingsClose = document.getElementById('settings-close');
const btnSaveSettings = document.getElementById('btn-save-settings');
const settingDir = document.getElementById('setting-dir');
const settingBrowserCookies = document.getElementById('setting-browser-cookies');

const toast = document.getElementById('toast');
const toastMessage = document.getElementById('toast-message');

function showToast(msg) {
  if (!toast || !toastMessage) return;
  toastMessage.textContent = msg;
  toast.classList.add('show');
  setTimeout(() => toast.classList.remove('show'), 3200);
}

function formatBytes(bytes) {
  if (!bytes || bytes <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.floor(Math.log(bytes) / Math.log(1024));
  return `${(bytes / Math.pow(1024, i)).toFixed(1)} ${units[i]}`;
}

// Tab Configurations for Rich Distinct Views
const TAB_CONFIG = {
  all: {
    title: 'Downloads',
    subtitle: 'Your downloads, all in one place.',
    emptyTitle: 'No Downloads Yet',
    emptySub: 'Paste or drag any video, audio, or document link to begin downloading.',
    emptyBtn: 'Paste from Clipboard',
    onEmptyAction: () => pasteFromClipboard()
  },
  active: {
    title: 'Active Queue',
    subtitle: 'Keep track of downloads in progress.',
    emptyTitle: 'Queue is Empty',
    emptySub: 'All downloads have completed. Paste a link to queue new transfers.',
    emptyBtn: 'Add New Download',
    onEmptyAction: () => switchTab('all')
  },
  completed: {
    title: 'Downloaded',
    subtitle: 'Ready to open, play, or reveal in Finder.',
    emptyTitle: 'No Downloaded Files',
    emptySub: 'Completed downloads will appear here with instant Finder access.',
    emptyBtn: 'Open Downloads in Finder',
    onEmptyAction: () => openDownloadsDirectory()
  },
  videos: {
    title: 'Movies & Videos',
    subtitle: 'Save videos to watch anytime.',
    emptyTitle: 'No Videos Yet',
    emptySub: 'Paste a video link above to start your collection.',
    emptyBtn: 'Paste Video Link',
    onEmptyAction: () => pasteToActiveInput('url-input-video')
  },
  audio: {
    title: 'Audio & Music',
    subtitle: 'Your music and podcasts, ready to listen.',
    emptyTitle: 'No Audio Tracks',
    emptySub: 'Paste a music, podcast, or video link above.',
    emptyBtn: 'Paste Music Link',
    onEmptyAction: () => pasteToActiveInput('url-input-audio')
  },
  manga: {
    title: 'Manga & Comics',
    subtitle: 'Keep your favorite chapters together.',
    emptyTitle: 'No Manga Chapters',
    emptySub: 'Paste a chapter link above to start your library.',
    emptyBtn: 'Paste Manga Link',
    onEmptyAction: () => pasteToActiveInput('url-input-manga')
  },
  files: {
    title: 'Files & Archives',
    subtitle: 'A home for your files and documents.',
    emptyTitle: 'No Files Yet',
    emptySub: 'Paste a file link above to get started.',
    emptyBtn: 'Paste File Link',
    onEmptyAction: () => pasteToActiveInput('url-input-files')
  }
};

// Switch Tab View
function switchTab(filter) {
  if (!TAB_CONFIG[filter]) filter = 'all';
  currentFilter = filter;

  // Update Sidebar active state
  document.querySelectorAll('.nav-item').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.filter === filter);
  });

  // Update Canvas Header
  const conf = TAB_CONFIG[filter];
  if (pageTitle) pageTitle.textContent = conf.title;
  if (pageSubtitle) pageSubtitle.textContent = conf.subtitle;

  // Show only relevant contextual toolbar
  document.querySelectorAll('.tab-context-panel').forEach(panel => {
    panel.style.display = panel.id === `context-${filter}` ? 'flex' : 'none';
  });

  // Set default input for paste based on tab
  if (filter === 'videos') activeInputForPaste = 'url-input-video';
  else if (filter === 'audio') activeInputForPaste = 'url-input-audio';
  else if (filter === 'manga') activeInputForPaste = 'url-input-manga';
  else if (filter === 'files') activeInputForPaste = 'url-input-files';
  else activeInputForPaste = 'url-input';

  renderTasks();
}

// Sidebar Navigation Bindings
document.querySelectorAll('.nav-item').forEach(btn => {
  btn.onclick = () => {
    const filter = btn.dataset.filter || 'all';
    switchTab(filter);
  };
});

// Search Filter Input
if (sidebarFilter) {
  sidebarFilter.addEventListener('input', (e) => {
    searchQuery = e.target.value.toLowerCase().trim();
    renderTasks();
  });
}

function onCompletedSearch(val) {
  searchQuery = (val || '').toLowerCase().trim();
  renderTasks();
}

// Open Downloads Directory (Finder)
async function openDownloadsDirectory() {
  showToast('Opening Downloads folder in Finder...');

  // 1. If in native macOS wrapper, call native Swift NSWorkspace
  if (window.webkit?.messageHandlers?.copitaNative) {
    window.webkit.messageHandlers.copitaNative.postMessage({ type: 'openDownloadsFolder' });
  }

  // 2. Also call backend endpoint to guarantee it opens
  try {
    const res = await fetch('/api/open-directory', { method: 'POST' });
    const data = await res.json();
    if (data.status === 'opened') {
      showToast('Opened: ' + (data.path || 'Downloads'));
    }
  } catch (e) {
    console.error('Failed to open directory via HTTP:', e);
  }
}

// Native Clipboard Bridge
window.onNativeClipboard = function(text) {
  if (text && text.trim()) {
    const target = document.getElementById(activeInputForPaste) || document.getElementById('url-input');
    if (target) {
      target.value = text.trim();
      target.focus();
      showToast('Pasted from clipboard');
    }
  } else {
    showToast('Clipboard is empty');
  }
};

async function pasteFromClipboard() {
  pasteToActiveInput(activeInputForPaste || 'url-input');
}

async function pasteToActiveInput(inputId) {
  activeInputForPaste = inputId;
  const target = document.getElementById(inputId);

  // 1. Try native macOS WKWebView message handler
  if (window.webkit?.messageHandlers?.copitaNative) {
    window.webkit.messageHandlers.copitaNative.postMessage({ type: 'getClipboard' });
    return;
  }

  // 2. Fallback to browser clipboard API
  try {
    const text = await navigator.clipboard.readText();
    if (text && text.trim()) {
      if (target) {
        target.value = text.trim();
        target.focus();
        showToast('Pasted from clipboard');
      }
    } else {
      showToast('Clipboard is empty');
    }
  } catch (e) {
    if (target) {
      target.focus();
      showToast('Press Cmd+V to paste');
    }
  }
}

function setPresetUrl(url) {
  setPresetUrlTo(url, 'url-input');
}

function setPresetUrlTo(url, inputId) {
  const target = document.getElementById(inputId);
  if (target) {
    target.value = url;
    target.focus();
  }
}

// Download Handler
async function startDownload() {
  startDownloadWithInput('url-input', false);
}

async function startDownloadWithInput(inputId, forceAudio) {
  const inputEl = document.getElementById(inputId);
  if (!inputEl) return;
  const url = inputEl.value.trim();
  if (!url) {
    showToast('Please enter a valid URL');
    inputEl.focus();
    return;
  }

  const chkAudioEl = document.getElementById('chk-audio-only');

  const threads = Number(localStorage.getItem('copita_default_threads')) || 16;
  const audioOnly = forceAudio || (chkAudioEl ? chkAudioEl.checked : false);

  const payload = {
    url: url,
    threads: threads,
    audio_only: audioOnly,
    audio_format: localStorage.getItem('copita_audio_format') || 'mp3',
    video_quality: localStorage.getItem('copita_video_quality') || 'best'
  };

  showToast('Connecting and analyzing stream...');

  try {
    const res = await fetch('/api/download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (data.status === 'success') {
      showToast(`Added: ${data.task.title || 'Download'}`);
      inputEl.value = '';
    } else {
      showToast(data.message || 'Download error');
    }
  } catch (e) {
    showToast(`Error: ${e.message}`);
  }
}

// Queue Controls
async function pauseAllQueue() {
  try {
    const res = await fetch('/api/queue/pause-all', { method: 'POST' });
    const data = await res.json();
    showToast(`Paused ${data.paused || 0} tasks`);
  } catch (e) {
    showToast('Failed to pause queue');
  }
}

async function resumeAllQueue() {
  try {
    const res = await fetch('/api/queue/resume-all', { method: 'POST' });
    const data = await res.json();
    showToast(`Resumed ${data.resumed || 0} tasks`);
  } catch (e) {
    showToast('Failed to resume queue');
  }
}

async function clearCompletedHistory() {
  try {
    const res = await fetch('/api/tasks-completed', { method: 'DELETE' });
    const data = await res.json();
    showToast(`Cleared ${data.cleared || 0} completed items`);
  } catch (e) {
    showToast('Failed to clear history');
  }
}

async function pauseTask(id) {
  await fetch(`/api/tasks/${id}/pause`, { method: 'POST' });
}

async function resumeTask(id) {
  await fetch(`/api/tasks/${id}/resume`, { method: 'POST' });
}

async function deleteTask(id) {
  await fetch(`/api/tasks/${id}?delete_file=true`, { method: 'DELETE' });
}

async function revealInFinder(id) {
  await fetch(`/api/tasks/${id}/open?reveal=true`, { method: 'POST' });
  showToast('Revealed in Finder');
}

// WebSocket Connection
function connectWebSocket() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = `${protocol}//${window.location.host}/ws`;
  socket = new WebSocket(wsUrl);

  socket.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      if (data.event === 'init') {
        tasks = {};
        data.tasks.forEach(t => tasks[t.id] = t);
        renderTasks();
      } else if (data.event === 'task_added' || data.event === 'task_updated') {
        if (pendingRemovals.has(data.task.id)) return;
        tasks[data.task.id] = data.task;
        renderTasks();
      } else if (data.event === 'task_deleted') {
        pendingRemovals.delete(data.task.id);
        delete tasks[data.task.id];
        renderTasks();
      } else if (data.event === 'requirement_updated') {
        renderRequirementRow(data.requirement);
      } else if (data.event === 'component_update') {
        // yt-dlp auto-update — brief confirmation on launch, louder when it acts
        if (data.message) showToast(data.message);
      }
    } catch (e) {
      console.error('WebSocket error:', e);
    }
  };

  socket.onclose = () => {
    setTimeout(connectWebSocket, 2000);
  };
}

// System Information
async function loadSystemInfo() {
  try {
    const res = await fetch('/api/system');
    const data = await res.json();
    const diskText = `${data.disk_free_gb} GB`;
    
    const storageTextEl = document.getElementById('storage-text');
    if (storageTextEl) storageTextEl.textContent = diskText;

    const freeStatEl = document.getElementById('storage-free-stat');
    if (freeStatEl) freeStatEl.textContent = `${data.disk_free_gb} GB free`;

    const totalStatEl = document.getElementById('storage-total-stat');
    if (totalStatEl) totalStatEl.textContent = `of ${data.disk_total_gb} GB total`;

    const dirPathEl = document.getElementById('storage-dir-path');
    if (dirPathEl) dirPathEl.textContent = data.download_dir;

    const fillEl = document.getElementById('storage-bar-fill');
    if (fillEl && data.disk_total_gb > 0) {
      const usedPct = Math.round(((data.disk_total_gb - data.disk_free_gb) / data.disk_total_gb) * 100);
      fillEl.style.width = `${Math.max(5, Math.min(100, usedPct))}%`;
    }

    if (settingDir) settingDir.value = data.download_dir;
    if (settingBrowserCookies && data.cookies_browser !== undefined) {
      settingBrowserCookies.value = data.cookies_browser;
    }
    const maxConcurrentEl = document.getElementById('setting-max-concurrent');
    if (maxConcurrentEl && data.max_concurrent_downloads) {
      maxConcurrentEl.value = String(data.max_concurrent_downloads);
    }
    const speedEl = document.getElementById('setting-speed-limit');
    if (speedEl && data.max_speed_kbps !== undefined) speedEl.value = String(data.max_speed_kbps);
    const proxyEl = document.getElementById('setting-proxy');
    if (proxyEl && data.proxy_url !== undefined) proxyEl.value = data.proxy_url;
  } catch (e) {
    console.error('Failed to load system info:', e);
  }
}

function reportedSpeed(activeTasks) {
  const downloading = activeTasks.filter(t => t.status === 'downloading');
  const reporting = downloading.filter(t => t.speed_available || t.speed > 0);
  if (!downloading.length) return activeTasks.length ? 'Preparing' : '0 B/s';
  if (!reporting.length) return 'Not available';
  return `${formatBytes(reporting.reduce((sum, t) => sum + (t.speed || 0), 0))}/s`;
}

// Render Tasks
function renderTasks() {
  const taskArr = Object.values(tasks).sort((a, b) => b.created_at - a.created_at);
  const totalCount = taskArr.length;

  const activeTasks = taskArr.filter(t => ['analyzing', 'downloading', 'merging', 'compressing', 'extracting', 'queued'].includes(t.status));
  const completedTasks = taskArr.filter(t => t.status === 'completed');
  const pausedTasks = taskArr.filter(t => t.status === 'paused');

  // Update Badges
  if (badgeAllCount) badgeAllCount.textContent = totalCount;
  if (badgeActiveCount) badgeActiveCount.textContent = activeTasks.length;
  if (badgeCompletedCount) badgeCompletedCount.textContent = completedTasks.length;

  // Update Active Queue Telemetry Cards
  const qActiveEl = document.getElementById('q-stat-active');
  const qSpeedEl = document.getElementById('q-stat-speed');
  const qPausedEl = document.getElementById('q-stat-paused');
  const totalSpeed = activeTasks.reduce((sum, t) => sum + (t.speed || 0), 0);

  if (qActiveEl) qActiveEl.textContent = activeTasks.length;
  if (qSpeedEl) {
    qSpeedEl.textContent = reportedSpeed(activeTasks);
    qSpeedEl.title = 'Combined speed from downloads that report transfer measurements.';
  }
  if (qPausedEl) qPausedEl.textContent = pausedTasks.length;

  document.getElementById('btn-pause-all-queue').disabled = !taskArr.some(t => t.can_pause);
  document.getElementById('btn-resume-all-queue').disabled = !pausedTasks.length;
  document.getElementById('btn-clear-completed-queue').disabled = !completedTasks.length;

  // Update Floating Bottom Pill
  updateFloatingPill(activeTasks, completedTasks, pausedTasks);

  // Apply Sidebar Filter
  let filtered = taskArr.filter(t => {
    if (currentFilter === 'active') return ['analyzing', 'downloading', 'merging', 'compressing', 'extracting', 'queued'].includes(t.status) || t.status === 'paused';
    if (currentFilter === 'completed') return t.status === 'completed';
    if (currentFilter === 'videos') return t.category === 'video';
    if (currentFilter === 'audio') return t.category === 'audio';
    if (currentFilter === 'manga') return t.category === 'manga';
    if (currentFilter === 'files') return ['file', 'document', 'model_3d', 'archive', 'torrent'].includes(t.category);
    return true;
  });

  // Apply Search Query
  if (searchQuery) {
    filtered = filtered.filter(t =>
      (t.title && t.title.toLowerCase().includes(searchQuery)) ||
      (t.url && t.url.toLowerCase().includes(searchQuery))
    );
  }

  // Handle Empty State
  if (filtered.length === 0) {
    const conf = TAB_CONFIG[currentFilter] || TAB_CONFIG.all;
    emptyState.style.display = 'flex';
    emptyState.querySelector('.empty-title').textContent = searchQuery ? 'No matching downloads' : conf.emptyTitle;
    emptyState.querySelector('.empty-subtitle').textContent = searchQuery ? 'Try another filename or clear your search.' : conf.emptySub;

    const actionBtn = emptyState.querySelector('.empty-paste-btn');
    if (actionBtn) {
      actionBtn.textContent = searchQuery ? 'Clear search' : conf.emptyBtn;
      actionBtn.onclick = searchQuery ? clearSearch : conf.onEmptyAction;
    }

    tasksList.innerHTML = '';
    tasksList.appendChild(emptyState);
    return;
  }

  emptyState.style.display = 'none';
  tasksList.innerHTML = '';

  // Build text from download metadata safely; filenames and errors are untrusted.
  filtered.forEach(t => {
    const card = document.createElement('div');
    card.className = `task-card ${t.status === 'completed' ? 'is-completed' : ''}`;
    const done = t.status === 'completed';
    const failed = t.status === 'failed';
    const pct = Math.min(100, Math.max(0, Number(t.percent) || 0));
    const previewable = /\.(mp4|webm|mov|m4v|mp3|m4a|wav|flac|ogg|pdf|png|jpe?g|webp|gif)$/i.test(t.filename || t.title || '');
    const cancelled = t.status === 'cancelled';
    // Unknown total (a live stream has no end) means percent is meaningless
    // -- a determinate bar frozen at 0% reads as stuck, not as "no total
    // yet." Same underlying issue as the missing 'cancelled' status text
    // below: both used to fall through to the generic percent/bytes
    // branch, which is actively misleading rather than just plain.
    const unknownTotal = t.status === 'downloading' && !(t.total > 0);
    const statusLabel = {completed: 'Downloaded', failed: 'Needs attention', cancelled: 'Cancelled', analyzing: 'Connecting', queued: 'Queued', merging: 'Finishing', compressing: 'Packaging'}[t.status] || t.status;
    card.innerHTML = `
      <div class="task-header">
        <div class="task-file-icon" aria-hidden="true">${done ? '✓' : failed ? '!' : '↓'}</div>
        <div class="task-info">
          <span class="task-title"></span>
          <div class="task-detail"><span class="task-status-tag ${escapeHTML(t.status)}">${escapeHTML(statusLabel)}</span>
          <span>${done ? formatBytes(t.total || t.downloaded) : failed ? 'Download interrupted' : cancelled ? 'Cancelled' : t.status === 'queued' ? 'Waiting for another download to finish…' : t.status === 'analyzing' ? (t.engine === 'torrent' ? 'Connecting to peers…' : 'Scanning…') : unknownTotal ? `${formatBytes(t.downloaded)} downloaded` : `${pct.toFixed(0)}% · ${formatBytes(t.downloaded)}${t.total > 0 ? ` of ${formatBytes(t.total)}` : ''}`}</span></div>
        </div>
        <div class="task-actions"></div>
      </div>
      ${!done && !failed && !cancelled ? `<div class="progress-track"><div class="progress-fill${(t.status === 'analyzing' || unknownTotal) ? ' indeterminate' : ''}" style="width:${pct}%"></div></div>` : ''}
      ${!done && !failed && t.speed > 0 ? `<div class="task-footer">${formatBytes(t.speed)}/s ${t.eta > 0 ? `· ${Math.round(t.eta)}s remaining` : ''}</div>` : ''}
    `;
    const title = card.querySelector('.task-title');
    title.textContent = t.title || t.url;
    title.title = t.title || t.url;
    const actions = card.querySelector('.task-actions');
    const addAction = (label, action, primary = false) => {
      const button = document.createElement('button');
      button.className = `task-btn${primary ? ' btn-play' : ''}`;
      button.textContent = label;
      button.onclick = action;
      actions.appendChild(button);
    };
    if (done) {
      if (previewable) addAction(/\.(pdf|png|jpe?g|webp|gif)$/i.test(t.filename || t.title) ? 'Preview' : 'Play', () => openPlayer(t.id), true);
      addAction('Show in Finder', () => revealInFinder(t.id));
      addAction('Remove', () => removeFromHistory(t.id));
    } else if (failed || t.status === 'cancelled') {
      addAction('Retry', () => retryTask(t.id), true);
      addAction('Remove', () => removeFromHistory(t.id));
    } else {
      if (t.can_pause) addAction('Pause', () => pauseTask(t.id));
      if (t.status === 'paused') addAction('Resume', () => resumeTask(t.id));
      addAction('Cancel', () => removeFromHistory(t.id));
    }
    if (failed) {
      const error = document.createElement('div');
      error.className = 'task-error';
      error.textContent = t.error || 'The download could not be completed. Try again.';
      card.appendChild(error);
      if (t.error_details) {
        const details = document.createElement('details');
        details.className = 'task-error-details';
        const summary = document.createElement('summary');
        summary.textContent = 'Technical details';
        const content = document.createElement('pre');
        content.textContent = t.error_details;
        details.append(summary, content);
        card.appendChild(details);
      }
    }
    tasksList.appendChild(card);
  });
}

function escapeHTML(text) {
  return String(text || '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
}
function clearSearch() {
  searchQuery = '';
  sidebarFilter.value = '';
  document.getElementById('completed-filter-input').value = '';
  renderTasks();
}
async function removeFromHistory(id) {
  // Remove instantly rather than waiting on the network/WebSocket round trip —
  // a slow or momentarily stalled backend (e.g. a torrent's background
  // thread doing heavy libtorrent work) shouldn't make Cancel/Remove look
  // unresponsive. pendingRemovals also blocks any stray "task_updated" for
  // this id from resurrecting it before the server confirms the delete.
  pendingRemovals.add(id);
  const hadTask = tasks[id];
  delete tasks[id];
  renderTasks();
  try {
    const res = await fetch(`/api/tasks/${id}`, {method: 'DELETE'});
    if (!res.ok) throw new Error('Could not remove download');
  } catch (error) {
    pendingRemovals.delete(id);
    if (hadTask) tasks[id] = hadTask;
    renderTasks();
    showToast(error.message);
  }
}
async function retryTask(id) {
  try {
    const res = await fetch(`/api/tasks/${id}/retry`, {method: 'POST'});
    if (!res.ok) throw new Error('Could not retry download');
    showToast('Retrying download…');
  } catch (error) { showToast(error.message); }
}

// Update Apple Floating Player Pill
function updateFloatingPill(activeTasks, completedTasks, pausedTasks) {
  const visible = activeTasks.length + pausedTasks.length > 0;
  document.getElementById('floating-pill').hidden = !visible;
  document.querySelector('.apple-content-canvas').classList.toggle('has-transfers', visible);
  document.getElementById('pill-btn-playpause').disabled = !pausedTasks.length && !activeTasks.some(t => t.can_pause);
  if (!visible) return;
  if (activeTasks.length > 0) {
    const topTask = activeTasks[0];
    const totalSpeed = activeTasks.reduce((sum, t) => sum + (t.speed || 0), 0);
    const avgPercent = activeTasks.reduce((sum, t) => sum + (t.percent || 0), 0) / activeTasks.length;

    pillTitle.textContent = topTask.title || 'Downloading...';
    pillSubtitle.textContent = `${activeTasks.length} active transfer${activeTasks.length > 1 ? 's' : ''}`;
    pillSpeedLabel.textContent = reportedSpeed(activeTasks);
    pillProgressFill.style.width = `${Math.min(100, Math.max(0, avgPercent))}%`;

    // Show Pause Icon
    if (pillPlayIcon) pillPlayIcon.style.display = 'none';
    if (pillPauseIcon) pillPauseIcon.style.display = 'block';
  } else if (pausedTasks.length > 0) {
    const topPaused = pausedTasks[0];
    pillTitle.textContent = topPaused.title || 'Paused';
    pillSubtitle.textContent = `${pausedTasks.length} transfer${pausedTasks.length > 1 ? 's' : ''} paused`;
    pillSpeedLabel.textContent = 'Paused';
    pillProgressFill.style.width = `${topPaused.percent || 0}%`;

    // Show Play Icon
    if (pillPlayIcon) pillPlayIcon.style.display = 'block';
    if (pillPauseIcon) pillPauseIcon.style.display = 'none';
  } else if (completedTasks.length > 0) {
    const latest = completedTasks[0];
    pillTitle.textContent = latest.title || 'Copita Downloader';
    pillSubtitle.textContent = 'All downloads finished';
    pillSpeedLabel.textContent = 'Complete';
    pillProgressFill.style.width = '100%';

    // Show Play Icon
    if (pillPlayIcon) pillPlayIcon.style.display = 'block';
    if (pillPauseIcon) pillPauseIcon.style.display = 'none';
  } else {
    pillTitle.textContent = 'Copita Downloader';
    pillSubtitle.textContent = 'Ready';
    pillSpeedLabel.textContent = '0 B/s';
    pillProgressFill.style.width = '0%';

    if (pillPlayIcon) pillPlayIcon.style.display = 'block';
    if (pillPauseIcon) pillPauseIcon.style.display = 'none';
  }
}

// Floating Pill Event Listeners
const pillBtnPlayPause = document.getElementById('pill-btn-playpause');
if (pillBtnPlayPause) {
  pillBtnPlayPause.onclick = () => {
    const activeTasks = Object.values(tasks).filter(t => ['analyzing', 'downloading', 'merging', 'compressing', 'extracting', 'queued'].includes(t.status));
    if (activeTasks.length > 0) {
      pauseAllQueue();
    } else {
      resumeAllQueue();
    }
  };
}

const pillBtnClear = document.getElementById('pill-btn-clear');
if (pillBtnClear) {
  pillBtnClear.onclick = () => clearCompletedHistory();
}

const pillBtnOpenDir = document.getElementById('pill-btn-open-dir');
if (pillBtnOpenDir) {
  pillBtnOpenDir.onclick = () => openDownloadsDirectory();
}

// In-App Player & Responsive File Viewer Stage
let currentPreviewTaskId = null;

function openPlayer(taskId, category, title) {
  currentPreviewTaskId = taskId;
  const streamUrl = `/api/stream/${taskId}`;
  const t = tasks[taskId];
  if (t) {
    if (!category) category = t.category;
    if (!title) title = t.title;
  }

  // Reset classes and media elements
  if (playerModalContent) {
    playerModalContent.className = 'modal-content player-modal-content';
  }

  if (videoPreview) {
    videoPreview.style.display = 'none';
    videoPreview.pause();
    videoPreview.src = '';
    videoPreview.onloadedmetadata = null;
  }

  if (audioContainer) {
    audioContainer.style.display = 'none';
  }
  if (audioPreview) {
    audioPreview.pause();
    audioPreview.src = '';
  }

  if (pdfPreview) {
    pdfPreview.style.display = 'none';
    pdfPreview.src = '';
  }

  if (imagePreview) {
    imagePreview.style.display = 'none';
    imagePreview.src = '';
  }

  const cleanTitle = title || (t ? t.title : 'Playback Preview');
  if (playerTitle) {
    playerTitle.textContent = cleanTitle;
    playerTitle.title = cleanTitle;
  }

  const filePath = (t && t.output_path) ? t.output_path.toLowerCase() : '';
  const isPdf = (category === 'document') || cleanTitle.toLowerCase().endsWith('.pdf') || filePath.endsWith('.pdf');
  const isImage = (category === 'image') || /\.(png|jpe?g|webp|gif|svg)$/i.test(cleanTitle) || /\.(png|jpe?g|webp|gif|svg)$/i.test(filePath);
  const isAudio = (category === 'audio') || /\.(mp3|m4a|wav|aac|flac|ogg|opus)$/i.test(cleanTitle) || /\.(mp3|m4a|wav|aac|flac|ogg|opus)$/i.test(filePath);

  if (isPdf) {
    if (playerModalContent) playerModalContent.classList.add('is-document');
    if (playerTypeBadge) playerTypeBadge.textContent = 'PDF Document';
    if (pdfPreview) {
      pdfPreview.style.display = 'block';
      pdfPreview.src = streamUrl;
    }
  } else if (isImage) {
    if (playerModalContent) playerModalContent.classList.add('is-landscape');
    if (playerTypeBadge) playerTypeBadge.textContent = 'Image';
    if (imagePreview) {
      imagePreview.style.display = 'block';
      imagePreview.src = streamUrl;
    }
  } else if (isAudio) {
    if (playerModalContent) playerModalContent.classList.add('is-audio');
    if (playerTypeBadge) playerTypeBadge.textContent = 'Audio Track';
    if (audioContainer) audioContainer.style.display = 'flex';
    if (audioPreview) {
      audioPreview.src = streamUrl;
      audioPreview.play().catch(() => {});
    }
  } else {
    // Video player: dynamically adapts to 9:16 portrait, 16:9 widescreen, 4:3, etc.
    if (playerTypeBadge) playerTypeBadge.textContent = 'Video';
    if (videoPreview) {
      videoPreview.style.display = 'block';
      videoPreview.src = streamUrl;

      videoPreview.onloadedmetadata = () => {
        const w = videoPreview.videoWidth;
        const h = videoPreview.videoHeight;
        if (w && h && playerModalContent) {
          const aspect = w / h;
          playerModalContent.classList.remove('is-portrait', 'is-landscape', 'is-standard');
          if (aspect < 0.8) {
            // 9:16 Vertical Video (Reels, TikTok, Shorts)
            playerModalContent.classList.add('is-portrait');
            if (playerTypeBadge) playerTypeBadge.textContent = '9:16 Reel';
          } else if (aspect > 1.4) {
            // 16:9 Landscape Widescreen
            playerModalContent.classList.add('is-landscape');
            if (playerTypeBadge) playerTypeBadge.textContent = '16:9 Video';
          } else {
            // Standard / Square / 4:3
            playerModalContent.classList.add('is-standard');
            if (playerTypeBadge) playerTypeBadge.textContent = `${w}×${h}`;
          }
        }
      };

      videoPreview.play().catch(() => {});
    }
  }

  if (playerModal) {
    playerModal.classList.add('open');
  }
}

function closePlayerModal() {
  if (videoPreview) {
    videoPreview.pause();
    videoPreview.src = '';
    videoPreview.onloadedmetadata = null;
  }
  if (audioPreview) {
    audioPreview.pause();
    audioPreview.src = '';
  }
  if (pdfPreview) {
    pdfPreview.src = '';
  }
  if (imagePreview) {
    imagePreview.src = '';
  }
  if (playerModal) {
    playerModal.classList.remove('open');
  }
  currentPreviewTaskId = null;
}

if (playerClose) {
  playerClose.onclick = () => closePlayerModal();
}

if (playerModal) {
  playerModal.onclick = (e) => {
    if (e.target === playerModal) {
      closePlayerModal();
    }
  };
}

if (playerFinderBtn) {
  playerFinderBtn.onclick = () => {
    if (currentPreviewTaskId) {
      revealInFinder(currentPreviewTaskId);
    } else {
      openDownloadsDirectory();
    }
  };
}

// Global Keyboard Listener: Escape key closes active modals
window.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    if (playerModal && playerModal.classList.contains('open')) {
      closePlayerModal();
    } else if (settingsModal && settingsModal.classList.contains('open')) {
      settingsModal.classList.remove('open');
    }
  }
});

// Settings Modal & Tabs
window.openSettingsModal = function(tabName = 'general') {
  if (settingsModal) {
    settingsModal.classList.add('open');
    switchSettingsTab(tabName);
  }
};

function switchSettingsTab(tabName) {
  const tabs = document.querySelectorAll('.settings-tab-btn');
  const panes = document.querySelectorAll('.settings-tab-pane');
  
  tabs.forEach(btn => {
    if (btn.getAttribute('data-tab') === tabName) {
      btn.classList.add('active');
    } else {
      btn.classList.remove('active');
    }
  });

  panes.forEach(pane => {
    if (pane.id === `tab-pane-${tabName}`) {
      pane.classList.add('active');
    } else {
      pane.classList.remove('active');
    }
  });

  if (tabName === 'engines') loadRequirements();
}

document.querySelectorAll('.settings-tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const target = btn.getAttribute('data-tab');
    if (target) switchSettingsTab(target);
  });
});

if (btnOpenSettings) btnOpenSettings.onclick = () => window.openSettingsModal('general');
if (settingsClose) settingsClose.onclick = () => settingsModal.classList.remove('open');

if (btnSaveSettings) {
  btnSaveSettings.onclick = async () => {
    const defaultThreads = document.getElementById('setting-default-threads');
    const maxConcurrent = document.getElementById('setting-max-concurrent');
    const speedEl = document.getElementById('setting-speed-limit');
    const proxyEl = document.getElementById('setting-proxy');
    btnSaveSettings.disabled = true;
    try {
      const response = await fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          cookies_browser: settingBrowserCookies.value,
          threads: Number(defaultThreads.value),
          max_concurrent: Number(maxConcurrent.value),
          max_speed_kbps: speedEl ? Number(speedEl.value) : 0,
          proxy_url: proxyEl ? proxyEl.value.trim() : ''
        })
      });
      const rj = await response.clone().json().catch(() => ({}));
      if (!response.ok || rj.status === 'error') throw new Error(rj.error || 'Could not save preferences');
      localStorage.setItem('copita_default_threads', defaultThreads.value);
      localStorage.setItem('copita_video_quality', document.getElementById('sel-video-quality').value);
      localStorage.setItem('copita_audio_format', document.getElementById('sel-audio-format').value);
      showToast('Preferences saved');
      settingsModal.classList.remove('open');
    } catch (error) {
      showToast(error.message);
    } finally {
      btnSaveSettings.disabled = false;
    }
  };
}

// Requirements Panel (Settings → Connection)
const requirementsList = document.getElementById('requirements-list');

async function loadRequirements() {
  if (!requirementsList) return;
  try {
    const res = await fetch('/api/requirements');
    const data = await res.json();
    requirementsList.innerHTML = '';
    (data.requirements || []).forEach(renderRequirementRow);
  } catch (e) {
    console.error('Failed to load requirements:', e);
  }
}

function renderRequirementRow(req) {
  if (!requirementsList) return;
  let row = requirementsList.querySelector(`[data-req-id="${req.id}"]`);
  if (!row) {
    row = document.createElement('div');
    row.className = 'requirement-row';
    row.dataset.reqId = req.id;
    requirementsList.appendChild(row);
  }

  const installing = req.status === 'installing';
  const badgeText = {installed: 'Installed', missing: 'Missing', installing: 'Installing…', error: 'Error'}[req.status] || req.status;

  let actionHtml = '';
  if (req.status === 'installed') {
    actionHtml = '';
  } else if (req.installable) {
    actionHtml = `<button class="apple-btn-secondary requirement-action" ${installing ? 'disabled' : ''}>${req.status === 'error' ? 'Retry' : 'Install'}</button>`;
  } else if (req.manual_url) {
    actionHtml = `<button class="apple-btn-secondary requirement-action" data-manual="${escapeHTML(req.manual_url)}">Open Site</button>`;
  }

  row.innerHTML = `
    <div class="requirement-info">
      <div class="requirement-label-row">
        <span class="requirement-label">${escapeHTML(req.label)}</span>
        <span class="requirement-badge ${req.status}">${badgeText}</span>
      </div>
      <span class="requirement-desc">${escapeHTML(req.version ? `${req.description} (${req.version})` : req.description)}</span>
      ${installing ? '<div class="progress-track requirement-progress"><div class="progress-fill indeterminate" style="width:40%"></div></div>' : ''}
      ${req.status === 'error' ? `<span class="requirement-desc" style="color:var(--red)">${escapeHTML(req.detail || '')}</span>` : ''}
    </div>
    ${actionHtml}
  `;

  const btn = row.querySelector('.requirement-action');
  if (btn) {
    if (btn.dataset.manual) {
      btn.onclick = () => window.open(btn.dataset.manual, '_blank');
    } else {
      btn.onclick = () => installRequirement(req.id);
    }
  }
}

async function installRequirement(id) {
  try {
    const res = await fetch(`/api/requirements/${id}/install`, { method: 'POST' });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Could not start install');
  } catch (e) {
    showToast(e.message);
  }
}

// Diagnostics Runner
window.runDiagnostics = async function() {
  const btn = document.getElementById('btn-run-diagnostics');
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span>Checking…</span>';
  }
  
  const start = performance.now();
  try {
    const res = await fetch('/api/health');
    const elapsed = Math.round(performance.now() - start);
    if (res.ok) {
      document.getElementById('diagnostics-status').textContent = `Download service connected · ${elapsed} ms`;
      showToast('Download service connected');
    } else {
      throw new Error('Service responded with status ' + res.status);
    }
  } catch (err) {
    document.getElementById('diagnostics-status').textContent = 'Unable to reach the download service.';
    showToast('Unable to reach the download service');
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = '<span>Run Health Check</span>';
    }
  }
};

// Initialize default threads from storage if set
const savedThreads = localStorage.getItem('copita_default_threads');
if (savedThreads) {
  const defThreadsSelect = document.getElementById('setting-default-threads');
  if (defThreadsSelect) defThreadsSelect.value = savedThreads;
  const selThreads = document.getElementById('sel-threads');
  if (selThreads) selThreads.value = savedThreads;
  const pillBadge = document.getElementById('pill-threads-badge');
  if (pillBadge) pillBadge.textContent = `${savedThreads}×`;
}

document.getElementById('sel-video-quality').value = localStorage.getItem('copita_video_quality') || 'best';
document.getElementById('sel-audio-format').value = localStorage.getItem('copita_audio_format') || 'mp3';

// Enter Key Listeners on all input fields
['url-input', 'url-input-video', 'url-input-audio', 'url-input-manga', 'url-input-files'].forEach(id => {
  const el = document.getElementById(id);
  if (el) {
    el.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        const isAudio = id === 'url-input-audio';
        startDownloadWithInput(id, isAudio);
      }
    });
  }
});

// Window Drag & Drop Handler (Invoked by native Swift wrapper)
window.handleDroppedURL = function(url) {
  if (url) {
    const target = document.getElementById(activeInputForPaste) || document.getElementById('url-input');
    if (target) target.value = url;
    startDownloadWithInput(activeInputForPaste || 'url-input', activeInputForPaste === 'url-input-audio');
  }
};

// Initializer
window.onload = () => {
  connectWebSocket();
  loadSystemInfo();
  switchTab('all');
};

