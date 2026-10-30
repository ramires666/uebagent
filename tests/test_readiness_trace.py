from worker.readiness_trace import ReadinessActionTrace
from worker.app import BrowserRuntime, create_app
from fastapi.testclient import TestClient
from types import SimpleNamespace
import asyncio
from worker.app import READINESS_ACTION_SNAPSHOT
from worker.app import WorkerError
from playwright.sync_api import sync_playwright


def test_trace_is_disabled_unless_exact_test_opt_in(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.delenv("READINESS_ACTION_TRACE", raising=False)
    assert not ReadinessActionTrace.enabled()
    monkeypatch.setenv("READINESS_ACTION_TRACE", "1")
    assert ReadinessActionTrace.enabled()
    monkeypatch.setenv("APP_ENV", "production")
    assert not ReadinessActionTrace.enabled()


def test_trace_filters_private_and_unknown_fields_and_bounds_ring():
    trace = ReadinessActionTrace()
    trace.append({"kind": "click", "x": 60.234, "y": 101, "viewport": {"width": 1440, "height": 900},
                  "focus_before": "body", "focus_after": "checkbox", "hit": "checkbox",
                  "target": "desktop_checkbox", "target_boxes": {"desktop_checkbox": {"x": 1, "y": 2, "width": 20, "height": 20}, "evil-id": {"x": 0, "y": 0, "width": 1, "height": 1}},
                  "elapsed_ms": 23.8, "outcome": "applied", "text": "private text",
                  "value": "private value", "password": "secret", "url": "https://user:pw@example.test/?token=secret"})
    record = trace.snapshot()[0]
    assert record == {"kind": "click", "x": 60.23, "y": 101.0,
                      "viewport": {"width": 1440, "height": 900}, "focus_before": "body",
                      "focus_after": "checkbox", "hit": "checkbox", "elapsed_ms": 23,
                      "target": "desktop_checkbox", "target_boxes": {"desktop_checkbox": {"x": 1.0, "y": 2.0, "width": 20.0, "height": 20.0}},
                      "outcome": "applied"}
    assert not ({"text", "value", "password", "url"} & set(record))
    for index in range(205):
        trace.append({"kind": "wait", "elapsed_ms": index, "outcome": "applied"})
    assert len(trace.snapshot()) == ReadinessActionTrace.LIMIT
    assert trace.snapshot()[0]["elapsed_ms"] == 5
    trace.reset()
    assert trace.snapshot() == []


def test_real_action_hook_records_sanitized_target_and_states(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("READINESS_ACTION_TRACE", "1")

    class Adapter:
        async def url(self): return "https://readiness-fixture.test/form"

    class Page:
        def __init__(self):
            self.calls = 0
            self.checked = False
            self.mouse = self
        async def click(self, *args, **kwargs): self.checked = True
        async def evaluate(self, script, *args):
            if "const selector = 'a,area" in script:
                return {"tag": "INPUT", "role": "", "type": "checkbox", "href": "", "target": "", "editable": False}
            self.calls += 1
            return {"focus": "body" if self.calls == 1 else "checkbox", "focus_target": "none" if self.calls == 1 else "desktop_checkbox",
                    "hit": "checkbox", "target": "desktop_checkbox", "bbox": {"x": 3, "y": 4, "width": 22, "height": 22},
                    "checks": {"home": False, "desktop": self.checked}, "filled": {"first_name": False}}

    page = Page()
    runtime = BrowserRuntime(tmp_path, adapter=Adapter(), page=page, test_mode=True)
    runtime.control_owner = "human"
    result = asyncio.run(runtime._execute_action({"kind": "click", "x": 53, "y": 71}, frozenset(),
        manual=True, allow_write=True, expected_epoch=0))
    record = runtime._readiness_trace.snapshot()[0]
    assert result["x"] == 53 and result["y"] == 71
    assert record["focus_before"] == "body" and record["focus_after"] == "checkbox"
    assert record["hit"] == "checkbox" and record["target"] == "desktop_checkbox"
    assert record["bbox"] == {"x": 3.0, "y": 4.0, "width": 22.0, "height": 22.0}
    assert record["checked_before"]["desktop"] is False and record["checked_after"]["desktop"] is True
    assert "value" not in record and "text" not in record and page.calls == 2


def test_diagnostic_routes_are_opt_in_authenticated_and_get_is_read_only(monkeypatch):
    class Adapter:
        calls = 0
        async def url(self):
            self.calls += 1
            return "https://readiness-fixture.test/tree"
    adapter = Adapter()
    runtime = SimpleNamespace(_started=True, _adapter=adapter, _readiness_trace=ReadinessActionTrace(),
                              _page=object(), _action_in_progress=False, _import_in_progress=False, control_owner="none")
    client = TestClient(create_app(runtime=runtime, token="test-token"))
    headers = {"Authorization": "Bearer test-token"}
    monkeypatch.setenv("APP_ENV", "production")
    assert client.get("/diagnostics/readiness-actions", headers=headers).status_code == 404
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("READINESS_ACTION_TRACE", "1")
    assert client.get("/diagnostics/readiness-actions").status_code == 401
    before = adapter.calls
    assert client.get("/diagnostics/readiness-actions", headers=headers).json() == {"records": []}
    assert adapter.calls == before + 1
    assert client.post("/diagnostics/readiness-fixture", headers=headers, json={"scenario": []}).status_code == 422
    runtime._action_in_progress = True
    assert client.post("/diagnostics/readiness-fixture", headers=headers, json={"scenario": "tree"}).status_code == 409


def test_snapshot_script_matches_current_role_checkbox_sibling_markup():
    markup = '''<style>.rc-tree-checkbox{display:inline-block;width:24px;height:24px}</style><main><div class="rc-tree-treenode"><button class="rc-tree-switcher"></button>
      <input type="checkbox" id="native-desktop"><span class="rc-tree-checkbox" role="checkbox"
      aria-label="Select Desktop" aria-checked="false"></span></div>
      <div class="rc-tree-treenode"><button class="rc-tree-switcher"></button>
      <span class="rc-tree-checkbox" role="checkbox" aria-label="Select Home" aria-checked="false"></span></div>
      <input id="firstName" value="private-value"></main>'''
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.set_content(markup)
        box = page.locator('[aria-label="Select Desktop"]').bounding_box()
        before = page.evaluate(READINESS_ACTION_SNAPSHOT, {"x": box["x"] + box["width"] / 2, "y": box["y"] + box["height"] / 2})
        assert before["target"] == "desktop_checkbox" and before["hit"] == "checkbox"
        assert before["checks"] == {"home": False, "desktop": False}
        assert before["bounds"]["desktop_checkbox"]["width"] > 0
        page.locator('[aria-label="Select Desktop"]').evaluate("e => {e.setAttribute('aria-checked','true');e.classList.add('rc-tree-checkbox-checked')}")
        after = page.evaluate(READINESS_ACTION_SNAPSHOT, {"x": box["x"] + box["width"] / 2, "y": box["y"] + box["height"] / 2})
        assert after["checks"]["desktop"] is True
        assert after["filled"]["first_name"] is True
        assert "private-value" not in str(after) and "Select Desktop" not in str(after)
        browser.close()


def test_action_outcome_unknown_is_not_reported_as_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("READINESS_ACTION_TRACE", "1")
    class Adapter:
        async def url(self): return "https://readiness-fixture.test/tree"
    class Page:
        async def evaluate(self, *args): return {"focus": "body", "focus_target": "none", "checks": {"home": False, "desktop": False}, "filled": {}}
    runtime = BrowserRuntime(tmp_path, adapter=Adapter(), page=Page(), test_mode=True)
    async def unknown(*args, **kwargs): raise WorkerError("ACTION_OUTCOME_UNKNOWN", "private error text")
    runtime._execute_action_untraced = unknown
    try:
        asyncio.run(runtime._execute_action({"kind": "wait", "seconds": 0}, frozenset(), manual=True,
                                            allow_write=True, expected_epoch=0))
    except WorkerError:
        pass
    record = runtime._readiness_trace.snapshot()[0]
    assert record["outcome"] == "unknown" and "private error text" not in str(record)
