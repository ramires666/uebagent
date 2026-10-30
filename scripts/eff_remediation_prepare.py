"""One-time, read-only-check-first reset of the existing EFF test tab to its CTA page."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

WORKER = "http://browser:7070"
CONTROL = "http://localhost:8080"
ORIGIN = "https://coveryourtracks.eff.org"
CHALLENGES = ("Verify you are human", "Access denied", "Robot Check",
              "unusual traffic", "Request blocked", "Security Measure")


def main() -> int:
    if (os.environ.get("APP_ENV") != "test" or os.environ.get("READINESS_DISPOSABLE") != "1"
            or os.environ.get("BROWSER_WORKER_URL") != WORKER
            or os.environ.get("BROWSER_MCP_ORIGIN") != "http://localhost:18081"
            or os.environ.get("ANTIBOT_PROFILE") != "p2"
            or os.environ.get("ANTIBOT_EFF_RUN") != "3"):
        raise SystemExit("isolated_p2_eff_run3_markers_required")
    key_path = os.environ["WORKER_API_KEY_FILE"]
    headers = {"Authorization": "Bearer " + open(key_path, encoding="utf-8").read().strip()}
    output = {"schema": "eff-remediation-preparation/v1", "navigation_performed": False,
              "challenge_seen_before": False, "gate_released": False}
    with httpx.Client(timeout=12, trust_env=False) as client:
        session = client.get(CONTROL + "/api/v1/operator/session")
        session.raise_for_status()
        if session.json().get("local_ui") is not True:
            raise SystemExit("local_operator_session_unavailable")
        tasks_response = client.get(CONTROL + "/api/v1/tasks")
        tasks_response.raise_for_status()
        tasks = tasks_response.json().get("tasks")
        if not isinstance(tasks, list):
            raise SystemExit("task_list_invalid")
        active = {"queued", "running", "pause_requested", "paused", "waiting_human",
                  "waiting_approval", "recovering"}
        active_count = sum(1 for task in tasks if isinstance(task, dict) and task.get("status") in active)
        if active_count:
            raise SystemExit("another_task_is_active")

        status_response = client.get(WORKER + "/status", headers=headers)
        status_response.raise_for_status()
        status = status_response.json()
        parsed = urlsplit(status.get("url", ""))
        if (status.get("control_owner") != "none" or status.get("action_in_progress") is not False
                or status.get("context_count") != 1 or status.get("page_count") != 1
                or status.get("resource_violation") is not False or status.get("recovery_needed") is not False
                or status.get("width") != 1440 or status.get("height") != 900
                or status.get("proxy_assignment_id") != "antibot-2026-10-31-direct"
                or status.get("route_generation") != 1 or parsed.scheme != "https"
                or parsed.hostname != "coveryourtracks.eff.org" or parsed.path != "/kcarter"):
            raise SystemExit("eff_run3_navigation_precondition_failed")
        challenge_response = client.post(WORKER + "/verify", headers=headers, json={
            "conditions": [{"kind": "text_present", "text": text} for text in CHALLENGES]})
        challenge_response.raise_for_status()
        checks = challenge_response.json().get("checks")
        if not isinstance(checks, list) or len(checks) != len(CHALLENGES):
            raise SystemExit("challenge_check_shape_invalid")
        output["challenge_seen_before"] = any(item.get("passed") is True for item in checks if isinstance(item, dict))
        if output["challenge_seen_before"]:
            raise SystemExit("challenge_present_stop_without_retry")

        gate_response = client.post(WORKER + "/gate", headers=headers, json={"owner": "human"})
        gate_response.raise_for_status()
        gate = gate_response.json()
        if gate.get("control_owner") != "human" or type(gate.get("control_epoch")) is not int:
            raise SystemExit("human_gate_not_acquired")
        try:
            nav_response = client.post(WORKER + "/manual/execute", headers=headers, json={
                "control_epoch": gate["control_epoch"], "allowed_origins": [ORIGIN],
                "allow_cross_origin_navigation": False,
                "action": {"kind": "navigate", "url": ORIGIN + "/"},
            })
            nav_response.raise_for_status()
            nav = nav_response.json()
            if nav.get("outcome") != "applied":
                raise SystemExit("eff_home_navigation_not_applied")
            output["navigation_performed"] = True
            output["page_origin"] = ORIGIN
            output["page_path"] = "/"
            output["context_count"] = nav.get("context_count")
            output["page_count"] = nav.get("page_count")
            output["observed_at_utc"] = datetime.now(timezone.utc).isoformat()
        finally:
            release = client.post(WORKER + "/gate", headers=headers, json={"owner": "none"})
            release.raise_for_status()
            output["gate_released"] = release.json().get("control_owner") == "none"
            if not output["gate_released"]:
                raise SystemExit("control_gate_release_failed")
    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
