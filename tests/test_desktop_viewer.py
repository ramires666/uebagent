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
            elif path == "/status":
                body = {"model": {"ready": True}, "worker": {"ready": True, "recovery_state": "recovering", "recovery_needed": True}}
            elif path == "/desktop/screenshot":
                await route.fulfill(status=409, json={"error": {"code": "CAPTURE_BUSY", "message": "busy"}})
                return
            elif path == "/tasks":
                body = {"tasks": [{"task_id": "slow-model", "version": 1, "status": "running", "phase": "checking_dependencies", "step_count": 3, "spec": {"goal": "<img src=x onerror=alert(1)>"}, "result": {"summary": "token=secret https://example.test/private"}}]}
            elif path.startswith("/tasks/slow-model/events"):
                after = int(request.url.split("after_event_id=", 1)[1].split("&", 1)[0])
                events = [{"event_id": n, "event_type": "action.applied", "payload": {"kind": "navigate", "private": "never render this"}} for n in range(1, 105)]
                events.append({"event_id": 105, "event_type": "task.activity", "payload": {"stage": "thinking", "step": 2}})
                body = {"events": [event for event in events if event["event_id"] > after][:100]}
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
        assert await page.locator(".manual-controls").get_attribute("open") is None
        assert await page.get_by_text("Дополнительное управление", exact=True).is_visible()
        assert not await page.locator("#navigate-form").is_visible()
        for control_id in ("navigate-form", "navigate-url", "text-form", "text-input"):
            assert await page.locator(f"#{control_id}").count() == 1
        assert await page.locator("[data-key='Tab']").count() == 1
        assert await page.locator("[data-scroll='480']").count() == 1
        await page.get_by_text("Дополнительное управление", exact=True).click()
        assert await page.locator("#navigate-form").is_visible()
        await page.get_by_text("Дополнительное управление", exact=True).click()
        await page.wait_for_function("document.getElementById(\x27task-status\x27).textContent === \x27Выполняется\x27")
        assert await page.locator("#task-goal").inner_text() == "<img src=x onerror=alert(1)>"
        assert await page.locator("#task-goal img").count() == 0
        detail = await page.locator("#task-detail").inner_text()
        assert "выполнено: 3" in await page.locator("#task-progress-count").inner_text()
        assert "сейчас: Обработка задачи моделью (шаг 2)" in detail
        assert "secret" not in detail and "example.test" not in detail
        assert "\u041e\u0431\u0440\u0430\u0431\u043e\u0442\u043a\u0430 \u0437\u0430\u0434\u0430\u0447\u0438 \u043c\u043e\u0434\u0435\u043b\u044c\u044e" in await page.locator("#task-phase").inner_text()
        assert await page.locator("#service-status").get_attribute("data-state") == "warning"
        assert await page.locator("#service-status").inner_text()
        assert "Docker / WSL2" in await page.locator("#browser-runtime").inner_text()
        assert not any(method == "POST" for method, _, _ in calls)
        assert await page.locator("#takeover").is_enabled()
        assert await page.locator("#release-control").is_disabled()
        assert await page.locator("#notice").inner_text() == ""
        assert not await page.locator("#notice").is_visible()
        await page.locator("#takeover").click()
        await page.locator("#release-control").wait_for(state="visible")
        await page.wait_for_function("!document.getElementById('release-control').disabled")
        assert await page.locator("#takeover").is_disabled()
        await page.locator("#release-control").click()
        await page.wait_for_function("!document.getElementById('takeover').disabled")
        posts = [(path, csrf) for method, path, csrf in calls if method == "POST"]
        assert posts == [("/profiles/default/takeover", "fixture-csrf"), ("/profiles/default/release-control", "fixture-csrf")]
        await page.evaluate("document.documentElement.classList.add('desktop-embed')")
        assert not await page.locator(".manual-controls").is_visible()
        assert not await page.locator("header").is_visible()
        assert await page.locator(".viewport").is_visible()
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
async def test_viewer_task_composer_posts_exact_goal_once_and_reuses_key_after_unknown_outcome():
    playwright_async = pytest.importorskip("playwright.async_api")
    if not CHROME.is_file():
        pytest.skip("local Chrome unavailable")
    server = _viewer_server()
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    playwright = await playwright_async.async_playwright().start()
    browser = None
    posts = []
    post_started = asyncio.Event()
    release_first = asyncio.Event()
    current_url = "https://example.test/account?private=path"
    try:
        browser = await playwright.chromium.launch(executable_path=str(CHROME), headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 900})

        async def api(route):
            nonlocal current_url
            req = route.request
            path = req.url.split("/api/v1", 1)[1]
            if path == "/operator/session":
                await route.fulfill(json={"authenticated": True, "csrf_token": "fixture-csrf"})
            elif path == "/desktop/state":
                await route.fulfill(json={"available": True, "control_owner": "agent", "control_epoch": 1,
                                          "url": current_url, "width": 1440, "height": 900})
            elif path == "/desktop/screenshot":
                await route.fulfill(status=409, json={"error": {"code": "CAPTURE_BUSY", "message": "busy"}})
            elif path == "/tasks" and req.method == "GET":
                await route.fulfill(json={"tasks": []})
            elif path == "/status":
                await route.fulfill(json={})
            elif path == "/tasks" and req.method == "POST":
                posts.append({"payload": req.post_data_json, "csrf": req.headers.get("x-csrf-token")})
                if len(posts) == 1:
                    post_started.set()
                    await release_first.wait()
                    await route.fulfill(status=503, json={"error": {"message": "temporary outage"}})
                else:
                    await route.fulfill(status=201, json={"task_id": "created-task", "status": "queued", "version": 1,
                                                          "spec": req.post_data_json})
            else:
                await route.fulfill(status=404, json={})

        await page.route("**/api/v1/**", api)
        await page.goto(f"http://127.0.0.1:{server.server_port}/desktop.html")
        await page.wait_for_function("document.getElementById('task-site-hint').textContent === 'Сайт: https://example.test'")
        assert "private" not in await page.locator("#task-site-hint").inner_text()
        command = "  \u0418\u0432\u0430\u043d: \u00ab\u0441\u0440\u0430\u0432\u043d\u0438 \u0446\u0435\u043d\u044b\u00bb.\n"
        await page.locator("#task-command").fill(command)
        await page.evaluate("""() => {
          const button = document.getElementById('task-command-submit');
          button.click(); button.click();
        }""")
        await asyncio.wait_for(post_started.wait(), timeout=5)
        assert len(posts) == 1
        assert await page.locator("#task-command-submit").is_disabled()
        release_first.set()
        await page.wait_for_function("document.getElementById('task-command-status').textContent.includes('Проверьте текущую задачу')")
        assert await page.locator("#task-command").input_value() == command
        current_url = "https://new.example/path"
        await page.locator("#refresh").click()
        await page.wait_for_function("document.getElementById('task-site-hint').textContent === 'Повторная отправка для сайта: https://example.test'")
        await page.locator("#task-command-submit").click()
        await page.wait_for_function("document.getElementById('task-command-status').textContent.includes('Задача создана: created-task')")
        assert len(posts) == 2
        assert posts[0] == posts[1]
        assert posts[0]["csrf"] == "fixture-csrf"
        payload = posts[0]["payload"]
        assert payload == {
            "goal_language": "ru", "success_conditions": [], "user_inputs": {}, "secret_refs": [],
            "policy_grant_id": None, "sync_cookies": True, "site_capabilities": [],
            "limits": {"timeout_s": 900, "max_steps": 80}, "profile_id": "default", "goal": command,
            "workflow": "agent", "verbatim_text": None, "allowed_origins": ["https://example.test"],
            "execution_mode": "autonomous", "idempotency_key": payload["idempotency_key"]
        }
        await page.evaluate("document.documentElement.classList.add('desktop-embed')")
        assert not await page.locator("#task-composer").is_visible()
    finally:
        if browser:
            await browser.close()
        await playwright.stop()
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.asyncio
async def test_viewer_confirms_takeover_and_forwards_coalesced_pointer_and_wheel():
    playwright_async = pytest.importorskip("playwright.async_api")
    if not CHROME.is_file():
        pytest.skip("local Chrome unavailable")
    server = _viewer_server()
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    playwright = await playwright_async.async_playwright().start()
    browser = None
    owner, epoch = "none", 1
    events, actions = [], []
    screenshot_calls = 0
    first_move_started = asyncio.Event()
    release_first_move = asyncio.Event()
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/dX8AAAAASUVORK5CYII=")
    try:
        browser = await playwright.chromium.launch(executable_path=str(CHROME), headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 900})

        async def api(route):
            nonlocal owner, epoch, screenshot_calls
            req = route.request
            path = req.url.split("/api/v1", 1)[1]
            if path == "/operator/session":
                body = {"authenticated": True, "csrf_token": "fixture-csrf"}
            elif path == "/desktop/state":
                body = {"available": True, "control_owner": owner, "control_epoch": epoch,
                        "width": 1440, "height": 900, "worker_instance_id": "synthetic-fixture"}
            elif path == "/desktop/screenshot":
                screenshot_calls += 1
                await route.fulfill(body=png, content_type="image/png"); return
            elif path == "/profiles/default/takeover":
                owner = "human"; epoch += 1
                body = {"control_epoch": epoch, "control_owner": owner}
            elif path == "/profiles/default/release-control":
                owner = "none"; epoch += 1
                body = {"control_epoch": epoch, "control_owner": owner}
            elif path == "/desktop/action":
                action = req.post_data_json["action"]
                actions.append(action)
                if action["kind"] == "mouse_move" and not first_move_started.is_set():
                    first_move_started.set()
                    await release_first_move.wait()
                epoch += 1
                body = {"outcome": "applied", "control_epoch": epoch, "control_owner": owner}
            else:
                await route.fulfill(status=404, json={}); return
            events.append((req.method, path, owner))
            await route.fulfill(json=body)

        await page.route("**/api/v1/**", api)
        await page.goto(f"http://127.0.0.1:{server.server_port}/desktop.html")
        await page.evaluate("""() => {
          window.noticeHistory = [];
          const notice = document.getElementById('notice');
          new MutationObserver(() => window.noticeHistory.push(notice.textContent)).observe(notice, {childList: true, subtree: true, characterData: true});
        }""")
        await page.locator("#takeover").click()
        await page.locator("#release-control").wait_for(state="visible", timeout=3000)
        await page.wait_for_function("!document.getElementById('release-control').disabled", timeout=3000)
        await page.wait_for_function("window.noticeHistory.some(text => text.includes('Управление передано'))", timeout=3000)
        post_index = next(i for i, event in enumerate(events) if event[:2] == ("POST", "/profiles/default/takeover"))
        confirmed_index = next(i for i, event in enumerate(events) if i > post_index and event == ("GET", "/desktop/state", "human"))
        assert confirmed_index > post_index

        await page.locator("#browser-image").wait_for(state="visible")
        image = page.locator("#browser-image")
        await image.evaluate("el => { el.style.width = '1200px'; el.style.height = '600px'; }")
        await page.wait_for_timeout(650)
        assert screenshot_calls >= 3

        bounds = await image.bounding_box()
        assert bounds
        first = {"x": bounds["x"] + 100, "y": bounds["y"] + 80}
        latest = {"x": bounds["x"] + 300, "y": bounds["y"] + 250}
        await page.mouse.move(first["x"], first["y"])
        await asyncio.wait_for(first_move_started.wait(), timeout=5)
        await page.mouse.move(latest["x"], latest["y"])
        await page.mouse.wheel(0, 400)
        await page.mouse.wheel(0, 400)
        release_first_move.set()
        for _ in range(100):
            if sum(a["kind"] == "scroll" for a in actions) >= 2 and sum(a["kind"] == "mouse_move" for a in actions) >= 2:
                break
            await asyncio.sleep(.05)

        moves = [a for a in actions if a["kind"] == "mouse_move"]
        scrolls = [a for a in actions if a["kind"] == "scroll"]
        expected_x = round((latest["x"] - bounds["x"]) * 1440 / bounds["width"])
        expected_y = round((latest["y"] - bounds["y"]) * 900 / bounds["height"])
        assert len(moves) == 2
        assert moves[-1]["x"] == expected_x and moves[-1]["y"] == expected_y
        assert len(scrolls) == 2 and sum(a["dy"] for a in scrolls) == 800
        assert all(a["x"] == expected_x and a["y"] == expected_y for a in scrolls)
        assert all(0 < a["dy"] <= 600 for a in scrolls)

        moved_geometry = await image.evaluate("(el, point) => { el.style.transform = 'translateY(12px)'; const wheel = new WheelEvent('wheel', {bubbles: true, cancelable: true, clientX: Math.round(point.x), clientY: Math.round(point.y), deltaY: 100}); const client = {x: wheel.clientX, y: wheel.clientY}; el.dispatchEvent(wheel); const rect = el.getBoundingClientRect(); return {client, rect: {left: rect.left, top: rect.top, width: rect.width, height: rect.height}}; }", latest)
        for _ in range(100):
            if sum(a["kind"] == "scroll" for a in actions) >= 3:
                break
            await asyncio.sleep(.02)
        moved_scroll = [a for a in actions if a["kind"] == "scroll"][-1]
        expected_moved_x = round((moved_geometry["client"]["x"] - moved_geometry["rect"]["left"]) * 1440 / moved_geometry["rect"]["width"])
        expected_moved_y = round((moved_geometry["client"]["y"] - moved_geometry["rect"]["top"]) * 900 / moved_geometry["rect"]["height"])
        assert moved_scroll["x"] == expected_moved_x and moved_scroll["y"] == expected_moved_y
    finally:
        release_first_move.set()
        if browser: await browser.close()
        await playwright.stop()
        server.shutdown(); server.server_close(); thread.join()


@pytest.mark.asyncio
async def test_embedded_viewer_is_browser_only_and_skips_task_service_polling():
    playwright_async = pytest.importorskip("playwright.async_api")
    if not CHROME.is_file():
        pytest.skip("local Chrome unavailable")
    server = _viewer_server()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    playwright = await playwright_async.async_playwright().start()
    browser = None
    calls = []
    screenshot_calls = 0
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/dX8AAAAASUVORK5CYII=")
    try:
        browser = await playwright.chromium.launch(executable_path=str(CHROME), headless=True)
        page = await browser.new_page(viewport={"width": 1200, "height": 600})

        async def api(route):
            nonlocal screenshot_calls
            path = route.request.url.split("/api/v1", 1)[1]
            calls.append((route.request.method, path))
            if path == "/operator/session":
                await route.fulfill(json={"authenticated": True, "csrf_token": "fixture"})
            elif path == "/desktop/state":
                await route.fulfill(json={"available": True, "control_owner": "agent", "control_epoch": 2, "width": 1440, "height": 900})
            elif path == "/desktop/screenshot":
                screenshot_calls += 1
                if screenshot_calls == 1:
                    await route.fulfill(body=png, content_type="image/png")
                else:
                    await route.fulfill(status=200, json={"not": "an image"})
            else:
                await route.fulfill(status=404, json={})

        await page.route("**/api/v1/**", api)
        await page.goto(f"http://127.0.0.1:{server.server_port}/desktop.html?embed=1")
        await page.wait_for_function("document.documentElement.classList.contains('desktop-embed')")
        await page.locator("#browser-image").wait_for(state="visible")
        await page.wait_for_function("document.querySelector('#owner-status').classList.contains('good')")
        assert await page.locator("header").evaluate("e => getComputedStyle(e).display") == "none"
        assert await page.locator(".progress-panels").evaluate("e => getComputedStyle(e).display") == "none"
        assert await page.locator(".controls").evaluate("e => getComputedStyle(e).display") == "none"
        assert await page.locator(".screen-head").evaluate("e => getComputedStyle(e).display") == "none"
        bounds = await page.locator(".viewport").bounding_box()
        assert bounds and bounds["width"] == 1200 and bounds["height"] == 600
        assert await page.locator("#remote-keyboard").get_attribute("type") == "password"
        assert "good" in await page.locator("#owner-status").get_attribute("class")
        await page.locator("#refresh").evaluate("e => e.click()")
        await page.wait_for_function("!document.querySelector('#owner-status').classList.contains('good')")
        assert "good" not in await page.locator("#owner-status").get_attribute("class")
        assert await page.locator("#control-owner-label").inner_text() == "\u0417\u0430\u0434\u0430\u0447\u0430 \u0443\u043f\u0440\u0430\u0432\u043b\u044f\u0435\u0442 \u0431\u0440\u0430\u0443\u0437\u0435\u0440\u043e\u043c"
        assert not any(path.startswith("/tasks") or path == "/status" for _, path in calls)
        assert any(path.startswith("/desktop/screenshot") for _, path in calls)
        assert not any(method == "POST" for method, _ in calls)
    finally:
        if browser:
            await browser.close()
        await playwright.stop()
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.asyncio
async def test_standalone_task_controls_use_fresh_version_and_only_explicit_post():
    playwright_async = pytest.importorskip("playwright.async_api")
    if not CHROME.is_file():
        pytest.skip("local Chrome unavailable")
    server = _viewer_server()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    playwright = await playwright_async.async_playwright().start()
    browser = None
    task = {"task_id": "standalone-task", "version": 7, "status": "running", "phase": "checking_dependencies", "step_count": 3, "spec": {"goal": "Inspect safely", "execution_mode": "autonomous"}}
    calls = []
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/dX8AAAAASUVORK5CYII=")
    try:
        browser = await playwright.chromium.launch(executable_path=str(CHROME), headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 900})

        async def api(route):
            nonlocal task
            req = route.request
            path = req.url.split("/api/v1", 1)[1].split("?", 1)[0]
            calls.append((req.method, path, req.headers.get("x-csrf-token"), req.post_data_json if req.post_data else None))
            if path == "/operator/session": body = {"authenticated": True, "csrf_token": "fixture"}
            elif path == "/desktop/state": body = {"available": True, "control_owner": "agent", "control_epoch": 1, "width": 1440, "height": 900}
            elif path == "/desktop/screenshot": await route.fulfill(body=png, content_type="image/png"); return
            elif path == "/status": body = {"model": {"ready": True}, "worker": {"ready": True, "recovery_state": "idle"}}
            elif path == "/tasks": body = {"tasks": [task]}
            elif path == "/tasks/standalone-task": body = {"task": task}
            elif path == "/tasks/standalone-task/events": body = {"events": []}
            elif path == "/tasks/standalone-task/pause": task = {**task, "version": 8, "status": "pause_requested"}; body = {"task": task}
            else: await route.fulfill(status=404, json={}); return
            await route.fulfill(json=body)

        await page.route("**/api/v1/**", api)
        await page.goto(f"http://127.0.0.1:{server.server_port}/desktop.html")
        await page.locator("#task-status").wait_for()
        await page.locator("#task-actions").get_by_role("button", name="Пауза").wait_for(state="visible")
        assert await page.locator("#task-actions").evaluate("el => el.parentElement.classList.contains('top-actions')")
        assert await page.locator("#task-actions").get_by_role("button", name="Отменить").is_visible()
        assert await page.get_by_role("button", name="Забрать управление").count() == 1
        assert not any(method == "POST" for method, _, _, _ in calls)
        await page.locator("#task-actions").get_by_role("button", name="Пауза").click()
        await page.wait_for_function("document.querySelector('#task-status').textContent.includes('запрошена')")
        pause_posts = [call for call in calls if call[0] == "POST" and call[1] == "/tasks/standalone-task/pause"]
        assert len(pause_posts) == 1
        assert pause_posts[0][2] == "fixture"
        assert pause_posts[0][3] == {"expected_version": 7}
        assert not [call for call in calls if call[0] == "POST" and call[1] == "/tasks"]
    finally:
        if browser:
            await browser.close()
        await playwright.stop()
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.asyncio
async def test_viewer_surfaces_actionable_stop_reason_and_failed_step():
    playwright_async = pytest.importorskip("playwright.async_api")
    if not CHROME.is_file():
        pytest.skip("local Chrome unavailable")
    server = _viewer_server()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    playwright = await playwright_async.async_playwright().start()
    browser = None
    calls = []
    try:
        browser = await playwright.chromium.launch(executable_path=str(CHROME), headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 900})

        async def api(route):
            path = route.request.url.split("/api/v1", 1)[1]
            calls.append((route.request.method, path))
            if path == "/operator/session":
                body = {"authenticated": True, "csrf_token": "fixture"}
            elif path == "/desktop/state":
                body = {"available": True, "control_owner": "agent", "control_epoch": 4, "width": 1440, "height": 900}
            elif path == "/desktop/screenshot":
                await route.fulfill(status=409, json={"error": {"code": "CAPTURE_BUSY", "message": "busy"}})
                return
            elif path == "/status":
                body = {"model": {"ready": True}, "worker": {"ready": True, "recovery_state": "idle"}}
            elif path == "/tasks":
                body = {"tasks": [{"task_id": "attention-task", "version": 1, "status": "waiting_human", "phase": "capability_unavailable", "step_count": 1, "error": {"code": "CAPABILITY_UNAVAILABLE", "message": "Gemini page is not in a safe state to refresh"}, "spec": {"goal": "Inspect current page safely", "execution_mode": "autonomous"}}]}
            elif path.startswith("/tasks/attention-task/events"):
                body = {"events": [{"event_id": 1, "event_type": "action.failed", "payload": {"code": "CAPABILITY_UNAVAILABLE", "kind": "gemini_refresh_session"}}]}
            else:
                await route.fulfill(status=404, json={})
                return
            await route.fulfill(json=body)

        await page.route("**/api/v1/**", api)
        await page.goto(f"http://127.0.0.1:{server.server_port}/desktop.html")
        reason = page.locator("#task-stop-reason")
        await reason.wait_for(state="visible")
        reason_text = await reason.inner_text()
        assert "CAPABILITY_UNAVAILABLE" in reason_text
        assert "Gemini page is not in a safe state to refresh" in reason_text
        assert "Проверка безопасности страницы Gemini" in reason_text
        assert "\u041f\u0440\u043e\u0432\u0435\u0440\u043a\u0430 \u0431\u0435\u0437\u043e\u043f\u0430\u0441\u043d\u043e\u0433\u043e \u0441\u043e\u0441\u0442\u043e\u044f\u043d\u0438\u044f \u0441\u0442\u0440\u0430\u043d\u0438\u0446\u044b" in await page.locator("#task-phase").inner_text()
        assert not any(method == "POST" for method, _ in calls)
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
            await page.locator('#refresh').focus()
            await page.wait_for_function("document.getElementById('notice').textContent.includes('удалён')")
        else:
            await page.keyboard.insert_text('queued-fixture')
            if scenario == 'ownership':
                owner = 'none'; epoch += 1
                await page.wait_for_function("document.getElementById('remote-keyboard').disabled")
            elif scenario == 'blur':
                await page.locator('#refresh').focus()
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

