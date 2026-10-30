"""Run the bounded public DemoQA home -> Elements -> history-back path."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

WORKER = os.environ.get("BROWSER_WORKER_URL", "http://browser:7070")
ORIGIN = "https://demoqa.com"
KEY = Path(os.environ["WORKER_API_KEY_FILE"]).read_text(encoding="utf-8").strip()
HEADERS = {"Authorization": "Bearer " + KEY}
CHALLENGES = ["Verify you are human", "Access denied", "Robot Check",
              "unusual traffic", "Request blocked", "Security Measure"]


def manual(client: httpx.Client, epoch: int, action: dict) -> int:
    r = client.post(WORKER + "/manual/execute", headers=HEADERS, json={
        "control_epoch": epoch, "allowed_origins": [ORIGIN],
        "allow_cross_origin_navigation": True, "action": action,
    })
    r.raise_for_status()
    receipt = r.json()
    if receipt.get("outcome") != "applied" or type(receipt.get("control_epoch")) is not int:
        raise RuntimeError("manual_action_not_confirmed")
    return receipt["control_epoch"]


def verify(client: httpx.Client, expected_url: str, labels: list[str]) -> tuple[bool, dict[str, bool]]:
    conditions = ([{"kind": "url_is", "url": expected_url}]
                  + [{"kind": "text_present", "text": x} for x in labels + CHALLENGES])
    r = client.post(WORKER + "/verify", headers=HEADERS, json={"conditions": conditions})
    r.raise_for_status()
    items = r.json().get("checks")
    if not isinstance(items, list) or len(items) != len(conditions):
        raise RuntimeError("verification_shape_invalid")
    passed = [x.get("passed") is True for x in items if isinstance(x, dict)]
    if len(passed) != len(conditions):
        raise RuntimeError("verification_result_invalid")
    return any(passed[1 + len(labels):]), {
        "expected_url": passed[0],
        **{label: passed[index + 1] for index, label in enumerate(labels)},
    }


def main() -> int:
    if os.environ.get("APP_ENV") != "test" or os.environ.get("READINESS_DISPOSABLE") != "1":
        raise SystemExit("disposable_runtime_markers_required")
    if (os.environ.get("BROWSER_WORKER_URL") != "http://browser:7070"
            or os.environ.get("BROWSER_MCP_ORIGIN") != "http://localhost:18081"):
        raise SystemExit("disposable_runtime_endpoints_required")
    profile_id = os.environ.get("ANTIBOT_PROFILE", "p1")
    if profile_id not in {"p1", "p2"}:
        raise SystemExit("profile_not_allowlisted")
    started = datetime.now(timezone.utc).isoformat()
    with httpx.Client(timeout=20, trust_env=False) as client:
        initial = client.get(WORKER + "/status", headers=HEADERS)
        initial.raise_for_status()
        before = initial.json()
        if (before.get("control_owner") != "none" or before.get("width") != 1440
                or before.get("height") != 900
                or before.get("proxy_assignment_id") != "antibot-2026-10-31-direct"
                or before.get("route_generation") != 1):
            raise SystemExit("demoqa_precondition_failed")
        observe = client.post(WORKER + "/observe", headers=HEADERS, json={})
        observe.raise_for_status()
        observed = observe.json()
        if (observed.get("context_count") != 1 or observed.get("page_count") != 1
                or observed.get("control_owner") != "none"):
            raise SystemExit("single_context_page_precondition_failed")
        gate = client.post(WORKER + "/gate", headers=HEADERS, json={"owner": "human"})
        gate.raise_for_status()
        epoch = gate.json().get("control_epoch")
        if type(epoch) is not int:
            raise SystemExit("human_gate_not_acquired")
        home_check: dict[str, bool] = {}
        elements_check: dict[str, bool] = {}
        back_ok = False
        challenge = False
        try:
            epoch = manual(client, epoch, {"kind": "navigate", "url": ORIGIN + "/"})
            time.sleep(5)
            challenge, home_check = verify(client, ORIGIN + "/", ["DemoQA", "Elements"])
            if not challenge and home_check.get("expected_url") and home_check.get("Elements"):
                epoch = manual(client, epoch, {"kind": "navigate", "url": ORIGIN + "/elements"})
                time.sleep(5)
                challenge, elements_check = verify(client, ORIGIN + "/elements",
                                                    ["Elements", "Text Box", "Check Box", "Web Tables"])
                if (not challenge and elements_check.get("expected_url")
                        and elements_check.get("Elements") and elements_check.get("Text Box")):
                    epoch = manual(client, epoch, {"kind": "history_back"})
                    time.sleep(3)
                    status = client.get(WORKER + "/status", headers=HEADERS)
                    status.raise_for_status()
                    back_url = status.json().get("url")
                    back_ok = back_url == ORIGIN + "/"
        finally:
            release = client.post(WORKER + "/gate", headers=HEADERS, json={"owner": "none"})
            release.raise_for_status()
            if release.json().get("control_owner") != "none":
                raise SystemExit("control_gate_release_failed")
        verdict = "BLOCKED" if challenge else ("PASS" if back_ok else "PARTIAL")
        report = {
            "schema": "antibot-demoqa/v1", "site": "demoqa", "profile_id": profile_id, "run": 1,
            "started_at_utc": started, "verdict": verdict,
            "scope": "public home -> Elements -> history back; read-only headings only",
            "guest_profile": {"provenance": "dedicated disposable worker_state volume",
                              "cookies_imported": False, "authentication": "no account credentials used"},
            "runtime": {"browser_backend": before.get("backend"), "viewport": [1440, 900],
                        "network_route": "antibot-2026-10-31-direct", "route_generation": 1},
            "steps": {"home_labels": home_check, "elements_labels": elements_check,
                      "history_back_to_home": back_ok, "challenge_detected": challenge},
            "evidence_scope": "fixed text and URL booleans; no screenshots, cookies, or raw page data",
        }
    out = Path("/tmp/antibot-artifacts/demoqa") / profile_id
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    (out / "metadata.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
