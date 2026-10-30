import asyncio

from fastapi.testclient import TestClient

from worker.app import create_app


class FakePage:
    def __init__(self, runtime, error=None):
        self.runtime = runtime
        self.error = error
        self.calls = 0

    async def request_gc(self):
        self.calls += 1
        assert self.runtime._action_in_progress is True
        assert self.runtime._capture_lock.locked() is True
        await asyncio.sleep(0)
        if self.error:
            raise self.error


class FakeAdapter:
    def __init__(self, url="https://readiness-fixture.test/tree", during_url=None):
        self.current_url = url
        self.during_url = during_url

    async def url(self):
        if self.during_url:
            self.during_url()
        return self.current_url


class FakeContext:
    def __init__(self, page):
        self.pages = [page]


class BusyLock:
    def locked(self):
        return True


class FakeRuntime:
    def __init__(self, *, url="https://readiness-fixture.test/tree", error=None):
        self._started = True
        self._main_page_closed = False
        self._resource_violation = False
        self._action_in_progress = False
        self._import_in_progress = False
        self._observation_pending = False
        self.control_owner = "none"
        self._capture_lock = asyncio.Lock()
        self._adapter = FakeAdapter(url)
        self._page = FakePage(self, error=error)
        self._context = FakeContext(self._page)


def _client(monkeypatch, runtime):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("READINESS_ACTION_TRACE", "1")
    return TestClient(create_app(runtime, token="worker-test"))


def test_gc_endpoint_requests_once_without_page_or_context_change(monkeypatch):
    runtime = FakeRuntime()
    page, context = runtime._page, runtime._context
    client = _client(monkeypatch, runtime)
    try:
        response = client.post("/diagnostics/readiness-memory-gc", json={},
                               headers={"Authorization": "Bearer worker-test"})
    finally:
        client.close()
    assert response.status_code == 200
    assert response.json() == {"requested": True}
    assert page.calls == 1
    assert runtime._page is page and runtime._context is context
    assert context.pages == [page]
    assert runtime.control_owner == "none"
    assert runtime._action_in_progress is False
    assert runtime._capture_lock.locked() is False


def test_gc_endpoint_rejects_unauthorized_and_non_test_runtime(monkeypatch):
    runtime = FakeRuntime()
    client = _client(monkeypatch, runtime)
    try:
        unauthorized = client.post("/diagnostics/readiness-memory-gc", json={})
        monkeypatch.setenv("APP_ENV", "production")
        production = client.post("/diagnostics/readiness-memory-gc", json={},
                                 headers={"Authorization": "Bearer worker-test"})
    finally:
        client.close()
    assert unauthorized.status_code == 401
    assert production.status_code == 404
    assert runtime._page.calls == 0


def test_gc_endpoint_rejects_body_and_non_fixture_page(monkeypatch):
    runtime = FakeRuntime()
    client = _client(monkeypatch, runtime)
    try:
        body = client.post("/diagnostics/readiness-memory-gc", json={"selector": "body"},
                           headers={"Authorization": "Bearer worker-test"})
    finally:
        client.close()
    assert body.status_code == 422
    assert body.json()["detail"]["code"] == "INVALID_REQUEST"
    assert runtime._page.calls == 0

    runtime = FakeRuntime(url="https://example.test/")
    client = _client(monkeypatch, runtime)
    try:
        wrong_target = client.post("/diagnostics/readiness-memory-gc", json={},
                                   headers={"Authorization": "Bearer worker-test"})
    finally:
        client.close()
    assert wrong_target.status_code == 404
    assert wrong_target.json()["detail"]["code"] == "NOT_FOUND"
    assert runtime._page.calls == 0


def test_gc_endpoint_rejects_busy_or_unsafe_runtime(monkeypatch):
    cases = (
        {"control_owner": "human"},
        {"_action_in_progress": True},
        {"_import_in_progress": True},
        {"_observation_pending": True},
    )
    for changes in cases:
        runtime = FakeRuntime()
        for key, value in changes.items():
            setattr(runtime, key, value)
        client = _client(monkeypatch, runtime)
        try:
            response = client.post("/diagnostics/readiness-memory-gc", json={},
                                   headers={"Authorization": "Bearer worker-test"})
        finally:
            client.close()
        assert response.status_code == 409
        assert runtime._page.calls == 0

    runtime = FakeRuntime()
    runtime._capture_lock = BusyLock()
    client = _client(monkeypatch, runtime)
    try:
        capture_busy = client.post("/diagnostics/readiness-memory-gc", json={},
                                   headers={"Authorization": "Bearer worker-test"})
    finally:
        client.close()
    assert capture_busy.status_code == 409
    assert capture_busy.json()["detail"]["code"] == "CONTROL_BUSY"
    assert runtime._page.calls == 0

    runtime = FakeRuntime()
    runtime._context.pages.append(object())
    client = _client(monkeypatch, runtime)
    try:
        unsafe = client.post("/diagnostics/readiness-memory-gc", json={},
                             headers={"Authorization": "Bearer worker-test"})
    finally:
        client.close()
    assert unsafe.status_code == 409
    assert unsafe.json()["detail"]["code"] == "PAGE_CONTEXT_UNSAFE"
    assert runtime._page.calls == 0


def test_gc_endpoint_rechecks_idle_state_after_url_await_without_clearing_other_flag(monkeypatch):
    runtime = FakeRuntime()
    runtime._adapter.during_url = lambda: setattr(runtime, "_action_in_progress", True)
    client = _client(monkeypatch, runtime)
    try:
        response = client.post("/diagnostics/readiness-memory-gc", json={},
                               headers={"Authorization": "Bearer worker-test"})
    finally:
        client.close()
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CONTROL_BUSY"
    assert runtime._page.calls == 0
    assert runtime._action_in_progress is True


def test_gc_endpoint_reports_fixed_unsupported_and_failure_codes(monkeypatch):
    runtime = FakeRuntime()
    runtime._page = object()
    runtime._context = FakeContext(runtime._page)
    client = _client(monkeypatch, runtime)
    try:
        unsupported = client.post("/diagnostics/readiness-memory-gc", json={},
                                  headers={"Authorization": "Bearer worker-test"})
    finally:
        client.close()
    assert unsupported.status_code == 503
    assert unsupported.json()["detail"]["code"] == "GC_UNSUPPORTED"

    runtime = FakeRuntime(error=RuntimeError("do not expose this"))
    client = _client(monkeypatch, runtime)
    try:
        failed = client.post("/diagnostics/readiness-memory-gc", json={},
                             headers={"Authorization": "Bearer worker-test"})
    finally:
        client.close()
    assert failed.status_code == 503
    assert failed.json()["detail"]["code"] == "GC_REQUEST_FAILED"
    assert "do not expose this" not in failed.text
    assert runtime._action_in_progress is False
    assert runtime._capture_lock.locked() is False
