"""Read-only guard before replacing the single production Camoufox worker."""
from __future__ import annotations

import json
import os
import sys

import httpx


def main() -> int:
    base = os.environ.get("DEMOQA_PRODUCTION_CONTROL_URL", "http://127.0.0.1:8080").rstrip("/")
    timeout = httpx.Timeout(5.0, connect=2.0)
    summary: dict[str, object] = {"ready": False}
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
            # In local mode this first GET creates the UI cookie session. Do not query
            # protected APIs before it or an auth redirect can look like valid JSON.
            session = client.get(base + "/api/v1/operator/session")
            if session.status_code != 200 or not session.json().get("authenticated"):
                raise RuntimeError("operator session is unavailable")
            state_response = client.get(base + "/api/v1/desktop/state")
            tasks_response = client.get(base + "/api/v1/tasks")
            state_response.raise_for_status()
            tasks_response.raise_for_status()
            state, task_payload = state_response.json(), tasks_response.json()
            tasks = task_payload.get("tasks", [])
            statuses = [str(row.get("status", "")).lower() for row in tasks if isinstance(row, dict)]
            active_statuses = sorted({status for status in statuses if status in {"queued", "running", "recovering"}})
            worker_idle = (
                state.get("browser_started") is True
                and state.get("control_owner") == "none"
                and state.get("action_in_progress") is False
                and state.get("import_in_progress") is False
                and state.get("context_count") == 1
                and state.get("page_count") == 1
            )
            tasks_idle = not active_statuses
            summary = {
                "ready": worker_idle and tasks_idle,
                "worker_idle": worker_idle,
                "tasks_idle": tasks_idle,
                "active_task_statuses": active_statuses,
                "task_count": len(tasks),
            }
    except (httpx.HTTPError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        summary = {"ready": False, "reason": type(exc).__name__}
    print(json.dumps(summary, ensure_ascii=True, separators=(",", ":")))
    return 0 if summary.get("ready") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
