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
        self.launch_args = None
        self.launch_kwargs = None
        self.stop_calls = 0

    async def start(self):
        return self

    @property
    def chromium(self):
        return self

    async def launch_persistent_context(self, *args, **kwargs):
        self.launches += 1
        self.launch_args = args
        self.launch_kwargs = kwargs
        await asyncio.sleep(0.01)
        return self.context

    async def stop(self):
        self.stop_calls += 1


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
    assert health["detail"] == "canonical browser page closed; recovery required"
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


def _install_fake_playwright(monkeypatch, contexts):
    managers = []

    def make_manager():
        manager = FakeManager(contexts[len(managers)])
        managers.append(manager)
        return manager

    playwright_module = ModuleType("playwright.async_api")
    playwright_module.async_playwright = make_manager
    package_module = ModuleType("playwright")
    package_module.async_api = playwright_module
    monkeypatch.setitem(sys.modules, "playwright", package_module)
    monkeypatch.setitem(sys.modules, "playwright.async_api", playwright_module)
    monkeypatch.setattr(worker_app, "_adapter_class", lambda: lambda page: FakeAdapter(page))
    return managers


def _expire_recovery_backoff(runtime):
    runtime._recovery_retry_after = 0.0


@pytest.mark.asyncio
async def test_crashed_canonical_page_recovers_same_profile_and_keeps_ledger(tmp_path, monkeypatch):
    first_page, second_page = FakePage(), FakePage()
    first_context, second_context = FakeContext([first_page]), FakeContext([second_page])
    managers = _install_fake_playwright(monkeypatch, [first_context, second_context])
    runtime = BrowserRuntime(tmp_path, backend="chromium", test_mode=True,
                             egress_ready=True, route_generation=1)
    await runtime.start()
    runtime.gate("agent")
    epoch = runtime.control_epoch
    runtime._db.execute(
        "INSERT INTO capability_uses(grant_id,action_id,state,created_at) VALUES('grant','action','started','now')")

    first_page.emit("crash")
    assert runtime.control_owner == "none"
    assert runtime.control_epoch > epoch
    assert (await runtime.health())["recovery_needed"] is True
    _expire_recovery_backoff(runtime)

    recovered = await runtime.recover()
    assert recovered["outcome"] == "recovered"
    assert recovered["recovery_generation"] == 1
    assert recovered["context_count"] == recovered["page_count"] == 1
    assert runtime.control_owner == "none"
    assert runtime.control_epoch > epoch
    assert managers[0].launch_args[0] == managers[1].launch_args[0] == str(tmp_path / "profiles" / "default")
    assert runtime._db.execute("SELECT COUNT(*) FROM capability_uses").fetchone()[0] == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_recovery_defers_while_action_is_inflight_then_restores(tmp_path, monkeypatch):
    first_page, second_page = FakePage(), FakePage()
    managers = _install_fake_playwright(monkeypatch, [FakeContext([first_page]), FakeContext([second_page])])
    runtime = BrowserRuntime(tmp_path, backend="chromium", test_mode=True,
                             egress_ready=True, route_generation=1)
    await runtime.start()
    first_page.closed = True
    first_page.emit("close")
    runtime._action_in_progress = True
    deferred = await runtime.recover()
    assert deferred["outcome"] == "deferred"
    assert deferred["recovery_reason"] == "operation_in_progress"
    assert len(managers) == 1
    runtime._action_in_progress = False
    _expire_recovery_backoff(runtime)

    recovered = await runtime.recover()
    assert recovered["outcome"] == "recovered"
    assert len(managers) == 2
    await runtime.close()


@pytest.mark.asyncio
async def test_recovery_fails_closed_for_resource_violation_or_unverified_egress(tmp_path, monkeypatch):
    page = FakePage()
    managers = _install_fake_playwright(monkeypatch, [FakeContext([page])])
    runtime = BrowserRuntime(tmp_path, backend="chromium", test_mode=True,
                             egress_ready=True, route_generation=1)
    await runtime.start()
    runtime._trip_resource_violation("test resource violation")
    blocked = await runtime.recover()
    assert blocked["outcome"] == "blocked"
    assert blocked["recovery_reason"] == "resource_violation"
    assert len(managers) == 1
    await runtime.close()

    page2 = FakePage()
    managers2 = _install_fake_playwright(monkeypatch, [FakeContext([page2])])
    runtime2 = BrowserRuntime(tmp_path / "egress", backend="chromium", test_mode=True,
                              egress_ready=False, route_generation=0)
    await runtime2.start()
    page2.emit("close")
    blocked_egress = await runtime2.recover()
    assert blocked_egress["outcome"] == "blocked"
    assert blocked_egress["recovery_reason"] == "egress_unavailable"
    assert len(managers2) == 1
    await runtime2.close()


@pytest.mark.asyncio
async def test_unconfirmed_old_context_teardown_blocks_second_profile_launch(tmp_path, monkeypatch):
    class UnclosableContext(FakeContext):
        async def close(self):
            self.close_calls += 1
            raise RuntimeError("close failed")

    first_page = FakePage()
    first_context = UnclosableContext([first_page])
    managers = _install_fake_playwright(monkeypatch, [first_context, FakeContext([FakePage()])])
    runtime = BrowserRuntime(tmp_path, backend="chromium", test_mode=True,
                             egress_ready=True, route_generation=1)
    await runtime.start()
    first_page.emit("close")
    _expire_recovery_backoff(runtime)

    failed = await runtime.recover()
    assert failed["outcome"] == "failed"
    assert failed["recovery_state"] == "blocked"
    assert failed["recovery_reason"] == "context_close_failed"
    assert len(managers) == 1
    assert first_context.close_calls == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_watchdog_recovers_closed_page_without_health_request(tmp_path, monkeypatch):
    first_page, second_page = FakePage(), FakePage()
    managers = _install_fake_playwright(monkeypatch, [FakeContext([first_page]), FakeContext([second_page])])
    runtime = BrowserRuntime(tmp_path, backend="chromium", test_mode=True,
                             egress_ready=True, route_generation=1)
    await runtime.start()
    monkeypatch.setattr(worker_app, "RECOVERY_BACKOFF_SECONDS", (0.0, 0.0, 0.0))
    monkeypatch.setattr(worker_app, "RECOVERY_WATCHDOG_INTERVAL_SECONDS", 0.01)
    watchdog = asyncio.create_task(runtime.recovery_watchdog())
    try:
        first_page.emit("close")
        for _ in range(100):
            if runtime._recovery_generation == 1:
                break
            await asyncio.sleep(0.01)
        assert runtime._recovery_generation == 1
        assert len(managers) == 2
        assert (await runtime.health())["ready"] is True
    finally:
        watchdog.cancel()
        await asyncio.gather(watchdog, return_exceptions=True)
        await runtime.close()


@pytest.mark.asyncio
async def test_watchdog_probes_disconnected_browser_when_close_event_is_missing(tmp_path, monkeypatch):
    class DisconnectedPage(FakePage):
        def __init__(self):
            super().__init__()
            self.disconnected = False

        async def evaluate(self, _script):
            return 1

        def is_closed(self):
            return self.disconnected

    first_page, second_page = DisconnectedPage(), FakePage()
    managers = _install_fake_playwright(monkeypatch, [FakeContext([first_page]), FakeContext([second_page])])
    runtime = BrowserRuntime(tmp_path, backend="chromium", test_mode=True,
                             egress_ready=True, route_generation=1)
    await runtime.start()
    monkeypatch.setattr(worker_app, "RECOVERY_BACKOFF_SECONDS", (0.0, 0.0, 0.0))
    monkeypatch.setattr(worker_app, "RECOVERY_WATCHDOG_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(worker_app, "LIVENESS_PROBE_TIMEOUT_SECONDS", 0.05)
    watchdog = asyncio.create_task(runtime.recovery_watchdog())
    try:
        first_page.disconnected = True
        for _ in range(100):
            if runtime._recovery_generation == 1:
                break
            await asyncio.sleep(0.01)
        assert runtime._recovery_generation == 1
        assert len(managers) == 2
        assert runtime._recovery_reason == "healthy"
        assert (await runtime.health())["ready"] is True
    finally:
        watchdog.cancel()
        await asyncio.gather(watchdog, return_exceptions=True)
        await runtime.close()


@pytest.mark.asyncio
async def test_watchdog_ignores_probe_timeout_and_transient_error(tmp_path, monkeypatch):
    class TransientPage(FakePage):
        async def evaluate(self, _script):
            if self.probe_mode == "timeout":
                await asyncio.sleep(0.05)
            if self.probe_mode:
                raise RuntimeError("transient probe failure")
            return 1

        def __init__(self):
            super().__init__()
            self.probe_mode = None

    first_page = TransientPage()
    managers = _install_fake_playwright(monkeypatch, [FakeContext([first_page])])
    runtime = BrowserRuntime(tmp_path, backend="chromium", test_mode=True,
                             egress_ready=True, route_generation=1)
    await runtime.start()
    runtime.gate("agent")
    epoch = runtime.control_epoch
    monkeypatch.setattr(worker_app, "RECOVERY_WATCHDOG_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(worker_app, "LIVENESS_PROBE_TIMEOUT_SECONDS", 0.005)
    watchdog = asyncio.create_task(runtime.recovery_watchdog())
    try:
        for mode in ("transient", "timeout"):
            first_page.probe_mode = mode
            await asyncio.sleep(0.08)
            assert runtime.control_epoch == epoch
            assert runtime.control_owner == "agent"
            assert len(managers) == 1
            assert (await runtime.health())["ready"] is True
    finally:
        watchdog.cancel()
        await asyncio.gather(watchdog, return_exceptions=True)
        await runtime.close()


@pytest.mark.asyncio
async def test_recovery_cancellation_waits_for_teardown_then_propagates(tmp_path, monkeypatch):
    close_started, close_release = asyncio.Event(), asyncio.Event()

    class SlowCloseContext(FakeContext):
        async def close(self):
            self.close_calls += 1
            close_started.set()
            await close_release.wait()
            for page in tuple(self.pages):
                if not page.closed:
                    page.closed = True
                    page.emit("close")

    first_page = FakePage()
    managers = _install_fake_playwright(
        monkeypatch, [SlowCloseContext([first_page]), FakeContext([FakePage()])])
    runtime = BrowserRuntime(tmp_path, backend="chromium", test_mode=True,
                             egress_ready=True, route_generation=1)
    await runtime.start()
    first_page.closed = True
    first_page.emit("close")
    _expire_recovery_backoff(runtime)
    recovery = asyncio.create_task(runtime.recover())
    await close_started.wait()
    recovery.cancel()
    close_release.set()
    with pytest.raises(asyncio.CancelledError):
        await recovery
    assert first_page.closed
    assert managers[0].stop_calls == 1
    assert len(managers) == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_recovery_cancellation_during_launch_stops_manager_before_return(tmp_path, monkeypatch):
    launch_started = asyncio.Event()

    class SlowLaunchManager(FakeManager):
        async def launch_persistent_context(self, *args, **kwargs):
            self.launch_args, self.launch_kwargs = args, kwargs
            self.launches += 1
            launch_started.set()
            await asyncio.Event().wait()

    first_context = FakeContext([FakePage()])
    second_context = FakeContext([FakePage()])
    managers = []

    def make_manager():
        manager = FakeManager(first_context) if not managers else SlowLaunchManager(second_context)
        managers.append(manager)
        return manager

    playwright_module = ModuleType("playwright.async_api")
    playwright_module.async_playwright = make_manager
    package_module = ModuleType("playwright")
    package_module.async_api = playwright_module
    monkeypatch.setitem(sys.modules, "playwright", package_module)
    monkeypatch.setitem(sys.modules, "playwright.async_api", playwright_module)
    monkeypatch.setattr(worker_app, "_adapter_class", lambda: lambda page: FakeAdapter(page))
    runtime = BrowserRuntime(tmp_path, backend="chromium", test_mode=True,
                             egress_ready=True, route_generation=1)
    await runtime.start()
    first_context.pages[0].emit("close")
    _expire_recovery_backoff(runtime)
    recovery = asyncio.create_task(runtime.recover())
    await launch_started.wait()
    recovery.cancel()
    with pytest.raises(asyncio.CancelledError):
        await recovery
    assert managers[1].stop_calls == 1
    assert runtime._context is None
    await runtime.close()


@pytest.mark.asyncio
async def test_recovery_attempts_are_bounded_and_open_circuit(tmp_path, monkeypatch):
    class FailingManager(FakeManager):
        async def launch_persistent_context(self, *args, **kwargs):
            self.launch_args, self.launch_kwargs = args, kwargs
            self.launches += 1
            raise RuntimeError("synthetic launch failure")

    first_page = FakePage()
    first_context = FakeContext([first_page])
    managers = []

    def make_manager():
        manager = FakeManager(first_context) if not managers else FailingManager(FakeContext())
        managers.append(manager)
        return manager

    playwright_module = ModuleType("playwright.async_api")
    playwright_module.async_playwright = make_manager
    package_module = ModuleType("playwright")
    package_module.async_api = playwright_module
    monkeypatch.setitem(sys.modules, "playwright", package_module)
    monkeypatch.setitem(sys.modules, "playwright.async_api", playwright_module)
    monkeypatch.setattr(worker_app, "_adapter_class", lambda: lambda page: FakeAdapter(page))
    monkeypatch.setattr(worker_app, "RECOVERY_BACKOFF_SECONDS", (0.0, 0.0, 0.0))
    runtime = BrowserRuntime(tmp_path, backend="chromium", test_mode=True,
                             egress_ready=True, route_generation=1)
    await runtime.start()
    first_page.closed = True
    first_page.emit("close")
    _expire_recovery_backoff(runtime)

    for _ in range(worker_app.RECOVERY_MAX_ATTEMPTS):
        result = await runtime.recover()
        assert result["outcome"] == "failed"
        runtime._recovery_retry_after = 0.0
    assert result["recovery_state"] == "exhausted"
    assert result["recovery_attempts"] == worker_app.RECOVERY_MAX_ATTEMPTS
    attempt_count = len(managers)
    blocked = await runtime.recover()
    assert blocked["recovery_reason"] == "attempts_exhausted"
    assert blocked["recovery_state"] == "exhausted"
    assert len(managers) == attempt_count
    await runtime.close()


@pytest.mark.asyncio
async def test_camoufox_target_closed_still_stops_base_playwright_manager(tmp_path, monkeypatch):
    from playwright._impl._errors import TargetClosedError

    class FakeConnection:
        def __init__(self):
            self.stop_calls = 0

        async def stop_async(self):
            self.stop_calls += 1

    class FakePlaywrightContextManager:
        async def __aexit__(self, *_args):
            await self._connection.stop_async()

    class ClosedCamoufoxManager:
        def __init__(self):
            self._exit_was_called = False
            self._connection = FakeConnection()

        async def __aexit__(self, *_args):
            raise TargetClosedError("synthetic already-closed target")

    page = FakePage()
    context = FakeContext([page])
    runtime = BrowserRuntime(tmp_path, adapter=FakeAdapter(page), page=page, context=context,
                             egress_ready=True, proxy_assignment_id="direct-development", route_generation=1)
    manager = ClosedCamoufoxManager()
    runtime._camoufox_cm = manager
    runtime._main_page_closed = True
    runtime._recovery_needed = True
    camoufox_module = ModuleType("camoufox.async_api")
    camoufox_module.PlaywrightContextManager = FakePlaywrightContextManager
    package_module = ModuleType("camoufox")
    package_module.async_api = camoufox_module
    monkeypatch.setitem(sys.modules, "camoufox", package_module)
    monkeypatch.setitem(sys.modules, "camoufox.async_api", camoufox_module)

    closed = await runtime._close_browser_for_recovery()
    assert closed is True, (runtime._recovery_teardown_reason, runtime._recovery_teardown_error_type,
                            manager._connection.stop_calls)
    assert manager._connection.stop_calls == 1
    assert runtime._camoufox_cm is None
    assert runtime._context is None and runtime._page is None
    await runtime.close()
