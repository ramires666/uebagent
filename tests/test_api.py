import asyncio
import base64
import json
import time

import pytest

from fastapi.testclient import TestClient

from browser_mcp.app import create_app
from browser_mcp.config import Settings
from browser_mcp.chrome import ChromeBroker
import browser_mcp.chrome as chrome_module


class FakeWorker:
    def __init__(self):
        self.epoch = 0
        self.owner = "none"
        self.imported = []
        self.executed = []

    async def health(self):
        return {"configured": True, "ready": True, "browser_started": True, "detail": "test fixture"}

    async def state(self):
        return {"control_epoch": self.epoch, "control_owner": self.owner, "url": "https://shop.test/", "width": 1440, "height": 900}

    async def gate(self, owner):
        self.owner = owner
        self.epoch += 1
        return await self.state()

    async def can_import(self):
        return self.owner == "none"

    async def import_cookies(self, job_id, records):
        self.imported = json.loads(json.dumps(records))
        return {"status": "done", "copied": len(records)}

    async def call(self, method, path, data=None, raw=False):
        if path == "/capabilities/gemini-auth/status":
            return getattr(self, "gemini_auth_payload", {})
        if path == "/observe":
            return {**await self.state(), "observation_id": "fixture-observation", "screenshot_png_b64": base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode()}
        if path == "/verify":
            return {"verified": False, "checks": []}
        if path == "/heartbeat":
            return await self.state()
        if path == "/manual/execute":
            return {"outcome": "applied"}
        if path == "/execute":
            self.executed.append(data)
            return {"outcome": "applied"}
        return {"outcome": "applied"}


class FakeModel:
    async def health(self):
        return {"configured": True, "ready": True, "detail": "test fixture"}

    async def next_action(self, goal, screenshot, history):
        return {"kind": "finish", "summary": "Claimed complete"}


def make_client(tmp_path):
    settings = Settings(data_dir=tmp_path, ui_dir=tmp_path / "no-ui", test_mode=True,
                        operator_token="test-operator-token", mcp_token="test-mcp-token")
    worker = FakeWorker()
    return TestClient(create_app(settings, worker=worker, model=FakeModel())), worker


def login(client):
    response = client.post("/api/v1/operator/session", json={"token": "test-operator-token"})
    assert response.status_code == 200
    return {"X-CSRF-Token": response.json()["csrf_token"]}


@pytest.mark.asyncio
async def test_cookie_import_waits_for_control_and_preserves_selection(tmp_path):
    worker = FakeWorker(); worker.owner = 'human'
    broker = ChromeBroker(tmp_path, worker, catalog_ttl_s=3)
    record = {'cookie_id': 'chosen', 'source_key': 'source', 'name': 'fixture'}
    catalog = {'created_at': time.monotonic(), 'source_id': 'src', 'sites': [{'site_id': 'site'}]}
    job = {'job_id': 'job', 'status': 'validating', 'counts': {'selected': 1}}
    exports = []
    async def ask(_source, _kind, payload):
        exports.append(payload)
        return {'cookies': [{**payload['selections'][0], 'value': 'synthetic'}]}
    broker.ask = ask
    task = asyncio.create_task(broker._import(job, catalog, [record], ['site']))
    await asyncio.sleep(.1)
    assert job['status'] == 'waiting_pause' and not exports
    assert 'Вернуть управление' in job['detail']
    worker.owner = 'none'
    await asyncio.wait_for(task, 2)
    assert job['status'] == 'done' and len(exports) == 1
    assert exports[0]['selections'] == [{'source_key': 'source', 'name': 'fixture'}]
    assert len(worker.imported) == 1


@pytest.mark.asyncio
async def test_cookie_import_expiry_and_unresponsive_worker_are_bounded(tmp_path):
    for hanging in (False, True):
        worker = FakeWorker(); worker.owner = 'human'
        if hanging:
            async def can_import():
                await asyncio.sleep(10)
            worker.can_import = can_import
        broker = ChromeBroker(tmp_path, worker, catalog_ttl_s=.15)
        exports = []
        async def ask(*args):
            exports.append(args)
        broker.ask = ask
        job = {'job_id': 'job', 'status': 'validating', 'counts': {'selected': 1}}
        await asyncio.wait_for(broker._import(job, {'created_at': time.monotonic(), 'source_id': 'src', 'sites': []}, [], []), 1)
        assert job['status'] == 'selection_stale'
        assert job['error']['code'] == 'COOKIE_SELECTION_STALE'
        assert not exports


@pytest.mark.asyncio
async def test_cookie_import_busy_deadline_is_terminal(tmp_path, monkeypatch):
    monkeypatch.setattr(chrome_module, 'IMPORT_WAIT_LIMIT_S', .15)
    worker = FakeWorker(); worker.owner = 'human'
    broker = ChromeBroker(tmp_path, worker, catalog_ttl_s=10)
    job = {'job_id': 'job', 'status': 'validating', 'counts': {'selected': 1}}
    await asyncio.wait_for(broker._import(job, {'created_at': time.monotonic(), 'source_id': 'src', 'sites': []}, [], []), 1)
    assert job['status'] == 'failed' and job['error']['code'] == 'IMPORT_CONTROL_BUSY'


def test_import_report_list_is_authorized_bounded_and_allowlisted(tmp_path):
    client, _ = make_client(tmp_path)
    endpoint = "/api/v1/profiles/default/cookie-import-jobs"
    with client:
        assert client.get(endpoint).status_code == 401
        login(client)
        assert client.get("/api/v1/profiles/unknown/cookie-import-jobs").status_code == 404
        imports = client.app.state.broker.imports
        for i in range(25):
            imports[str(i)] = {
                "job_id": str(i), "profile_id": "default", "status": "partial",
                "counts": {"selected": 2, "copied": 1, "failed": 1, "value": "SECRET_COUNT"},
                "error": {"code": "FAIL", "message": "failed", "value": "SECRET_ERROR"},
                "detail": "partial", "outcomes_truncated": True,
                "outcomes": [{"name": "session", "domain": "example.test", "path": "/", "outcome": "failed", "error": "failed", "value": "SECRET_VALUE"}],
                "selected_cookie_ids": ["SECRET_SELECTED"], "secret_refs": ["SECRET_REF"],
            }
        response = client.get(endpoint)
        assert response.status_code == 200
        jobs = response.json()["jobs"]
        assert len(jobs) == 20
        assert [job["job_id"] for job in jobs] == [str(i) for i in range(24, 4, -1)]
        assert set(jobs[0]) == {"job_id", "status", "counts", "error", "detail", "outcomes", "outcomes_truncated"}
        assert set(jobs[0]["counts"]) == {"selected", "copied", "failed"}
        assert set(jobs[0]["error"]) == {"code", "message"}
        assert set(jobs[0]["outcomes"][0]) == {"name", "domain", "path", "outcome", "error"}
        assert "SECRET_" not in response.text


def test_desktop_state_reports_browser_availability(tmp_path):
    client, worker = make_client(tmp_path)
    with client:
        assert client.get("/api/v1/desktop/state").status_code == 401
        login(client)
        for started in (False, True):
            async def state():
                return {"browser_started": started, "control_owner": "none", "width": 800}

            worker.state = state
            response = client.get("/api/v1/desktop/state")
            assert response.status_code == 200
            assert response.json() == {"available": started, "browser_started": started,
                                       "control_owner": "none", "width": 800, "height": 900}


def test_manual_navigation_uses_only_explicit_destination_origin(tmp_path):
    client, worker = make_client(tmp_path)
    calls = []
    async def call(method, path, data=None, raw=False):
        calls.append((method, path, data))
        return {"outcome": "applied"}
    worker.call = call
    with client:
        headers = login(client)
        endpoint = "/api/v1/desktop/action"
        action = {"control_epoch": 3, "action": {"kind": "navigate", "url": "https://Gemini.Google.com:443/app?private=query"}}
        assert client.post(endpoint, json=action, headers=headers).status_code == 200
        assert calls[-1] == ("POST", "/manual/execute", {**action, "allowed_origins": ["https://gemini.google.com"]})
        ordinary = {"control_epoch": 4, "action": {"kind": "press_key", "key": "Escape"}}
        assert client.post(endpoint, json=ordinary, headers=headers).status_code == 200
        assert calls[-1][2] == ordinary
        count = len(calls)
        for url in ("http://example.test/", "https://user:pass@example.test/", "https://", "https://example.test:99999/", "https://bad_host.test/", "https://example.test:0/", "https://example.test\n.evil.test/"):
            response = client.post(endpoint, json={"control_epoch": 3, "action": {"kind": "navigate", "url": url}}, headers=headers)
            assert response.status_code >= 400
        assert client.post(endpoint, json={**action, "allowed_origins": ["https://evil.test"]}, headers=headers).status_code >= 400
        assert client.post(endpoint, json={"control_epoch": 3, "action": {**action["action"], "extra": True}}, headers=headers).status_code >= 400
        assert len(calls) == count


def test_auth_csrf_and_dns_rebinding_boundaries(tmp_path):
    client, _ = make_client(tmp_path)
    with client:
        assert client.get("/api/v1/tasks").status_code == 401
        headers = login(client)
        assert client.get("/api/v1/tasks").status_code == 200
        assert client.post("/api/v1/extension/pairing-codes").status_code == 403
        assert client.post("/api/v1/extension/pairing-codes", headers=headers).status_code == 200
        assert client.get("/api/v1/tasks", headers={"Origin": "https://evil.test"}).status_code == 403
        assert client.get("/health/live", headers={"Host": "evil.test"}).status_code == 403
        assert client.post("/api/v1/operator/session", json={"token": "чужой ключ"}).status_code == 401


def test_finish_without_conditions_is_marked_unverified(tmp_path):
    client, _ = make_client(tmp_path)
    with client:
        headers = login(client)
        spec = {"goal": "Read the site", "allowed_origins": ["https://shop.test"], "idempotency_key": "fixture-start-001", "sync_cookies": False}
        response = client.post("/api/v1/tasks", json=spec, headers=headers)
        assert response.status_code == 200
        task_id = response.json()["task_id"]
        result = client.get(f"/api/v1/tasks/{task_id}").json()
        for _ in range(20):
            if result["status"] == "succeeded":
                break
            result = client.get(f"/api/v1/tasks/{task_id}").json()
        assert result["status"] == "succeeded"
        assert result["phase"] == "completed_unverified"
        assert result["result"]["verified"] is False
        retry = client.post("/api/v1/tasks", json=spec, headers=headers)
        assert retry.json()["task_id"] == task_id
        assert client.post(f"/api/v1/tasks/{task_id}/cancel", json={"expected_version": result["version"]}, headers=headers).status_code == 409


def test_mcp_is_separately_authenticated_and_exposes_no_cookie_export(tmp_path):
    client, _ = make_client(tmp_path)
    with client:
        login(client)
        message = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        headers = {"Accept": "application/json, text/event-stream", "Authorization": "Bearer test-mcp-token"}
        assert client.post("/mcp", json=message).status_code == 401
        response = client.post("/mcp", json=message, headers=headers)
        assert response.status_code == 200, response.text
        names = {tool["name"] for tool in response.json()["result"]["tools"]}
        assert {"browser_task_start", "browser_task_pause", "browser_task_result"} <= names
        assert not any("cookie" in name or "approve" in name for name in names)


def test_mcp_browser_task_start_exposes_and_preserves_verbatim_text(tmp_path):
    class VerbatimModel(FakeModel):
        def __init__(self):
            self.actions = iter([{"kind": "type_text", "text": "__VERBATIM_TEXT__"},
                                 {"kind": "finish", "summary": "fixture"}])

        async def next_action(self, goal, screenshot, history):
            return next(self.actions)

    worker = FakeWorker()
    settings = Settings(data_dir=tmp_path, ui_dir=tmp_path / "no-ui", test_mode=True,
                        operator_token="test-operator-token", mcp_token="test-mcp-token")
    client = TestClient(create_app(settings, worker=worker, model=VerbatimModel()))
    headers = {"Accept": "application/json, text/event-stream", "Authorization": "Bearer test-mcp-token"}
    exact_text = "  Café О’Neil\nline two  "
    with client:
        login(client)
        listed = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                             headers=headers)
        assert listed.status_code == 200
        start_tool = next(tool for tool in listed.json()["result"]["tools"]
                          if tool["name"] == "browser_task_start")
        property_schema = start_tool["inputSchema"]["properties"]["verbatim_text"]
        string_schema = next(variant for variant in property_schema["anyOf"] if variant.get("type") == "string")
        assert string_schema["maxLength"] == 100_000
        assert string_schema["minLength"] == 1
        assert "verbatim_text" not in start_tool["inputSchema"].get("required", [])
        assert start_tool["inputSchema"]["properties"]["sync_cookies"]["default"] is True
        assert start_tool["inputSchema"]["properties"]["execution_mode"]["default"] == "autonomous"

        called = client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "browser_task_start", "arguments": {
                "goal": "Enter the separately supplied text exactly.",
                "allowed_origins": ["https://shop.test"],
                "idempotency_key": "mcp-verbatim-0001",
                "verbatim_text": exact_text,
                "sync_cookies": False,
            }},
        }, headers=headers)
        assert called.status_code == 200, called.text
        assert not called.json()["result"].get("isError", False)
        deadline = time.monotonic() + 2
        while not worker.executed and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(worker.executed) == 1
        assert worker.executed[0]["action"] == {"kind": "type_text", "text": exact_text}
        record = client.app.state.repository.list()[0]
        assert record.spec.verbatim_text == exact_text
        assert record.spec.sync_cookies is False
        assert record.spec.execution_mode == "autonomous"


def test_malformed_json_objects_are_rejected_and_logout_revokes_session(tmp_path):
    client, _ = make_client(tmp_path)
    with client:
        assert client.post("/api/v1/operator/session", json=[]).status_code == 422
        headers = login(client)
        for path in ("/api/v1/desktop/action", "/api/v1/profiles/default/cookie-import-jobs"):
            assert client.post(path, json=[], headers=headers).status_code == 422
        assert client.delete("/api/v1/operator/session", headers=headers).status_code == 200
        assert client.get("/api/v1/tasks").status_code == 401


def test_local_ui_auto_session_keeps_csrf_origin_and_mcp_auth(tmp_path):
    settings = Settings(data_dir=tmp_path, ui_dir=tmp_path / "no-ui", test_mode=True,
                        operator_token="test-operator-token", mcp_token="test-mcp-token", operator_auth_mode="local")
    with TestClient(create_app(settings, worker=FakeWorker(), model=FakeModel())) as client:
        session = client.get("/api/v1/operator/session")
        assert session.status_code == 200 and session.json()["local_ui"] is True
        assert "httponly" in session.headers["set-cookie"].lower()
        assert client.get("/api/v1/tasks").status_code == 200
        assert client.post("/api/v1/extension/pairing-codes").status_code == 403
        headers = {"X-CSRF-Token": session.json()["csrf_token"]}
        assert client.post("/api/v1/extension/pairing-codes", headers=headers).status_code == 200
        assert client.get("/api/v1/operator/session", headers={"Origin": "https://evil.test"}).status_code == 403
        assert client.post("/mcp", json={"jsonrpc":"2.0", "id":1, "method":"tools/list"}).status_code == 401
        client.cookies.clear()
        client.cookies.set("operator_session", "stale-session-from-before-restart")
        recovered = client.get("/api/v1/desktop/state")
        assert recovered.status_code == 200 and recovered.json()["available"] is True
        assert recovered.headers.get("X-CSRF-Token")
        assert client.post("/api/v1/extension/pairing-codes", headers=headers).status_code == 403
        assert client.post("/api/v1/extension/pairing-codes", headers={"X-CSRF-Token":recovered.headers["X-CSRF-Token"]}).status_code == 200


def test_local_ui_session_cap_recovers_without_sharing_csrf(tmp_path):
    settings = Settings(data_dir=tmp_path, ui_dir=tmp_path / "no-ui", test_mode=True,
                        operator_token="test-operator-token", mcp_token="test-mcp-token", operator_auth_mode="local")
    with TestClient(create_app(settings, worker=FakeWorker(), model=FakeModel())) as client:
        issued = []
        for _ in range(65):
            client.cookies.clear()
            response = client.get("/api/v1/operator/session")
            assert response.status_code == 200
            issued.append((response.cookies.get("operator_session"), response.json()["csrf_token"]))

        newest_cookie, newest_csrf = issued[-1]
        client.cookies.set("operator_session", newest_cookie)
        assert client.get("/api/v1/status").status_code == 200
        assert client.post("/api/v1/extension/pairing-codes", headers={"X-CSRF-Token": "wrong"}).status_code == 403
        assert client.post("/api/v1/extension/pairing-codes", headers={"X-CSRF-Token": newest_csrf}).status_code == 200

        # The first anonymous session was evicted. Its old CSRF token cannot
        # authorize writes, and automatic GET recovery issues a distinct token.
        client.cookies.set("operator_session", issued[0][0])
        assert client.post("/api/v1/extension/pairing-codes",
                           headers={"X-CSRF-Token": issued[0][1]}).status_code == 401
        stale_recovery = client.get("/api/v1/desktop/state")
        assert stale_recovery.status_code == 200
        fresh_csrf = stale_recovery.headers.get("X-CSRF-Token")
        assert fresh_csrf and fresh_csrf != issued[0][1]
        client.cookies.set("operator_session", stale_recovery.cookies.get("operator_session"))
        assert client.post("/api/v1/extension/pairing-codes",
                           headers={"X-CSRF-Token": issued[0][1]}).status_code == 403


def test_gemini_auth_status_proxy_is_bounded_and_requires_operator_session(tmp_path):
    client, worker = make_client(tmp_path)
    with client:
        unauthenticated = client.get("/api/v1/capabilities/gemini-auth/status")
        assert unauthenticated.status_code == 401

        headers = login(client)
        worker.gemini_auth_payload = {
            "status": "signed_in", "reason_code": "ACCOUNT_CONTROLS_PRESENT",
            "account_email": "private@example.test", "page_text": "private", "url": "https://private.test/",
        }
        response = client.get("/api/v1/capabilities/gemini-auth/status", headers=headers)
        assert response.status_code == 200
        assert response.json() == {"status": "signed_in", "reason_code": "ACCOUNT_CONTROLS_PRESENT"}

        worker.gemini_auth_payload = {"status": "signed_out", "reason_code": "PROOF_INCOMPLETE", "cookie": "secret"}
        assert client.get("/api/v1/capabilities/gemini-auth/status", headers=headers).json() == {
            "status": "unknown", "reason_code": "PROBE_UNAVAILABLE"}

        worker.call = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("fixture failure"))
        assert client.get("/api/v1/capabilities/gemini-auth/status", headers=headers).json() == {
            "status": "unknown", "reason_code": "PROBE_UNAVAILABLE"}


def test_websocket_cookie_catalog_and_exact_selected_export(tmp_path):
    client, worker = make_client(tmp_path)
    with client:
        headers = login(client)
        code = client.post("/api/v1/extension/pairing-codes", headers=headers).json()["code"]
        origin = "chrome-extension://" + "a"*32
        paired = client.post("/api/v1/extension/pair", headers={"Origin": origin}, json={"code": code, "extension_id": "a"*32, "label": "Fixture Chrome"})
        assert paired.status_code == 200, paired.text
        source = paired.json()
        with client.websocket_connect("/api/v1/extension/channel", headers={"Origin": origin}) as socket:
            socket.send_json({"type": "auth", **source})
            assert socket.receive_json()["type"] == "authenticated"
            sid = source["source_id"]
            client.post(f"/api/v1/chrome/sources/{sid}/open-sites/refresh", headers=headers)
            request = socket.receive_json()
            site = {"site_id": "site-1", "origin": "https://shop.test", "store_id": "0", "tab_count": 2}
            socket.send_json({"type": "result", "request_id": request["request_id"], "ok": True, "data": {"sites": [site]}})
            for _ in range(20):
                inventory = client.get(f"/api/v1/chrome/sources/{sid}/open-sites").json()
                if inventory["status"] == "ready":
                    break
            catalog_id = client.post(f"/api/v1/chrome/sources/{sid}/cookie-catalogs", headers=headers,
                                     json={"site_ids": ["site-1"], "discovery_revision": inventory["discovery_revision"]}).json()["catalog_id"]
            request = socket.receive_json()
            def cookie(name):
                return {"source_key": name, "site_ids": ["site-1"], "name": name, "domain": "shop.test", "path": "/", "store_id": "0", "host_only": True,
                        "secure": True, "http_only": True, "same_site": "lax", "expires_at": None, "partition_key": None, "unsupported_reason": None}
            socket.send_json({"type": "result", "request_id": request["request_id"], "ok": True, "data": {"cookies": [cookie("session"), cookie("preference")], "sites": [site]}})
            for _ in range(20):
                catalog = client.get(f"/api/v1/chrome/cookie-catalogs/{catalog_id}").json()
                if catalog["status"] == "ready":
                    break
            assert all("value" not in record for record in catalog["cookies"])
            selection = {"source_id": sid, "discovery_revision": inventory["discovery_revision"], "catalog_id": catalog_id, "catalog_revision": catalog["revision"],
                         "selected_site_ids": ["site-1"], "selected_cookie_ids": [catalog["cookies"][0]["cookie_id"]], "conflict_mode": "replace_selected_matches", "idempotency_key": "fixture-copy-001"}
            job = client.post("/api/v1/profiles/default/cookie-import-jobs", json=selection, headers=headers).json()
            request = socket.receive_json()
            assert request["kind"] == "cookie_export" and len(request["payload"]["selections"]) == 1
            exported = {**request["payload"]["selections"][0], "value": "SYNTHETIC_ROTATED_VALUE"}
            socket.send_json({"type": "result", "request_id": request["request_id"], "ok": True, "data": {"cookies": [exported], "allowed_origins": [site["origin"]]}})
            for _ in range(20):
                report = client.get(f"/api/v1/profiles/default/cookie-import-jobs/{job['job_id']}").json()
                if report["status"] == "done":
                    break
            assert report["status"] == "done", report
            assert len(worker.imported) == 1 and worker.imported[0]["name"] == "session"
            assert "SYNTHETIC_ROTATED_VALUE" not in json.dumps(report)
            retry = client.post("/api/v1/profiles/default/cookie-import-jobs", json=selection, headers=headers).json()
            assert retry["job_id"] == job["job_id"]
