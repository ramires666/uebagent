"""Sanitized, read-only EFF worker state probe for the disposable test runtime."""
from __future__ import annotations

import json
import os
from collections import Counter
from urllib.parse import urlsplit

import httpx

WORKER = "http://browser:7070"
EFF_ORIGINS = frozenset({
    "https://coveryourtracks.eff.org",
    "https://firstpartysimulator.net",
    "https://firstpartysimulator.org",
    "https://trackersimulator.org",
    "https://eviltracker.net",
    "https://do-not-tracker.org",
    "https://media.net",
})


def main() -> int:
    if (os.environ.get("APP_ENV") != "test"
            or os.environ.get("READINESS_DISPOSABLE") != "1"
            or os.environ.get("BROWSER_MCP_ORIGIN") != "http://localhost:18081"
            or os.environ.get("BROWSER_WORKER_URL") != WORKER):
        raise SystemExit("disposable_eff_runtime_markers_required")
    key_path = os.environ["WORKER_API_KEY_FILE"]
    headers = {"Authorization": "Bearer " + open(key_path, encoding="utf-8").read().strip()}
    with httpx.Client(timeout=8, trust_env=False) as client:
        response = client.get(WORKER + "/status", headers=headers)
        response.raise_for_status()
        status = response.json()

    parsed = urlsplit(status.get("url", ""))
    if (status.get("proxy_assignment_id") != "antibot-2026-10-31-direct"
            or status.get("route_generation") != 1):
        raise SystemExit("isolated_eff_route_precondition_failed")
    raw_origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else ""
    page_origin = raw_origin if raw_origin in EFF_ORIGINS else "other_origin"
    page_path = parsed.path if parsed.path in {"/", "/kcarter", "/results"} else "other_path"
    blocked_reasons: Counter[str] = Counter()
    blocked_origin_classes: Counter[str] = Counter()
    for item in status.get("blocked_resources", []):
        if not isinstance(item, dict):
            continue
        reason = item.get("reason")
        if isinstance(reason, str) and reason.isascii() and reason.replace("_", "").isalnum():
            count = item.get("count", 1)
            blocked_reasons[reason] += count if type(count) is int and 1 <= count <= 100000 else 1
        origin = item.get("origin")
        if not isinstance(origin, str):
            origin_class = "unknown_origin"
        else:
            host = urlsplit(origin).hostname
            candidate = "https://" + host if host else ""
            origin_class = candidate if candidate in EFF_ORIGINS else "other_origin"
        blocked_origin_classes[origin_class] += 1

    output = {
        "schema": "eff-remediation-state/v1",
        "page_origin": page_origin,
        "page_path": page_path,
        "control_owner": status.get("control_owner"),
        "manual_cross_origin_navigation": status.get("manual_cross_origin_navigation"),
        "context_count": status.get("context_count"),
        "page_count": status.get("page_count"),
        "action_in_progress": status.get("action_in_progress"),
        "resource_violation": status.get("resource_violation"),
        "recovery_needed": status.get("recovery_needed"),
        "viewport": [status.get("width"), status.get("height")],
        "route_generation": status.get("route_generation"),
        "blocked_reason_counts": dict(sorted(blocked_reasons.items())),
        "blocked_origin_class_counts": dict(sorted(blocked_origin_classes.items())),
    }
    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
