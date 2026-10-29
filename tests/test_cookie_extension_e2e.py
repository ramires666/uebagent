"""Isolated Chromium integration; never uses an existing browser profile or service."""
import asyncio
from contextlib import contextmanager
import json
import queue
import threading
import os
from pathlib import Path
import shutil

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]



@contextmanager
def local_test_api(*, task_export_origins=None):
    aiohttp = pytest.importorskip("aiohttp.web")
    loop = asyncio.new_event_loop()
    ready = queue.Queue()
    evidence = queue.Queue()

    async def pair(request):
        body = await request.json()
        if body.get("code") != "synthetic-code":
            return aiohttp.Response(status=403)
        return aiohttp.json_response({"source_id": "synthetic-source", "token": "synthetic-token-123456789"})

    async def channel(request):
        ws = aiohttp.WebSocketResponse()
        await ws.prepare(request)
        auth = await ws.receive_json()
        if auth.get("source_id") != "synthetic-source" or auth.get("token") != "synthetic-token-123456789":
            evidence.put(False)
            return ws
        if task_export_origins is not None:
            await ws.send_json({"type": "job", "request_id": "task-cookie-export", "kind": "task_cookie_export",
                                "payload": {"origins": task_export_origins}})
            result = await ws.receive_json()
            data = result.get("data", {})
            cookies = data.get("cookies", [])
            evidence.put(result.get("ok") is True and result.get("request_id") == "task-cookie-export"
                         and data.get("allowed_origins") == task_export_origins
                         and {row.get("name") for row in cookies} == {"host", "parent"}
                         and all(row.get("site_ids") == [] for row in cookies)
                         and any(row.get("name") == "parent" and row.get("value") == "synthetic-parent-task" for row in cookies)
                         and not any(row.get("name") == "sibling" for row in cookies))
            await ws.close()
            return ws
        await ws.send_json({"type": "job", "request_id": "synthetic-job", "kind": "open_sites", "payload": {}})
        result = await ws.receive_json()
        sites = [s for s in result.get("data", {}).get("sites", []) if s.get("origin") == "https://app.example.test"]
        if not (result.get("ok") and result.get("request_id") == "synthetic-job" and len(sites) == 1):
            evidence.put(False)
            return ws
        await ws.send_json({"type": "job", "request_id": "catalog", "kind": "cookie_catalog", "payload": {"sites": sites}})
        catalog = await ws.receive_json()
        rows = catalog.get("data", {}).get("cookies", [])
        parent = next((row for row in rows if row.get("name") == "parent"), None)
        if not (catalog.get("ok") and parent and all("value" not in row for row in rows)
                and not any(row.get("name") == "sibling" for row in rows)):
            evidence.put(False)
            return ws
        await ws.send_json({"type": "job", "request_id": "export", "kind": "cookie_export",
                            "payload": {"sites": sites, "selections": [parent]}})
        exported = await ws.receive_json()
        cookies = exported.get("data", {}).get("cookies", [])
        evidence.put(exported.get("ok") is True and exported.get("request_id") == "export"
                     and len(cookies) == 1 and cookies[0].get("name") == "parent"
                     and cookies[0].get("value") == "synthetic-parent")
        await ws.close()
        return ws

    async def start():
        app = aiohttp.Application()
        app.router.add_post("/api/v1/extension/pair", pair)
        app.router.add_get("/api/v1/extension/channel", channel)
        runner = aiohttp.AppRunner(app)
        await runner.setup()
        site = aiohttp.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        ready.put((runner, site._server.sockets[0].getsockname()[1]))

    def run():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(start())
        loop.run_forever()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    runner, port = ready.get(timeout=10)
    try:
        yield f"http://127.0.0.1:{port}", evidence
    finally:
        asyncio.run_coroutine_threadsafe(runner.cleanup(), loop).result(timeout=10)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=10)
        loop.close()


def test_unpacked_extension_with_isolated_chromium(tmp_path):
    extension = tmp_path / "extension"
    shutil.copytree(ROOT / "chrome_extension", extension)
    manifest_path = extension / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    # Test-only pregrant, emulating the operator's Chrome permission approval.
    manifest["host_permissions"].extend([
        "https://app.example.test/*", "https://example.test/*",
        "http://app.example.test/*", "http://example.test/*",
    ])
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with sync_playwright() as playwright:
        executable = os.environ.get("COOKIE_E2E_CHROMIUM") or playwright.chromium.executable_path
        if not Path(executable).is_file():
            pytest.skip("Set COOKIE_E2E_CHROMIUM to an extension-capable Chromium executable")
        context = playwright.chromium.launch_persistent_context(
            str(tmp_path / "isolated-profile"), executable_path=executable, headless=True,
            args=[f"--disable-extensions-except={extension}", f"--load-extension={extension}"],
        )
        try:
            context.route("https://app.example.test/**", lambda route: route.fulfill(status=200, body="synthetic test"))
            page = context.pages[0] if context.pages else context.new_page()
            page.goto("https://app.example.test/account", wait_until="domcontentloaded")
            context.add_cookies([
                {"name": "parent", "value": "synthetic-parent", "domain": ".example.test", "path": "/account", "secure": True, "httpOnly": True, "sameSite": "Lax"},
                {"name": "selected", "value": "synthetic-before", "domain": "app.example.test", "path": "/", "secure": True, "httpOnly": True, "sameSite": "Strict"},
                {"name": "SID", "value": "synthetic-insecure-sid", "domain": "app.example.test", "path": "/", "secure": False, "httpOnly": False, "sameSite": "Lax"},
                {"name": "unselected", "value": "synthetic-unselected", "domain": "app.example.test", "path": "/", "secure": True},
                {"name": "sibling", "value": "synthetic-sibling", "domain": "other.example.test", "path": "/", "secure": True},
            ])
            worker = context.service_workers[0] if context.service_workers else context.wait_for_event("serviceworker", timeout=15000)
            extension_page = context.new_page()
            extension_page.goto(worker.url.rsplit("/", 1)[0] + "/popup/popup.html")
            assert extension_page.locator("#extension-version").inner_text() == "Extension 0.1.3"
            result = extension_page.evaluate("""async () => {
              const m = await import(chrome.runtime.getURL('service_worker.js'));
              const {sites} = await m.jobOpenSites({}, 'http://localhost:8090');
              const selectedSites = sites.filter(s => s.origin === 'https://app.example.test');
              const cat = await m.jobCookieCatalog({sites: selectedSites}, 'http://localhost:8090');
              const row = cat.cookies.find(c => c.name === 'selected');
              const parent = cat.cookies.find(c => c.name === 'parent');
              const sid = cat.cookies.find(c => c.name === 'SID');
              if (!row) return {failure: 'selected host cookie absent', names: cat.cookies.map(c => c.name)};
              await chrome.cookies.set({url:'https://app.example.test/',name:'selected',value:'synthetic-after',secure:true,httpOnly:true,sameSite:'strict'});
              const out = await m.jobCookieExport({sites: selectedSites, selections:[row]}, 'http://localhost:8090');
              await chrome.cookies.remove({url:'https://app.example.test/',name:'selected'});
              let deleted = false;
              try { await m.jobCookieExport({sites:selectedSites,selections:[row]}, 'http://localhost:8090'); }
              catch (e) { deleted = e.code === 'COOKIE_SELECTION_STALE'; }
              return {parent: !!parent, parentPath: parent?.path, parentHttpOnly: parent?.http_only,
                sid: !!sid, sidSecure: sid?.secure, sidHttpOnly: sid?.http_only,
                noValues: cat.cookies.every(c => !('value' in c)), siblingAbsent: !cat.cookies.some(c => c.name === 'sibling'),
                selectedOnly: out.cookies.length === 1 && out.cookies[0].name === 'selected',
                rotated: out.cookies[0].value === 'synthetic-after', deleted,
                store: !!row.store_id, secure: row.secure, httpOnly: row.http_only, sameSite: row.same_site};
            }""")
            assert result == {"parent": True, "parentPath": "/account", "parentHttpOnly": True,
                "sid": True, "sidSecure": False, "sidHttpOnly": False,
                "noValues": True, "siblingAbsent": True, "selectedOnly": True, "rotated": True,
                "deleted": True, "store": True, "secure": True, "httpOnly": True, "sameSite": "strict"}
            # Synthetic route only: prove a missing permission becomes visible in the popup.
            context.route("https://gemini.google.com/**", lambda route: route.fulfill(status=200, body="synthetic test"))
            google_page = context.new_page()
            google_page.goto("https://gemini.google.com/", wait_until="domcontentloaded")
            missing = extension_page.evaluate("""async () => {
              const m = await import(chrome.runtime.getURL('service_worker.js'));
              const {sites} = await m.jobOpenSites({}, 'http://localhost:8090');
              try { await m.jobCookieCatalog({sites: sites.filter(s => s.origin === 'https://gemini.google.com')}, 'http://localhost:8090'); }
              catch (e) { return {code: e.code, origins: e.details?.origins}; }
            }""")
            assert missing == {"code": "CHROME_PERMISSION_REQUIRED", "origins": [
                "http://gemini.google.com", "http://google.com", "https://gemini.google.com", "https://google.com"]}
            extension_page.reload()
            extension_page.locator("#permission-section").wait_for(state="visible")
            assert extension_page.locator("#permission-list").inner_text().splitlines() == missing["origins"]
            google_page.close()
            with local_test_api() as (api_origin, evidence):
                paired = extension_page.evaluate("""async origin => await chrome.runtime.sendMessage({
                    type:'pair', api_origin:origin, code:'synthetic-code', label:'isolated-test'})""", api_origin)
                assert paired.get("ok") is True
                assert evidence.get(timeout=10) is True
        finally:
            context.close()


def test_task_cookie_export_uses_normal_store_when_task_site_is_closed(tmp_path):
    extension = tmp_path / "extension-task-export"
    shutil.copytree(ROOT / "chrome_extension", extension)
    manifest_path = extension / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["host_permissions"].extend([
        "https://app.example.test/*", "https://example.test/*",
        "http://app.example.test/*", "http://example.test/*",
    ])
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    origins = ["https://app.example.test"]
    with sync_playwright() as playwright:
        executable = os.environ.get("COOKIE_E2E_CHROMIUM") or playwright.chromium.executable_path
        if not Path(executable).is_file():
            pytest.skip("Set COOKIE_E2E_CHROMIUM to an extension-capable Chromium executable")
        context = playwright.chromium.launch_persistent_context(
            str(tmp_path / "isolated-task-profile"), executable_path=executable, headless=True,
            args=[f"--disable-extensions-except={extension}", f"--load-extension={extension}"],
        )
        try:
            context.route("https://app.example.test/**", lambda route: route.fulfill(status=200, body="synthetic task site"))
            context.route("https://unrelated.example.net/**", lambda route: route.fulfill(status=200, body="synthetic normal tab"))
            task_page = context.new_page()
            awaitable = task_page.goto("https://app.example.test/account", wait_until="domcontentloaded")
            assert awaitable is not None
            context.add_cookies([
                {"name": "host", "value": "synthetic-host-task", "domain": "app.example.test", "path": "/", "secure": True, "httpOnly": True, "sameSite": "Lax"},
                {"name": "parent", "value": "synthetic-parent-task", "domain": ".example.test", "path": "/", "secure": True, "httpOnly": True, "sameSite": "Lax"},
                {"name": "sibling", "value": "must-not-export", "domain": "other.example.test", "path": "/", "secure": True},
            ])
            task_page.close()
            assert all("app.example.test" not in (tab.url or "") for tab in context.pages)
            normal_anchor = context.new_page()
            normal_anchor.goto("https://unrelated.example.net/", wait_until="domcontentloaded")

            worker = context.service_workers[0] if context.service_workers else context.wait_for_event("serviceworker", timeout=15000)
            extension_page = context.new_page()
            extension_page.goto(worker.url.rsplit("/", 1)[0] + "/popup/popup.html")
            with local_test_api(task_export_origins=origins) as (api_origin, evidence):
                paired = extension_page.evaluate("""async origin => await chrome.runtime.sendMessage({
                    type:'pair', api_origin:origin, code:'synthetic-code', label:'closed-task-test'})""", api_origin)
                assert paired.get("ok") is True
                assert evidence.get(timeout=10) is True
        finally:
            context.close()


def test_insecure_cookie_requires_exact_http_permission_even_for_https_site(tmp_path):
    extension = tmp_path / "extension-http-permission"
    shutil.copytree(ROOT / "chrome_extension", extension)
    manifest_path = extension / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert "http://*/*" in manifest["optional_host_permissions"]
    assert "https://*/*" in manifest["optional_host_permissions"]
    # Emulate a legacy install that granted HTTPS only. The extension must fail
    # closed and expose only the exact HTTP candidates that are still missing.
    manifest["host_permissions"].extend(["https://app.example.test/*", "https://example.test/*"])
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with sync_playwright() as playwright:
        executable = os.environ.get("COOKIE_E2E_CHROMIUM") or playwright.chromium.executable_path
        if not Path(executable).is_file():
            pytest.skip("Set COOKIE_E2E_CHROMIUM to an extension-capable Chromium executable")
        context = playwright.chromium.launch_persistent_context(
            str(tmp_path / "isolated-http-permission-profile"), executable_path=executable,
            headless=True, args=[f"--disable-extensions-except={extension}", f"--load-extension={extension}"],
        )
        try:
            worker = context.service_workers[0] if context.service_workers else context.wait_for_event("serviceworker", timeout=15000)
            popup = context.new_page()
            popup.goto(worker.url.rsplit("/", 1)[0] + "/popup/popup.html")
            missing = popup.evaluate("""async () => {
              const m = await import(chrome.runtime.getURL('service_worker.js'));
              try { await m.requirePermissions(['https://app.example.test']); return null; }
              catch (e) { return {code: e.code, origins: e.details?.origins}; }
            }""")
            assert missing == {"code": "CHROME_PERMISSION_REQUIRED", "origins": [
                "http://app.example.test", "http://example.test"]}
            popup.reload()
            popup.locator("#permission-section").wait_for(state="visible")
            assert popup.locator("#permission-list").inner_text().splitlines() == missing["origins"]
        finally:
            context.close()
