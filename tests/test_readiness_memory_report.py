import asyncio

from fastapi.testclient import TestClient

from worker.app import create_app
from worker.readiness_memory_report import MEMORY_REPORT_DOM_SNAPSHOT, MEMORY_REPORT_READY, normalize_dom_snapshot


class FakePage:
    def __init__(self, runtime, *, payload=None, fail_restore=False):
        self.runtime = runtime
        self.payload = payload or {"processes": [
            {"kind": "main", "metrics": {"resident": 123456}},
            {"kind": "web_content", "metrics": {
                "explicit": 1234567, "window-objects": 3456, "heap-unclassified": 789}},
        ]}
        self.current_url = "https://readiness-fixture.test/tree"
        self.verbose = False
        self.anonymize = False
        self.measure_calls = 0
        self.fail_restore = fail_restore
        self.goto_urls = []

    async def goto(self, url, **_kwargs):
        assert self.runtime._action_in_progress is True
        assert self.runtime._capture_lock.locked() is True
        self.goto_urls.append(url)
        if url == "https://readiness-fixture.test/tree" and self.fail_restore:
            raise RuntimeError("private restore error")
        self.current_url = url

    def locator(self, selector):
        assert selector in {"#verbose", "#measureButton"}
        return FakeLocator(self, selector)

    def get_by_label(self, label):
        assert label == "anonymize"
        return FakeLocator(self, "anonymize")

    async def wait_for_function(self, script, *, timeout):
        assert script == MEMORY_REPORT_READY
        assert timeout > 0
        assert self.current_url == "about:memory"
        return True

    async def evaluate(self, script):
        assert script == MEMORY_REPORT_DOM_SNAPSHOT
        assert self.current_url == "about:memory"
        assert self.verbose is True and self.anonymize is True
        return self.payload


class FakeLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    async def is_checked(self):
        return self.page.verbose if self.selector == "#verbose" else self.page.anonymize

    async def check(self):
        if self.selector == "#verbose":
            self.page.verbose = True
        else:
            self.page.anonymize = True

    async def click(self):
        assert self.selector == "#measureButton"
        assert self.page.verbose and self.page.anonymize
        self.page.measure_calls += 1


class FakeAdapter:
    def __init__(self, page, during_url=None):
        self.page = page
        self.during_url = during_url

    async def url(self):
        if self.during_url:
            self.during_url()
        return self.page.current_url


class FakeContext:
    def __init__(self, page):
        self.pages = [page]


class BusyLock:
    def locked(self):
        return True


class FakeRuntime:
    def __init__(self, *, fail_restore=False):
        self._started = True
        self._main_page_closed = False
        self._resource_violation = False
        self._action_in_progress = False
        self._import_in_progress = False
        self._observation_pending = False
        self.control_owner = "none"
        self.control_epoch = 5
        self._last_observation_id = "stale-observation"
        self._capture_lock = asyncio.Lock()
        self._page = FakePage(self, fail_restore=fail_restore)
        self._context = FakeContext(self._page)
        self._adapter = FakeAdapter(self._page)
        self._recovery_needed = False
        self._recovery_state = "idle"
        self.unavailable_reason = None

    def _mark_main_page_unavailable(self, reason):
        self.unavailable_reason = reason
        self._started = False
        self._main_page_closed = True
        self._recovery_needed = True
        self._recovery_state = "backoff"
        self.control_owner = "none"
        self.control_epoch += 1
        self._last_observation_id = None


def _client(monkeypatch, runtime):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("READINESS_ACTION_TRACE", "1")
    return TestClient(create_app(runtime=runtime, token="report-test"))


def test_normalizer_accepts_only_safe_roles_and_allowlisted_integer_metrics():
    payload = {"processes": [{"kind": "web_content", "metrics": {
        "explicit": 2048, "window-objects": 1024, "heap-unclassified": 256}},
        {"kind": "other", "metrics": {"resident": 4096}}]}
    assert normalize_dom_snapshot(payload) == [
        {"kind": "web_content", "metrics": {
            "explicit": 2048, "window_objects": 1024, "heap_unclassified": 256}},
        {"kind": "other", "metrics": {"resident": 4096}},
    ]
    for invalid in (
        {"processes": [{"kind": "web_content", "pid": 12, "metrics": {"resident": 1}}]},
        {"processes": [{"kind": "web_content", "metrics": {"url": "secret"}}]},
        {"processes": [{"kind": "Web Content (pid 12)", "metrics": {"resident": 1}}]},
        {"processes": [{"kind": "main", "metrics": {"resident": True}}]},
        {"processes": [{"kind": "main", "metrics": {"resident": -1}}]},
        {"processes": [{"kind": "main", "metrics": {"resident": 1_099_511_627_777}}]},
    ):
        try:
            normalize_dom_snapshot(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe report payload was accepted")


def test_readiness_predicate_waits_for_complete_measurement_without_requiring_destroyed_renderer():
    assert "about:memory (live measurement)" in MEMORY_REPORT_READY
    assert "querySelectorAll('.mrValue').length > 0" in MEMORY_REPORT_READY
    assert "names.some" not in MEMORY_REPORT_READY


def test_memory_report_is_authenticated_test_only_and_empty_body(monkeypatch):
    runtime = FakeRuntime()
    client = _client(monkeypatch, runtime)
    headers = {"Authorization": "Bearer report-test"}
    try:
        assert client.post("/diagnostics/readiness-memory-report", json={}, headers={}).status_code == 401
        assert client.post("/diagnostics/readiness-memory-report", json={"url": "about:memory"},
                           headers=headers).status_code == 422
        monkeypatch.setenv("APP_ENV", "production")
        assert client.post("/diagnostics/readiness-memory-report", json={}, headers=headers).status_code == 404
    finally:
        client.close()
    assert runtime._page.goto_urls == []


def test_report_measures_once_restores_same_page_and_invalidates_old_observation(monkeypatch):
    runtime = FakeRuntime()
    page, context = runtime._page, runtime._context
    client = _client(monkeypatch, runtime)
    try:
        response = client.post("/diagnostics/readiness-memory-report", json={},
                               headers={"Authorization": "Bearer report-test"})
    finally:
        client.close()
    assert response.status_code == 200
    assert response.json() == {
        "schema": "readiness-memory-report/v1", "intrusive": True,
        "processes": [
            {"kind": "main", "metrics": {"resident": 123456}},
            {"kind": "web_content", "metrics": {
                "explicit": 1234567, "window_objects": 3456, "heap_unclassified": 789}},
        ],
        "page_restored": True,
    }
    assert page.goto_urls == ["about:memory", "https://readiness-fixture.test/tree"]
    assert page.measure_calls == 1
    assert runtime._page is page and runtime._context is context and context.pages == [page]
    assert runtime._adapter.page is page
    assert runtime.control_epoch == 6 and runtime._last_observation_id is None
    assert runtime.control_owner == "none"
    assert runtime._action_in_progress is False and runtime._capture_lock.locked() is False


def test_report_rechecks_race_and_leaves_competing_action_flag_untouched(monkeypatch):
    runtime = FakeRuntime()
    runtime._adapter.during_url = lambda: setattr(runtime, "_action_in_progress", True)
    client = _client(monkeypatch, runtime)
    try:
        response = client.post("/diagnostics/readiness-memory-report", json={},
                               headers={"Authorization": "Bearer report-test"})
    finally:
        client.close()
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CONTROL_BUSY"
    assert runtime._page.goto_urls == []
    assert runtime._action_in_progress is True


def test_report_parse_failure_is_fixed_and_still_restores_page(monkeypatch):
    runtime = FakeRuntime()
    runtime._page.payload = {"processes": [{"kind": "Web Content (pid 123)", "metrics": {"resident": 1}}]}
    client = _client(monkeypatch, runtime)
    try:
        response = client.post("/diagnostics/readiness-memory-report", json={},
                               headers={"Authorization": "Bearer report-test"})
    finally:
        client.close()
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "CAPABILITY_UNAVAILABLE"
    assert runtime._page.goto_urls == ["about:memory", "https://readiness-fixture.test/tree"]
    assert runtime.control_epoch == 6 and runtime._last_observation_id is None
    assert runtime._started is True


def test_report_conflicts_when_gate_or_context_is_not_idle(monkeypatch):
    for change in ("human", "extra_page"):
        runtime = FakeRuntime()
        if change == "human":
            runtime.control_owner = "human"
        else:
            runtime._context.pages.append(object())
        client = _client(monkeypatch, runtime)
        try:
            response = client.post("/diagnostics/readiness-memory-report", json={},
                                   headers={"Authorization": "Bearer report-test"})
        finally:
            client.close()
        assert response.status_code == 409
        assert runtime._page.goto_urls == []

    runtime = FakeRuntime()
    runtime._capture_lock = BusyLock()
    client = _client(monkeypatch, runtime)
    try:
        response = client.post("/diagnostics/readiness-memory-report", json={},
                               headers={"Authorization": "Bearer report-test"})
    finally:
        client.close()
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CONTROL_BUSY"
    assert runtime._page.goto_urls == []


def test_failed_restore_marks_runtime_unavailable_and_returns_fixed_error(monkeypatch):
    runtime = FakeRuntime(fail_restore=True)
    client = _client(monkeypatch, runtime)
    try:
        response = client.post("/diagnostics/readiness-memory-report", json={},
                               headers={"Authorization": "Bearer report-test"})
    finally:
        client.close()
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "PAGE_RESTORE_FAILED"
    assert runtime.unavailable_reason == "readiness_memory_report_restore_failed"
    assert runtime._started is False and runtime._recovery_needed is True
    assert runtime._action_in_progress is False and runtime._capture_lock.locked() is False
