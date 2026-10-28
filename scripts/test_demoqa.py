"""Run serial synthetic DemoQA tasks through an isolated MCP and real Fara.

Run only inside a disposable control container paired with a disposable worker.
No production tokens, volumes, or user profiles may be mounted. This script
never creates browser contexts, executes browser JS, or changes Docker services.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from collections import Counter
from pathlib import Path

import httpx

ORIGIN = "https://demoqa.com"
CONTROL = os.environ.get("DEMOQA_CONTROL_URL", "http://localhost:8080")
WORKER = os.environ.get("BROWSER_WORKER_URL", "http://browser:7070")
DATA_DIR = Path(os.environ.get("BROWSER_MCP_DATA_DIR", "/app/runtime/control-data"))
WORKER_KEY = Path(os.environ["WORKER_API_KEY_FILE"]).read_text(encoding="utf-8").strip()
MCP_KEY = (DATA_DIR / "mcp-token.txt").read_text(encoding="utf-8").strip()
TERMINAL = {"succeeded", "failed", "cancelled", "expired", "waiting_human", "waiting_approval", "recovering", "paused"}

# All data is synthetic. Each positive assertion describes a result produced by
# the page, not merely a label already present before taking the action.
SCENARIOS = [
    ("text_box", "/text-box", "Fill the text box form using Full Name Test Person, Email test@example.com, Current Address 42 Example Street, Permanent Address 42 Example Street. Submit and finish once the output appears.", [{"kind":"field_equals","selector":"#userName","value":"Test Person"}, {"kind":"field_equals","selector":"#userEmail","value":"test@example.com"}, {"kind":"field_equals","selector":"#currentAddress","value":"42 Example Street"}, {"kind":"field_equals","selector":"textarea#permanentAddress","value":"42 Example Street"}, {"kind":"element_text_contains","selector":"#output #name","text":"Test Person"}, {"kind":"element_text_contains","selector":"#output #email","text":"test@example.com"}, {"kind":"element_text_contains","selector":"#output #currentAddress","text":"42 Example Street"}, {"kind":"element_text_contains","selector":"#output #permanentAddress","text":"42 Example Street"}]),
    ("invalid_email", "/text-box", "Fill Full Name Test Person, Email exactly test@example.test, Current Address 42 Example Street, and Permanent Address 42 Example Street. Click Submit once. Do not replace, reformat, or suggest another email. If the page rejects that exact address, stop and report the validation; finish when the email remains unchanged and no submitted output appears.", [{"kind":"field_equals","selector":"#userEmail","value":"test@example.test"}, {"kind":"element_visible","selector":"#output #email","visible":False}]),
    ("checkbox", "/checkbox", "Expand Home and leave its children visible. Select the Desktop checkbox and finish when the selected result appears.", [{"kind":"element_checked","selector":"#tree-node-desktop","checked":True}, {"kind":"element_text_contains","selector":"#result","text":"desktop"}]),
    ("radio", "/radio-button", "Choose the Yes radio button and finish when the selected result is displayed.", [{"kind":"element_checked","selector":"#yesRadio","checked":True}, {"kind":"element_text_contains","selector":"p.mt-3","text":"Yes"}]),
    ("double_click", "/buttons", "Double click the Double Click Me button and finish after its confirmation appears.", [{"kind":"element_text_contains","selector":"#doubleClickMessage","text":"You have done a double click"}]),
    ("single_click", "/buttons", "Click the Click Me button and finish after its confirmation appears.", [{"kind":"element_text_contains","selector":"#dynamicClickMessage","text":"You have done a dynamic click"}]),
    ("web_table", "/webtables", "Add a new row to the web table: first name Testperson, last name Example, email test@example.com, age 30, salary 12345, department Testing. Submit and finish when the new row appears with all these values.", [{"kind":"element_text_contains","selector":"table tbody tr:has-text('test@example.com'), .rt-tr-group:has-text('test@example.com')","text":"Testperson"}, {"kind":"element_text_contains","selector":"table tbody tr:has-text('test@example.com'), .rt-tr-group:has-text('test@example.com')","text":"Example"}, {"kind":"element_text_contains","selector":"table tbody tr:has-text('test@example.com'), .rt-tr-group:has-text('test@example.com')","text":"30"}, {"kind":"element_text_contains","selector":"table tbody tr:has-text('test@example.com'), .rt-tr-group:has-text('test@example.com')","text":"12345"}, {"kind":"element_text_contains","selector":"table tbody tr:has-text('test@example.com'), .rt-tr-group:has-text('test@example.com')","text":"Testing"}]),
    ("dynamic", "/dynamic-properties", "Wait for the visible after five seconds button to appear and finish.", [{"kind":"element_visible","selector":"#visibleAfter","visible":True}]),
    ("notify_then_click", "/buttons", "First call ask_user_question to notify the user that human assistance might be useful, but do not wait for a reply; autonomous mode is explicitly authorized. Then single-click Click Me and finish after confirmation.", [{"kind":"element_text_contains","selector":"#dynamicClickMessage","text":"You have done a dynamic click"}]),
]


def main():
    headers = {"Authorization": "Bearer " + MCP_KEY, "Accept": "application/json, text/event-stream"}
    worker_headers = {"Authorization": "Bearer " + WORKER_KEY}
    with httpx.Client(timeout=45, trust_env=False) as client:
        def worker(method, path, data=None):
            r = client.request(method, WORKER + path, headers=worker_headers, json=data)
            r.raise_for_status()
            return r.json()

        def mcp(name, args):
            r = client.post(CONTROL + "/mcp", headers=headers,
                            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
            r.raise_for_status()
            payload = r.json()
            if "error" in payload or payload.get("result", {}).get("isError"):
                raise RuntimeError("MCP tool returned an error")
            result = payload["result"]
            if isinstance(result.get("structuredContent"), dict):
                return result["structuredContent"]
            return json.loads(result["content"][0]["text"])

        def demoqa_metadata():
            return worker("GET", "/diagnostics/demoqa-inputs")

        def desktop_checkbox(metadata):
            inputs = [entry for entry in metadata.get("inputs", []) if entry.get("tag") == "input"
                      and entry.get("type") == "checkbox"
                      and any(" ".join(str(label).split()).casefold() == "desktop"
                              for label in entry.get("labels", []))]
            roles = [entry for entry in metadata.get("role_checkboxes", [])
                     if entry.get("visible") is True
                     and (" ".join(str(entry.get("static_label", "")).split()).casefold() == "desktop"
                          or " ".join(str(entry.get("aria_label", "")).split()).casefold() == "select desktop"
                          or " ".join(str(entry.get("accessible_label", "")).split()).casefold() == "select desktop")]
            return inputs, roles

        def checkbox_metadata_summary(metadata):
            fields = ("tag", "id", "role", "aria_label", "aria_checked", "checked", "visible",
                      "class_names", "static_label", "accessible_label", "selector")
            entries = metadata.get("role_checkboxes", [])
            return [{key: entry.get(key) for key in fields if key in entry} for entry in entries[:64]
                    if isinstance(entry, dict)]

        health = worker("GET", "/health")
        status = worker("GET", "/status")
        if not health.get("ready") or status.get("control_owner") != "none" or status.get("context_count") != 1 or status.get("page_count") != 1:
            raise RuntimeError("disposable worker not ready, idle, or limited to one page")
        catalog = client.post(CONTROL + "/mcp", headers=headers,
                              json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        catalog.raise_for_status()
        catalog_payload = catalog.json()
        if "error" in catalog_payload or not catalog_payload.get("result", {}).get("tools"):
            raise RuntimeError("disposable MCP endpoint did not return its tool catalog")
        if os.environ.get("DEMOQA_DIAGNOSTIC_ONLY") == "1":
            preflight = worker("GET", "/status")
            if (preflight.get("control_owner") != "none" or preflight.get("action_in_progress")
                    or preflight.get("import_in_progress") or preflight.get("context_count") != 1
                    or preflight.get("page_count") != 1):
                raise RuntimeError("diagnostic probe requires an idle one-page test worker")
            gate = worker("POST", "/gate", {"owner": "human"})
            try:
                nav = worker("POST", "/manual/execute", {
                    "control_epoch": gate["control_epoch"], "allowed_origins": [ORIGIN],
                    "action": {"kind": "navigate", "url": ORIGIN + "/webtables"},
                })
                if nav.get("outcome") != "applied":
                    raise RuntimeError("font probe navigation was not confirmed")
            finally:
                worker("POST", "/gate", {"owner": "none"})
            time.sleep(1)
            print(json.dumps({"scenario": "font_probe_phase", "phase": "one_second"}), flush=True)
            time.sleep(4)
            print(json.dumps({"scenario": "font_probe_phase", "phase": "five_seconds"}), flush=True)
            font_metadata = worker("GET", "/diagnostics/demoqa-fonts")
            print(json.dumps({"scenario": "font_probe", "status": "passed", "diagnostic_only": True,
                              "font_metadata": font_metadata,
                              "screenshot_files": ["font-probe-1s.png", "font-probe-5s.png"]}, ensure_ascii=False), flush=True)
            return
        results = []
        requested = os.environ.get("DEMOQA_CASES", "")
        selected = set(requested.split(",")) if requested else {s[0] for s in SCENARIOS if s[0] != "notify_then_click"}
        if not selected or selected - {s[0] for s in SCENARIOS}:
            raise RuntimeError("unknown or empty DEMOQA_CASES")
        for name, path, goal, conditions in SCENARIOS:
            if name not in selected: continue
            start = time.monotonic()
            extended = os.environ.get("DEMOQA_EXTENDED_COMPLEX") == "1" and name in {"text_box", "checkbox", "web_table"}
            timeout_s, max_steps = (180, 40) if extended else (120, 30)
            item = {"scenario": name, "status": "blocked", "steps": 0, "action_kinds": {}, "model_action_observed": False,
                    "timeout_s": timeout_s, "max_steps": max_steps}
            task_id = None
            record = None
            scenario_conditions = conditions
            try:
                preflight = worker("GET", "/status")
                if (preflight.get("context_count"), preflight.get("page_count")) != (1, 1):
                    item["cleanup"] = "resource_limit_violation"
                    raise RuntimeError("test worker does not have one context and page")
                gate = worker("POST", "/gate", {"owner": "human"})
                try:
                    nav = worker("POST", "/manual/execute", {"control_epoch": gate["control_epoch"], "allowed_origins": [ORIGIN], "action": {"kind": "navigate", "url": ORIGIN + path}})
                    if nav.get("outcome") != "applied": raise RuntimeError("navigation not confirmed")
                finally:
                    worker("POST", "/gate", {"owner": "none"})
                if name == "checkbox":
                    metadata = demoqa_metadata()
                    desktop_inputs, desktop_roles = desktop_checkbox(metadata)
                    item["selector_diagnostics"] = {
                        "selector_counts": metadata.get("selector_counts"),
                        "role_checkboxes": checkbox_metadata_summary(metadata),
                        "desktop_input_candidates": [{k: entry.get(k) for k in ("tag", "type", "id", "aria_label", "checked", "labels")}
                                                      for entry in desktop_inputs],
                        "desktop_role_candidates": [{k: entry.get(k) for k in ("tag", "id", "selector", "role", "aria_label", "aria_checked", "checked", "visible", "class_names", "static_label")}
                                                     for entry in desktop_roles],
                    }
                    scenario_conditions = [dict(condition) for condition in conditions]
                    checked = next((condition for condition in scenario_conditions if condition["kind"] == "element_checked"), None)
                    if checked is None:
                        raise RuntimeError("checkbox scenario is missing its checked assertion")
                    if len(desktop_inputs) == 1 and isinstance(desktop_inputs[0].get("id"), str) and desktop_inputs[0]["id"]:
                        checked["selector"] = "#" + desktop_inputs[0]["id"]
                    elif len(desktop_roles) == 1 and isinstance(desktop_roles[0].get("selector"), str) and desktop_roles[0]["selector"]:
                        checked["selector"] = desktop_roles[0]["selector"]
                    else:
                        # DemoQA's current ARIA tree labels this role "Select Desktop";
                        # keep the exact target condition even while the Home subtree is collapsed.
                        checked["selector"] = '[role="checkbox"][aria-label="Select Desktop"]'
                baseline = worker("POST", "/verify", {"conditions": scenario_conditions})
                item["baseline_verified"] = baseline.get("verified") is True
                record = mcp("browser_task_start", {"goal": f"Open {ORIGIN + path} if needed. {goal}", "allowed_origins": [ORIGIN], "execution_mode": "autonomous", "idempotency_key": "demoqa-" + uuid.uuid4().hex, "limits": {"timeout_s": timeout_s, "max_steps": max_steps}, "success_conditions": scenario_conditions})
                task_id = record["task_id"]
                deadline = time.monotonic() + timeout_s + 15
                while time.monotonic() < deadline:
                    record = mcp("browser_task_status", {"task_id": task_id})
                    if record["status"] in TERMINAL: break
                    time.sleep(2)
                if record["status"] not in TERMINAL:
                    mcp("browser_task_cancel", {"task_id": task_id})
                    record = mcp("browser_task_status", {"task_id": task_id})
                events = mcp("browser_task_events", {"task_id": task_id, "limit": 100}).get("events", [])
                kinds = Counter(e.get("payload", {}).get("kind") for e in events if e.get("event_type") == "action.planned")
                item.update(steps=record.get("step_count", 0), action_kinds={k: v for k, v in kinds.items() if isinstance(k, str)},
                            model_action_observed=bool(kinds or any(e.get("event_type") in {"attention.notice", "question"} for e in events)),
                            notice_count=sum(e.get("event_type") == "attention.notice" for e in events),
                            task_status=record.get("status"), error_code=(record.get("error") or {}).get("code"))
                check = worker("POST", "/verify", {"conditions": scenario_conditions})
                item["independent_verified"] = check.get("verified") is True
                item["verification_checks"] = check.get("checks", [])
                if name == "checkbox":
                    metadata = demoqa_metadata()
                    desktop_inputs, desktop_roles = desktop_checkbox(metadata)
                    item["selector_diagnostics_after"] = {
                        "selector_counts": metadata.get("selector_counts"),
                        "role_checkboxes": checkbox_metadata_summary(metadata),
                        "desktop_input_candidates": [{k: entry.get(k) for k in ("tag", "type", "id", "aria_label", "checked", "labels")}
                                                      for entry in desktop_inputs],
                        "desktop_role_candidates": [{k: entry.get(k) for k in ("tag", "id", "selector", "role", "aria_label", "aria_checked", "checked", "visible", "class_names", "static_label")}
                                                     for entry in desktop_roles],
                    }
                    if len(desktop_inputs) == 1 and isinstance(desktop_inputs[0].get("id"), str) and desktop_inputs[0]["id"]:
                        checked_check = worker("POST", "/verify", {"conditions": [
                            {"kind": "element_checked", "selector": "#" + desktop_inputs[0]["id"], "checked": True}
                        ]})
                        item["desktop_input_checked"] = checked_check.get("verified") is True
                    elif len(desktop_roles) == 1:
                        role = desktop_roles[0]
                        selector = role.get("selector") or '[role="checkbox"][aria-label="Select Desktop"]'
                        if isinstance(selector, str) and selector:
                            checked_check = worker("POST", "/verify", {"conditions": [
                                {"kind": "element_checked", "selector": selector, "checked": True}
                            ]})
                            item["desktop_input_checked"] = checked_check.get("verified") is True
                        else:
                            item["desktop_input_checked"] = role.get("checked") is True or role.get("aria_checked") == "true"
                    else:
                        selector = '[role="checkbox"][aria-label="Select Desktop"]'
                        checked_check = worker("POST", "/verify", {"conditions": [
                            {"kind": "element_checked", "selector": selector, "checked": True}
                        ]})
                        item["desktop_input_checked"] = checked_check.get("verified") is True
                    item["independent_verified"] = item["independent_verified"] and item["desktop_input_checked"]
                if name == "invalid_email":
                    metadata = demoqa_metadata()
                    email = [entry for entry in metadata.get("inputs", []) if entry.get("id") == "userEmail"]
                    email_meta = [{k: entry.get(k) for k in ("id", "type", "pattern", "validity_valid", "class_names")}
                                  for entry in email]
                    item["email_validity"] = email_meta
                    classes = email[0].get("class_names", []) if len(email) == 1 else []
                    invalid_class = any(any(marker in str(class_name).casefold() for marker in ("invalid", "error", "validation"))
                                         for class_name in classes)
                    item["email_invalid_signal"] = (len(email) == 1 and email[0].get("validity_valid") is False) or invalid_class
                    if len(email) != 1 or not item["email_invalid_signal"]:
                        item["independent_verified"] = False
                item["status"] = ("inconclusive" if item["baseline_verified"] else
                                  "passed" if record.get("status") == "succeeded" and item["independent_verified"] else "failed")
                if name == "notify_then_click" and item["status"] == "passed" and item["notice_count"] == 0:
                    item["status"] = "inconclusive"
                state = worker("GET", "/status")
                item["context_count"] = state.get("context_count")
                item["page_count"] = state.get("page_count")
                item["blocked_resource_counts"] = dict(Counter(x.get("resource_type", "other") for x in state.get("blocked_resources", [])))
                if item["status"] != "passed":
                    try:
                        response = client.get(WORKER + "/screenshot", headers=worker_headers)
                        if response.status_code == 200 and len(response.content) <= 16 * 1024 * 1024 and response.content.startswith(b'\x89PNG\r\n\x1a\n'):
                            directory = Path(os.environ.get('DEMOQA_ARTIFACT_DIR', '/tmp/demoqa-artifacts')); directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                            (directory / (name + '.png')).write_bytes(response.content)
                            item['screenshot_saved'] = True
                            item['screenshot_file'] = name + '.png'
                            item['artifact_directory'] = str(directory)
                    except httpx.HTTPError:
                        item['screenshot_saved'] = False
            except (httpx.HTTPError, KeyError, ValueError, RuntimeError) as exc:
                item["error_type"] = type(exc).__name__
            finally:
                if task_id and (record or {}).get("status") not in {"succeeded", "failed", "cancelled", "expired"}:
                    try: mcp("browser_task_cancel", {"task_id": task_id})
                    except Exception: item["cleanup"] = "cancel_unconfirmed"
                item["elapsed_s"] = round(time.monotonic() - start, 2)
                results.append(item)
                print(json.dumps(item, ensure_ascii=False), flush=True)
                if item.get("status") != "passed" and item.get("screenshot_saved") is True:
                    time.sleep(2)
                if item.get("cleanup") in {"cancel_unconfirmed", "resource_limit_violation"}:
                    break
        print(json.dumps({"results": results, "unsupported_not_run": ["upload", "browser_dialog", "frame_targeting", "multiple_windows"]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
