"""Run one fixed public diagnostic navigation in the isolated headed worker."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

TARGETS = {
    "sannysoft": ("https://bot.sannysoft.com/", "https://bot.sannysoft.com",
                  ["Intoli.com tests", "WebDriver", "WebDriver Advanced", "Chrome",
                   "permissions", "plugins", "languages"]),
    "browserleaks_js": ("https://browserleaks.com/javascript", "https://browserleaks.com",
                        ["BrowserLeaks.com", "JavaScript"]),
    "browserleaks_webgl": ("https://browserleaks.com/webgl", "https://browserleaks.com",
                           ["BrowserLeaks.com", "WebGL"]),
    "eff": ("https://coveryourtracks.eff.org/", "https://coveryourtracks.eff.org",
            ["Cover Your Tracks", "Test Your Browser"]),
    "demoqa_elements": ("https://demoqa.com/elements", "https://demoqa.com",
                        ["Elements", "Text Box"]),
    "amazon_home": ("https://www.amazon.com/", "https://www.amazon.com",
                    ["Amazon", "Search"]),
    "ebay_home": ("https://www.ebay.com/", "https://www.ebay.com",
                  ["eBay", "Search"]),
}
ALLOWED_INITIAL_ORIGINS = {item[1] for item in TARGETS.values()}
CHALLENGE_TEXT = ["Verify you are human", "Access denied", "Robot Check",
                  "unusual traffic", "Request blocked", "Security Measure",
                  "Click the button below to continue shopping"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def origin_of(url: object) -> str | None:
    try:
        parsed = urlsplit(url if isinstance(url, str) else "")
        if parsed.scheme == "https" and parsed.hostname:
            return f"https://{parsed.hostname.lower()}"
    except ValueError:
        pass
    return None


def main() -> int:
    site = os.environ.get("ANTIBOT_SITE", "")
    if site not in TARGETS:
        raise SystemExit("site_not_allowlisted")
    if (os.environ.get("APP_ENV") != "test" or os.environ.get("READINESS_DISPOSABLE") != "1"
            or os.environ.get("BROWSER_WORKER_URL") != "http://browser:7070"
            or os.environ.get("BROWSER_MCP_ORIGIN") != "http://localhost:18081"):
        raise SystemExit("disposable_runtime_markers_required")
    url, expected_origin, required_text = TARGETS[site]
    profile_id = os.environ.get("ANTIBOT_PROFILE", "p1")
    if profile_id not in {"p1", "p2"}:
        raise SystemExit("profile_not_allowlisted")
    run = int(os.environ.get("ANTIBOT_RUN", "1"))
    key = Path(os.environ["WORKER_API_KEY_FILE"]).read_text(encoding="utf-8").strip()
    worker_url = os.environ.get("BROWSER_WORKER_URL", "http://browser:7070")
    headers = {"Authorization": f"Bearer {key}"}
    out_dir = Path("/tmp/antibot-artifacts") / site / profile_id
    out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    started_at = utc_now()
    gate_acquired = False
    report = None

    with httpx.Client(timeout=20.0, trust_env=False) as client:
        initial = client.get(worker_url + "/status", headers=headers)
        initial.raise_for_status()
        initial_status = initial.json()
        if (initial_status.get("browser_started") is not True
                or initial_status.get("control_owner") != "none"
                or initial_status.get("action_in_progress") is not False
                or initial_status.get("import_in_progress") is not False
                or initial_status.get("width") != 1440
                or initial_status.get("height") != 900
                or initial_status.get("proxy_assignment_id") != "antibot-2026-10-31-direct"
                or initial_status.get("route_generation") != 1):
            raise SystemExit("idle_single_page_precondition_failed")
        desktop_response = client.get("http://localhost:8080/api/v1/desktop/state")
        desktop_response.raise_for_status()
        desktop_state = desktop_response.json()
        if (desktop_state.get("context_count") != 1 or desktop_state.get("page_count") != 1
                or desktop_state.get("control_owner") != "none"):
            raise SystemExit("single_context_page_or_owner_precondition_failed")
        initial_origin = origin_of(initial_status.get("url"))
        if (initial_status.get("url") not in {None, "about:blank"}
                and initial_origin not in ALLOWED_INITIAL_ORIGINS):
            raise SystemExit("unrecognized_initial_page")

        try:
            gate_response = client.post(worker_url + "/gate", headers=headers, json={"owner": "human"})
            gate_response.raise_for_status()
            gate = gate_response.json()
            epoch = gate.get("control_epoch")
            if type(epoch) is not int or gate.get("control_owner") != "human":
                raise SystemExit("human_gate_not_acquired")
            gate_acquired = True

            navigation = client.post(worker_url + "/manual/execute", headers=headers, json={
                "control_epoch": epoch,
                "allowed_origins": [expected_origin],
                "allow_cross_origin_navigation": True,
                "action": {"kind": "navigate", "url": url},
            })
            navigation.raise_for_status()
            receipt = navigation.json()
            if receipt.get("outcome") != "applied":
                raise SystemExit("navigation_not_applied")
            epoch = receipt.get("control_epoch")
            time.sleep(5)

            conditions = ([{"kind": "url_is", "url": url}]
                          + [{"kind": "text_present", "text": value} for value in required_text]
                          + [{"kind": "text_present", "text": value} for value in CHALLENGE_TEXT])
            response = client.post(worker_url + "/verify", headers=headers,
                                   json={"conditions": conditions})
            response.raise_for_status()
            verification = response.json()
            checks = verification.get("checks")
            if not isinstance(checks, list) or len(checks) != len(conditions):
                raise SystemExit("verification_shape_invalid")
            passed = [item.get("passed") is True for item in checks if isinstance(item, dict)]
            if len(passed) != len(conditions):
                raise SystemExit("verification_result_invalid")

            final_response = client.get(worker_url + "/status", headers=headers)
            final_response.raise_for_status()
            final_status = final_response.json()
            observed_origin = origin_of(final_status.get("url"))
            required_checks = passed[1:1 + len(required_text)]
            challenge_checks = passed[1 + len(required_text):]
            challenge = any(challenge_checks)
            expected_url_loaded = passed[0]
            if challenge or (observed_origin not in {None, expected_origin}):
                verdict = "BLOCKED"
            elif observed_origin is None:
                verdict = "UNAVAILABLE"
            elif all(required_checks) and expected_url_loaded:
                verdict = "PARTIAL"
            else:
                verdict = "PARTIAL"

            report = {
                "schema": "antibot-probe/v1", "site": site, "profile_id": profile_id,
                "run": run, "started_at_utc": started_at, "observed_at_utc": utc_now(),
                "verdict": verdict,
                "scope": "fixed public diagnostic navigation; labels only; no outcome inference",
                "guest_profile": {
                    "provenance": "new dedicated disposable worker_state volume",
                    "cookies_imported": False,
                    "initial_page_origin": initial_origin,
                    "site_authentication_checked": "not applicable to this diagnostic page",
                },
                "runtime": {
                    "application_image": os.environ.get("ANTIBOT_CONTROL_IMAGE", "not recorded; see profile provenance"),
                    "browser_backend": final_status.get("backend"),
                    "browser_version": "not exposed by worker status API",
                    "os": "Linux browser container on Docker/WSL2",
                    "viewport": {"width": 1440, "height": 900},
                    "browser_timezone": "not measured",
                    "host_timezone": "Asia/Qyzylorda",
                    "network_route": "dedicated direct-egress network; no proxy rotation",
                    "route_generation": final_status.get("route_generation"),
                },
                "navigation": {"expected_origin": expected_origin,
                               "observed_origin": observed_origin,
                               "expected_url_loaded": expected_url_loaded},
                "required_label_presence": {label: required_checks[index]
                                             for index, label in enumerate(required_text)},
                "challenge_text_detected": challenge,
                "notes": ["No CAPTCHA/challenge was solved or bypassed.",
                          "No IP, cookies, raw fingerprint fields, screenshots, or network dumps retained."],
                "evidence": {"safe_screenshot": None,
                             "raw_screenshot_requested": False,
                             "raw_screenshot_retained": False},
            }
            (out_dir / "metadata.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"site": site, "verdict": verdict,
                              "observed_origin": observed_origin,
                              "required_label_presence": report["required_label_presence"],
                              "challenge_text_detected": challenge}, sort_keys=True))
        finally:
            if gate_acquired:
                release = client.post(worker_url + "/gate", headers=headers, json={"owner": "none"})
                if release.status_code != 200 or release.json().get("control_owner") != "none":
                    raise SystemExit("control_gate_release_failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
