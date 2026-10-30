import asyncio
import json
import sys
from types import ModuleType, SimpleNamespace

from fastapi.testclient import TestClient

from worker.app import BrowserRuntime, create_app
from worker.fingerprint import profile_launch_options, readiness_default_addons_enabled
from worker.readiness_trace import ReadinessActionTrace


def test_variant_switches_default_on_and_are_test_trace_only(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("READINESS_ACTION_TRACE", "1")
    monkeypatch.delenv("READINESS_SKIP_ACTION_SNAPSHOTS", raising=False)
    monkeypatch.delenv("READINESS_DISABLE_DEFAULT_ADDONS", raising=False)
    assert ReadinessActionTrace.enabled() is True
    assert ReadinessActionTrace.capture_enabled() is True
    assert readiness_default_addons_enabled() is True

    monkeypatch.setenv("READINESS_SKIP_ACTION_SNAPSHOTS", "1")
    monkeypatch.setenv("READINESS_DISABLE_DEFAULT_ADDONS", "1")
    assert ReadinessActionTrace.enabled() is True  # trace routes remain available
    assert ReadinessActionTrace.capture_enabled() is False
    assert readiness_default_addons_enabled() is False

    monkeypatch.setenv("APP_ENV", "production")
    assert ReadinessActionTrace.capture_enabled() is False
    assert readiness_default_addons_enabled() is True

    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.delenv("READINESS_ACTION_TRACE")
    assert ReadinessActionTrace.capture_enabled() is False
    assert readiness_default_addons_enabled() is True


def test_snapshots_off_keeps_action_and_trace_routes_but_skips_page_evaluates(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("READINESS_ACTION_TRACE", "1")
    monkeypatch.setenv("READINESS_SKIP_ACTION_SNAPSHOTS", "1")

    class Adapter:
        async def url(self):
            return "https://readiness-fixture.test/tree"

    class Page:
        evaluations = 0

        async def evaluate(self, *_args):
            self.evaluations += 1
            return {}

    page = Page()
    runtime = BrowserRuntime(tmp_path, adapter=Adapter(), page=page, test_mode=True)
    runtime.control_owner = "human"
    result = asyncio.run(runtime._execute_action({"kind": "wait", "seconds": 0}, frozenset(),
        manual=True, allow_write=True, expected_epoch=0))
    assert result == {"seconds": 0.0}
    assert page.evaluations == 0
    assert runtime._readiness_trace.snapshot() == []
    assert ReadinessActionTrace.enabled() is True


def test_default_addon_variant_uses_camoufox_exclude_api_on_new_and_saved_profiles(monkeypatch, tmp_path):
    calls = []
    fake_camoufox = ModuleType("camoufox")
    fake_camoufox.DefaultAddons = SimpleNamespace(UBO="ubo-enum")
    fake_utils = ModuleType("camoufox.utils")

    def launch_options(**kwargs):
        config = kwargs["config"]
        config.update({"navigator.userAgent": "fixture", "fonts:spacing_seed": 7,
                       "canvas:aaOffset": 1})
        if "ubo-enum" not in kwargs.get("exclude_addons", []):
            config["addons"] = ["/installed/default-ubo.xpi"]
        calls.append({**kwargs, "effective_config": dict(config)})
        return {"env": {}, "firefox_user_prefs": {}}

    fake_utils.launch_options = launch_options
    fake_utils.validate_config = lambda _config: None
    monkeypatch.setitem(sys.modules, "camoufox", fake_camoufox)
    monkeypatch.setitem(sys.modules, "camoufox.utils", fake_utils)
    monkeypatch.setattr("importlib.metadata.version", lambda _name: "0.4.11")
    monkeypatch.setattr("worker.fingerprint._prepare_fontconfig", lambda *_args: None)
    monkeypatch.setattr("worker.fingerprint._apply_system_ui_font_preferences", lambda _options: None)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("READINESS_ACTION_TRACE", "1")
    monkeypatch.delenv("READINESS_DISABLE_DEFAULT_ADDONS", raising=False)

    profile = tmp_path / "profiles" / "default"
    profile.parent.mkdir()
    kwargs = {"headless": True, "viewport": {"width": 1440, "height": 900}, "env": {}}
    baseline_state = {}
    profile_launch_options(profile, **kwargs, readiness_config=baseline_state)
    state_path = profile.parent / "default.camoufox-fingerprint.json"
    persisted_before = state_path.read_text(encoding="utf-8")
    assert baseline_state == {"default_addons_enabled": True}
    assert json.loads(persisted_before)["config"]["addons"] == ["/installed/default-ubo.xpi"]

    monkeypatch.setenv("READINESS_DISABLE_DEFAULT_ADDONS", "1")
    no_addon_state = {}
    profile_launch_options(profile, **kwargs, readiness_config=no_addon_state)
    assert no_addon_state == {"default_addons_enabled": False}
    assert calls[-1]["exclude_addons"] == ["ubo-enum"]
    assert "addons" not in calls[-1]["effective_config"]
    assert state_path.read_text(encoding="utf-8") == persisted_before

    monkeypatch.delenv("READINESS_DISABLE_DEFAULT_ADDONS")
    restored_state = {}
    profile_launch_options(profile, **kwargs, readiness_config=restored_state)
    assert restored_state == {"default_addons_enabled": True}
    assert "exclude_addons" not in calls[-1]
    assert calls[-1]["effective_config"]["addons"] == ["/installed/default-ubo.xpi"]


def test_readiness_memory_config_reports_only_variant_bools_backend_and_is_guarded(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("READINESS_ACTION_TRACE", "1")
    runtime = SimpleNamespace(_readiness_trace_snapshots_enabled=False,
                              _readiness_default_addons_enabled=False, backend="camoufox")
    client = TestClient(create_app(runtime=runtime, token="test-token"))
    headers = {"Authorization": "Bearer test-token"}
    try:
        assert client.get("/diagnostics/readiness-memory-config").status_code == 401
        response = client.get("/diagnostics/readiness-memory-config", headers=headers)
        assert response.status_code == 200
        assert response.json() == {"trace_snapshots_enabled": False,
                                   "default_addons_enabled": False,
                                   "backend": "camoufox"}
        monkeypatch.setenv("APP_ENV", "production")
        assert client.get("/diagnostics/readiness-memory-config", headers=headers).status_code == 404
    finally:
        client.close()
