"""Run two bounded, read-only model coverage scenarios on the disposable control."""
from __future__ import annotations

import argparse
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:  # script execution inside the disposable control
    from antibot_model_task import (Client, ProbeError, TERMINAL, action_trace,
                                    read_key, validate_environment, validate_worker)
except ImportError:  # package import by offline tests
    from scripts.antibot_model_task import (Client, ProbeError, TERMINAL, action_trace,
                                            read_key, validate_environment, validate_worker)

DEMOQA = "https://demoqa.com"
EBAY = "https://www.ebay.com"
CHALLENGES = ("Verify you are human", "Access denied", "Robot Check",
              "unusual traffic", "Request blocked", "Security Measure")
CONFIRMED_TERMINAL = {"succeeded", "failed", "cancelled", "expired"}
CONTROL_ORIGIN = "http://localhost:18081"
SCENARIOS: dict[str, dict[str, Any]] = {
    "demoqa_checkbox_readonly": {
        "origin": DEMOQA, "home": DEMOQA + "/", "entry": DEMOQA + "/checkbox",
        "milestone_url": DEMOQA + "/checkbox", "final_url": DEMOQA + "/checkbox",
        "milestone_text": ("Check Box", "Home"), "final_text": ("Check Box", "Home"),
        "query": None,
        "goal": ("Open https://demoqa.com/ and confirm the home page. Click the visible Elements section, "
                 "then open Check Box. Read the visible Check Box and Home labels and leave the tree unchanged. "
                 "Do not select any checkbox, use forms, or sign in. Stop immediately if a challenge or access "
                 "denial appears; do not retry. Finish on the Check Box page."),
        "conditions": [{"kind": "url_is", "url": DEMOQA + "/checkbox"},
                       {"kind": "text_present", "text": "Check Box"},
                       {"kind": "text_present", "text": "Home"},
                       {"kind": "element_checked", "selector": '[role="checkbox"][aria-label="Select Home"]', "checked": False}],
        "negative_conditions": [{"kind": "text_present", "text": "You have selected"}],
    },
    "ebay_guest_search": {
        "origin": EBAY, "home": EBAY + "/", "entry": EBAY + "/",
        "milestone_url": EBAY + "/", "final_url": EBAY + "/sch/i.html?_nkw=notebook",
        "milestone_text": ("Sign in or register",), "final_text": ("notebook",),
        "query": "notebook",
        "goal": ("Open the public eBay home page. Before searching, verify that the visible page says "
                 "'Sign in or register' (signed out). If it does not, or any challenge/access denial appears, "
                 "stop immediately without searching or retrying. Then navigate once to the public search URL "
                 "https://www.ebay.com/sch/i.html?_nkw=notebook (this is the only search). Read the results, "
                 "open one clearly available public item, verify its item page, then use browser history back. "
                 "Do not sign in; do not type into fields, add to cart, watch, purchase, or submit any form. If no public "
                 "item is available, stop; the run is incomplete. Finish back on the search results page."),
        "conditions": [{"kind": "url_is", "url": EBAY + "/sch/i.html?_nkw=notebook"},
                       {"kind": "text_present", "text": "notebook"}],
    },
}


def _verify(client: Client, conditions: list[dict[str, Any]]) -> list[bool]:
    result = client.worker_verify(conditions)
    checks = result.get("checks")
    if not isinstance(checks, list) or len(checks) != len(conditions):
        raise ProbeError("verify", "invalid_check_shape")
    flags = [item.get("passed") for item in checks if isinstance(item, dict)]
    if len(flags) != len(conditions) or any(type(flag) is not bool for flag in flags):
        raise ProbeError("verify", "invalid_check_result")
    return flags


def classify(*, scenario: str, task_status: str, phase: str | None, verified: bool,
             milestone: bool, final_ok: bool, actions: list[str],
             challenge: bool, error_code: str | None, presearch_guest: bool = True,
             item_observed: bool = False, history_back_verified: bool = False) -> str:
    if challenge:
        return "BLOCKED"
    if task_status != "succeeded" or phase != "verified" or not verified or not milestone or not final_ok or not presearch_guest:
        return "PARTIAL" if task_status in {"waiting_human", "waiting_approval", "running", "queued"} else "FAIL"
    if not actions or not set(actions) <= {"navigate", "click", "history_back", "wait", "scroll", "mouse_move", "finish"}:
        return "FAIL"
    if scenario == "demoqa_checkbox_readonly" and actions.count("click") < 2:
        return "FAIL"
    if scenario == "ebay_guest_search":
        if not (item_observed and history_back_verified and "navigate" in actions
                and "click" in actions and "history_back" in actions
                and actions.index("click") < actions.index("history_back")):
            return "FAIL"
    if error_code:
        return "FAIL"
    return "PASS"


def run_one(client: Client, name: str, spec: dict[str, Any], *, timeout_s: int = 420,
            max_steps: int = 16, sleep=time.sleep, now=time.monotonic) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    client.ensure_no_active_tasks()
    validate_worker(client.worker_status())
    payload = {"goal": spec["goal"], "allowed_origins": [spec["origin"]],
               "idempotency_key": "readiness-model-coverage-" + uuid.uuid4().hex,
               "profile_id": "default", "success_conditions": spec["conditions"],
               "sync_cookies": False, "execution_mode": "autonomous",
               "limits": {"timeout_s": timeout_s, "max_steps": max_steps}}
    record = client.mcp("browser_task_start", payload)
    task_id = record.get("task_id")
    if not isinstance(task_id, str) or not re.fullmatch(r"[0-9a-f-]{36}", task_id):
        raise ProbeError("task_start", "missing_task_id")
    client.owned_task_id = task_id
    observed = False
    item_observed = False
    history_back_verified = False
    challenge = False
    presearch_guest = name != "ebay_guest_search"
    search_seen = False
    entry_checks = ([{"kind": "url_is", "url": spec["milestone_url"]}]
                    + [{"kind": "text_present", "text": value} for value in spec["milestone_text"]]
                    + [{"kind": "text_present", "text": value} for value in CHALLENGES])
    deadline = now() + timeout_s + 30
    while now() < deadline:
        record = client.mcp("browser_task_status", {"task_id": task_id})
        state = client.worker_status()
        challenge_flags = _verify(client, [{"kind": "text_present", "text": value} for value in CHALLENGES])
        challenge = challenge or any(challenge_flags)
        if challenge:
            client.mcp("browser_task_cancel", {"task_id": task_id})
            record = client.mcp("browser_task_status", {"task_id": task_id})
            break
        current_url = state.get("url")
        if name == "ebay_guest_search" and current_url == spec["final_url"]:
            search_seen = True
        if name == "ebay_guest_search" and current_url == spec["home"] and not search_seen and not presearch_guest:
            guest_flags = _verify(client, [{"kind": "url_is", "url": spec["home"]},
                                           {"kind": "text_present", "text": "Sign in or register"},
                                           *[{"kind": "text_present", "text": value} for value in CHALLENGES]])
            challenge = challenge or any(guest_flags[2:])
            presearch_guest = guest_flags[0] and guest_flags[1] and not challenge
            if challenge:
                client.mcp("browser_task_cancel", {"task_id": task_id})
                record = client.mcp("browser_task_status", {"task_id": task_id})
                break
            if not presearch_guest:
                # A missing exact guest marker is a stop condition, not a reason to continue to search.
                client.mcp("browser_task_cancel", {"task_id": task_id})
                record = client.mcp("browser_task_status", {"task_id": task_id})
                break
        if name == "ebay_guest_search" and search_seen and not presearch_guest:
            client.mcp("browser_task_cancel", {"task_id": task_id})
            record = client.mcp("browser_task_status", {"task_id": task_id})
            break
        if state.get("url") == spec["milestone_url"]:
            flags = _verify(client, entry_checks)
            challenge = challenge or any(flags[1 + len(spec["milestone_text"]):])
            observed = observed or (flags[0] and all(flags[1:1 + len(spec["milestone_text"])]) and not challenge)
        if name == "ebay_guest_search" and current_url == spec["final_url"]:
            observed = observed or (presearch_guest and all(_verify(client, spec["conditions"])))
        if name == "ebay_guest_search" and isinstance(current_url, str):
            from urllib.parse import urlsplit
            parsed = urlsplit(current_url)
            if parsed.scheme == "https" and parsed.hostname == "www.ebay.com" and "/itm/" in parsed.path:
                product_flags = _verify(client, [
                    {"kind": "element_visible", "selector": "h1.x-item-title__mainTitle", "visible": True},
                    *[{"kind": "text_present", "text": value} for value in CHALLENGES],
                ])
                challenge = challenge or any(product_flags[1:])
                item_observed = item_observed or (product_flags[0] and not challenge)
        elif record.get("status") in {"waiting_human", "waiting_approval"}:
            flags = _verify(client, [{"kind": "text_present", "text": x} for x in CHALLENGES])
            challenge = challenge or any(flags)
        if challenge:
            client.mcp("browser_task_cancel", {"task_id": task_id})
            record = client.mcp("browser_task_status", {"task_id": task_id})
            break
        if record.get("status") in TERMINAL:
            break
        sleep(1)
    else:
        client.mcp("browser_task_cancel", {"task_id": task_id})
        record = client.mcp("browser_task_status", {"task_id": task_id})
    actions, _, write_violation = action_trace(client, task_id)
    write_violation = write_violation or not set(actions) <= {"navigate", "click", "history_back", "wait", "scroll", "mouse_move", "finish"}
    final_flags = _verify(client, spec["conditions"]) if not challenge else [False] * len(spec["conditions"])
    negative_flags = (_verify(client, spec["negative_conditions"])
                      if not challenge and spec.get("negative_conditions") else [])
    final_ok = all(final_flags) and not any(negative_flags)
    history_back_verified = (name == "ebay_guest_search" and item_observed and
                             actions.count("history_back") >= 1 and
                             state.get("url") == spec["final_url"] and final_ok)
    verified = isinstance(record.get("result"), dict) and record["result"].get("verified") is True
    err = record.get("error")
    raw_error_code = err.get("code") if isinstance(err, dict) else None
    error_code = (raw_error_code if isinstance(raw_error_code, str)
                  and re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", raw_error_code) else
                  ("UNKNOWN_TASK_ERROR" if err is not None else None))
    status = classify(scenario=name, task_status=str(record.get("status")), phase=record.get("phase"),
                      verified=verified, milestone=observed, final_ok=final_ok,
                      actions=actions, challenge=challenge, error_code=error_code,
                      presearch_guest=presearch_guest, item_observed=item_observed,
                      history_back_verified=history_back_verified)
    if write_violation:
        status = "FAIL"
    return {"scenario": name, "status": status, "task_id": task_id,
            "started_at": started_at, "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "limits": {"max_steps": max_steps, "timeout_s": timeout_s},
            "control_origin": CONTROL_ORIGIN,
            "runtime_profile_hint": os.environ.get("ANTIBOT_PROFILE")
            if os.environ.get("ANTIBOT_PROFILE") in {"p1", "p2"} else None,
            "prompt_provenance": "scripts/readiness_model_coverage.py:SCENARIOS",
            "task_status": record.get("status"),
            "phase": record.get("phase"), "verified": verified,
            "milestone_independently_verified": observed,
            "final_conditions_independently_verified": final_ok,
            "signed_out_verified_before_search": presearch_guest,
            "item_independently_verified": item_observed,
            "history_back_to_results_verified": history_back_verified,
            "action_kinds": actions, "read_only_actions_only": not write_violation,
            "challenge_detected": challenge, "error_code": error_code}


def cleanup_owned_task(client: Client) -> bool:
    """Cancel only our task and clear its ID only after terminal status is confirmed."""
    task_id = client.owned_task_id
    if not task_id:
        return True
    try:
        record = client.mcp("browser_task_status", {"task_id": task_id})
        if record.get("status") not in CONFIRMED_TERMINAL:
            client.mcp("browser_task_cancel", {"task_id": task_id})
            record = client.mcp("browser_task_status", {"task_id": task_id})
        if record.get("status") not in CONFIRMED_TERMINAL:
            return False
        client.owned_task_id = None
        return True
    except Exception:
        return False


def run_scenarios(client: Client, names: list[str], *, runner=run_one,
                  cleanup=cleanup_owned_task) -> list[dict[str, Any]]:
    """Preserve each scenario result and confirm cleanup before starting another."""
    rows: list[dict[str, Any]] = []
    for name in names:
        try:
            row = runner(client, name, SCENARIOS[name])
        except Exception as exc:
            code = getattr(exc, "code", None)
            row = {"scenario": name, "status": "FAIL",
                   "error_code": code if isinstance(code, str)
                   and re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}|[a-z][a-z0-9_-]{1,63}", code)
                   else "scenario_failed"}
        if not isinstance(row, dict):
            row = {"scenario": name, "status": "FAIL", "error_code": "invalid_scenario_result"}
        rows.append(row)
        if not cleanup(client):
            row.update({"status": "FAIL", "cleanup": "unconfirmed",
                        "error_code": "task_cleanup_unconfirmed"})
            break
        if row.get("status") == "BLOCKED" or row.get("error_code") == "worker_not_idle_on_expected_route":
            break
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--i-confirm-disposable", action="store_true")
    parser.add_argument("--scenario", choices=["all", *SCENARIOS], default="all")
    parser.add_argument("--output", type=Path, default=Path("/tmp/antibot-artifacts/model-coverage.json"))
    args = parser.parse_args(argv)
    try:
        worker_key_path, mcp_key_path = validate_environment(args.i_confirm_disposable)
        client = Client(read_key(worker_key_path), read_key(mcp_key_path))
        try:
            names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
            rows = run_scenarios(client, names)
            report = {"schema": "readiness-model-coverage/v1", "status": "PASS" if rows and all(row["status"] == "PASS" for row in rows) else "FAIL", "scenarios": rows}
        finally:
            client.cancel_owned_task_if_active()
            client.http.close()
    except Exception as exc:
        code = getattr(exc, "code", None)
        report = {"schema": "readiness-model-coverage/v1", "status": "FAIL",
                  "error": code if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", code) else "probe_failed"}
    args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
