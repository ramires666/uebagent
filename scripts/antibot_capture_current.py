"""Record a bounded, sanitized verification of the already-open antibot test page.

This helper never navigates and persists only URL/origin, fixed page labels,
challenge indicators, run metadata, and verifier pass/fail booleans.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

TARGETS = {
    "browserleaks_js": ("https://browserleaks.com/javascript", ["BrowserLeaks.com", "JavaScript"]),
    "browserleaks_webgl": ("https://browserleaks.com/webgl", ["BrowserLeaks.com", "WebGL"]),
    "eff": ("https://coveryourtracks.eff.org/", ["Cover Your Tracks", "Test Your Browser"]),
    "demoqa_elements": ("https://demoqa.com/elements", ["Elements", "Text Box"]),
    "amazon_home": ("https://www.amazon.com/", ["Amazon", "Click the button below to continue shopping", "Continue shopping"]),
    "ebay_home": ("https://www.ebay.com/", ["eBay", "Search"]),
}
CHALLENGE_TEXT = ["Verify you are human", "Access denied", "Robot Check",
                  "unusual traffic", "Request blocked", "Security Measure",
                  "Click the button below to continue shopping"]


def main() -> int:
    site = os.environ.get("ANTIBOT_SITE", "")
    if site not in TARGETS:
        raise SystemExit("site_not_allowlisted")
    worker = os.environ.get("BROWSER_WORKER_URL", "http://browser:7070")
    if (os.environ.get("APP_ENV") != "test" or os.environ.get("READINESS_DISPOSABLE") != "1"
            or worker != "http://browser:7070"
            or os.environ.get("BROWSER_MCP_ORIGIN") != "http://localhost:18081"):
        raise SystemExit("disposable_runtime_markers_required")
    key_path = Path(os.environ["WORKER_API_KEY_FILE"])
    headers = {"Authorization": "Bearer " + key_path.read_text(encoding="utf-8").strip()}
    expected_url, labels = TARGETS[site]
    conditions = ([{"kind": "url_is", "url": expected_url}]
                  + [{"kind": "text_present", "text": x} for x in labels]
                  + [{"kind": "text_present", "text": x} for x in CHALLENGE_TEXT])
    with httpx.Client(timeout=15, trust_env=False) as client:
        status_response = client.get(worker + "/status", headers=headers)
        status_response.raise_for_status()
        status = status_response.json()
        if (status.get("control_owner") != "none" or status.get("width") != 1440
                or status.get("height") != 900
                or status.get("proxy_assignment_id") != "antibot-2026-10-31-direct"
                or status.get("route_generation") != 1):
            raise SystemExit("current_page_precondition_failed")
        # Do not query or retain JS values, network details, cookies, or fingerprints.
        if status.get("url") != expected_url:
            raise SystemExit("current_url_mismatch_no_navigation_performed")
        response = client.post(worker + "/verify", headers=headers, json={"conditions": conditions})
        response.raise_for_status()
        checks = response.json().get("checks")
        if not isinstance(checks, list) or len(checks) != len(conditions):
            raise SystemExit("verification_shape_invalid")
        passed = [item.get("passed") is True for item in checks if isinstance(item, dict)]
        if len(passed) != len(conditions):
            raise SystemExit("verification_result_invalid")
    url = status["url"]
    origin = urlsplit(url)
    challenge_start = 1 + len(labels)
    metadata = {
        "schema": "antibot-current-capture/v1",
        "site": site,
        "profile_id": os.environ.get("ANTIBOT_PROFILE", "p1"),
        "run": int(os.environ.get("ANTIBOT_RUN", "1")),
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "verdict": "BLOCKED" if any(passed[challenge_start:]) else ("PARTIAL" if not all(passed[:challenge_start]) else "PAGE_VERIFIED"),
        "runtime": {"browser_backend": "camoufox", "viewport": [1440, 900],
                    "network_route": "antibot-2026-10-31-direct", "route_generation": 1},
        "navigation": {"expected_url": expected_url, "observed_url": expected_url,
                       "observed_origin": f"{origin.scheme}://{origin.netloc}",
                       "performed_by_capture": False, "page_url_check_passed": passed[0]},
        "page_labels": {label: passed[index + 1] for index, label in enumerate(labels)},
        "challenge_indicators": {label: passed[challenge_start + index]
                                 for index, label in enumerate(CHALLENGE_TEXT)},
        "evidence_scope": "bounded URL, fixed text-presence checks, no raw page data",
    }
    out = Path("/tmp/antibot-artifacts") / site / metadata["profile_id"]
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    (out / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metadata, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
