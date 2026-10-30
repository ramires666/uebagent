from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from scripts.readiness_manual_browsing import (
    LINK_POINT, SOURCE_ORIGIN, SOURCE_URL, TARGET_URL, ManualBrowsingProof, WORKER_URL,
)
from scripts.readiness_p0_fixture import fixture_html
from worker.app import create_app


class Response:
    def __init__(self, status_code, value):
        self.status_code = status_code
        self.value = value

    def json(self):
        return self.value


class FakeClient:
    def __init__(self):
        self.owner = "none"
        self.epoch = 0
        self.url = "about:blank"
        self.calls = []

    def request(self, method, url, *, headers, json=None, timeout=None):
        assert headers == {"Authorization": "Bearer test-key"}
        assert timeout == 10.0
        path = url.removeprefix(WORKER_URL)
        self.calls.append((method, path, json))
        if path == "/status":
            return Response(200, {"browser_started": True, "context_count": 1, "page_count": 1,
                                  "control_owner": self.owner, "action_in_progress": False,
                                  "import_in_progress": False, "resource_violation": False,
                                  "page_id": "main", "manual_cross_origin_navigation": self.owner == "human"})
        if path == "/diagnostics/readiness-fixture":
            assert json == {"scenario": "cross_origin"}
            return Response(200, {"installed": True, "scenario": "cross_origin"})
        if path == "/gate":
            self.owner = json["owner"]
            self.epoch += 1
            return Response(200, {"control_owner": self.owner, "control_epoch": self.epoch})
        if path == "/manual/execute":
            action = json["action"]
            if action["kind"] == "navigate":
                self.url = action["url"]
            elif json.get("allow_cross_origin_navigation") is False:
                return Response(409, {"detail": {"code": "POLICY_DENIED"}})
            else:
                self.url = TARGET_URL
            self.epoch += 1
            return Response(200, {"outcome": "applied", "control_epoch": self.epoch,
                                  "control_owner": "human", "result": {"url": self.url}})
        if path == "/verify":
            passed = self.url == TARGET_URL and json["conditions"][0]["text"] == "TARGET_READY"
            return Response(200, {"checks": [{"passed": passed}]})
        if path == "/observe":
            return Response(200, {"observation_id": "observation-1"})
        if path == "/execute":
            assert json["allowed_origins"] == [SOURCE_ORIGIN]
            assert json["action"] == {"kind": "click", **LINK_POINT}
            return Response(409, {"detail": {"code": "POLICY_DENIED"}})
        raise AssertionError(f"unexpected request path: {path}")


class FakePage:
    def __init__(self):
        self.routes = []

    async def route(self, pattern, handler):
        self.routes.append((pattern, handler))


class Route:
    def __init__(self, url):
        self.request = SimpleNamespace(url=url)
        self.fulfilled = None
        self.aborted = False

    async def fulfill(self, **kwargs):
        self.fulfilled = kwargs

    async def abort(self):
        self.aborted = True


class Trace:
    def reset(self):
        pass


def test_cross_origin_fixture_is_static_and_uses_one_fixed_target():
    source = fixture_html("cross_origin")
    target = fixture_html("cross_origin_target")
    assert 'href="https://readiness-target.test/landing"' in source
    assert "TARGET_READY" in target
    assert "input" not in source.lower()
    assert "script" not in source.lower()
    assert LINK_POINT == {"x": 720, "y": 285}


def test_fixture_endpoint_installs_only_exact_source_and_target_routes(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("READINESS_ACTION_TRACE", "1")
    page = FakePage()
    runtime = SimpleNamespace(_started=True, _page=page, _context=None,
                              _action_in_progress=False, _import_in_progress=False,
                              control_owner="none", _readiness_trace=Trace())
    client = TestClient(create_app(runtime=runtime, token="test-key"))
    response = client.post("/diagnostics/readiness-fixture",
                           headers={"Authorization": "Bearer test-key"},
                           json={"scenario": "cross_origin"})
    assert response.status_code == 200
    assert [pattern for pattern, _ in page.routes] == [
        "https://readiness-fixture.test/cross_origin", "https://readiness-target.test/landing"]
    for (pattern, handler), marker in zip(page.routes, ("cross-origin source", "TARGET_READY")):
        route = Route(pattern)
        asyncio.run(handler(route))
        assert route.fulfilled["status"] == 200
        assert marker in route.fulfilled["body"]
    unrelated = Route("https://readiness-target.test/other")
    asyncio.run(page.routes[1][1](unrelated))
    assert unrelated.aborted
    assert len(page.routes) == 2


def test_manual_browsing_proof_demonstrates_default_and_keeps_agent_allowlist():
    client = FakeClient()
    report = ManualBrowsingProof(client, "test-key").run()
    assert report == {
        "schema": "readiness-manual-browsing/v1", "status": "DIAGNOSTIC",
        "scenario": "cross_origin", "default_navigation_enabled": True,
        "default_click_reached_target": True, "opt_out_error_code": "POLICY_DENIED",
        "agent_error_code": "POLICY_DENIED", "single_page_preserved": True,
    }
    manual_requests = [body for method, path, body in client.calls
                       if method == "POST" and path == "/manual/execute"]
    assert len(manual_requests) == 4
    assert "allow_cross_origin_navigation" not in manual_requests[0]
    assert "allow_cross_origin_navigation" not in manual_requests[1]
    assert manual_requests[2].get("allow_cross_origin_navigation") is None
    assert manual_requests[3]["allow_cross_origin_navigation"] is False
    assert manual_requests[3]["action"] == {"kind": "click", **LINK_POINT}


def test_runner_environment_guard_uses_existing_disposable_contract(tmp_path):
    from scripts.readiness_soak import validate_environment

    key_file = tmp_path / "key"
    key_file.write_text("synthetic-key\n", encoding="utf-8")
    env = {"APP_ENV": "test", "READINESS_DISPOSABLE": "1",
           "READINESS_RUNTIME_INTEGRATION": "1", "READINESS_ACTION_TRACE": "1",
           "BROWSER_OBSERVATION_HINTS": "1", "BROWSER_WORKER_URL": WORKER_URL,
           "WORKER_API_KEY_FILE": str(key_file)}
    assert validate_environment(confirmed=True, worker_url=WORKER_URL,
                                control_url="http://localhost:8080", key_file=key_file,
                                environ=env, docker_marker=True) == "synthetic-key"
    try:
        validate_environment(confirmed=False, worker_url=WORKER_URL,
                             control_url="http://localhost:8080", key_file=key_file,
                             environ=env, docker_marker=True)
    except ValueError as exc:
        assert str(exc) == "explicit_disposable_confirmation_required"
    else:
        raise AssertionError("confirmation guard was not enforced")

