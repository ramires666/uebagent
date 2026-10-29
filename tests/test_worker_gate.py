from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from worker.app import BrowserRuntime, create_app


PNG = b"\x89PNG\r\n\x1a\nworker-fixture"


class FakeLocator:
    async def inner_text(self, timeout=2000):
        return "Safe fixture page"

    async def input_value(self, timeout=2000):
        return "fixture-value"


class FakeFirst:
    async def input_value(self, timeout=2000):
        return "fixture-value"


class FakeLocatorChain:
    @property
    def first(self):
        return FakeFirst()

    async def inner_text(self, timeout=2000):
        return "Safe fixture page"


class FakeMouse:
    def __init__(self):
        self.calls = []

    async def click(self, x, y, **kwargs):
        self.calls.append(("click", x, y, kwargs))

    async def move(self, x, y):
        self.calls.append(("move", x, y))

    async def wheel(self, dx, dy):
        self.calls.append(("wheel", dx, dy))


class FakeKeyboard:
    def __init__(self):
        self.calls = []

    async def insert_text(self, value):
        self.calls.append(("text", value))

    async def press(self, key):
        self.calls.append(("key", key))


class FakePage:
    def __init__(self):
        self.mouse = FakeMouse()
        self.keyboard = FakeKeyboard()
        self.target = {"tag": "a", "role": "link", "type": "", "href": "https://shop.example/next"}
        self.back_count = 0

    async def evaluate(self, expression, arg=None):
        if "elementFromPoint" in expression:
            return self.target
        if "activeElement" in expression:
            return True
        if "innerWidth" in expression:
            return {"width": 1440, "height": 900}
        return None

    def locator(self, selector):
        return FakeLocatorChain()

    async def go_back(self, **kwargs):
        self.back_count += 1


class FakeContext:
    def __init__(self, pages):
        self.pages = list(pages)


class FakeAdapter:
    def __init__(self):
        self.current_url = "about:blank"
        self.cookies = []

    async def screenshot(self, **kwargs):
        return PNG

    async def url(self):
        return self.current_url

    async def goto(self, url, **kwargs):
        self.current_url = url
        return {"url": url, "status": 200}

    async def set_cookies(self, cookies):
        self.cookies.extend(cookies)

    async def get_cookies(self, urls=None):
        return [{"expires": -1, **cookie} for cookie in self.cookies]


def make_runtime(tmp_path, *, egress_ready=True):
    adapter, page = FakeAdapter(), FakePage()
    runtime = BrowserRuntime(tmp_path, egress_ready=egress_ready, route_generation=1 if egress_ready else 0,
                             adapter=adapter, page=page, context=FakeContext([page]), test_mode=True)
    return runtime, adapter, page


async def request(app, method, url, *, json=None, token="worker-test"):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://worker") as client:
        headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
        return await client.request(method, url, json=json, headers=headers)


@pytest.mark.asyncio
async def test_auth_gate_observe_and_screenshot_keep_observation_current(tmp_path):
    runtime, _, page = make_runtime(tmp_path)
    app = create_app(runtime, token="worker-test")
    assert (await request(app, "GET", "/status", token=None)).status_code == 401
    health = await request(app, "GET", "/health")
    assert health.json()["ready"] is True
    assert health.json()["browser_started"] is True

    gate = await request(app, "POST", "/gate", json={"owner": "agent"})
    state = gate.json()
    observed = await request(app, "POST", "/observe", json={})
    obs = observed.json()
    before = obs["observation_id"]
    shot = await request(app, "GET", "/screenshot")
    assert shot.content == PNG and shot.headers["content-type"] == "image/png"
    assert runtime._last_observation_id == before
    executed = await request(app, "POST", "/execute", json={
        "action_id": "act-1", "control_epoch": state["control_epoch"],
        "observation_id": before, "allowed_origins": ["https://shop.example"],
        "action": {"kind": "click", "x": 20, "y": 30, "button": "left", "count": 1},
    })
    assert executed.status_code == 200 and executed.json()["outcome"] == "applied"
    assert len(page.mouse.calls) == 1
    replay = await request(app, "POST", "/execute", json={
        "action_id": "act-1", "control_epoch": state["control_epoch"],
        "observation_id": before, "allowed_origins": ["https://shop.example"],
        "action": {"kind": "click", "x": 20, "y": 30, "button": "left", "count": 1},
    })
    assert replay.json() == executed.json() and len(page.mouse.calls) == 1
    conflict = await request(app, "POST", "/execute", json={
        "action_id": "act-1", "control_epoch": state["control_epoch"],
        "observation_id": before, "allowed_origins": ["https://shop.example"],
        "action": {"kind": "click", "x": 21, "y": 30, "button": "left", "count": 1},
    })
    assert conflict.status_code == 409 and conflict.json()["detail"]["code"] == "ACTION_ID_CONFLICT"
    stale = await request(app, "POST", "/execute", json={
        "action_id": "act-2", "control_epoch": state["control_epoch"],
        "observation_id": before, "action": {"kind": "history_back"},
    })
    assert stale.status_code == 409
    await runtime.close()


@pytest.mark.asyncio
async def test_agent_heartbeat_refreshes_short_control_lease(tmp_path):
    runtime, _, _ = make_runtime(tmp_path)
    app = create_app(runtime, token="worker-test")
    agent = (await request(app, "POST", "/gate", json={"owner": "agent"})).json()
    runtime._lease_deadline = time.monotonic() - 1
    renewed = await request(app, "POST", "/heartbeat", json={"control_epoch": agent["control_epoch"]})
    assert renewed.status_code == 200 and renewed.json()["lease_expires_in_s"] == 5
    await runtime.close()


@pytest.mark.asyncio
async def test_button_preflight_fences_agent_and_manual_control_allows_enter(tmp_path):
    runtime, _, page = make_runtime(tmp_path)
    app = create_app(runtime, token="worker-test")
    agent = (await request(app, "POST", "/gate", json={"owner": "agent"})).json()
    obs = (await request(app, "POST", "/observe")).json()
    page.target = {"tag": "button", "role": "button", "type": "submit", "href": ""}
    response = await request(app, "POST", "/execute", json={
        "action_id": "submit-button", "control_epoch": agent["control_epoch"],
        "observation_id": obs["observation_id"], "action": {"kind": "click", "x": 2, "y": 2},
    })
    assert response.status_code == 409 and response.json()["detail"]["code"] == "APPROVAL_REQUIRED"

    human = (await request(app, "POST", "/gate", json={"owner": "human"})).json()
    manual = await request(app, "POST", "/manual/execute", json={
        "control_epoch": human["control_epoch"], "action": {"kind": "press_key", "key": "Enter"},
    })
    assert manual.status_code == 200 and manual.json()["control_owner"] == "human"
    assert manual.json()["control_epoch"] == human["control_epoch"] + 1
    rejected = await request(app, "POST", "/manual/execute", json={
        "control_epoch": human["control_epoch"], "action": {"kind": "press_key", "key": "Escape"},
    })
    assert rejected.status_code == 409
    await runtime.close()


@pytest.mark.asyncio
async def test_agent_lease_expires_fail_closed_and_health_reflects_missing_egress(tmp_path):
    runtime, _, _ = make_runtime(tmp_path, egress_ready=False)
    app = create_app(runtime, token="worker-test")
    assert (await request(app, "GET", "/health")).json()["ready"] is False
    agent = (await request(app, "POST", "/gate", json={"owner": "agent"})).json()
    obs = (await request(app, "POST", "/observe")).json()
    blocked = await request(app, "POST", "/execute", json={
        "action_id": "egress-blocked", "control_epoch": agent["control_epoch"],
        "observation_id": obs["observation_id"], "action": {"kind": "history_back"},
    })
    assert blocked.status_code == 503 and blocked.json()["detail"]["code"] == "EGRESS_NOT_READY"
    runtime._lease_deadline = time.monotonic() - 1
    response = await request(app, "POST", "/execute", json={
        "action_id": "expired", "control_epoch": agent["control_epoch"],
        "observation_id": obs["observation_id"], "action": {"kind": "history_back"},
    })
    assert response.status_code == 409
    assert (await request(app, "GET", "/status")).json()["control_owner"] == "none"
    await runtime.close()


@pytest.mark.asyncio
async def test_manual_navigation_requires_egress_and_agent_navigation_requires_allowlist(tmp_path):
    runtime, adapter, _ = make_runtime(tmp_path, egress_ready=False)
    app = create_app(runtime, token="worker-test")
    human = (await request(app, "POST", "/gate", json={"owner": "human"})).json()
    blocked = await request(app, "POST", "/manual/execute", json={
        "control_epoch": human["control_epoch"], "action": {"kind": "navigate", "url": "https://shop.example"},
    })
    assert blocked.status_code == 503 and adapter.current_url == "about:blank"
    await runtime.close()

    runtime, adapter, _ = make_runtime(tmp_path / "ready", egress_ready=True)
    app = create_app(runtime, token="worker-test")
    agent = (await request(app, "POST", "/gate", json={"owner": "agent"})).json()
    obs = (await request(app, "POST", "/observe")).json()
    denied = await request(app, "POST", "/execute", json={
        "action_id": "nav-denied", "control_epoch": agent["control_epoch"],
        "observation_id": obs["observation_id"], "allowed_origins": [],
        "action": {"kind": "navigate", "url": "https://shop.example"},
    })
    assert denied.status_code == 409 and adapter.current_url == "about:blank"
    await runtime.close()


@pytest.mark.asyncio
async def test_cookie_import_validates_all_values_idempotently_and_never_returns_cookie_values(tmp_path):
    runtime, adapter, _ = make_runtime(tmp_path)
    app = create_app(runtime, token="worker-test")
    cookies = [
        {"source_key": "sid-key", "name": "sid", "value": "sensitive-value", "domain": ".shop.example", "path": "/",
         "store_id": "1", "host_only": True, "secure": True, "http_only": True, "same_site": "no_restriction",
         "expires_at": None, "partition_key": None, "unsupported_reason": "", "site_ids": ["site-1"]},
        {"name": "prefs", "value": "compact", "domain": ".shop.example", "path": "/account", "secure": True, "hostOnly": False},
    ]
    invalid = await request(app, "POST", "/cookies/import", json={"job_id": "bad-job", "cookies": [cookies[0], {"name": "bad", "value": "x", "domain": "bad domain"}]})
    assert invalid.status_code == 422 and adapter.cookies == []
    first = await request(app, "POST", "/cookies/import", json={"job_id": "job-1", "cookies": cookies})
    assert first.status_code == 200 and first.json()["applied"] == 2
    assert "sensitive-value" not in first.text
    assert adapter.cookies[0]["domain"] == "shop.example" and adapter.cookies[0]["sameSite"] == "None"
    assert adapter.cookies[1]["domain"] == ".shop.example"
    repeated = await request(app, "POST", "/cookies/import", json={"job_id": "job-1", "cookies": cookies})
    assert repeated.json() == first.json() and len(adapter.cookies) == 2
    conflict = await request(app, "POST", "/cookies/import", json={"job_id": "job-1", "cookies": [cookies[1]]})
    assert conflict.status_code == 409
    await runtime.close()


@pytest.mark.asyncio
async def test_verify_supports_only_read_only_conditions_and_empty_is_not_success(tmp_path):
    runtime, adapter, _ = make_runtime(tmp_path)
    adapter.current_url = "https://shop.example/item"
    app = create_app(runtime, token="worker-test")
    empty = await request(app, "POST", "/verify", json={"conditions": []})
    assert empty.json() == {"verified": False, "checks": []}
    verified = await request(app, "POST", "/verify", json={"conditions": [
        {"kind": "url_is", "url": "https://shop.example/item"},
        {"kind": "text_present", "text": "fixture page"},
        {"kind": "field_equals", "selector": "#account", "value": "fixture-value"},
    ]})
    assert verified.json()["verified"] is True
    unknown = await request(app, "POST", "/verify", json={"conditions": [{"kind": "javascript", "code": "alert(1)"}]})
    assert unknown.status_code == 422
    await runtime.close()


@pytest.mark.asyncio
async def test_stale_command_and_cached_replay_do_not_change_network_scope(tmp_path):
    runtime, _, page = make_runtime(tmp_path)
    app = create_app(runtime, token="worker-test")
    agent = runtime.gate("agent")
    obs = await runtime.observe()
    command = {"action_id": "move-once", "control_epoch": agent["control_epoch"],
               "observation_id": obs["observation_id"], "allowed_origins": ["https://shop.example"],
               "action": {"kind": "mouse_move", "x": 12, "y": 34}}
    first = await request(app, "POST", "/execute", json=command)
    assert first.status_code == 200 and page.mouse.calls == [("move", 12.0, 34.0)]
    runtime.gate("human")
    runtime._route_origins = frozenset({"https://manual.example"})
    replay = await request(app, "POST", "/execute", json=command)
    assert replay.json() == first.json()
    assert runtime._route_origins == frozenset({"https://manual.example"})
    command["action_id"] = "stale-new-action"
    rejected = await request(app, "POST", "/execute", json=command)
    assert rejected.status_code == 409
    assert runtime._route_origins == frozenset({"https://manual.example"})
    assert len(page.mouse.calls) == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_cookie_import_fences_new_owners_and_reports_partial_failure(tmp_path):
    from worker.app import CookieImportBody, WorkerError

    runtime, adapter, _ = make_runtime(tmp_path)
    entered, proceed = asyncio.Event(), asyncio.Event()

    async def delayed_set(cookies):
        entered.set()
        await proceed.wait()
        if cookies[0]["name"] == "fails":
            raise RuntimeError("synthetic failure")
        adapter.cookies.extend(cookies)

    adapter.set_cookies = delayed_set
    task = asyncio.create_task(runtime.import_cookies(CookieImportBody(job_id="partial-import", cookies=[
        {"name": "works", "value": "private-one", "domain": "shop.example", "path": "/"},
        {"name": "fails", "value": "private-two", "domain": "shop.example", "path": "/"},
    ])))
    await asyncio.wait_for(entered.wait(), 1)
    for owner in ("agent", "human"):
        with pytest.raises(WorkerError) as error:
            runtime.gate(owner)
        assert error.value.code == "CONTROL_BUSY"
    proceed.set()
    result = await task
    assert result["applied"] == 1 and result["failed"] == 1
    assert result["outcome"] == "unknown" and result["cookies"][1]["outcome"] == "unknown"
    assert "private-one" not in str(result) and "private-two" not in str(result)
    assert not runtime._import_in_progress and runtime.control_owner == "none"
    assert runtime.gate("human")["control_owner"] == "human"
    await runtime.close()


@pytest.mark.asyncio
async def test_cookie_import_readback_distinguishes_missing_and_unknown(tmp_path):
    from worker.app import CookieImportBody

    runtime, adapter, _ = make_runtime(tmp_path)
    cookie = {"name": "fixture", "value": "private-fixture", "domain": "shop.example", "path": "/"}
    async def absent(urls):
        assert urls == ["https://shop.example/"]
        return []
    adapter.get_cookies = absent
    missing = await runtime.import_cookies(CookieImportBody(job_id="missing", cookies=[cookie]))
    assert missing["cookies"][0]["outcome"] == "failed"
    assert missing["cookies"][0]["error"] == "COOKIE_NOT_VERIFIED"
    async def unavailable(urls):
        raise RuntimeError("private-fixture")
    adapter.get_cookies = unavailable
    unknown = await runtime.import_cookies(CookieImportBody(job_id="unknown", cookies=[cookie, {**cookie, "name": "second"}]))
    assert unknown["outcome"] == "unknown"
    assert [row["outcome"] for row in unknown["cookies"]] == ["unknown", "not_attempted"]
    assert "private-fixture" not in str(unknown)
    await runtime.close()


@pytest.mark.asyncio
async def test_cookie_readback_url_encodes_path_delimiters(tmp_path):
    from worker.app import CookieImportBody

    runtime, adapter, _ = make_runtime(tmp_path)
    async def readback(urls):
        assert urls == ["https://shop.example/a%3Fb%23c%25d"]
        return [{"expires": -1, **adapter.cookies[0]}]
    adapter.get_cookies = readback
    result = await runtime.import_cookies(CookieImportBody(job_id="path-encoding", cookies=[{
        "name": "fixture", "value": "synthetic", "domain": "shop.example", "path": "/a?b#c%d"
    }]))
    assert result["applied"] == 1
    await runtime.close()


def test_cookie_domain_normalization_without_explicit_host_only():
    from worker.app import BrowserRuntime
    base = {"name": "fixture", "value": "synthetic", "path": "/"}
    assert BrowserRuntime._normalize_cookie({**base, "domain": "BÜCHER.Example"})["domain"] == "xn--bcher-kva.example"
    assert BrowserRuntime._normalize_cookie({**base, "domain": ".BÜCHER.Example"})["domain"] == ".xn--bcher-kva.example"


@pytest.mark.asyncio
async def test_cookie_readback_timeout_and_epoch_change_are_unknown(tmp_path, monkeypatch):
    from worker.app import CookieImportBody

    runtime, adapter, _ = make_runtime(tmp_path)
    cookie = {"name": "fixture", "value": "secret-fixture", "domain": "shop.example", "path": "/"}
    original_wait_for = asyncio.wait_for
    async def short_wait(awaitable, timeout):
        return await original_wait_for(awaitable, timeout=0.02)
    monkeypatch.setattr("worker.app.asyncio.wait_for", short_wait)
    async def stalled(urls):
        await asyncio.sleep(1)
    adapter.get_cookies = stalled
    result = await runtime.import_cookies(CookieImportBody(job_id="timeout", cookies=[cookie]))
    assert result["outcome"] == "unknown" and result["cookies"][0]["error"] == "COOKIE_OUTCOME_UNKNOWN"
    assert "secret-fixture" not in str(result)
    async def changed(urls):
        runtime.control_epoch += 1
        return [{"expires": -1, **adapter.cookies[-1]}]
    adapter.get_cookies = changed
    result = await runtime.import_cookies(CookieImportBody(job_id="epoch", cookies=[cookie]))
    assert result["outcome"] == "unknown" and result["cookies"][0]["outcome"] == "unknown"
    assert "secret-fixture" not in str(result)
    await runtime.close()


@pytest.mark.asyncio
async def test_reset_old_view_preserves_cookies_and_requires_current_agent(tmp_path):
    runtime, adapter, _ = make_runtime(tmp_path)
    adapter.current_url = "https://old.example/"
    adapter.cookies = [{"name": "keep-session", "value": "fixture"}]
    app = create_app(runtime, token="worker-test")
    agent = runtime.gate("agent")
    runtime._route_origins = frozenset({"https://old.example"})
    result = await request(app, "POST", "/view/reset", json={"control_epoch": agent["control_epoch"]})
    assert result.status_code == 200 and result.json()["outcome"] == "applied"
    assert adapter.current_url == "about:blank" and adapter.cookies[0]["name"] == "keep-session"
    assert not runtime._route_origins
    runtime.gate("human")
    adapter.current_url = "https://manual.example/"
    rejected = await request(app, "POST", "/view/reset", json={"control_epoch": agent["control_epoch"]})
    assert rejected.status_code == 409 and adapter.current_url == "https://manual.example/"
    await runtime.close()
