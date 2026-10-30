from __future__ import annotations

import pytest
import asyncio
from pydantic import ValidationError

from worker import app as worker_app
from worker.app import BrowserRuntime, ManualExecuteBody, WorkerError


class _RouteRequest:
    def __init__(self, url: str):
        self.url = url
        self.method = "GET"
        self.resource_type = "document"
        self.is_navigation_request = lambda: True


class _Route:
    def __init__(self, url: str):
        self.request = _RouteRequest(url)
        self.result = None

    async def continue_(self):
        self.result = "continued"

    async def abort(self, reason):
        self.result = reason


class _Mouse:
    def __init__(self, adapter):
        self.adapter = adapter

    async def click(self, *_args, **_kwargs):
        self.adapter.current_url = "https://destination.example/landing"


class _Page:
    def __init__(self, adapter):
        self.mouse = _Mouse(adapter)


class _Context:
    @property
    def pages(self):
        return [object()]


class _Adapter:
    def __init__(self):
        self.current_url = "https://source.example/start"

    async def url(self):
        return self.current_url

    async def goto(self, url, **_kwargs):
        self.current_url = "https://redirect.example/final" if url.endswith("redirect-start") else url
        return {"url": self.current_url, "status": 200}


@pytest.mark.asyncio
async def test_manual_link_click_allows_public_cross_origin_but_agent_scope_stays_exact(tmp_path, monkeypatch):
    async def public(_host):
        return True

    monkeypatch.setattr(worker_app, "_public_host", public)
    adapter = _Adapter()
    page = _Page(adapter)
    runtime = BrowserRuntime(tmp_path, egress_ready=True, route_generation=1,
                             adapter=adapter, page=page, context=_Context(), test_mode=True)

    async def classify(_x, _y):
        return {"tag": "a", "href": "https://destination.example/landing", "target": ""}

    runtime._classify_click = classify
    human = runtime.gate("human")
    receipt = await runtime.execute_manual(ManualExecuteBody(
        control_epoch=human["control_epoch"],
        action={"kind": "click", "x": 10, "y": 10},
    ))
    assert receipt["outcome"] == "applied"
    assert receipt["manual_cross_origin_navigation"] is True
    assert adapter.current_url == "https://destination.example/landing"

    agent = runtime.gate("agent")
    assert agent["manual_cross_origin_navigation"] is False
    with pytest.raises(WorkerError, match="link destination is outside allowed_origins"):
        await runtime._execute_action_untraced(
            {"kind": "click", "x": 10, "y": 10},
            frozenset({"https://source.example"}), manual=False, allow_write=True,
            expected_epoch=agent["control_epoch"],
        )

    disabled_adapter = _Adapter()
    disabled_page = _Page(disabled_adapter)
    disabled_runtime = BrowserRuntime(tmp_path / "disabled", egress_ready=True, route_generation=1,
                                      adapter=disabled_adapter, page=disabled_page,
                                      context=_Context(), test_mode=True)
    disabled_runtime._classify_click = classify
    disabled_human = disabled_runtime.gate("human")
    with pytest.raises(WorkerError, match="link destination is outside allowed_origins"):
        await disabled_runtime.execute_manual(ManualExecuteBody(
            control_epoch=disabled_human["control_epoch"],
            action={"kind": "click", "x": 10, "y": 10},
            allow_cross_origin_navigation=False,
        ))
    assert disabled_adapter.current_url == "https://source.example/start"
    assert disabled_runtime.status_base()["manual_cross_origin_navigation"] is False


@pytest.mark.asyncio
async def test_manual_scope_option_can_disable_cross_origin_and_gate_release_fences_it(tmp_path):
    runtime = BrowserRuntime(tmp_path, egress_ready=True, route_generation=1)
    human = runtime.gate("human")
    assert human["manual_cross_origin_navigation"] is True
    # A strict request field avoids accepting ambiguous string values as policy.
    with pytest.raises(ValidationError):
        ManualExecuteBody(control_epoch=human["control_epoch"], action={"kind": "wait", "seconds": 0},
                          allow_cross_origin_navigation="false")
    assert ManualExecuteBody(control_epoch=0, action={"kind": "wait", "seconds": 0}).allow_cross_origin_navigation is True
    released = runtime.gate("none")
    assert released["manual_cross_origin_navigation"] is False
    agent = runtime.gate("agent")
    assert agent["manual_cross_origin_navigation"] is False


@pytest.mark.asyncio
async def test_manual_address_navigation_allows_public_redirect_and_blank_link_reuses_page(tmp_path):
    adapter = _Adapter()
    runtime = BrowserRuntime(tmp_path, egress_ready=True, route_generation=1,
                             adapter=adapter, page=_Page(adapter), context=_Context(), test_mode=True)
    human = runtime.gate("human")
    nav = await runtime.execute_manual(ManualExecuteBody(
        control_epoch=human["control_epoch"],
        action={"kind": "navigate", "url": "https://start.example/redirect-start"},
    ))
    assert nav["outcome"] == "applied"
    assert nav["result"]["url"] == "https://redirect.example/final"

    async def classify(_x, _y):
        return {"tag": "a", "href": "https://destination.example/landing", "target": "_blank"}

    runtime._classify_click = classify
    click = await runtime.execute_manual(ManualExecuteBody(
        control_epoch=nav["control_epoch"],
        action={"kind": "click", "x": 10, "y": 10},
    ))
    assert click["outcome"] == "applied"
    assert click["result"]["opened_in_current_page"] is True
    assert runtime.status_base()["page_count"] == 1


@pytest.mark.asyncio
async def test_manual_cross_origin_still_requires_public_egress(tmp_path, monkeypatch):
    async def private(_host):
        return False

    monkeypatch.setattr(worker_app, "_public_host", private)
    runtime = BrowserRuntime(tmp_path, egress_ready=True, route_generation=1)
    runtime._route_origins = frozenset({"https://source.example"})
    runtime.gate("human")
    route = _Route("https://private.example/path")
    await runtime._route_guard(route)
    assert route.result == "blockedbyclient"
    assert runtime.status_base()["blocked_resources"][-1]["reason"] == "non_public_or_unresolved_host"

    async def public(_host):
        return True

    monkeypatch.setattr(worker_app, "_public_host", public)
    route = _Route("https://other-public.example/path")
    await runtime._route_guard(route)
    assert route.result == "continued"

    runtime.gate("agent")
    route = _Route("https://other-public.example/path")
    await runtime._route_guard(route)
    assert route.result == "blockedbyclient"
    assert runtime.status_base()["blocked_resources"][-1]["reason"] == "origin_not_allowed"


@pytest.mark.asyncio
async def test_rejected_busy_manual_request_does_not_change_current_manual_policy(tmp_path):
    adapter = _Adapter()
    runtime = BrowserRuntime(tmp_path, egress_ready=True, route_generation=1,
                             adapter=adapter, page=_Page(adapter), context=_Context(), test_mode=True)
    human = runtime.gate("human")
    runtime._action_in_progress = True
    with pytest.raises(WorkerError, match="another input action is running"):
        await runtime.execute_manual(ManualExecuteBody(
            control_epoch=human["control_epoch"],
            action={"kind": "wait", "seconds": 0},
            allowed_origins=[], allow_cross_origin_navigation=False,
        ))
    assert runtime._manual_cross_origin_navigation is True


@pytest.mark.asyncio
async def test_stale_manual_request_after_url_wait_cannot_set_policy(tmp_path):
    class _BlockingAdapter(_Adapter):
        def __init__(self):
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def url(self):
            self.entered.set()
            await self.release.wait()
            return self.current_url

    adapter = _BlockingAdapter()
    runtime = BrowserRuntime(tmp_path, egress_ready=True, route_generation=1,
                             adapter=adapter, page=_Page(adapter), context=_Context(), test_mode=True)
    human = runtime.gate("human")
    request = asyncio.create_task(runtime.execute_manual(ManualExecuteBody(
        control_epoch=human["control_epoch"],
        action={"kind": "wait", "seconds": 0},
        allow_cross_origin_navigation=False,
    )))
    await adapter.entered.wait()
    runtime.gate("none")
    adapter.release.set()
    with pytest.raises(WorkerError) as stale:
        await request
    assert stale.value.code == "STALE_CONTROL_EPOCH"
    assert runtime._manual_cross_origin_navigation is False
