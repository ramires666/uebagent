#!/usr/bin/env python3
"""Safely prepare or explicitly resume-and-clone the expired Gemini task.

Dry-run is the default. This script intentionally reports only identifiers,
statuses, safe error codes, and action/event counts. It never prints task text,
user inputs, cookies, session data, or authentication tokens.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import sys
import time
from typing import Any

import httpx


SOURCE_TASK_ID = "87fa3533-843b-4b13-864f-dbe3dcbd30ad"
BASE_URL = "http://localhost:8090"
EXPECTED_FAILURE = "CAPABILITY_UNAVAILABLE"
EXPECTED_PHASE = "capability_unavailable"
EXPECTED_ACTION_KINDS = {"navigate", "gemini_refresh_session"}


class SafetyStop(RuntimeError):
    pass


class ApiFailure(SafetyStop):
    def __init__(self, message: str, code: str | None):
        super().__init__(message)
        self.code = code


def safe_record(record: dict[str, Any]) -> dict[str, Any]:
    error = record.get("error")
    return {
        "task_id": record.get("task_id"),
        "status": record.get("status"),
        "version": record.get("version"),
        "phase": record.get("phase"),
        "step_count": record.get("step_count"),
        "error_code": error.get("code") if isinstance(error, dict) else None,
    }


def api(client: httpx.Client, method: str, path: str, **kwargs: Any) -> Any:
    response = client.request(method, path, **kwargs)
    if response.status_code >= 400:
        try:
            body = response.json()
            error = body.get("error", {}) if isinstance(body, dict) else {}
            code = error.get("code") if isinstance(error, dict) else None
        except (ValueError, UnicodeDecodeError):
            code = None
        raise ApiFailure(f"API request failed: {method} {path} HTTP {response.status_code} code={code or 'UNKNOWN'}", code)
    try:
        return response.json()
    except (ValueError, UnicodeDecodeError) as exc:
        raise SafetyStop(f"API returned invalid JSON: {method} {path}") from exc


def read_all_events(client: httpx.Client, base_url: str, task_id: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    cursor = 0
    while True:
        page = api(client, "GET", f"{base_url}/api/v1/tasks/{task_id}/events",
                   params={"after_event_id": cursor})
        batch = page.get("events") if isinstance(page, dict) else None
        if not isinstance(batch, list):
            raise SafetyStop("Task event history was malformed")
        if not batch:
            return events
        for event in batch:
            event_id = event.get("event_id") if isinstance(event, dict) else None
            if type(event_id) is not int or event_id <= cursor:
                raise SafetyStop("Task event history cursor was invalid")
            cursor = event_id
            events.append(event)


def validate_action_history(events: list[dict[str, Any]]) -> dict[str, int]:
    planned: dict[str, str] = {}
    terminals: dict[str, tuple[str, str | None]] = {}
    counts = {"navigate_applied": 0, "refresh_failed": 0, "planned": 0}
    for event in events:
        kind = event.get("event_type")
        payload = event.get("payload")
        if not isinstance(payload, dict):
            if isinstance(kind, str) and kind.startswith("action."):
                raise SafetyStop("Action history contains a malformed action event")
            continue
        action_id = payload.get("action_id")
        action_kind = payload.get("kind")
        if kind == "action.planned":
            if (not isinstance(action_id, str) or action_kind not in EXPECTED_ACTION_KINDS
                    or action_id in planned):
                raise SafetyStop("Action history contains an unexpected or duplicate planned action")
            planned[action_id] = action_kind
            counts["planned"] += 1
        elif kind in {"action.applied", "action.failed"}:
            if not isinstance(action_id, str):
                candidates = [candidate for candidate, planned_kind in planned.items()
                              if planned_kind == action_kind and candidate not in terminals]
                if len(candidates) != 1:
                    raise SafetyStop("Legacy action receipt is ambiguous")
                action_id = candidates[0]
            if action_id not in planned:
                raise SafetyStop("Action history contains an unmatched action receipt")
            if action_kind != planned[action_id] or action_id in terminals:
                raise SafetyStop("Action history contains a conflicting action receipt")
            code = payload.get("code") if isinstance(payload.get("code"), str) else None
            if kind == "action.applied" and action_kind == "navigate":
                counts["navigate_applied"] += 1
            elif (kind == "action.failed" and action_kind == "gemini_refresh_session"
                  and code == EXPECTED_FAILURE):
                counts["refresh_failed"] += 1
            else:
                raise SafetyStop("Action history includes a write or outcome outside the expected safe failure")
            terminals[action_id] = (kind, code)
        elif isinstance(kind, str) and kind.startswith("action."):
            raise SafetyStop("Action history contains an unexpected action event")
    if set(planned) != set(terminals):
        raise SafetyStop("Action history has an unresolved planned action")
    if counts["navigate_applied"] != 1 or counts["refresh_failed"] != 1 or counts["planned"] != 2:
        raise SafetyStop("Action history does not match the expected navigate plus failed refresh sequence")
    return counts


def verify_readiness(client: httpx.Client, base_url: str) -> dict[str, Any]:
    status = api(client, "GET", f"{base_url}/api/v1/status")
    worker = status.get("worker", {}) if isinstance(status, dict) else {}
    if (not isinstance(worker, dict) or worker.get("ready") is not True or worker.get("browser_started") is not True
            or worker.get("context_count") != 1 or worker.get("page_count") != 1
            or worker.get("resource_violation") is not False or worker.get("egress_ready") is not True):
        raise SafetyStop("Browser worker is not ready with exactly one healthy context and page")
    sources_body = api(client, "GET", f"{base_url}/api/v1/chrome/sources")
    sources = sources_body.get("sources") if isinstance(sources_body, dict) else None
    online = [source for source in sources or [] if isinstance(source, dict) and source.get("online") is True]
    if len(online) != 1:
        raise SafetyStop("Exactly one paired Chrome cookie source must be online")
    browser = api(client, "GET", f"{base_url}/api/v1/desktop/state")
    url = browser.get("url") if isinstance(browser, dict) else None
    if not isinstance(browser, dict) or browser.get("available") is not True or not isinstance(url, str):
        raise SafetyStop("Browser page state could not be confirmed")
    if url == "about:blank":
        # A new recovered page has no Gemini session to probe yet. The task
        # runtime checks authentication after its ordinary public navigation
        # and before any typed input or submit.
        auth_state = "deferred_until_navigation"
    elif url.startswith("https://gemini.google.com/"):
        auth = api(client, "GET", f"{base_url}/api/v1/capabilities/gemini-auth/status")
        if auth.get("status") != "signed_in" or auth.get("reason_code") != "ACCOUNT_CONTROLS_PRESENT":
            raise SafetyStop("Gemini sign-in state is not independently confirmed")
        auth_state = "signed_in"
    else:
        raise SafetyStop("Current browser page is neither blank nor on Gemini")
    return {"worker_ready": True, "context_count": 1, "page_count": 1, "chrome_sources_online": 1,
            "gemini_auth": auth_state}


def deadline_elapsed(record: dict[str, Any]) -> bool:
    spec = record.get("spec")
    limits = spec.get("limits") if isinstance(spec, dict) else None
    timeout = limits.get("timeout_s") if isinstance(limits, dict) else None
    created = record.get("created_at")
    if type(timeout) is not int or not isinstance(created, str):
        return False
    try:
        stamp = datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - stamp).total_seconds() > timeout + 120


def run(args: argparse.Namespace) -> int:
    base_url = args.base_url.rstrip("/")
    with httpx.Client(timeout=httpx.Timeout(15, connect=3), trust_env=False) as client:
        session = api(client, "GET", f"{base_url}/api/v1/operator/session")
        csrf = session.get("csrf_token") if isinstance(session, dict) else None
        if not isinstance(session, dict) or session.get("authenticated") is not True or not isinstance(csrf, str):
            raise SafetyStop("Local operator session is unavailable")
        headers = {"X-CSRF-Token": csrf}
        source = api(client, "GET", f"{base_url}/api/v1/tasks/{args.task_id}")
        if not isinstance(source, dict):
            raise SafetyStop("Source task record was malformed")
        safe = safe_record(source)
        still_waiting = (source.get("status") == "waiting_human"
                         and source.get("phase") == EXPECTED_PHASE
                         and safe["error_code"] == EXPECTED_FAILURE)
        already_expired = (source.get("status") == "expired"
                           and source.get("phase") == "task_deadline_exceeded"
                           and safe["error_code"] == "TASK_EXPIRED")
        if (not (still_waiting or already_expired) or source.get("step_count") != 1):
            raise SafetyStop("Source task is not the expected expired capability failure")
        spec = source.get("spec")
        if not isinstance(spec, dict) or spec.get("workflow") != "agent":
            raise SafetyStop("Source task does not have the expected generic task workflow")
        events = read_all_events(client, base_url, args.task_id)
        actions = validate_action_history(events)
        readiness = verify_readiness(client, base_url)
        if not deadline_elapsed(source):
            raise SafetyStop("Source task deadline has not safely elapsed according to its stored timestamp")
        result = {"mode": "execute" if args.execute else "dry_run", "source": safe,
                  "event_count": len(events), "action_counts": actions, "readiness": readiness}
        if not args.execute:
            print(result)
            print("Dry run only. No task state or external service was changed.")
            return 0

        resumed = None
        if source.get("status") != "expired":
            try:
                resumed = api(client, "POST", f"{base_url}/api/v1/tasks/{args.task_id}/resume",
                              headers=headers, json={"expected_version": source["version"]})
            except ApiFailure as exc:
                if exc.code != "TASK_EXPIRED":
                    raise
                current = api(client, "GET", f"{base_url}/api/v1/tasks/{args.task_id}")
                if current.get("status") != "expired":
                    raise
            if resumed is not None and resumed.get("status") != "expired":
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    time.sleep(0.25)
                    current = api(client, "GET", f"{base_url}/api/v1/tasks/{args.task_id}")
                    if current.get("status") == "expired":
                        break
                    if current.get("status") != "running":
                        raise SafetyStop("Source task did not transition directly to expired")
                else:
                    raise SafetyStop("Source task did not expire promptly; clone was not created")

        expired = api(client, "GET", f"{base_url}/api/v1/tasks/{args.task_id}")
        if (not isinstance(expired, dict) or expired.get("status") != "expired"
                or expired.get("spec") != spec):
            raise SafetyStop("Source task is not confirmed expired; clone was not created")
        post_resume_actions = validate_action_history(read_all_events(client, base_url, args.task_id))
        if post_resume_actions != actions:
            raise SafetyStop("Source action history changed; clone was not created")
        clone_spec = dict(spec)
        clone_spec["idempotency_key"] = "recovery-" + args.task_id
        created = api(client, "POST", f"{base_url}/api/v1/tasks", headers=headers, json=clone_spec)
        if (not isinstance(created, dict) or created.get("spec") != clone_spec
                or created.get("task_id") == args.task_id):
            raise SafetyStop("Created task did not preserve the source spec exactly")
        safe_new = safe_record(created)
        print({"mode": "executed", "source": safe_record(expired), "new_task": safe_new,
               "action_counts": actions, "readiness": readiness})
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", default=SOURCE_TASK_ID)
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--execute", action="store_true", help="perform the versioned resume and exact-spec clone")
    args = parser.parse_args()
    try:
        return run(args)
    except SafetyStop as exc:
        print(f"Stopped safely: {exc}", file=sys.stderr)
        return 2
    except httpx.HTTPError:
        print("Stopped safely: local API request failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
