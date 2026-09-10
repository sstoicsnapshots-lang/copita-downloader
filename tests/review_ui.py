"""Browser smoke checks with fixture downloads; no external services required."""
from pathlib import Path
import json
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / 'copita-web'
OUT = ROOT / 'notes/previews'
OUT.mkdir(parents=True, exist_ok=True)
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1060, 'height': 720})
    errors, requests = [], []
    page.on('pageerror', lambda e: errors.append(str(e)))
    def route_request(route):
        path = route.request.url.split('copita.test/')[1].split('?')[0]
        if path.startswith('api/'):
            if route.request.method == 'POST':
                requests.append((path, route.request.post_data_json))
            route.fulfill(json={'disk_free_gb':143.7, 'disk_total_gb':512, 'download_dir':'/Downloads', 'status':'success', 'task':{'title':'Test'}})
        else:
            route.fulfill(path=str(WEB / (path or 'index.html')))
    page.route('http://copita.test/**', route_request)
    page.route_web_socket('ws://copita.test/ws', lambda ws: ws.send(json.dumps({'event':'init', 'tasks':[]})))
    page.goto('http://copita.test/')
    for width in (820, 1060, 1440):
        page.set_viewport_size({'width':width, 'height':720})
        for tab in ('all','active','completed','videos','audio','manga','files'):
            page.locator(f'.nav-item[data-filter="{tab}"]').click()
            assert page.locator('#floating-pill').is_hidden()
            assert page.locator('main select').count() == 0
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            canvas = page.locator('main').bounding_box()
            assert abs(canvas['x'] + canvas['width'] - width) < 2, canvas
            for selector in ('.download-action-bar', '.completed-toolbar-row', '.queue-actions-row'):
                for element in page.locator(selector).all():
                    if element.is_visible():
                        bounds = element.bounding_box()
                        assert bounds['x'] >= canvas['x'] and bounds['x'] + bounds['width'] <= width, (width, tab, bounds)
    page.set_viewport_size({'width':1060,'height':720})
    page.locator('.nav-item[data-filter="manga"]').click()
    page.screenshot(animations='disabled', path=str(OUT / 'manga.png'))
    page.evaluate("""() => {
      tasks = {
        video: {id:'video',title:'The scene from the Silo episode 8.mp4',filename:'silo.mp4',category:'video',status:'completed',percent:100,total:3780000,created_at:3},
        long: {id:'long',title:'A very long download filename that should stay within the available row width — episode two.mp4',filename:'long.mp4',category:'video',status:'completed',percent:100,total:22000000,created_at:2},
        failure: {id:'failure',title:'<img src=x onerror=alert(1)> Reddit clip',category:'video',status:'failed',error:'Reddit did not respond. Check that the post opens in your browser, then retry.',error_details:'TransportError: timed out',created_at:1}
      }; switchTab('videos');
    }""")
    assert page.locator('.task-title img').count() == 0
    assert page.locator('.is-completed .progress-track').count() == 0
    assert page.locator('#floating-pill').is_hidden()
    page.screenshot(animations='disabled', path=str(OUT / 'videos.png'))
    page.set_viewport_size({'width':820,'height':650})
    page.screenshot(animations='disabled', path=str(OUT / 'compact.png'))
    page.locator('#sidebar-filter').fill('not a matching filename')
    assert page.locator('.empty-title').inner_text() == 'No matching downloads'
    page.get_by_role('button', name='Clear search', exact=True).click()
    assert page.locator('.task-card').count() == 3
    page.set_viewport_size({'width':1060,'height':720})
    page.locator('#btn-open-settings').click()
    page.wait_for_function("getComputedStyle(document.getElementById('settings-modal')).opacity === '1'")
    page.locator('#setting-default-threads').select_option('8')
    page.locator('#sel-video-quality').select_option('720')
    page.locator('#sel-audio-format').select_option('m4a')
    page.screenshot(animations='disabled', path=str(OUT / 'preferences.png'))
    page.locator('#btn-save-settings').click()
    page.locator('#url-input-video').fill('https://reddit.com/comments/1w773bo/')
    page.locator('#context-videos .apple-btn-primary').click()
    page.wait_for_timeout(150)
    payload = next(data for path,data in requests if path == 'api/download')
    assert (payload['threads'],payload['video_quality'],payload['audio_format']) == (8,'720','m4a'), payload
    page.get_by_role('button',name='Retry',exact=True).click()
    page.wait_for_timeout(100)
    assert any(path == 'api/tasks/failure/retry' for path,_ in requests)
    page.evaluate("tasks.failure.status = 'analyzing'; renderTasks()")
    assert page.locator('#floating-pill').is_visible()
    page.evaluate("tasks.failure.status = 'downloading'; tasks.failure.speed_available = false; switchTab('active')")
    assert page.locator('#q-stat-speed').inner_text() == 'Not available'
    page.evaluate("tasks.failure.speed_available = true; tasks.failure.speed = 2048; renderTasks()")
    assert page.locator('#q-stat-speed').inner_text() == '2.0 KB/s'
    page.evaluate("tasks.failure.status = 'paused'; renderTasks()")
    assert page.locator('#q-stat-paused').inner_text() == '1'
    page.emulate_media(color_scheme='dark')
    page.screenshot(animations='disabled', path=str(OUT / 'dark.png'))
    assert not errors, errors
    print('PASS: 21 tab/width layouts, safe filenames, completed rows, search, settings payload, retry, active status, dark appearance; no JavaScript errors.')
    browser.close()