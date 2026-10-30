"""Read-only guard before replacing the single production Camoufox worker."""
from __future__ import annotations

import argparse
import json
import os
from urllib.parse import urlsplit

import httpx


TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "expired"}
SUSPENDED_STATUSES = {"paused", "waiting_human", "waiting_approval"}
DEFAULT_CONTROL_URL = "http://127.0.0.1:8090"


def _valid_control_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme == "http"
            and parsed.hostname in {"localhost", "127.0.0.1"}
            and parsed.port == 8090
            and parsed.path in {"", "/"}
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        return False


def evaluate(
    base: str,
    *,
    allow_suspended_tasks: bool = False,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, object]:
    if not _valid_control_url(base):
        return {"ready": False, "reason": "control_url_rejected"}
    base = base.rstrip("/")
    timeout = httpx.Timeout(5.0, connect=2.0)
    try:
        with httpx.Client(
            timeout=timeout, follow_redirects=False, trust_env=False, transport=transport
        ) as client:
            # Local operator mode initializes the UI session on this first GET.
            session = client.get(base + "/api/v1/operator/session")
            try:
                authenticated = session.status_code == 200 and session.json().get("authenticated") is True
            except (ValueError, TypeError, AttributeError):
                authenticated = False
            if not authenticated:
                return {"ready": False, "reason": "operator_session_unavailable"}

            state_response = client.get(base + "/api/v1/desktop/state")
            tasks_response = client.get(base + "/api/v1/tasks")
            if state_response.status_code != 200 or tasks_response.status_code != 200:
                return {"ready": False, "reason": "production_state_unavailable"}
            state, task_payload = state_response.json(), tasks_response.json()
            if not isinstance(state, dict) or not isinstance(task_payload, dict):
                return {"ready": False, "reason": "production_state_invalid"}
            tasks = task_payload.get("tasks")
            if not isinstance(tasks, list) or any(not isinstance(row, dict) for row in tasks):
                return {"ready": False, "reason": "production_tasks_invalid"}

            statuses = [str(row.get("status", "")).lower() for row in tasks]
            suspended = [status for status in statuses if status in SUSPENDED_STATUSES]
            blocked_statuses = sorted({
                status for status in statuses
                if status not in TERMINAL_STATUSES
                and not (allow_suspended_tasks and status in SUSPENDED_STATUSES)
            })
            worker_idle = (
                state.get("available") is True
                and state.get("browser_started") is True
                and state.get("control_owner") == "none"
                and state.get("action_in_progress") is False
                and state.get("import_in_progress") is False
                and state.get("context_count") == 1
                and state.get("page_count") == 1
                and state.get("recovery_state") == "idle"
                and state.get("recovery_needed") is False
                and state.get("resource_violation") is False
            )
            tasks_idle = not blocked_statuses
            return {
                "ready": worker_idle and tasks_idle,
                "worker_idle": worker_idle,
                "tasks_idle": tasks_idle,
                "active_task_statuses": blocked_statuses,
                "suspended_task_count": len(suspended),
                "suspended_task_statuses": sorted(set(suspended)),
                "task_count": len(tasks),
            }
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        return {"ready": False, "reason": type(exc).__name__}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--control-url",
        default=os.environ.get("DEMOQA_PRODUCTION_CONTROL_URL", DEFAULT_CONTROL_URL),
    )
    parser.add_argument("--allow-suspended-tasks", action="store_true")
    args = parser.parse_args()
    summary = evaluate(args.control_url, allow_suspended_tasks=args.allow_suspended_tasks)
    print(json.dumps(summary, ensure_ascii=True, separators=(",", ":")))
    return 0 if summary.get("ready") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
