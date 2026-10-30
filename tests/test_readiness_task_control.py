from __future__ import annotations

import httpx
import pytest

from scripts.readiness_task_control import (
    DEFAULT_WORKER_URL,
    ProbeError,
    event_counts,
    auth_state,
    mcp_result,
    validate_environment,
    worker_idle_status,
)


def test_environment_requires_explicit_test_markers_and_known_worker(tmp_path):
    worker_key = tmp_path / "worker-key"
    mcp_key = tmp_path / "mcp-token.txt"
    worker_key.write_text("worker-secret", encoding="utf-8")
    mcp_key.write_text("mcp-secret", encoding="utf-8")
    env = {"APP_ENV": "test", "READINESS_DISPOSABLE": "1", "BROWSER_MCP_DATA_DIR": str(tmp_path),
           "WORKER_API_KEY_FILE": str(worker_key), "BROWSER_WORKER_URL": DEFAULT_WORKER_URL}
    assert validate_environment(True, env) == (DEFAULT_WORKER_URL, worker_key, mcp_key)
    with pytest.raises(ValueError, match="explicit_disposable_confirmation_required"):
        validate_environment(False, env)
    with pytest.raises(ValueError, match="disposable_test_markers_required"):
        validate_environment(True, {**env, "APP_ENV": "production"})
    with pytest.raises(ValueError, match="worker_url_must_be_known_internal_service"):
        validate_environment(True, {**env, "BROWSER_WORKER_URL": "https://example.invalid:7070"})


def test_mcp_result_extracts_structured_result_and_sanitizes_errors():
    response = httpx.Response(200, json={"result": {"structuredContent": {"task_id": "synthetic-id"}}})
    assert mcp_result(response) == {"task_id": "synthetic-id"}

    failure = httpx.Response(200, json={"result": {"isError": True, "content": [{"text": "INVALID_TRANSITION private-value"}]}})
    with pytest.raises(ProbeError) as err:
        mcp_result(failure)
    assert err.value.code == "INVALID_TRANSITION"
    assert "private-value" not in str(err.value)


def test_event_counts_only_action_planned_events():
    assert event_counts({"events": [
        {"event_type": "action.planned"}, {"event_type": "action.completed"},
        {"event_type": "action.planned"},
    ]}) == (2, 3)
    with pytest.raises(ProbeError):
        event_counts({"events": "bad"})


def test_worker_idle_gate_requires_single_context_page_and_no_owner():
    class FakeClient:
        def __init__(self, status):
            self.status = status

        def worker_json(self, method, path, *, json_body=None):
            assert (method, path) == ("GET", "/status")
            return self.status

    idle = {"context_count": 1, "page_count": 1, "control_owner": "none",
            "action_in_progress": False, "import_in_progress": False,
            "resource_violation": False, "recovery_needed": False, "recovery_state": "idle",
            "control_epoch": 7}
    assert worker_idle_status(FakeClient(idle)) == idle
    with pytest.raises(ProbeError, match="worker_not_idle_or_one_page"):
        worker_idle_status(FakeClient({**idle, "page_count": 2}))
    with pytest.raises(ProbeError, match="worker_not_idle_or_one_page"):
        worker_idle_status(FakeClient({**idle, "resource_violation": True}))
    with pytest.raises(ProbeError, match="worker_not_idle_or_one_page"):
        worker_idle_status(FakeClient({**idle, "recovery_state": "recovering", "recovery_needed": True}))


def test_auth_state_includes_epoch_and_observation_side_effect_flags():
    status = {"control_epoch": 7, "control_owner": "none", "context_count": 1, "page_count": 1,
              "action_in_progress": False, "import_in_progress": False, "resource_violation": False,
              "recovery_needed": False, "recovery_state": "idle"}
    assert auth_state(status) == auth_state(dict(status))
    assert auth_state(status) != auth_state({**status, "control_epoch": 8})

