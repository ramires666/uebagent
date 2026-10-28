from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import ModuleType

import pytest

import worker.app as worker_app
from worker.app import BrowserRuntime, WorkerError, _adapter_class


CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")


class EventSource:
    def __init__(self):
        self.listeners = {}

    def on(self, event, callback):
        self.listeners.setdefault(event, []).append(callback)

    def off(self, event, callback):
        self.listeners[event].remove(callback)

    def emit(self, event, *args):
        for callback in tuple(self.listeners.get(event, [])):
            callback(*args)


class FakePage(EventSource):
    def __init__(self):
        super().__init__()
        self.closed = False
        self.close_release = None
        self.mouse = SimpleMouse()

    def set_default_timeout(self, _timeout):
        pass

    async def close(self):
        if self.close_release is not None:
            await self.close_release.wait()
        self.closed = True
        self.emit("close")

    async def evaluate(self, _script):
        return {"width": 1440, "height": 900}


class FakeContext(EventSource):
    def __init__(self, pages=None):
        super().__init__()
        self.pages = list(pages or [])
        for page in self.pages:
            page.on("close", lambda *_, page=page: self._discard(page))
        self.route_calls = 0
        self.close_calls = 0

    def _discard(self, page):
        if page in self.pages:
            self.pages.remove(page)

    async def new_page(self):
        page = FakePage()
        self.pages.append(page)
        page.on("close", lambda _closed=None, page=page: self._discard(page))
        self.emit("page", page)
        return page

    async def route(self, *_args):
        self.route_calls += 1

    async def close(self):
        self.close_calls += 1
        for page in tuple(self.pages):
            if not page.closed:
                page.closed = True
                page.emit("close")


class FakeAdapter:
    def __init__(self, page=None, *, capture_started=None, capture_release=None):
        self.page = page
        self.capture_started = capture_started
        self.capture_release = capture_release
        self.calls = 0
        self.goto_calls = 0

    async def screenshot(self, **_kwargs):
        self.calls += 1
        if self.capture_started:
            self.capture_started.set()
            await self.capture_release.wait()
        return b"png"

    async def url(self):
        return "about:blank"

    async def goto(self, url, **_kwargs):
        self.goto_calls += 1
        return {"url": url, "status": 200}


class SimpleMouse:
    def __init__(self):
        self.clicks = []

    async def click(self, *args, **kwargs):
        self.clicks.append((args, kwargs))


class FakeManager:
    def __init__(self, context):
        self.context = context
        self.launches = 0

    async def start(self):
        return self

    @property
    def chromium(self):
        return self

    async def launch_persistent_context(self, *_args, **_kwargs):
        self.launches += 1
        await asyncio.sleep(0.01)
        return self.context

    async def stop(self):
        pass


@pytest.mark.asyncio
async def test_concurrent_start_uses_one_context_and_one_page(tmp_path, monkeypatch):
    context = FakeContext([FakePage()])
    manager = FakeManager(context)
    playwright_module = ModuleType("playwright.async_api")
    playwright_module.async_playwright = lambda: manager
    package_module = ModuleType("playwright")
    package_module.async_api = playwright_module
    monkeypatch.setitem(sys.modules, "playwright", package_module)
    monkeypatch.setitem(sys.modules, "playwright.async_api", playwright_module)
    monkeypatch.setattr(worker_app, "_adapter_class", lambda: lambda page: FakeAdapter(page))
    runtime = BrowserRuntime(tmp_path, backend="chromium", test_mode=True)

    await asyncio.gather(runtime.start(), runtime.start(), runtime.start())
    assert manager.launches == 1
    assert runtime.status_base()["context_count"] == 1
    assert runtime.status_base()["page_count"] == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_popup_is_closed_and_main_page_close_invalidates_control(tmp_path):
    page = FakePage()
    context = FakeContext([page])
    runtime = BrowserRuntime(tmp_path, adapter=FakeAdapter(page), page=page, context=context)
    runtime.gate("human")
    popup = FakePage()
    context.pages.append(popup)
    popup.on("close", lambda _closed=None: context._discard(popup))
    context.emit("page", popup)
    await asyncio.sleep(0)
    assert popup.closed
    resources = runtime.status_base()
    assert resources["page_count"] == 1
    assert resources["page_limit"] == 1
    assert resources["closed_extra_pages"] == 1

    page.emit("close")
    health = await runtime.health()
    assert health["ready"] is False
    assert health["detail"] == "canonical browser page closed; restart required"
    assert runtime.control_owner == "none"
    assert runtime._last_observation_id is None
    with pytest.raises(WorkerError) as unavailable:
        await runtime.observe()
    assert unavailable.value.code == "BROWSER_UNAVAILABLE"
    await runtime.close()


@pytest.mark.asyncio
async def test_overlapping_capture_rejected_without_queue_or_retained_image(tmp_path):
    page = FakePage()
    started, release = asyncio.Event(), asyncio.Event()
    adapter = FakeAdapter(page, capture_started=started, capture_release=release)
    runtime = BrowserRuntime(tmp_path, adapter=adapter, page=page, context=FakeContext([page]))
    first = asyncio.create_task(runtime.screenshot())
    await started.wait()
    with pytest.raises(WorkerError) as busy:
        await runtime.screenshot()
    assert busy.value.code == "CAPTURE_BUSY"
    release.set()
    assert await first == b"png"
    assert adapter.calls == 1
    assert not hasattr(runtime, "_screenshot_cache")
    await runtime.close()


@pytest.mark.asyncio
async def test_recognized_target_blank_link_uses_canonical_page(tmp_path, monkeypatch):
    page = FakePage()
    adapter = FakeAdapter(page)
    runtime = BrowserRuntime(tmp_path, adapter=adapter, page=page, context=FakeContext([page]),
                             egress_ready=True, route_generation=1)
    monkeypatch.setattr(runtime, "_classify_click", lambda *_args: _async_value({
        "tag": "a", "role": "link", "type": "", "href": "https://allowed.example/path",
        "target": "_blank",
    }))
    epoch = runtime.gate("human")["control_epoch"]
    result = await runtime._execute_action(
        {"kind": "click", "x": 1, "y": 1, "button": "left", "count": 1},
        frozenset({"https://allowed.example"}), manual=True, allow_write=False, expected_epoch=epoch,
    )
    assert result == {"url": "https://allowed.example/path", "status": 200, "opened_in_current_page": True}
    assert runtime.status_base()["page_count"] == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_target_blank_right_click_keeps_context_menu_behavior(tmp_path, monkeypatch):
    page = FakePage()
    adapter = FakeAdapter(page)
    runtime = BrowserRuntime(tmp_path, adapter=adapter, page=page, context=FakeContext([page]),
                             egress_ready=True, route_generation=1)
    monkeypatch.setattr(runtime, "_classify_click", lambda *_args: _async_value({
        "tag": "a", "role": "link", "href": "https://allowed.example/path", "target": "_blank",
    }))
    epoch = runtime.gate("human")["control_epoch"]
    result = await runtime._execute_action(
        {"kind": "click", "x": 1, "y": 1, "button": "right", "count": 1},
        frozenset({"https://allowed.example"}), manual=True, allow_write=False, expected_epoch=epoch,
    )
    assert adapter.goto_calls == 0
    assert result["button"] == "right"
    assert len(page.mouse.clicks) == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_observation_has_priority_over_one_viewer_capture(tmp_path):
    page = FakePage()
    started, release = asyncio.Event(), asyncio.Event()
    adapter = FakeAdapter(page, capture_started=started, capture_release=release)
    runtime = BrowserRuntime(tmp_path, adapter=adapter, page=page, context=FakeContext([page]))
    viewer = asyncio.create_task(runtime.screenshot())
    await started.wait()
    observation = asyncio.create_task(runtime.observe())
    await asyncio.sleep(0)
    with pytest.raises(WorkerError) as second_observe:
        await runtime.observe()
    assert second_observe.value.code == "CAPTURE_BUSY"
    with pytest.raises(WorkerError) as next_viewer:
        await runtime.screenshot()
    assert next_viewer.value.code == "CAPTURE_BUSY"
    release.set()
    assert await viewer == b"png"
    assert (await observation)["observation_id"]
    await runtime.close()


@pytest.mark.asyncio
async def test_popup_storm_caps_pending_tasks_and_quarantines_context(tmp_path):
    page = FakePage()
    context = FakeContext([page])
    runtime = BrowserRuntime(tmp_path, adapter=FakeAdapter(page), page=page, context=context)
    release = asyncio.Event()
    popups = []
    for _ in range(worker_app.MAX_PENDING_PAGE_CLOSES + 4):
        popup = FakePage()
        popup.close_release = release
        popups.append(popup)
        context.pages.append(popup)
        context.emit("page", popup)
    assert len(runtime._extra_page_close_tasks) <= worker_app.MAX_PENDING_PAGE_CLOSES
    health = await runtime.health()
    assert not health["ready"]
    assert health["resource_violation"] is True
    assert runtime.control_owner == "none"
    assert runtime._last_observation_id is None

    await asyncio.wait_for(runtime._quarantine_task, timeout=1)
    assert runtime._context is None
    assert runtime._extra_page_close_tasks == {}
    assert runtime._closed_extra_pages == 0
    assert context.close_calls == 1
    await runtime.close()
    assert runtime._quarantine_task is None


@pytest.mark.asyncio
async def test_stalled_popup_close_times_out_and_quarantines(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_app, "PAGE_CLOSE_TIMEOUT_SECONDS", 0.01)
    page = FakePage()
    context = FakeContext([page])
    runtime = BrowserRuntime(tmp_path, adapter=FakeAdapter(page), page=page, context=context)
    popup = FakePage()
    popup.close_release = asyncio.Event()
    context.pages.append(popup)
    context.emit("page", popup)

    await asyncio.sleep(0.05)
    assert runtime._resource_violation is True
    assert runtime._closed_extra_pages == 0
    assert runtime._quarantine_task is not None
    await asyncio.wait_for(runtime._quarantine_task, timeout=1)
    assert context.close_calls == 1
    assert runtime._context is None
    assert runtime._extra_page_close_tasks == {}
    await runtime.close()


async def _async_value(value):
    return value


@pytest.mark.asyncio
async def test_headless_chromium_resolves_nested_link_and_label_hits(tmp_path):
    playwright_async = pytest.importorskip("playwright.async_api", reason="Playwright is not installed")
    if not CHROME.is_file():
        pytest.skip("the local Chrome executable is unavailable")
    playwright = await playwright_async.async_playwright().start()
    browser = None
    runtime = None
    try:
        try:
            browser = await playwright.chromium.launch(executable_path=str(CHROME), headless=True)
        except Exception as exc:
            pytest.skip(f"local Chrome could not launch in headless mode: {type(exc).__name__}")
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await context.new_page()
        await page.set_content("""<!doctype html><html><body>
          <a id="link" href="https://allowed.example/path" target="_blank"><span id="link-hit"><svg width="24" height="24"><circle cx="12" cy="12" r="10"></circle></svg></span></a>
          <label id="label" for="field"><span id="label-hit">Search</span></label><input id="field" type="text">
          <a id="outer" href="https://wrong.example"><button id="button"><span id="button-hit">Submit</span></button></a>
        </body></html>""")
        runtime = BrowserRuntime(tmp_path, backend="chromium", headless=True, adapter=_adapter_class()(page),
                                 page=page, context=context, test_mode=True)
        async def hit(selector):
            box = await page.locator(selector).bounding_box()
            assert box is not None
            return await runtime._classify_click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)

        link = await hit("#link-hit")
        assert link["tag"] == "a"
        assert link["href"] == "https://allowed.example/path"
        assert link["target"] == "_blank"
        label = await hit("#label-hit")
        assert label["tag"] == "input"
        assert label["type"] == "text"
        button = await hit("#button-hit")
        assert button["tag"] == "button"
        assert button["href"] == ""
    finally:
        if runtime is not None:
            await runtime.close()
        elif browser is not None:
            await browser.close()
        await playwright.stop()
