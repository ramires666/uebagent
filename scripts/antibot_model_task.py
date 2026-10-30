"""Run one bounded, read-only DemoQA model task in the disposable antibot control."""
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
WORKER_URL = "http://browser:7070"
PUBLIC_ORIGIN = "https://demoqa.com"
EXPECTED_ASSIGNMENT = "antibot-2026-10-31-direct"
EXPECTED_ROUTE_GENERATION = 1
TASK_TIMEOUT_S = 300
TASK_MAX_STEPS = 12
POLL_TIMEOUT_S = 330
TERMINAL = {"succeeded", "failed", "cancelled", "expired", "waiting_human", "waiting_approval", "recovering"}
FINISHED = {"succeeded", "failed", "cancelled", "expired", "recovering"}
READ_ONLY_ACTIONS = {"navigate", "click", "history_back", "wait", "scroll", "mouse_move", "finish", "ask_user"}


class ProbeError(RuntimeError):
    def __init__(self, step: str, code: str):
        super().__init__(f"{step}: {code}")
        self.step, self.code = step, code


def validate_environment(confirmed: bool, environ: dict[str, str] | None = None) -> tuple[Path, Path]:
    env = os.environ if environ is None else environ
    if not confirmed:
        raise ValueError("explicit_disposable_confirmation_required")
    if (env.get("APP_ENV", "").lower() != "test" or env.get("READINESS_DISPOSABLE") != "1"
            or env.get("BROWSER_MCP_ORIGIN") != "http://localhost:18081"):
        raise ValueError("disposable_test_control_markers_required")
    worker_url = env.get("BROWSER_WORKER_URL", WORKER_URL)
    parsed = urlsplit(worker_url)
    if (worker_url != WORKER_URL or parsed.scheme != "http" or parsed.hostname != "browser"
            or parsed.port != 7070 or parsed.path or parsed.query or parsed.fragment):
        raise ValueError("worker_url_must_be_antibot_service")
    data_dir = Path(env.get("BROWSER_MCP_DATA_DIR", "/app/runtime/control-data"))
    worker_key_path = Path(env.get("WORKER_API_KEY_FILE", ""))
    mcp_key_path = data_dir / "mcp-token.txt"
    if not worker_key_path.is_file() or not mcp_key_path.is_file():
        raise ValueError("disposable_token_files_missing")
    return worker_key_path, mcp_key_path


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
    if not isinstance(envelope, dict) or "error" in envelope:
        raise ProbeError("mcp_call", "jsonrpc_error")
    result = envelope.get("result")
    if not isinstance(result, dict) or result.get("isError"):
        raise ProbeError("mcp_call", "tool_error")
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


class Client:
    def __init__(self, worker_key: str, mcp_key: str):
        self.http = httpx.Client(timeout=10, trust_env=False, follow_redirects=False)
        self.worker_headers = {"Authorization": f"Bearer {worker_key}"}
        self.mcp_headers = {"Authorization": f"Bearer {mcp_key}",
                            "Accept": "application/json, text/event-stream"}
        self.owned_task_id: str | None = None

    def worker_status(self) -> dict:
        try:
            response = self.http.get(WORKER_URL + "/status", headers=self.worker_headers)
            response.raise_for_status()
            value = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProbeError("worker_status", "request_failed") from exc
        if not isinstance(value, dict):
            raise ProbeError("worker_status", "invalid_response")
        return value

    def worker_verify(self, conditions: list[dict]) -> dict:
        try:
            response = self.http.post(WORKER_URL + "/verify", headers=self.worker_headers,
                                      json={"conditions": conditions})
            response.raise_for_status()
            value = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProbeError("worker_verify", "request_failed") from exc
        if not isinstance(value, dict):
            raise ProbeError("worker_verify", "invalid_response")
        return value

    def ensure_no_active_tasks(self) -> None:
        # Local operator session is explicitly enabled for the disposable test
        # control; this read-only listing prevents overlap with any task we do
        # not own. The session token remains in the HTTP cookie jar only.
        try:
            session = self.http.get(CONTROL_URL + "/api/v1/operator/session")
            session.raise_for_status()
            session_data = session.json()
            if not isinstance(session_data, dict) or session_data.get("local_ui") is not True:
                raise ProbeError("task_list", "local_operator_session_unavailable")
            response = self.http.get(CONTROL_URL + "/api/v1/tasks")
            response.raise_for_status()
            value = response.json()
        except httpx.HTTPError as exc:
            raise ProbeError("task_list", "request_failed") from exc
        except ValueError as exc:
            raise ProbeError("task_list", "invalid_response") from exc
        tasks = value.get("tasks") if isinstance(value, dict) else None
        if not isinstance(tasks, list):
            raise ProbeError("task_list", "invalid_response")
        active = {"queued", "running", "pause_requested", "paused", "waiting_human",
                  "waiting_approval", "recovering"}
        if any(isinstance(task, dict) and task.get("status") in active for task in tasks):
            raise ProbeError("task_list", "another_task_is_active")

    def mcp(self, name: str, args: dict) -> dict:
        try:
            response = self.http.post(CONTROL_URL + "/mcp", headers=self.mcp_headers,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                      "params": {"name": name, "arguments": args}})
        except httpx.HTTPError as exc:
            raise ProbeError("mcp_request", "connection_error") from exc
        return mcp_result(response)

    def cancel_owned_task_if_active(self) -> None:
        task_id = self.owned_task_id
        if not task_id:
            return
        try:
            record = self.mcp("browser_task_status", {"task_id": task_id})
            if record.get("status") not in FINISHED:
                self.mcp("browser_task_cancel", {"task_id": task_id})
        except Exception:
            # Best-effort cleanup is limited to the ID created by this run.
            pass


def validate_worker(status: dict) -> None:
    if (status.get("browser_started") is not True or status.get("control_owner") != "none"
            or status.get("action_in_progress") is not False or status.get("import_in_progress") is not False
            or status.get("resource_violation") is not False or status.get("recovery_needed") is not False
            or status.get("recovery_state") != "idle" or status.get("context_count") != 1
            or status.get("page_count") != 1 or status.get("width") != 1440 or status.get("height") != 900
            or status.get("proxy_assignment_id") != EXPECTED_ASSIGNMENT
            or status.get("route_generation") != EXPECTED_ROUTE_GENERATION):
        raise ProbeError("worker_guard", "worker_not_idle_on_expected_route")


def action_trace(client: Client, task_id: str) -> tuple[list[str], bool, bool]:
    actions: list[str] = []
    after = 0
    while True:
        result = client.mcp("browser_task_events", {"task_id": task_id, "after_event_id": after, "limit": 100})
        events = result.get("events")
        if not isinstance(events, list):
            raise ProbeError("task_events", "invalid_events")
        for event in events:
            if not isinstance(event, dict):
                continue
            event_id = event.get("event_id")
            if type(event_id) is int:
                after = max(after, event_id)
            if event.get("event_type") == "action.applied":
                payload = event.get("payload")
                kind = payload.get("kind") if isinstance(payload, dict) else None
                if isinstance(kind, str) and re.fullmatch(r"[a-z_]{1,32}", kind):
                    actions.append(kind)
        if len(events) < 100:
            break
    click_index = next((i for i, kind in enumerate(actions) if kind == "click"), None)
    back_index = next((i for i, kind in enumerate(actions) if kind == "history_back"), None)
    trace_ok = click_index is not None and back_index is not None and click_index < back_index
    write_violation = not set(actions) <= READ_ONLY_ACTIONS
    return actions, trace_ok, write_violation


def task_payload() -> dict:
    return {
        "goal": (
            "Use the guest public DemoQA site only. Open https://demoqa.com/ and confirm the home page is ready. "
            "On that page, find and click the visible Elements section (do not navigate directly to its URL). "
            "Read the section heading, then use browser history back and confirm the DemoQA home page is visible. "
            "Do not fill or submit forms, sign in, or interact with any other controls. If a challenge or access "
            "denial blocks the task, stop and ask for help. Finish only after returning home."
        ),
        "allowed_origins": [PUBLIC_ORIGIN],
        "idempotency_key": "antibot-model-demoqa-" + uuid.uuid4().hex,
        "profile_id": "default",
        "success_conditions": [{"kind": "url_is", "url": PUBLIC_ORIGIN + "/"}],
        "sync_cookies": False,
        "execution_mode": "autonomous",
        "limits": {"timeout_s": TASK_TIMEOUT_S, "max_steps": TASK_MAX_STEPS},
    }


def run(client: Client, *, now=time.monotonic, sleep=time.sleep) -> dict:
    report = {"schema": "antibot-model-task/v1", "status": "FAIL", "task": {}, "error": None}
    client.ensure_no_active_tasks()
    validate_worker(client.worker_status())
    created = client.mcp("browser_task_start", task_payload())
    task_id = created.get("task_id")
    if not isinstance(task_id, str) or not re.fullmatch(r"[0-9a-f-]{36}", task_id):
        raise ProbeError("task_start", "missing_task_id")
    client.owned_task_id = task_id
    report["task"]["id"] = task_id
    milestone_seen = False
    challenge_seen = False
    deadline = now() + POLL_TIMEOUT_S
    record = created
    timed_out = False
    while now() < deadline:
        record = client.mcp("browser_task_status", {"task_id": task_id})
        browser = client.worker_status()
        if browser.get("url") == PUBLIC_ORIGIN + "/elements":
            conditions = ([{"kind": "text_present", "text": marker}
                           for marker in ("Verify you are human", "Access denied", "Robot Check",
                                          "unusual traffic", "Request blocked", "Security Measure")]
                          + [{"kind": "text_present", "text": "Elements"},
                             {"kind": "text_present", "text": "Text Box"}])
            check = client.worker_verify([{"kind": "url_is", "url": PUBLIC_ORIGIN + "/elements"}, *conditions])
            checks = check.get("checks")
            if isinstance(checks, list) and len(checks) == len(conditions) + 1:
                challenge_seen = challenge_seen or any(
                    isinstance(item, dict) and item.get("passed") is True for item in checks[1:7])
                milestone_seen = milestone_seen or (
                    isinstance(checks[0], dict) and checks[0].get("passed") is True
                    and all(isinstance(checks[index], dict) and checks[index].get("passed") is False
                        for index in range(1, 7))
                    and isinstance(checks[7], dict) and checks[7].get("passed") is True
                    and isinstance(checks[8], dict) and checks[8].get("passed") is True)
        status = record.get("status")
        if status == "waiting_human" and not challenge_seen:
            markers = ("Verify you are human", "Access denied", "Robot Check",
                       "unusual traffic", "Request blocked", "Security Measure")
            challenge_check = client.worker_verify([{"kind": "text_present", "text": marker}
                                                    for marker in markers])
            checks = challenge_check.get("checks")
            if isinstance(checks, list) and len(checks) == len(markers):
                challenge_seen = any(isinstance(item, dict) and item.get("passed") is True for item in checks)
        if status in TERMINAL:
            break
        sleep(1.0)
    else:
        timed_out = True
        # This is the only task ID this invocation created and may cancel.
        cancelled = client.mcp("browser_task_cancel", {"task_id": task_id})
        report["task"]["cancel_requested"] = cancelled.get("status") in {"cancelled", "recovering"}
        record = client.mcp("browser_task_status", {"task_id": task_id})

    actions, trace_ok, write_violation = action_trace(client, task_id)
    verified = record.get("result", {}).get("verified") is True if isinstance(record.get("result"), dict) else False
    status = record.get("status")
    if status == "waiting_human" and challenge_seen:
        outcome = "BLOCKED"
    elif status == "waiting_approval":
        outcome = "PARTIAL"
    elif status == "succeeded" and verified and trace_ok and milestone_seen and not write_violation and not timed_out:
        outcome = "PASS"
    elif status == "succeeded" and write_violation:
        outcome = "FAIL"
    elif status == "succeeded":
        outcome = "PARTIAL"
    elif status == "waiting_human":
        outcome = "PARTIAL"
    elif status in {"running", "queued", "created", "starting", "recovering"} or timed_out:
        outcome = "PARTIAL"
    else:
        outcome = "FAIL"
    report["status"] = outcome
    report["task"].update({
        "status": status if status in TERMINAL | {"queued", "created", "starting", "running"} else "unknown",
        "phase": record.get("phase") if isinstance(record.get("phase"), str) else None,
        "step_count": record.get("step_count") if type(record.get("step_count")) is int else None,
        "verified": verified,
        "final_home_url_verified": verified,
        "applied_action_kinds": actions,
        "trace_has_click_then_history_back": trace_ok,
        "elements_url_and_content_independently_observed": milestone_seen,
        "read_only_actions_only": not write_violation,
        "task_spec_profile_id": "default",
        "browser_runtime_profile_hint": os.environ.get("ANTIBOT_PROFILE")
            if os.environ.get("ANTIBOT_PROFILE") in {"p1", "p2"} else None,
        "summary_present": bool(isinstance(record.get("result"), dict)
                                 and isinstance(record["result"].get("summary"), str)),
    })
    if status == "failed":
        error = record.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        report["error"] = code if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", code) else "TASK_FAILED"
    elif status == "waiting_human":
        error = record.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        safe_code = code if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", code) else None
        report["error"] = safe_code or ("CHALLENGE_REQUIRES_HANDOFF" if challenge_seen
                                         else "MANUAL_HANDOFF_REQUIRED")
    elif not verified and status == "succeeded":
        report["error"] = "FINAL_URL_NOT_VERIFIED"
    elif not trace_ok and status == "succeeded":
        report["error"] = "CLICK_BACK_TRACE_MISSING"
    elif not milestone_seen and status == "succeeded":
        report["error"] = "ELEMENTS_MILESTONE_NOT_OBSERVED"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--i-confirm-disposable", action="store_true",
                        help="confirm the disposable antibot test control/worker pair")
    parser.add_argument("--output", type=Path, default=Path("/tmp/antibot-artifacts/demoqa/model-task.json"),
                        help="sanitized report destination (default: /tmp/antibot-artifacts/demoqa/model-task.json)")
    args = parser.parse_args(argv)
    try:
        worker_key_path, mcp_key_path = validate_environment(args.i_confirm_disposable)
        client = Client(read_key(worker_key_path), read_key(mcp_key_path))
        try:
            report = run(client)
        finally:
            client.cancel_owned_task_if_active()
            client.http.close()
    except (ValueError, ProbeError) as exc:
        code = getattr(exc, "code", None)
        if not isinstance(code, str) or not re.fullmatch(r"[a-zA-Z0-9_:-]{1,80}", code):
            code = "configuration_rejected" if isinstance(exc, ValueError) else "probe_failed"
        report = {"schema": "antibot-model-task/v1", "status": "FAIL",
                  "error": {"step": getattr(exc, "step", "guard"), "code": code}}
    except Exception:
        report = {"schema": "antibot-model-task/v1", "status": "FAIL",
                  "error": {"step": "unexpected", "code": "internal_error"}}
    try:
        args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        report = {"schema": "antibot-model-task/v1", "status": "FAIL",
                  "error": {"step": "report_write", "code": "write_failed"}}
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
