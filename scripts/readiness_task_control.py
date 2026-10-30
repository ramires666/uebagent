"""Bounded live MCP task idempotency/cancel and worker bearer audit.

Run only inside a disposable control container paired with its disposable
worker. The script never creates browser contexts or pages itself.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx

CONTROL_URL = "http://127.0.0.1:8080"
DEFAULT_WORKER_URL = "http://browser:7070"
TASK_CANCEL_ACCEPTABLE = {"cancelled", "recovering"}
MAX_WAIT_S = 12.0
STABILITY_S = 2.0


class ProbeError(RuntimeError):
    def __init__(self, step: str, code: str):
        super().__init__(f"{step}: {code}")
        self.step, self.code = step, code


def validate_environment(confirmed: bool, environ: dict[str, str] | None = None) -> tuple[str, Path, Path]:
    env = os.environ if environ is None else environ
    if not confirmed:
        raise ValueError("explicit_disposable_confirmation_required")
    if env.get("APP_ENV", "").lower() != "test" or env.get("READINESS_DISPOSABLE") != "1":
        raise ValueError("disposable_test_markers_required")
    worker_url = env.get("BROWSER_WORKER_URL", DEFAULT_WORKER_URL)
    parsed = urlsplit(worker_url)
    if (parsed.scheme != "http" or parsed.hostname not in {"browser", "127.0.0.1", "localhost"}
            or parsed.port != 7070 or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
        raise ValueError("worker_url_must_be_known_internal_service")
    data_dir = Path(env.get("BROWSER_MCP_DATA_DIR", "/app/runtime/control-data"))
    worker_key_path = Path(env.get("WORKER_API_KEY_FILE", ""))
    if not worker_key_path.is_file() or not (data_dir / "mcp-token.txt").is_file():
        raise ValueError("disposable_token_files_missing")
    return worker_url.rstrip("/"), worker_key_path, data_dir / "mcp-token.txt"


def read_key(path: Path) -> str:
    value = path.read_text(encoding="utf-8").strip()
    if not value or "\n" in value or "\r" in value:
        raise ValueError("invalid_token_file")
    return value


def mcp_result(response: httpx.Response) -> dict:
    if response.status_code != 200:
        raise ProbeError("mcp_http", f"http_{response.status_code}")
    try:
        envelope = response.json()
    except ValueError as exc:
        raise ProbeError("mcp_parse", "invalid_json") from exc
    if isinstance(envelope, dict) and "error" in envelope:
        error = envelope.get("error") or {}
        code = error.get("code") if isinstance(error, dict) else None
        raise ProbeError("mcp_call", safe_code(code) or "jsonrpc_error")
    result = envelope.get("result", {}) if isinstance(envelope, dict) else {}
    if not isinstance(result, dict):
        raise ProbeError("mcp_parse", "invalid_result")
    if result.get("isError"):
        blocks = result.get("content", [])
        code = "tool_error"
        if blocks and isinstance(blocks[0], dict):
            match = re.search(r"\b[A-Z][A-Z0-9_]{2,48}\b", str(blocks[0].get("text", "")))
            if match:
                code = safe_code(match.group(0)) or code
        raise ProbeError("mcp_call", code)
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    blocks = result.get("content", [])
    if blocks and isinstance(blocks[0], dict):
        try:
            value = json.loads(blocks[0].get("text", ""))
            if isinstance(value, dict):
                return value
        except (ValueError, TypeError):
            pass
    raise ProbeError("mcp_parse", "missing_tool_result")


def safe_code(code: object) -> str | None:
    if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", code):
        return code
    return None


class LiveClient:
    def __init__(self, worker_url: str, worker_key: str, mcp_key: str):
        self.client = httpx.Client(timeout=5.0, trust_env=False, follow_redirects=False)
        self.worker_url = worker_url
        self.worker_key = worker_key
        self.mcp_headers = {"Authorization": f"Bearer {mcp_key}", "Accept": "application/json, text/event-stream"}

    def worker_request(self, method: str, path: str, *, auth: str | None, json_body: dict | None = None) -> httpx.Response:
        headers = {"Authorization": f"Bearer {auth}"} if auth is not None else {}
        try:
            return self.client.request(method, self.worker_url + path, headers=headers, json=json_body)
        except httpx.HTTPError as exc:
            raise ProbeError("worker_request", "connection_error") from exc

    def worker_json(self, method: str, path: str, *, json_body: dict | None = None) -> dict:
        response = self.worker_request(method, path, auth=self.worker_key, json_body=json_body)
        if response.status_code != 200:
            raise ProbeError("worker_authenticated_request", f"http_{response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise ProbeError("worker_parse", "invalid_json") from exc
        if not isinstance(data, dict):
            raise ProbeError("worker_parse", "invalid_object")
        return data

    def mcp(self, name: str, args: dict) -> dict:
        try:
            response = self.client.post(CONTROL_URL + "/mcp", headers=self.mcp_headers,
                                        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                              "params": {"name": name, "arguments": args}})
        except httpx.HTTPError as exc:
            raise ProbeError("mcp_request", "connection_error") from exc
        return mcp_result(response)


def event_counts(events_result: dict) -> tuple[int, int]:
    events = events_result.get("events", [])
    if not isinstance(events, list):
        raise ProbeError("task_events", "invalid_events")
    planned = sum(1 for event in events if isinstance(event, dict) and event.get("event_type") == "action.planned")
    return planned, len(events)


def worker_idle_status(client: LiveClient) -> dict:
    status = client.worker_json("GET", "/status")
    if (status.get("context_count") != 1 or status.get("page_count") != 1
            or status.get("control_owner") != "none" or status.get("action_in_progress")
            or status.get("import_in_progress") or status.get("resource_violation") is not False
            or status.get("recovery_needed") is not False or status.get("recovery_state") != "idle"):
        raise ProbeError("worker_idle", "worker_not_idle_or_one_page")
    return status


def auth_state(status: dict) -> tuple:
    """Fields that must not change when worker bearer auth rejects a request."""
    return tuple(status.get(key) for key in (
        "control_epoch", "control_owner", "context_count", "page_count",
        "action_in_progress", "import_in_progress", "resource_violation",
        "recovery_needed", "recovery_state",
    ))


def run_probe(client: LiveClient, *, now=time.monotonic, sleep=time.sleep) -> dict:
    report: dict = {"schema": "readiness-task-control/v1", "status": "FAIL", "checks": {}, "task": {}}
    # Exercise worker auth before any task can cause an action.
    baseline = worker_idle_status(client)
    good_health = client.worker_json("GET", "/health")
    if (not good_health.get("ready") or not good_health.get("browser_started")
            or good_health.get("resource_violation") is not False
            or good_health.get("recovery_needed") is not False
            or good_health.get("recovery_state") != "idle"):
        raise ProbeError("worker_positive_auth", "worker_not_ready")
    report["checks"]["worker_bearer_positive"] = "PASS"
    no_auth = client.worker_request("GET", "/health", auth=None)
    bad_auth = client.worker_request("GET", "/health", auth="readiness-invalid-token")
    no_auth_observe = client.worker_request("POST", "/observe", auth=None)
    bad_auth_observe = client.worker_request("POST", "/observe", auth="readiness-invalid-token")
    rejected = (no_auth.status_code == 401 and bad_auth.status_code == 401
                and no_auth_observe.status_code == 401 and bad_auth_observe.status_code == 401)
    if not rejected:
        raise ProbeError("worker_bearer_negative", "expected_401")
    after_auth = worker_idle_status(client)
    if auth_state(after_auth) != auth_state(baseline):
        raise ProbeError("worker_bearer_negative", "worker_state_changed")
    report["checks"]["worker_bearer_negative"] = "PASS"
    report["worker_auth"] = {"positive_health_ready": True,
                              "unauth_health_status": no_auth.status_code,
                              "wrong_bearer_health_status": bad_auth.status_code,
                              "unauth_observe_status": no_auth_observe.status_code,
                              "wrong_bearer_observe_status": bad_auth_observe.status_code}

    idem = "readiness-cancel-" + uuid.uuid4().hex
    condition = {"kind": "element_visible", "selector": "#visibleAfter", "visible": True}
    payload = {"goal": "Open https://demoqa.com/dynamic-properties, wait for the visible after five seconds button, then finish.",
               "allowed_origins": ["https://demoqa.com"], "idempotency_key": idem,
               "execution_mode": "autonomous", "sync_cookies": False,
               "limits": {"timeout_s": 30, "max_steps": 2}, "success_conditions": [condition]}
    first = client.mcp("browser_task_start", payload)
    task_id = first.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise ProbeError("task_start", "missing_task_id")
    second = client.mcp("browser_task_start", payload)
    if second.get("task_id") != task_id:
        raise ProbeError("idempotency", "task_id_mismatch")
    report["checks"]["mcp_start_idempotency"] = "PASS"
    report["task"]["duplicate_start_same_id"] = True

    deadline = now() + MAX_WAIT_S
    record = first
    planned = 0
    trigger = ""
    while now() < deadline:
        record = client.mcp("browser_task_status", {"task_id": task_id})
        planned, _ = event_counts(client.mcp("browser_task_events", {"task_id": task_id, "limit": 100}))
        status = record.get("status")
        if planned > 0:
            trigger = "first_action_planned"
            break
        if status not in {"queued", "created", "starting", "running"}:
            trigger = "nonqueued_state"
            break
        sleep(0.25)
    if not trigger:
        raise ProbeError("task_start_wait", "12s_trigger_timeout")
    report["task"]["cancel_scope"] = (
        "action_planned_before_cancel; in_flight_dispatch_not_proven" if planned > 0
        else "cancelled_before_first_action; in_flight_cancel_not_tested"
    )
    report["task"]["g4_scope"] = "bounded_task_control_smoke_only; restart_concurrency_and_uncertain_dispatch_not_covered"

    cancel_error = None
    try:
        cancel_result = client.mcp("browser_task_cancel", {"task_id": task_id})
        cancel_result_status = cancel_result.get("status")
    except ProbeError as exc:
        cancel_error = exc.code
        cancel_result_status = None
    cancel_deadline = now() + 8.0
    record = client.mcp("browser_task_status", {"task_id": task_id})
    status_after_cancel = record.get("status")
    while status_after_cancel not in TASK_CANCEL_ACCEPTABLE and now() < cancel_deadline:
        sleep(0.25)
        record = client.mcp("browser_task_status", {"task_id": task_id})
        status_after_cancel = record.get("status")
    if status_after_cancel not in TASK_CANCEL_ACCEPTABLE:
        # A task that reached a terminal state before cancellation is not
        # evidence that cancel stopped it.
        raise ProbeError("task_cancel", safe_code(cancel_error) or "unexpected_status")
    try:
        second_cancel = client.mcp("browser_task_cancel", {"task_id": task_id})
        repeat_cancel_status = second_cancel.get("status")
        repeat_cancel_error = None
    except ProbeError as exc:
        repeat_cancel_status = None
        repeat_cancel_error = exc.code
        if exc.code != "INVALID_TRANSITION":
            raise

    events_after = client.mcp("browser_task_events", {"task_id": task_id, "limit": 100})
    planned_after, event_total_after = event_counts(events_after)
    planned_at_stability_start = planned_after
    stable_status = None
    stable = True
    end_stability = now() + STABILITY_S
    while now() < end_stability:
        current = client.mcp("browser_task_status", {"task_id": task_id})
        current_planned, _ = event_counts(client.mcp("browser_task_events", {"task_id": task_id, "limit": 100}))
        if current.get("status") != status_after_cancel or current_planned != planned_after:
            stable = False
        stable_status = current.get("status")
        sleep(min(0.25, max(0, end_stability - now())))
    if not stable or planned_after < planned:
        raise ProbeError("cancel_stability", "post_cancel_state_changed")
    if stable_status not in TASK_CANCEL_ACCEPTABLE:
        raise ProbeError("cancel_stability", "task_not_stable_terminal")
    # INVALID_TRANSITION on repeat cancel is an allowed terminal-state effect;
    # it is not reported as an idempotent cancel response.
    report["task"].update({"task_id_present": True, "trigger": trigger, "first_action_planned_count": planned,
                           "planned_count_after_stability": planned_after, "event_count_after_stability": event_total_after,
                           "status_after_cancel": status_after_cancel, "stable_final_status": stable_status,
                           "cancel_result_status": cancel_result_status,
                           "cancel_first_call_code": cancel_error,
                           "repeat_cancel_result_status": repeat_cancel_status,
                           "repeat_cancel_code": repeat_cancel_error,
                           "repeat_cancel_is_idempotent_claim": False,
                           "no_additional_actions_during_2s": stable and planned_after == planned_at_stability_start})
    report["checks"]["cancel_terminal_and_stable"] = "PASS"
    # Worker must be quiescent after cancellation; allow recovery to settle.
    idle_deadline = now() + 8.0
    last_status = None
    while now() < idle_deadline:
        try:
            last_status = worker_idle_status(client)
            break
        except ProbeError:
            sleep(0.25)
    if last_status is None:
        raise ProbeError("worker_post_cancel_idle", "worker_not_idle_or_one_page")
    final_health = client.worker_json("GET", "/health")
    if (not final_health.get("ready") or not final_health.get("browser_started")
            or final_health.get("context_count") != 1 or final_health.get("page_count") != 1
            or final_health.get("resource_violation") is not False
            or final_health.get("recovery_needed") is not False
            or final_health.get("recovery_state") != "idle"):
        raise ProbeError("worker_post_cancel_health", "worker_not_ready")
    report["checks"]["worker_post_cancel_idle"] = "PASS"
    report["worker"] = {"ready": bool(final_health.get("ready")), "browser_started": bool(final_health.get("browser_started")),
                        "control_owner": last_status.get("control_owner"),
                        "context_count": last_status.get("context_count"), "page_count": last_status.get("page_count"),
                        "action_in_progress": bool(last_status.get("action_in_progress")),
                        "import_in_progress": bool(last_status.get("import_in_progress")),
                        "resource_violation": bool(final_health.get("resource_violation")),
                        "recovery_needed": bool(final_health.get("recovery_needed")),
                        "recovery_state": final_health.get("recovery_state")}
    report["status"] = "PASS" if all(value == "PASS" for value in report["checks"].values()) else "FAIL"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--i-confirm-disposable", action="store_true", help="confirm this is a disposable test control/worker pair")
    parser.add_argument("--output", default="/tmp/readiness-task-control.json", help="path for sanitized full JSON report")
    args = parser.parse_args(argv)
    try:
        worker_url, worker_key_path, mcp_key_path = validate_environment(args.i_confirm_disposable)
        client = LiveClient(worker_url, read_key(worker_key_path), read_key(mcp_key_path))
        try:
            report = run_probe(client)
        finally:
            client.client.close()
    except (ValueError, ProbeError) as exc:
        report = {"schema": "readiness-task-control/v1", "status": "FAIL",
                  "error": {"step": getattr(exc, "step", "guard"), "code": getattr(exc, "code", str(exc))},
                  "checks": {}, "task": {}}
    except Exception:
        report = {"schema": "readiness-task-control/v1", "status": "FAIL",
                  "error": {"step": "unexpected", "code": "internal_error"}, "checks": {}, "task": {}}
    output = Path(args.output)
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        report = {"schema": "readiness-task-control/v1", "status": "FAIL",
                  "error": {"step": "report_write", "code": "write_failed"}, "checks": {}, "task": {}}
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
