"""Run one corrective EFF test while retaining the test-only manual route lease."""
from __future__ import annotations

import json
import os
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

WORKER = "http://browser:7070"
CONTROL = "http://localhost:8080"
ORIGIN = "https://coveryourtracks.eff.org"
CHALLENGES = ["Verify you are human", "Access denied", "Robot Check",
              "unusual traffic", "Request blocked", "Security Measure"]
RESULT_LABELS = ["Blocking tracking ads?", "Blocking invisible trackers?",
                 "Protecting you from fingerprinting?"]


def verify(client: httpx.Client, headers: dict, conditions: list[dict]) -> list[bool]:
    response = client.post(WORKER + "/verify", headers=headers, json={"conditions": conditions})
    response.raise_for_status()
    checks = response.json().get("checks")
    if not isinstance(checks, list) or len(checks) != len(conditions):
        raise RuntimeError("verification_shape_invalid")
    values = [item.get("passed") is True for item in checks if isinstance(item, dict)]
    if len(values) != len(conditions):
        raise RuntimeError("verification_result_invalid")
    return values


def safe_blocked_reason_counts(status: dict) -> dict[str, int]:
    counts: Counter[str] = Counter()
    entries = status.get("blocked_resources", [])
    if not isinstance(entries, list):
        return {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        reason = entry.get("reason")
        safe = reason if isinstance(reason, str) and re.fullmatch(r"[a-zA-Z0-9_:-]{1,48}", reason) else "other"
        count = entry.get("count", 1)
        counts[safe] += count if type(count) is int and 0 < count <= 100000 else 1
    return dict(sorted(counts.items()))


def observe_after_deadline() -> int:
    """Read the existing corrective run's delayed page once; never navigate or click."""
    if (os.environ.get("APP_ENV") != "test" or os.environ.get("READINESS_DISPOSABLE") != "1"
            or os.environ.get("BROWSER_WORKER_URL") != WORKER
            or os.environ.get("BROWSER_MCP_ORIGIN") != "http://localhost:18081"
            or os.environ.get("ANTIBOT_PROFILE") != "p2"):
        raise SystemExit("disposable_post_deadline_observation_required")
    key_path = Path(os.environ["WORKER_API_KEY_FILE"])
    headers = {"Authorization": "Bearer " + key_path.read_text(encoding="utf-8").strip()}
    labels = [{"kind": "text_present", "text": "Here are your Cover Your Tracks results"},
              {"kind": "text_present", "text": "Our tests indicate that"}]
    labels += [{"kind": "text_present", "text": item} for item in RESULT_LABELS + CHALLENGES]

    def sample(client: httpx.Client) -> dict:
        response = client.get(WORKER + "/status", headers=headers)
        response.raise_for_status()
        status = response.json()
        parsed = urlsplit(status.get("url", ""))
        if (parsed.scheme != "https" or parsed.hostname != "coveryourtracks.eff.org"
                or status.get("control_owner") != "none" or status.get("context_count") != 1
                or status.get("page_count") != 1 or status.get("proxy_assignment_id") != "antibot-2026-10-31-direct"
                or status.get("route_generation") != 1):
            raise RuntimeError("post_deadline_state_precondition_failed")
        values = verify(client, headers, labels)
        return {"observed_at_utc": datetime.now(timezone.utc).isoformat(),
                "path": parsed.path, "control_owner": "none", "context_count": 1,
                "page_count": 1, "challenge_indicators": dict(zip(CHALLENGES, values[5:])),
                "summary_predicates": {"results_heading_exact": values[0],
                                       "results_heading_alternative": values[1],
                                       **{item: values[index + 2] for index, item in enumerate(RESULT_LABELS)}},
                "blocked_resource_reason_counts": safe_blocked_reason_counts(status)}

    with httpx.Client(timeout=15, trust_env=False) as client:
        first = sample(client)
        time.sleep(15)
        second = sample(client)
    output = {"schema": "antibot-eff-post-deadline/v1", "profile_id": "p2", "run": 2,
              "action_taken": False, "navigation_performed": False,
              "observations": [first, second],
              "verdict": ("BLOCKED" if any(any(item["challenge_indicators"].values())
                                           for item in (first, second)) else "PARTIAL"),
              "evidence_scope": "two bounded read-only URL/summary/challenge/resource-reason observations; no page text, query string, IP, cookies, or fingerprint values"}
    dest = Path("/tmp/antibot-artifacts/eff/corrective-gate-hold/post-deadline-observation.json")
    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    dest.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(output, sort_keys=True))
    return 0


def no_active_tasks(client: httpx.Client) -> tuple[bool, int]:
    session = client.get(CONTROL + "/api/v1/operator/session")
    session.raise_for_status()
    value = session.json()
    if not isinstance(value, dict) or value.get("local_ui") is not True:
        raise RuntimeError("local_operator_session_unavailable")
    response = client.get(CONTROL + "/api/v1/tasks")
    response.raise_for_status()
    tasks = response.json().get("tasks")
    if not isinstance(tasks, list):
        raise RuntimeError("task_list_invalid")
    active = {"queued", "running", "pause_requested", "paused", "waiting_human",
              "waiting_approval", "recovering"}
    active_count = sum(1 for task in tasks if isinstance(task, dict) and task.get("status") in active)
    return active_count == 0, active_count


def main() -> int:
    if (os.environ.get("APP_ENV") != "test" or os.environ.get("READINESS_DISPOSABLE") != "1"
            or os.environ.get("BROWSER_WORKER_URL") != WORKER
            or os.environ.get("BROWSER_MCP_ORIGIN") != "http://localhost:18081"):
        raise SystemExit("disposable_runtime_markers_required")
    if os.environ.get("ANTIBOT_PROFILE") != "p2" or os.environ.get("ANTIBOT_EFF_RUN") != "2":
        raise SystemExit("corrective_run_requires_profile_p2_run2")
    if os.environ.get("ANTIBOT_EFF_POST_DEADLINE_OBSERVE") == "1":
        return observe_after_deadline()
    if os.environ.get("ANTIBOT_EFF_CTA_VISUALLY_CONFIRMED") != "1":
        raise SystemExit("fresh_cta_visual_confirmation_required")
    try:
        click_x = int(os.environ["ANTIBOT_EFF_CTA_X"])
        click_y = int(os.environ["ANTIBOT_EFF_CTA_Y"])
    except (KeyError, ValueError) as exc:
        raise SystemExit("caller_confirmed_cta_coordinates_required") from exc
    if not 0 <= click_x < 1440 or not 0 <= click_y < 900:
        raise SystemExit("cta_coordinates_out_of_viewport")
    key_path = Path(os.environ["WORKER_API_KEY_FILE"])
    headers = {"Authorization": "Bearer " + key_path.read_text(encoding="utf-8").strip()}
    wait_s = int(os.environ.get("ANTIBOT_EFF_WAIT", "45"))
    if not 5 <= wait_s <= 90:
        raise SystemExit("bounded_wait_out_of_range")
    started_at = datetime.now(timezone.utc).isoformat()
    report: dict = {"schema": "antibot-eff/v1", "site": "eff", "profile_id": "p2",
                    "run": 2, "run_type": "corrective_gate_hold", "verdict": "PARTIAL",
                    "challenge_detected": False, "lease": {"human_gate_acquired": False,
                    "manual_cross_origin_enabled": False, "released": False}}
    gate_acquired = False
    try:
        with httpx.Client(timeout=20, trust_env=False) as client:
            no_active, active_count = no_active_tasks(client)
            if not no_active:
                raise RuntimeError("another_task_is_active")
            initial_response = client.get(WORKER + "/status", headers=headers)
            initial_response.raise_for_status()
            initial = initial_response.json()
            if (initial.get("url") != ORIGIN + "/" or initial.get("control_owner") != "none"
                    or initial.get("action_in_progress") is not False
                    or initial.get("import_in_progress") is not False
                    or initial.get("resource_violation") is not False
                    or initial.get("recovery_needed") is not False
                    or initial.get("width") != 1440 or initial.get("height") != 900
                    or initial.get("proxy_assignment_id") != "antibot-2026-10-31-direct"
                    or initial.get("route_generation") != 1):
                raise RuntimeError("eff_corrective_page_precondition_failed")
            observation = client.post(WORKER + "/observe", headers=headers, json={})
            observation.raise_for_status()
            observed = observation.json()
            if observed.get("context_count") != 1 or observed.get("page_count") != 1:
                raise RuntimeError("single_context_page_precondition_failed")
            cta_checks = verify(client, headers, [
                {"kind": "text_present", "text": "TEST YOUR BROWSER"},
                {"kind": "text_present", "text": "Only anonymous data"},
                *[{"kind": "text_present", "text": text} for text in CHALLENGES],
            ])
            challenge_seen = any(cta_checks[2:])
            report["challenge_detected"] = challenge_seen
            report["cta_visible_verified"] = cta_checks[0]
            report["anonymous_data_notice_visible"] = cta_checks[1]
            report["preflight"] = {"no_active_task": no_active, "active_task_count": active_count,
                                   "url_is_eff_home": True, "owner_none": True,
                                   "context_count": 1, "page_count": 1}
            blocked_counts: dict[str, int] = {}
            aggregate: dict[str, bool] = {}
            lease_lost = False
            if challenge_seen:
                report["verdict"] = "BLOCKED"
            elif not (cta_checks[0] and cta_checks[1]):
                report["verdict"] = "PARTIAL"
            else:
                gate = client.post(WORKER + "/gate", headers=headers, json={"owner": "human"})
                gate.raise_for_status()
                if gate.json().get("control_owner") != "human":
                    raise RuntimeError("human_gate_not_acquired")
                gate_acquired = True
                epoch = gate.json().get("control_epoch")
                if type(epoch) is not int:
                    raise RuntimeError("human_gate_epoch_invalid")
                report["lease"]["human_gate_acquired"] = True
                try:
                    click = client.post(WORKER + "/manual/execute", headers=headers, json={
                        "control_epoch": epoch, "allowed_origins": [ORIGIN],
                        "allow_cross_origin_navigation": True,
                        "action": {"kind": "click", "x": click_x, "y": click_y},
                    })
                    click.raise_for_status()
                    receipt = click.json()
                    if receipt.get("outcome") != "applied":
                        raise RuntimeError("eff_cta_click_not_applied")
                    report["click_outcome"] = "applied"
                    report["cta_visual_confirmation"] = {"confirmed_before_click": True,
                                                          "coordinates": [click_x, click_y]}
                    deadline = time.monotonic() + wait_s
                    result_checks = ([{"kind": "text_present", "text": "Here are your Cover Your Tracks results"},
                                      {"kind": "text_present", "text": "Our tests indicate that"}]
                                     + [{"kind": "text_present", "text": label} for label in RESULT_LABELS]
                                     + [{"kind": "text_present", "text": label} for label in CHALLENGES])
                    while time.monotonic() < deadline:
                        time.sleep(min(5, max(0, deadline - time.monotonic())))
                        current_response = client.get(WORKER + "/status", headers=headers)
                        current_response.raise_for_status()
                        current = current_response.json()
                        blocked_counts = safe_blocked_reason_counts(current)
                        if (current.get("control_owner") != "human"
                                or current.get("manual_cross_origin_navigation") is not True):
                            lease_lost = True
                            break
                        url = current.get("url", "")
                        parsed = urlsplit(url)
                        if parsed.scheme != "https" or parsed.hostname != "coveryourtracks.eff.org":
                            report["stop_reason"] = "unexpected_origin"
                            report["verdict"] = "PARTIAL"
                            break
                        values = verify(client, headers, result_checks)
                        challenge_now = any(values[5:])
                        report["challenge_detected"] = challenge_now
                        aggregate = {"results_heading_exact": values[0], "results_heading_alternative": values[1],
                                     **{label: values[index + 2] for index, label in enumerate(RESULT_LABELS)}}
                        if challenge_now:
                            report["stop_reason"] = "challenge_text_detected"
                            break
                        if (any(values[:2]) and all(values[2:5])):
                            break
                    report["lease"]["owner_during_wait"] = "human"
                    report["lease"]["manual_cross_origin_enabled"] = not lease_lost
                    report["lease"]["lease_lost"] = lease_lost
                    report["blocked_resource_reason_counts"] = blocked_counts
                    if lease_lost:
                        report["stop_reason"] = "manual_route_lease_lost"
                    report["result_page"] = {"origin": ORIGIN,
                                              "path": (parsed.path if 'parsed' in locals()
                                                       and parsed.hostname == "coveryourtracks.eff.org" else None),
                                              "summary_predicates": aggregate,
                                              "summary_values_retained": False,
                                              "summary_labels_complete": bool(aggregate) and all(
                                                  aggregate.get(label, False) for label in RESULT_LABELS)}
                    if report["challenge_detected"]:
                        report["verdict"] = "BLOCKED"
                    elif report.get("verdict") != "BLOCKED":
                        report["verdict"] = "PARTIAL"
                finally:
                    if gate_acquired:
                        release = client.post(WORKER + "/gate", headers=headers, json={"owner": "none"})
                        release.raise_for_status()
                        gate_acquired = False
                        report["lease"]["released"] = release.json().get("control_owner") == "none"
                        if not report["lease"]["released"]:
                            raise RuntimeError("control_gate_release_failed")
            report.setdefault("result_page", {"origin": ORIGIN, "path": None,
                                               "summary_predicates": aggregate, "summary_values_retained": False})
            report["observed_at_utc"] = datetime.now(timezone.utc).isoformat()
            report["started_at_utc"] = started_at
            report["scope"] = "one public EFF CTA with human route lease held through bounded wait and verifier"
            report["guest_profile"] = {"provenance": "dedicated fresh p2 worker_state volume",
                                        "cookies_imported": False,
                                        "authentication": "no account credentials or login submitted"}
            report["runtime"] = {"application_image": "browser-mcp-antibot-control:prompt-3184fb4",
                                 "browser_backend": initial.get("backend"), "viewport": [1440, 900],
                                 "network_route": "antibot-2026-10-31-direct", "route_generation": 1}
            report["evidence_scope"] = "challenge booleans, summary label booleans, blocked reason counts only; no URLs beyond origin, raw page, cookies, IP, fingerprint values, or screenshots"
    except Exception as exc:
        report["verdict"] = "PARTIAL"
        report["error_code"] = "CORRECTIVE_RUN_FAILED"
        report["error_class"] = type(exc).__name__
    finally:
        if gate_acquired:
            try:
                with httpx.Client(timeout=5, trust_env=False) as cleanup:
                    release = cleanup.post(WORKER + "/gate", headers=headers, json={"owner": "none"})
                    release.raise_for_status()
                    report["lease"]["released"] = release.json().get("control_owner") == "none"
            except Exception:
                report["lease"]["released"] = False
    out = Path("/tmp/antibot-artifacts/eff/corrective-gate-hold")
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    (out / "metadata.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
