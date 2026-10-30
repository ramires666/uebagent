import base64
import json
import threading
import time

import httpx
import pytest

from scripts.readiness_soak import (
    CONTROL_URL, WORKER_URL, Soak, SoakError, latency_summary, validate_environment,
)


def test_environment_requires_all_disposable_runtime_markers(tmp_path):
    key_file = tmp_path / "worker-key"
    key_file.write_text("synthetic-key\n", encoding="utf-8")
    env = {"APP_ENV": "test", "READINESS_DISPOSABLE": "1",
           "READINESS_RUNTIME_INTEGRATION": "1", "READINESS_ACTION_TRACE": "1",
           "BROWSER_OBSERVATION_HINTS": "1",
           "BROWSER_WORKER_URL": WORKER_URL, "WORKER_API_KEY_FILE": str(key_file)}
    assert validate_environment(confirmed=True, worker_url=WORKER_URL, control_url=CONTROL_URL,
                                key_file=key_file, environ=env, docker_marker=True) == "synthetic-key"
    for changes in ({"READINESS_DISPOSABLE": "0"}, {"READINESS_RUNTIME_INTEGRATION": "0"},
                    {"READINESS_ACTION_TRACE": "0"},
                    {"BROWSER_OBSERVATION_HINTS": "0"},
                    {"BROWSER_WORKER_URL": "http://example.com:7070"}):
        with pytest.raises(ValueError):
            validate_environment(confirmed=True, worker_url=WORKER_URL, control_url=CONTROL_URL,
                                 key_file=key_file, environ={**env, **changes}, docker_marker=True)
    with pytest.raises(ValueError):
        validate_environment(confirmed=False, worker_url=WORKER_URL, control_url=CONTROL_URL,
                             key_file=key_file, environ=env, docker_marker=True)
    with pytest.raises(ValueError):
        validate_environment(confirmed=True, worker_url="http://127.0.0.1:7070", control_url=CONTROL_URL,
                             key_file=key_file, environ=env, docker_marker=True)


def test_latency_summary_uses_p50_p95_and_max_in_milliseconds():
    assert latency_summary([.001, .003, .002]) == {
        "count": 3, "p50_ms": 2.0, "p95_ms": 3.0, "max_ms": 3.0,
    }
    assert latency_summary([]) == {"count": 0}


def test_memory_maintenance_failure_is_allowlisted_and_rejected():
    worker, control, _calls = _clients()
    soak = Soak(worker, control, "key", duration=1, progress=lambda _line: None)
    status = {"enabled": True, "supported": True, "state": "disabled_after_failure",
              "completed_actions_since_attempt": 0, "attempts": 1, "successes": 0,
              "failures": 1, "last_duration_ms": 2.5, "last_error_code": "gc_timeout",
              "ignored_sensitive_field": "must_not_be_exported"}
    with pytest.raises(SoakError, match="memory_maintenance_initial_state_failed"):
        soak._record_memory_maintenance({"memory_maintenance": status})
    assert soak._memory_maintenance_report() == {
        "enabled_at_start": True, "enabled_at_end": True, "attempts": 1,
        "successes": 0, "failures": 1, "last_error_code": "gc_timeout",
        "last_duration_ms": 2.5, "max_last_duration_ms": 2.5,
    }
    worker.close()
    control.close()


def test_memory_maintenance_disappearing_after_enable_fails():
    worker, control, _calls = _clients()
    soak = Soak(worker, control, "key", duration=1, progress=lambda _line: None)
    state = {"enabled": True, "supported": True, "state": "enabled",
             "completed_actions_since_attempt": 0, "attempts": 0, "successes": 0,
             "failures": 0, "last_duration_ms": None, "last_error_code": None}
    soak._record_memory_maintenance({"memory_maintenance": state})
    with pytest.raises(SoakError, match="memory_maintenance_status_missing"):
        soak._record_memory_maintenance({})
    worker.close()
    control.close()


def _clients(*, bad_control_poll=False, barrier=None):
    calls = {"fixture": 0}

    def worker_handler(request):
        path = request.url.path
        if path == "/status":
            return httpx.Response(200, json={"context_count": 1, "page_count": 1,
                "browser_started": True, "control_owner": "none", "action_in_progress": False,
                "import_in_progress": False, "resource_violation": False, "recovery_needed": False,
                "recovery_state": "idle", "recovery_generation": 3})
        if path == "/health":
            if barrier:
                barrier.wait(timeout=2)
            return httpx.Response(200, json={"ready": True, "recovery_state": "idle"})
        if path == "/observe":
            return httpx.Response(200, json={"width": 1440, "height": 900,
                "screenshot_png_b64": base64.b64encode(b"\x89PNG\r\n\x1a\nimage").decode("ascii"),
                "interaction_hints": {"version": 1, "coordinate_space": "normalized_1000", "elements": []}})
        if path == "/diagnostics/readiness-fixture":
            calls["fixture"] += 1
            return httpx.Response(200, json={"installed": True})
        if path == "/gate":
            owner = request.read().decode("utf-8")
            is_human = '"human"' in owner
            return httpx.Response(200, json={"control_epoch": 1,
                                             "control_owner": "human" if is_human else "none"})
        return httpx.Response(200, json={})

    def control_handler(request):
        if barrier and request.url.path == "/api/v1/tasks":
            barrier.wait(timeout=2)
        if request.url.path == "/api/v1/operator/session":
            return httpx.Response(200, json={"authenticated": True})
        if request.url.path == "/api/v1/tasks":
            return httpx.Response(200, json={"wrong": []} if bad_control_poll else {"tasks": []})
        return httpx.Response(200, json={})

    worker = httpx.Client(transport=httpx.MockTransport(worker_handler), trust_env=False)
    control = httpx.Client(transport=httpx.MockTransport(control_handler), trust_env=False)
    return worker, control, calls


def test_paired_poll_observes_concurrency_and_fixture_installs_once(monkeypatch):
    barrier = threading.Barrier(2)
    worker, control, calls = _clients(barrier=barrier)
    soak = Soak(worker, control, "key", duration=.2, progress=lambda _line: None)
    cycles = []

    def cycle():
        cycles.append(1)
        soak.observe()
        soak.cycles += 1
        soak.action_latency.append(.001)
        time.sleep(.005)

    monkeypatch.setattr(soak, "cycle", cycle)
    try:
        report = soak.run()
    finally:
        worker.close()
        control.close()
    assert report["status"] == "PASS", report["functional_failures"]
    assert report["polling"]["max_observed_concurrency"] == 2
    assert calls["fixture"] == 1
    assert cycles


def test_invalid_background_poll_cannot_produce_pass(monkeypatch):
    worker, control, _calls = _clients(bad_control_poll=True)
    soak = Soak(worker, control, "key", duration=.2, progress=lambda _line: None)
    def cycle():
        soak.observe()
        soak.cycles += 1
        soak.action_latency.append(.001)
        time.sleep(.002)
    monkeypatch.setattr(soak, "cycle", cycle)
    try:
        report = soak.run()
    finally:
        worker.close()
        control.close()
    assert report["status"] == "FAIL"
    assert "background_poll_failed" in report["functional_failures"]


def test_functional_failure_stops_after_one_cycle_and_fails(monkeypatch):
    worker, control, _calls = _clients()
    soak = Soak(worker, control, "key", duration=10, progress=lambda _line: None)
    cycles = []

    def fail_cycle():
        cycles.append(1)
        raise SoakError("fixture_effect_verification_failed")

    monkeypatch.setattr(soak, "cycle", fail_cycle)
    try:
        report = soak.run()
    finally:
        worker.close()
        control.close()
    assert report["status"] == "FAIL"
    assert report["functional_failures"] == ["fixture_effect_verification_failed"]
    assert len(cycles) == 1


def test_manual_navigation_receipt_epoch_is_used_for_following_clicks():
    state = {"epoch": 0, "owner": "none"}
    calls = []
    png = base64.b64encode(b"\x89PNG\r\n\x1a\nimage").decode("ascii")

    def status():
        return {"context_count": 1, "page_count": 1, "browser_started": True,
                "control_owner": state["owner"], "action_in_progress": False,
                "import_in_progress": False, "resource_violation": False,
                "recovery_needed": False, "recovery_state": "idle", "recovery_generation": 3}

    def worker_handler(request):
        path = request.url.path
        if path == "/status":
            return httpx.Response(200, json=status())
        if path == "/diagnostics/readiness-fixture":
            return httpx.Response(200, json={"installed": True})
        if path == "/gate":
            body = json.loads(request.read())
            state["epoch"] += 1
            state["owner"] = body["owner"]
            return httpx.Response(200, json={**status(), "control_epoch": state["epoch"]})
        if path == "/manual/execute":
            body = json.loads(request.read())
            assert body["control_epoch"] == state["epoch"]
            assert state["owner"] == "human"
            calls.append((body["action"]["kind"], body["control_epoch"]))
            state["epoch"] += 1
            return httpx.Response(200, json={"outcome": "applied", **status(), "control_epoch": state["epoch"]})
        if path == "/verify":
            return httpx.Response(200, json={"checks": [{"passed": True}, {"passed": True}]})
        if path == "/observe":
            return httpx.Response(200, json={"width": 1440, "height": 900, "screenshot_png_b64": png,
                "interaction_hints": {"version": 1, "coordinate_space": "normalized_1000", "elements": []}})
        raise AssertionError(path)

    def control_handler(request):
        return httpx.Response(200, json={"authenticated": True})

    worker = httpx.Client(transport=httpx.MockTransport(worker_handler), trust_env=False)
    control = httpx.Client(transport=httpx.MockTransport(control_handler), trust_env=False)
    soak = Soak(worker, control, "key", duration=1, progress=lambda _line: None)
    try:
        soak.status()
        soak.install_fixture()
        soak.cycle()
    finally:
        worker.close()
        control.close()
    assert calls == [("navigate", 1), ("click", 2), ("click", 3)]
    assert soak.cycles == 1
    assert state["owner"] == "none"
