"""Run bounded model diagnostics on synthetic fixtures and DemoQA failures.

This entry point is intended only for the disposable test stack. Results contain
task summaries, independent verifier outcomes, and the worker's sanitized action
trace; model output, action text, typed values, and credentials are excluded.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from demoqa_task_spec import browser_task_start_payload

CONTROL = "http://localhost:8080"
WORKER = "http://browser:7070"
DATA_DIR = Path(os.environ.get("BROWSER_MCP_DATA_DIR", "/app/runtime/control-data"))
ACTIVE = {"queued", "running", "pause_requested"}
TERMINAL = {"succeeded", "failed", "cancelled", "expired", "waiting_human", "waiting_approval", "recovering", "paused"}
SUSPENDED = {"waiting_human", "waiting_approval", "recovering", "paused"}
TRACEABLE_ACTION_KINDS = {"click", "type_text", "press_key", "scroll", "wait", "navigate", "drag", "mouse_move", "finish"}
DEMOQA = "https://demoqa.com"
FIXTURE = "https://readiness-fixture.test"

SCENARIOS: dict[str, dict[str, Any]] = {
    "checkbox": {
        "url": DEMOQA + "/checkbox", "origin": DEMOQA,
        "goal": "Expand Home and leave its children visible. Select the Desktop checkbox and finish when the selected result appears.",
        "conditions": [
            {"kind": "element_checked", "selector": '[role="checkbox"][aria-label="Select Desktop"]', "checked": True},
            {"kind": "element_text_contains", "selector": "#result", "text": "desktop"},
        ],
    },
    "web_table": {
        "url": DEMOQA + "/webtables", "origin": DEMOQA,
        "goal": "Add a new row to the web table: first name Testperson, last name Example, email test@example.com, age 30, salary 12345, department Testing. Submit and finish when the new row appears with all these values.",
        "conditions": [{"kind": "element_text_contains", "selector": "table tbody tr:has-text('test@example.com'), .rt-tr-group:has-text('test@example.com')", "text": value}
                       for value in ("Testperson", "Example", "30", "12345", "Testing", "test@example.com")],
    },
    "fixture_tree": {
        "url": FIXTURE + "/tree", "origin": FIXTURE,
        "goal": "On this synthetic page, expand Projects, select Desktop, and finish when the selected result appears.",
        "conditions": [
            {"kind": "element_checked", "selector": "#desktop", "checked": True},
            {"kind": "element_text_contains", "selector": "#result", "text": "Desktop"},
        ],
        "fixture": "tree",
    },
    "fixture_form": {
        "url": FIXTURE + "/form", "origin": FIXTURE,
        "goal": "On this synthetic page, add a row with first name Testperson, last name Example, email test@example.com, age 30, salary 12345, and department Testing. Save the row and finish when all six values appear.",
        "conditions": [{"kind": "element_text_contains", "selector": "#result", "text": value}
                       for value in ("Testperson", "Example", "30", "12345", "Testing", "test@example.com")],
        "fixture": "form",
    },
}


def _mcp(client: httpx.Client, headers: dict[str, str], control: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    response = client.post(control + "/mcp", headers=headers,
                           json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                 "params": {"name": name, "arguments": args}})
    response.raise_for_status()
    payload = response.json()
    if "error" in payload or payload.get("result", {}).get("isError"):
        raise RuntimeError("MCP tool returned an error")
    result = payload["result"]
    if isinstance(result.get("structuredContent"), dict):
        return result["structuredContent"]
    return json.loads(result["content"][0]["text"])


def run_scenario(client: httpx.Client, mcp_headers: dict[str, str], worker_headers: dict[str, str],
                 name: str, spec: dict[str, Any], *, control: str = CONTROL, worker: str = WORKER) -> dict[str, Any]:
    result: dict[str, Any] = {"scenario": name, "status": "failed", "steps": 0,
                              "timeout_s": 180, "max_steps": 40}
    started = time.monotonic()
    stage = "worker_preflight"
    task_id = None
    record: dict[str, Any] = {}
    try:
        status_response = client.get(worker + "/status", headers=worker_headers)
        status_response.raise_for_status()
        status = status_response.json()
        if not _worker_idle(status):
            raise RuntimeError("worker must be idle with exactly one context and page")
        if spec.get("fixture"):
            stage = "fixture_install"
            installed = client.post(worker + "/diagnostics/readiness-fixture", headers=worker_headers,
                                    json={"scenario": spec["fixture"]})
            installed.raise_for_status()
        stage = "scenario_navigation"
        gate_response = client.post(worker + "/gate", headers=worker_headers, json={"owner": "human"})
        gate_response.raise_for_status()
        gate = gate_response.json()
        try:
            navigation = client.post(worker + "/manual/execute", headers=worker_headers, json={
                "control_epoch": gate["control_epoch"], "allowed_origins": [spec["origin"]],
                "action": {"kind": "navigate", "url": spec["url"]},
            })
            navigation.raise_for_status()
            if navigation.json().get("outcome") != "applied":
                raise RuntimeError("scenario navigation was not confirmed")
        finally:
            release = client.post(worker + "/gate", headers=worker_headers, json={"owner": "none"})
            release.raise_for_status()
        stage = "baseline_verification"
        reset = client.post(worker + "/diagnostics/readiness-actions/reset", headers=worker_headers)
        reset.raise_for_status()
        baseline = client.post(WORKER + "/verify", headers=worker_headers,
                               json={"conditions": spec["conditions"]})
        baseline.raise_for_status()
        result["baseline_verified"] = baseline.json().get("verified") is True
        stage = "task_start"
        task_started = time.monotonic()
        record = _mcp(client, mcp_headers, control, "browser_task_start", browser_task_start_payload(
            goal=f"Open {spec['url']} if needed. {spec['goal']}", origin=spec["origin"],
            idempotency_key="readiness-p0-" + uuid.uuid4().hex,
            timeout_s=180, max_steps=40, success_conditions=spec["conditions"],
        ))
        task_id = record["task_id"]
        deadline = time.monotonic() + 195
        while time.monotonic() < deadline:
            record = _mcp(client, mcp_headers, control, "browser_task_status", {"task_id": task_id})
            if record.get("status") in TERMINAL:
                break
            time.sleep(2)
        if record.get("status") not in TERMINAL:
            _mcp(client, mcp_headers, control, "browser_task_cancel", {"task_id": task_id})
            record = _mcp(client, mcp_headers, control, "browser_task_status", {"task_id": task_id})
        if record.get("status") in SUSPENDED:
            result.update({"status": "blocked", "task_status": record.get("status"),
                           "stop_reason": "task_suspended_for_operator_review",
                           "scenario_elapsed_ms": max(0, round((time.monotonic() - started) * 1000))})
            return result
        stage = "task_events"
        events = _mcp(client, mcp_headers, control, "browser_task_events", {"task_id": task_id, "limit": 100}).get("events", [])
        action_kinds: dict[str, int] = {}
        planned_events = []
        event_offsets = []
        for event in events:
            if event.get("event_type") == "action.planned":
                kind = event.get("payload", {}).get("kind")
                if isinstance(kind, str) and kind in TRACEABLE_ACTION_KINDS:
                    action_kinds[kind] = action_kinds.get(kind, 0) + 1
                stamp = event.get("created_at") or event.get("timestamp")
                try:
                    parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
                    if isinstance(kind, str) and kind in TRACEABLE_ACTION_KINDS:
                        event_offsets.append((parsed.timestamp(), kind))
                except (TypeError, ValueError, OverflowError):
                    pass
        event_offsets.sort(key=lambda item: item[0])
        previous_offset = None
        for stamp, kind in event_offsets:
            planned_events.append({"kind": kind,
                                   "elapsed_since_previous_ms": None if previous_offset is None else max(0, round((stamp - previous_offset) * 1000))})
            previous_offset = stamp
        stage = "independent_verification"
        verification = client.post(worker + "/verify", headers=worker_headers,
                                   json={"conditions": spec["conditions"]})
        verification.raise_for_status()
        checked = verification.json()
        stage = "action_trace"
        trace_response = client.get(worker + "/diagnostics/readiness-actions", headers=worker_headers)
        trace_response.raise_for_status()
        trace = trace_response.json()
        result.update({"steps": record.get("step_count", 0), "action_kinds": action_kinds,
                       "model_action_observed": bool(action_kinds), "task_status": record.get("status"),
                       "error_code": ((record.get("error") or {}).get("code")
                                      if str((record.get("error") or {}).get("code", "")).replace("_", "").isalnum()
                                      and len(str((record.get("error") or {}).get("code", ""))) <= 64 else None),
                       "independent_verified": checked.get("verified") is True,
                       "verification_checks": checked.get("checks", []),
                       "planned_events": planned_events,
                       "trace": trace.get("records", []),
                       "trace_record_count": len(trace.get("records", []))})
        result["task_elapsed_ms"] = max(0, round((time.monotonic() - task_started) * 1000))
        result["status"] = ("inconclusive" if result["baseline_verified"] else
                            "passed" if record.get("status") == "succeeded" and result["independent_verified"] else "failed")
    except httpx.HTTPError as exc:
        result.update({"failure_stage": stage, "failure_code": "HTTP_ERROR",
                       "http_status": getattr(getattr(exc, "response", None), "status_code", None)})
    except (KeyError, ValueError):
        result.update({"failure_stage": stage, "failure_code": "INVALID_RESPONSE"})
    except RuntimeError:
        result.update({"failure_stage": stage, "failure_code": "DIAGNOSTIC_PRECONDITION_FAILED"})
    except Exception:
        result.update({"failure_stage": stage, "failure_code": "UNEXPECTED_ERROR"})
    finally:
        if task_id and record.get("status") not in {"succeeded", "failed", "cancelled", "expired"}:
            try:
                if record.get("status") in SUSPENDED:
                    result.update({"status": "blocked", "stop_reason": "task_suspended_for_operator_review"})
                    result["cleanup"] = "left_suspended_for_operator_review"
                else:
                    _mcp(client, mcp_headers, control, "browser_task_cancel", {"task_id": task_id})
                    result["cleanup"] = "cancel_requested"
            except Exception:
                result["cleanup"] = "cancel_unconfirmed"
                result.update({"status": "blocked", "stop_reason": "cancel_unconfirmed"})
    result["scenario_elapsed_ms"] = max(0, round((time.monotonic() - started) * 1000))
    return result


def main() -> int:
    if os.environ.get("APP_ENV", "").lower() != "test" or os.environ.get("READINESS_ACTION_TRACE") != "1":
        raise RuntimeError("P0 diagnostics require APP_ENV=test and READINESS_ACTION_TRACE=1")
    global CONTROL, WORKER
    CONTROL, WORKER = _validate_internal_urls(os.environ.get("DEMOQA_CONTROL_URL", CONTROL),
                                               os.environ.get("BROWSER_WORKER_URL", WORKER))
    requested = os.environ.get("READINESS_P0_SCENARIOS", "checkbox,web_table")
    names = requested.split(",")
    if not names or len(names) > 4 or len(set(names)) != len(names) or set(names) - SCENARIOS.keys():
        raise RuntimeError("READINESS_P0_SCENARIOS must contain up to four known scenarios")
    worker_key_path = os.environ.get("WORKER_API_KEY_FILE", "/run/demoqa-secrets/key")
    worker_key = Path(worker_key_path).read_text(encoding="utf-8").strip()
    mcp_key = (Path(os.environ.get("BROWSER_MCP_DATA_DIR", str(DATA_DIR))) / "mcp-token.txt").read_text(encoding="utf-8").strip()
    if not worker_key or not mcp_key:
        raise RuntimeError("disposable stack credentials are unavailable")
    mcp_headers = {"Authorization": "Bearer " + mcp_key, "Accept": "application/json, text/event-stream"}
    worker_headers = {"Authorization": "Bearer " + worker_key}
    results = []
    with httpx.Client(timeout=45, trust_env=False) as client:
        for name in names:
            results.append(run_scenario(client, mcp_headers, worker_headers, name, SCENARIOS[name]))
            print(json.dumps(results[-1], ensure_ascii=False), flush=True)
            _write_report(results, len(names), final=False)
            if results[-1].get("status") == "blocked" or results[-1].get("cleanup") == "cancel_unconfirmed":
                break
            try:
                worker_state = client.get(WORKER + "/status", headers=worker_headers)
                idle = worker_state.status_code == 200 and _worker_idle(worker_state.json())
            except Exception:
                idle = False
            if not idle:
                results[-1].update({"status": "blocked", "stop_reason": "worker_not_idle_after_scenario"})
                _write_report(results, len(names), final=False)
                break
    report = _write_report(results, len(names), final=True)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report["status"] == "PASS" else 2


def _validate_internal_urls(control: str, worker: str) -> tuple[str, str]:
    """Accept only the known in-network disposable service URLs."""
    if control != "http://localhost:8080" or worker != "http://browser:7070":
        raise RuntimeError("P0 runner accepts only its fixed disposable control and worker URLs")
    for value, host, port in ((control, "localhost", 8080), (worker, "browser", 7070)):
        parsed = urlsplit(value)
        if (parsed.scheme != "http" or parsed.hostname != host or parsed.port != port or parsed.username
                or parsed.password or parsed.path or parsed.query or parsed.fragment):
            raise RuntimeError("P0 runner rejected a non-internal service URL")
    return control, worker


def _worker_idle(status: dict[str, Any]) -> bool:
    return (status.get("browser_started") is True and status.get("control_owner") == "none"
            and status.get("action_in_progress") is not True
            and status.get("import_in_progress") is not True and status.get("context_count") == 1
            and status.get("page_count") == 1 and status.get("recovery_state") == "idle"
            and status.get("recovery_needed") is not True and status.get("resource_violation") is not True)


def _write_report(results: list[dict[str, Any]], requested_count: int, *, final: bool) -> dict[str, Any]:
    if any(item.get("status") == "blocked" for item in results):
        status = "BLOCKED"
    elif any(item.get("status") != "passed" for item in results):
        status = "FAIL"
    elif final and len(results) == requested_count:
        status = "PASS"
    else:
        status = "IN_PROGRESS"
    report = {"schema": "readiness-p0/v1", "status": status,
              "requested_scenarios": requested_count, "completed_scenarios": len(results),
              "viewport": {"width": 1440, "height": 900}, "scenarios": results,
              "raw_model_output_recorded": False, "typed_values_recorded": False}
    output = os.environ.get("READINESS_P0_OUTPUT")
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    raise SystemExit(main())
