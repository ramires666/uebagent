from __future__ import annotations

import asyncio
import base64
import errno
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from random import SystemRandom
from threading import Thread

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")


def _viewer_server() -> ThreadingHTTPServer:
    handler = partial(SimpleHTTPRequestHandler, directory=str(ROOT / "ui"))
    for _ in range(128):
        # The OS ephemeral range can include Chrome's restricted ports.
        port = SystemRandom().randrange(20000, 60001)
        try:
            return ThreadingHTTPServer(("127.0.0.1", port), handler)
        except OSError as exc:
            if exc.errno not in {errno.EADDRINUSE, errno.EACCES} and getattr(exc, "winerror", None) not in {10013, 10048}:
                raise
    raise RuntimeError("could not bind an available viewer test port")


@pytest.mark.asyncio
async def test_viewer_task_progress_and_explicit_control():
    playwright_async = pytest.importorskip("playwright.async_api")
    if not CHROME.is_file():
        pytest.skip("local Chrome unavailable")
    server = _viewer_server()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    playwright = await playwright_async.async_playwright().start()
    browser = None
    calls = []
    owner = "agent"
    try:
        browser = await playwright.chromium.launch(executable_path=str(CHROME), headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 900})

        async def api(route):
            nonlocal owner
            request = route.request
            path = request.url.split("/api/v1", 1)[1]
            calls.append((request.method, path, request.headers.get("x-csrf-token")))
            if path == "/operator/session":
                body = {"authenticated": True, "csrf_token": "fixture-csrf"}
            elif path == "/desktop/state":
                body = {"available": True, "control_owner": owner, "control_epoch": 2, "width": 1440, "height": 900, "backend": "Camoufox", "runtime_environment": "docker"}
            elif path == "/desktop/screenshot":
                await route.fulfill(status=409, json={"error": {"code": "CAPTURE_BUSY", "message": "busy"}})
                return
            elif path == "/tasks":
                body = {"tasks": [{"status": "running", "step_count": 3, "spec": {"goal": "<img src=x onerror=alert(1)>"}, "result": {"summary": "token=secret https://example.test/private"}}]}
            elif path == "/profiles/default/takeover":
                owner = "human"
                body = {"ok": True}
            elif path == "/profiles/default/release-control":
                owner = "none"
                body = {"ok": True}
            else:
                await route.fulfill(status=404, json={})
                return
            await route.fulfill(json=body)

        await page.route("**/api/v1/**", api)
        await page.goto(f"http://127.0.0.1:{server.server_port}/desktop.html")
        await page.wait_for_function("document.getElementById(\x27task-status\x27).textContent === \x27Выполняется\x27")
        assert await page.locator("#task-goal").inner_text() == "<img src=x onerror=alert(1)>"
        assert await page.locator("#task-goal img").count() == 0
        detail = await page.locator("#task-detail").inner_text()
        assert "Шагов: 3" in detail and "secret" not in detail and "example.test" not in detail
        assert "Docker / WSL2" in await page.locator("#browser-runtime").inner_text()
        assert not any(method == "POST" for method, _, _ in calls)
        assert await page.locator("#takeover").is_enabled()
        assert await page.locator("#release-control").is_disabled()
        await page.locator("#takeover").click()
        await page.locator("#release-control").wait_for(state="visible")
        await page.wait_for_function("!document.getElementById('release-control').disabled")
        assert await page.locator("#takeover").is_disabled()
        await page.locator("#release-control").click()
        await page.wait_for_function("!document.getElementById('takeover').disabled")
        posts = [(path, csrf) for method, path, csrf in calls if method == "POST"]
        assert posts == [("/profiles/default/takeover", "fixture-csrf"), ("/profiles/default/release-control", "fixture-csrf")]
        await asyncio.sleep(2.2)
        assert 1 <= sum(path == "/tasks" for _, path, _ in calls) <= 2
        assert "busy" not in await page.locator("#notice").inner_text()
    finally:
        if browser:
            await browser.close()
        await playwright.stop()
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.asyncio
async def test_viewer_forwards_keyboard_in_order_with_fresh_epochs():
    playwright_async = pytest.importorskip("playwright.async_api")
    if not CHROME.is_file():
        pytest.skip("local Chrome unavailable")
    server = _viewer_server()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    playwright = await playwright_async.async_playwright().start()
    browser = None
    owner = "none"
    epoch = 1
    actions = []
    bad_epochs = []
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/dX8AAAAASUVORK5CYII=")
    try:
        browser = await playwright.chromium.launch(executable_path=str(CHROME), headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 900})

        async def api(route):
            nonlocal owner, epoch
            req = route.request
            path = req.url.split('/api/v1', 1)[1]
            if path == '/operator/session': body = {'authenticated': True, 'csrf_token': 'fixture'}
            elif path == '/tasks': body = {'tasks': []}
            elif path == '/desktop/screenshot':
                await route.fulfill(body=png, content_type='image/png'); return
            elif path == '/desktop/state':
                body = {'available': True, 'control_owner': owner, 'control_epoch': max(1, epoch - 1) if epoch > 2 else epoch, 'width': 1440, 'height': 900, 'worker_instance_id': 'fixture'}
            elif path == '/profiles/default/takeover': owner = 'human'; epoch += 1; body = {'control_epoch': epoch}
            elif path == '/desktop/action':
                payload = req.post_data_json
                if payload['control_epoch'] != epoch: bad_epochs.append(True)
                actions.append(payload['action'])
                await asyncio.sleep(.08)
                epoch += 1
                body = {'outcome': 'applied', 'control_epoch': epoch, 'control_owner': owner}
            else: body = {}
            await route.fulfill(json=body)

        await page.route('**/api/v1/**', api)
        await page.goto(f'http://127.0.0.1:{server.server_port}/desktop.html')
        await page.locator('#takeover').click()
        await page.wait_for_function("!document.getElementById('remote-keyboard').disabled")
        await page.locator('#browser-image').wait_for(state='visible')
        await page.locator('#browser-image').click()
        assert await page.locator('#remote-keyboard').get_attribute('type') == 'password'
        await page.keyboard.type('Ab')
        await page.keyboard.insert_text('Привет😀')
        await page.evaluate("""() => { const d = new DataTransfer(); d.setData('text/plain', 'Paste✓'); document.getElementById('remote-keyboard').dispatchEvent(new ClipboardEvent('paste', {clipboardData:d, bubbles:true, cancelable:true})); }""")
        await page.keyboard.press('Control+a')
        await page.keyboard.press('Backspace')
        await page.keyboard.press('Tab')
        await page.keyboard.press('Enter')
        await page.wait_for_function("document.querySelector('#notice').textContent.includes('отправлено')", timeout=10000)
        for _ in range(100):
            if len([a for a in actions if a.get('kind') == 'press_key']) == 4: break
            await asyncio.sleep(.05)
        assert actions[0]['kind'] == 'click'
        assert ''.join(a['text'] for a in actions if a['kind'] == 'type_text') == 'AbПривет😀Paste✓'
        assert [a['key'] for a in actions if a['kind'] == 'press_key'] == ['Control+a', 'Backspace', 'Tab', 'Enter']
        assert not bad_epochs
        assert await page.locator('#remote-keyboard').input_value() == ''
    finally:
        if browser:
            await browser.close()
        await playwright.stop()
        server.shutdown(); server.server_close(); thread.join()


@pytest.mark.asyncio
@pytest.mark.parametrize('scenario', ['ownership', 'unknown', 'overflow', 'blur', 'composition'])
async def test_viewer_drops_queued_input_safely(scenario):
    playwright_async = pytest.importorskip('playwright.async_api')
    if not CHROME.is_file():
        pytest.skip('local Chrome unavailable')
    server = _viewer_server()
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    playwright = await playwright_async.async_playwright().start()
    browser = None
    owner, epoch, actions = 'human', 1, []
    gate = asyncio.Event()
    png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/dX8AAAAASUVORK5CYII=')
    try:
        browser = await playwright.chromium.launch(executable_path=str(CHROME), headless=True)
        page = await browser.new_page(viewport={'width': 1440, 'height': 900})
        async def api(route):
            nonlocal owner, epoch
            req = route.request; path = req.url.split('/api/v1', 1)[1]
            if path == '/operator/session': body = {'authenticated': True, 'csrf_token': 'fixture'}
            elif path == '/tasks': body = {'tasks': []}
            elif path == '/desktop/screenshot':
                await route.fulfill(body=png, content_type='image/png'); return
            elif path == '/desktop/state': body = {'available': True, 'control_owner': owner, 'control_epoch': epoch, 'worker_instance_id': 'fixture', 'width': 1440, 'height': 900}
            elif path == '/desktop/action':
                actions.append(req.post_data_json['action'])
                if len(actions) == 1: await gate.wait()
                epoch += 1
                body = {'outcome': 'unknown' if scenario == 'unknown' and len(actions) == 1 else 'applied', 'control_epoch': epoch}
            else: body = {}
            await route.fulfill(json=body)
        await page.route('**/api/v1/**', api)
        await page.goto(f'http://127.0.0.1:{server.server_port}/desktop.html')
        await page.wait_for_function("!document.getElementById('remote-keyboard').disabled")
        await page.locator('#browser-image').click()
        await page.wait_for_timeout(100)
        if scenario == 'overflow':
            await page.keyboard.insert_text('x' * 8193)
            await page.wait_for_function("document.getElementById('notice').textContent.includes('переполнена')")
        elif scenario == 'composition':
            await page.evaluate("""() => { const el = document.getElementById('remote-keyboard'); el.value = 'composition-fixture'; el.dispatchEvent(new InputEvent('input', {bubbles: true, isComposing: true})); }""")
            await page.locator('#navigate-url').focus()
            await page.wait_for_function("document.getElementById('notice').textContent.includes('удалён')")
        else:
            await page.keyboard.insert_text('queued-fixture')
            if scenario == 'ownership':
                owner = 'none'; epoch += 1
                await page.wait_for_function("document.getElementById('remote-keyboard').disabled")
            elif scenario == 'blur':
                await page.locator('#navigate-url').focus()
                await page.wait_for_function("document.getElementById('notice').textContent.includes('удалён')")
        gate.set()
        await page.wait_for_timeout(500)
        assert [a['kind'] for a in actions] == ['click']
        assert await page.locator('#remote-keyboard').input_value() == ''
        if scenario == 'unknown':
            assert 'неизвестен' in await page.locator('#notice').inner_text()
            await page.keyboard.insert_text('later-fixture')
            await page.wait_for_timeout(200)
            assert len(actions) == 1
    finally:
        gate.set()
        if browser: await browser.close()
        await playwright.stop()
        server.shutdown(); server.server_close(); thread.join()


@pytest.mark.asyncio
async def test_screenshot_urls_remain_bounded_when_images_never_load():
    playwright_async = pytest.importorskip("playwright.async_api")
    if not CHROME.is_file():
        pytest.skip("local Chrome unavailable")
    server = _viewer_server()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    playwright = await playwright_async.async_playwright().start()
    browser = None
    try:
        browser = await playwright.chromium.launch(executable_path=str(CHROME), headless=True)
        page = await browser.new_page()
        await page.add_init_script("""(() => {
          const active = new Set();
          window.urlStats = { active, created: 0, max: 0 };
          URL.createObjectURL = () => {
            const url = `blob:fixture-${++window.urlStats.created}`;
            active.add(url);
            window.urlStats.max = Math.max(window.urlStats.max, active.size);
            return url;
          };
          URL.revokeObjectURL = (url) => active.delete(url);
          // Leave each frame pending forever, as if decoding had stalled.
          let pendingSrc = '';
          Object.defineProperty(HTMLImageElement.prototype, 'src', {
            configurable: true, set(value) { pendingSrc = value; },
            get() { return pendingSrc; }
          });
        })()""")

        async def api(route):
            path = route.request.url.split("/api/v1", 1)[1]
            if path == "/desktop/screenshot":
                await route.fulfill(status=200, content_type="image/png", body=b"fake-frame")
            elif path == "/desktop/state":
                await route.fulfill(json={"available": True, "control_owner": "agent", "control_epoch": 1})
            elif path == "/operator/session":
                await route.fulfill(json={"authenticated": True, "csrf_token": "fixture"})
            else:
                await route.fulfill(json={"tasks": []})

        await page.route("**/api/v1/**", api)
        await page.goto(f"http://127.0.0.1:{server.server_port}/desktop.html")
        await page.wait_for_function("window.urlStats.created >= 1")
        for target in range(2, 7):
            await page.locator("#refresh").click()
            await page.wait_for_function("n => window.urlStats.created >= n", arg=target)
        assert await page.evaluate("window.urlStats.max") <= 2
        assert await page.evaluate("window.urlStats.active.size") == 1
        await page.evaluate("window.dispatchEvent(new Event('unload'))")
        assert await page.evaluate("window.urlStats.active.size") == 0
    finally:
        if browser:
            await browser.close()
        await playwright.stop()
        server.shutdown()
        server.server_close()
        thread.join()

