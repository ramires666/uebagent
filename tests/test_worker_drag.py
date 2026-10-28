from pathlib import Path
import asyncio

import pytest

from worker.app import AgentExecuteBody, BrowserRuntime, WorkerError, _adapter_class

CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")


class FakeMouse:
    def __init__(self):
        self.calls = []
        self.on_down = None

    async def move(self, x, y):
        self.calls.append(("move", x, y))

    async def down(self, **kwargs):
        self.calls.append(("down",))
        if self.on_down:
            self.on_down()

    async def up(self, **kwargs):
        self.calls.append(("up",))


class FakePage:
    def __init__(self):
        self.mouse = FakeMouse()
        self.safe = True

    async def evaluate(self, script, arg):
        return self.safe


class FakeAdapter:
    async def url(self):
        return "about:blank"


@pytest.mark.asyncio
async def test_drag_requires_known_and_safe_start_and_fences_takeover(tmp_path):
    page = FakePage()
    runtime = BrowserRuntime(tmp_path, adapter=FakeAdapter(), page=page, test_mode=True,
                             egress_ready=True, route_generation=1)
    try:
        epoch = runtime.gate("agent")["control_epoch"]
        with pytest.raises(WorkerError) as error:
            await runtime._execute_action({"kind": "drag", "x": 40, "y": 40}, frozenset(), manual=False, allow_write=False, expected_epoch=epoch)
        assert error.value.code == "DRAG_START_UNKNOWN"
        await runtime._execute_action({"kind": "mouse_move", "x": 10, "y": 10}, frozenset(), manual=False, allow_write=False, expected_epoch=epoch)
        page.safe = False
        with pytest.raises(WorkerError) as error:
            await runtime._execute_action({"kind": "drag", "x": 40, "y": 40}, frozenset(), manual=False, allow_write=True, expected_epoch=epoch)
        assert error.value.code == "APPROVAL_REQUIRED"
        assert not any(call[0] == "down" for call in page.mouse.calls)
        page.safe = True
        runtime._last_observation_id = "obs"
        page.mouse.on_down = lambda: runtime.gate("human")
        body = AgentExecuteBody(action_id="takeover-drag", control_epoch=epoch, observation_id="obs",
                                action={"kind": "drag", "x": 40, "y": 40})
        with pytest.raises(WorkerError) as error:
            await runtime.execute_agent(body)
        assert error.value.code == "STALE_CONTROL_EPOCH"
        assert page.mouse.calls[-2:] == [("down",), ("up",)]
        replay = await runtime.execute_agent(body)
        assert replay["outcome"] == "unknown"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_headless_textarea_resize_preserves_value_and_rejects_other_targets(tmp_path):
    playwright_async = pytest.importorskip("playwright.async_api")
    if not CHROME.is_file():
        pytest.skip("local Chrome unavailable")
    playwright = await playwright_async.async_playwright().start()
    browser = runtime = None
    try:
        browser = await playwright.chromium.launch(executable_path=str(CHROME), headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 900})
        page = await context.new_page()
        await page.set_content('<textarea id="t" style="width:200px;height:100px;resize:both">one\ntwo\nthree\nfour</textarea><button id="b">Submit</button><input id="file" type="file">')
        runtime = BrowserRuntime(tmp_path, adapter=_adapter_class()(page), page=page, context=context,
                                 backend="chromium", test_mode=True, egress_ready=True, route_generation=1)
        epoch = runtime.gate("agent")["control_epoch"]
        box = await page.locator("#t").bounding_box()
        value = await page.locator("#t").input_value()
        x, y = box["x"] + box["width"] - 3, box["y"] + box["height"] - 3
        await runtime._execute_action({"kind": "mouse_move", "x": x, "y": y}, frozenset(), manual=False, allow_write=False, expected_epoch=epoch)
        await runtime._execute_action({"kind": "drag", "x": x + 80, "y": y + 60}, frozenset(), manual=False, allow_write=False, expected_epoch=epoch)
        resized = await page.locator("#t").bounding_box()
        assert resized["width"] > box["width"] and resized["height"] > box["height"]
        assert await page.locator("#t").input_value() == value
        for selector in ("#b", "#file", "#t"):
            target = await page.locator(selector).bounding_box()
            await runtime._execute_action({"kind": "mouse_move", "x": target["x"] + 2, "y": target["y"] + 2}, frozenset(), manual=False, allow_write=False, expected_epoch=epoch)
            with pytest.raises(WorkerError) as error:
                await runtime._execute_action({"kind": "drag", "x": 500, "y": 500}, frozenset(), manual=False, allow_write=False, expected_epoch=epoch)
            assert error.value.code == "APPROVAL_REQUIRED"
    finally:
        if runtime:
            await runtime.close()
        if browser:
            await browser.close()
        await playwright.stop()

@pytest.mark.asyncio
async def test_drag_timeout_releases_and_records_unknown(tmp_path):
    page = FakePage()
    runtime = BrowserRuntime(tmp_path, adapter=FakeAdapter(), page=page, test_mode=True,
                             egress_ready=True, route_generation=1)
    try:
        epoch = runtime.gate("agent")["control_epoch"]
        runtime._pointer_position = (10, 10)
        runtime._last_observation_id = "obs"
        async def stalled_down(**kwargs):
            page.mouse.calls.append(("down",))
            await asyncio.Event().wait()
        page.mouse.down = stalled_down
        body = AgentExecuteBody(action_id="timeout-drag", control_epoch=epoch, observation_id="obs",
                                action={"kind": "drag", "x": 40, "y": 40})
        result = await runtime.execute_agent(body)
        assert result["outcome"] == "unknown" and result["error"] == "ACTION_OUTCOME_UNKNOWN"
        assert page.mouse.calls == [("down",), ("up",)]
        assert await runtime.execute_agent(body) == result
    finally:
        await runtime.close()

@pytest.mark.asyncio
async def test_stalled_release_returns_unknown_quarantines_and_unlocks(tmp_path):
    page = FakePage()
    runtime = BrowserRuntime(tmp_path, adapter=FakeAdapter(), page=page, test_mode=True,
                             egress_ready=True, route_generation=1)
    try:
        epoch = runtime.gate("agent")["control_epoch"]
        runtime._pointer_position = (10, 10)
        runtime._last_observation_id = "obs"
        async def stalled_up(**kwargs):
            page.mouse.calls.append(("up",))
            await asyncio.Event().wait()
        page.mouse.up = stalled_up
        body = AgentExecuteBody(action_id="stalled-release", control_epoch=epoch, observation_id="obs",
                                action={"kind": "drag", "x": 40, "y": 40})
        result = await asyncio.wait_for(runtime.execute_agent(body), timeout=2)
        assert result["outcome"] == "unknown" and result["error"] == "ACTION_OUTCOME_UNKNOWN"
        assert page.mouse.calls[-1] == ("up",)
        assert runtime.control_owner == "none" and runtime.control_epoch > epoch
        assert runtime._resource_violation and not runtime._started
        assert runtime._pointer_position is None and not runtime._input_lock.locked()
        with pytest.raises(WorkerError) as error:
            runtime.gate("agent")
        assert error.value.code == "BROWSER_UNAVAILABLE"
        assert await runtime.execute_agent(body) == result
    finally:
        await runtime.close()
