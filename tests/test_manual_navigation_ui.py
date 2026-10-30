from __future__ import annotations

import errno
import asyncio
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from random import SystemRandom
from threading import Thread

import pytest
from fastapi.testclient import TestClient

from browser_mcp.app import create_app
from browser_mcp.config import Settings


ROOT = Path(__file__).resolve().parents[1]
CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")


class FakeWorker:
    def __init__(self):
        self.actions = []

    async def health(self):
        return {"configured": True, "ready": True, "browser_started": True}

    async def state(self):
        return {"available": True, "browser_started": True, "control_owner": "human",
                "control_epoch": 12, "url": "https://start.example/", "width": 1440,
                "height": 900, "page_id": "main"}

    async def gate(self, owner):
        return {"control_owner": owner, "control_epoch": 12}

    async def can_import(self):
        return False

    async def call(self, method, path, data=None, raw=False):
        if path == "/manual/execute":
            self.actions.append(data)
            return {"outcome": "applied", "control_owner": "human", "control_epoch": 13}
        if path == "/screenshot":
            return b"\x89PNG\r\n\x1a\nfixture"
        return {"ready": True, "configured": True}


class FakeModel:
    async def health(self):
        return {"configured": True, "ready": True}


def make_client(tmp_path):
    worker = FakeWorker()
    settings = Settings(data_dir=tmp_path, ui_dir=ROOT / "ui", test_mode=True,
                        operator_token="operator-test", mcp_token="mcp-test")
    return TestClient(create_app(settings, worker=worker, model=FakeModel())), worker


def login(client):
    response = client.post("/api/v1/operator/session", json={"token": "operator-test"})
    assert response.status_code == 200
    return {"X-CSRF-Token": response.json()["csrf_token"]}


def test_manual_navigation_field_is_optional_boolean_and_forwarded(tmp_path):
    client, worker = make_client(tmp_path)
    with client:
        headers = login(client)
        endpoint = "/api/v1/desktop/action"
        legacy = {"control_epoch": 4,
                  "action": {"kind": "navigate", "url": "https://One.Example:443/path"}}
        response = client.post(endpoint, json=legacy, headers=headers)
        assert response.status_code == 200
        assert worker.actions[-1] == {**legacy, "allowed_origins": ["https://one.example"]}

        opted_out = {**legacy, "allow_cross_origin_navigation": False}
        response = client.post(endpoint, json=opted_out, headers=headers)
        assert response.status_code == 200
        assert worker.actions[-1] == {**opted_out, "allowed_origins": ["https://one.example"]}

        opted_in_key = {"control_epoch": 5, "action": {"kind": "press_key", "key": "Enter"},
                        "allow_cross_origin_navigation": True}
        response = client.post(endpoint, json=opted_in_key, headers=headers)
        assert response.status_code == 200 and worker.actions[-1] == opted_in_key

        before = len(worker.actions)
        for value in (None, 0, "false", []):
            invalid = {**legacy, "allow_cross_origin_navigation": value}
            response = client.post(endpoint, json=invalid, headers=headers)
            assert response.status_code >= 400
        invalid_extra = {**legacy, "unexpected": True}
        response = client.post(endpoint, json=invalid_extra, headers=headers)
        assert response.status_code >= 400
        assert len(worker.actions) == before


def _viewer_server() -> ThreadingHTTPServer:
    handler = partial(SimpleHTTPRequestHandler, directory=str(ROOT / "ui"))
    for _ in range(128):
        port = SystemRandom().randrange(20000, 60001)
        try:
            return ThreadingHTTPServer(("127.0.0.1", port), handler)
        except OSError as exc:
            if exc.errno not in {errno.EADDRINUSE, errno.EACCES} and getattr(exc, "winerror", None) not in {10013, 10048}:
                raise
    raise RuntimeError("could not bind an available viewer test port")


@pytest.mark.asyncio
async def test_manual_navigation_preference_defaults_true_persists_and_applies_next_action():
    playwright_async = pytest.importorskip("playwright.async_api")
    if not CHROME.is_file():
        pytest.skip("local Chrome unavailable")
    server = _viewer_server()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    playwright = await playwright_async.async_playwright().start()
    browser = None
    actions = []
    try:
        browser = await playwright.chromium.launch(executable_path=str(CHROME), headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        action_event = asyncio.Event()
        post_paths = []

        async def api(route):
            request = route.request
            path = request.url.split("/api/v1", 1)[1]
            if request.method == "POST":
                post_paths.append(path)
            if path == "/operator/session":
                body = {"authenticated": True, "csrf_token": "fixture-csrf"}
            elif path == "/desktop/state":
                body = {"available": True, "browser_started": True, "control_owner": "human",
                        "control_epoch": 12, "width": 1440, "height": 900,
                        "url": "https://start.example/", "page_id": "main"}
            elif path == "/desktop/screenshot":
                await route.fulfill(status=200, body=b"\x89PNG\r\n\x1a\n", content_type="image/png")
                return
            elif path == "/status":
                body = {"model": {"ready": True}, "worker": {"ready": True}}
            elif path == "/tasks":
                body = {"tasks": []}
            elif path == "/desktop/action":
                action = request.post_data_json
                actions.append(action)
                action_event.set()
                body = {"outcome": "applied", "control_epoch": 13, "control_owner": "human",
                        "worker_instance_id": "fixture", "session_id": "fixture", "page_id": "main"}
            else:
                await route.fulfill(status=404, json={})
                return
            await route.fulfill(json=body)

        await page.route("**/api/v1/**", api)
        await page.goto(f"http://127.0.0.1:{server.server_port}/desktop.html")
        checkbox = page.locator("#allow-cross-origin-navigation")
        await page.locator(".manual-controls > summary").click()
        await checkbox.wait_for(state="visible")
        assert await checkbox.is_checked()
        await checkbox.uncheck()
        assert actions == []
        await page.reload()
        await page.locator(".manual-controls > summary").click()
        assert not await checkbox.is_checked()
        await page.locator("#navigate-url").fill("https://other.example/path")
        await page.locator("#navigate-form button[type=submit]").click()
        await asyncio.wait_for(action_event.wait(), timeout=2)
        assert actions[-1]["allow_cross_origin_navigation"] is False
        assert actions[-1]["action"] == {"kind": "navigate", "url": "https://other.example/path"}

        action_event.clear()
        await checkbox.check()
        assert len(actions) == 1
        await page.locator("#navigate-url").fill("https://third.example/")
        await page.locator("#navigate-form button[type=submit]").click()
        await asyncio.wait_for(action_event.wait(), timeout=2)
        assert actions[-1]["allow_cross_origin_navigation"] is True
        assert len(actions) == 2
        assert "/tasks" not in post_paths
    finally:
        if browser:
            await browser.close()
        await playwright.stop()
        server.shutdown()
        server.server_close()
        thread.join()
