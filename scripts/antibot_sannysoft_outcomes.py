"""Read allowlisted outcome predicates from the already-open Sannysoft page."""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

PREDICATES = {
    "webdriver": ["missing (passed)", "present (failed)"],
    "chrome": ["missing (failed)", "present (passed)"],
    "permissions": ["prompt", "granted", "denied"],
}
SELECTORS = {
    "webdriver": "#webdriver-result",
    "chrome": "#chrome-result",
    "permissions": "#permissions-result",
}


def main() -> int:
    worker_url = os.environ.get("BROWSER_WORKER_URL", "http://browser:7070")
    if (os.environ.get("APP_ENV") != "test" or os.environ.get("READINESS_DISPOSABLE") != "1"
            or worker_url != "http://browser:7070"
            or os.environ.get("BROWSER_MCP_ORIGIN") != "http://localhost:18081"):
        raise SystemExit("disposable_runtime_markers_required")
    key = Path(os.environ["WORKER_API_KEY_FILE"]).read_text(encoding="utf-8").strip()
    headers = {"Authorization": f"Bearer {key}"}
    with httpx.Client(timeout=5, trust_env=False) as client:
        status = client.get(worker_url + "/status", headers=headers)
        status.raise_for_status()
        state = status.json()
        if state.get("url") != "https://bot.sannysoft.com/" or state.get("control_owner") != "none":
            raise SystemExit("open_sannysoft_page_precondition_failed")
        conditions = [
            {"kind": "element_text_contains", "selector": SELECTORS[name], "text": candidate}
            for name, candidates in PREDICATES.items() for candidate in candidates
        ]
        response = client.post(worker_url + "/verify", headers=headers, json={"conditions": conditions})
        response.raise_for_status()
        result = response.json()
        checks = result.get("checks")
        if not isinstance(checks, list) or len(checks) != len(conditions):
            raise SystemExit("verification_shape_invalid")
        values = [item.get("passed") is True for item in checks if isinstance(item, dict)]
        if len(values) != len(conditions):
            raise SystemExit("verification_result_invalid")
    output = {}
    cursor = 0
    for name, candidates in PREDICATES.items():
        matches = [candidate for candidate, passed in zip(candidates, values[cursor:cursor + len(candidates)]) if passed]
        output[name] = matches[0] if len(matches) == 1 else "unverified_or_ambiguous"
        cursor += len(candidates)
    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
