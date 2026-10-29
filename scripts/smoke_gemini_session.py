"""Bounded live check for default cookie sync and exact Gemini fast submission.

Run without arguments for a read only auth/readiness check. The live send path
requires the explicit --send-test flag and uses only the fixed harmless prompt.
This source file is ASCII-only; Russian text is constructed with Unicode escapes.
"""

from __future__ import annotations

import argparse
import collections
import time
import uuid

import httpx


BASE_URL = "http://127.0.0.1:8090"
GEMINI = "https://gemini.google.com"
GOAL = "\u0422\u0435\u0441\u0442 \u043a\u0438\u0440\u0438\u043b\u043b\u0438\u0446\u044b \u0432 Gemini"
PROMPT = "\u041e\u0442\u0432\u0435\u0442\u044c \u043e\u0434\u043d\u0438\u043c \u0441\u043b\u043e\u0432\u043e\u043c: \u0413\u041e\u0422\u041e\u0412\u041e."
CYRILLIC = ("\u0400", "\u04ff")


def codepoint_counts(value: str) -> tuple[int, int, int]:
    cyrillic = sum(CYRILLIC[0] <= char <= CYRILLIC[1] for char in value)
    return len(value), cyrillic, value.count("?")


def report_http_failure(label: str, response: httpx.Response) -> None:
    code = "unknown"
    try:
        body = response.json()
        error = body.get("error", body.get("detail", {})) if isinstance(body, dict) else {}
        code = error.get("code", "unknown") if isinstance(error, dict) else "unknown"
    except (ValueError, UnicodeDecodeError):
        pass
    print(f"{label}_http={response.status_code} code={code}", flush=True)


def run(send_test: bool) -> int:
    if codepoint_counts(PROMPT) != (28, 23, 0):
        raise RuntimeError("the fixed smoke prompt failed its Unicode codepoint check")
    if not send_test:
        with httpx.Client(base_url=BASE_URL, trust_env=False, timeout=8) as client:
            session = client.get("/api/v1/operator/session")
            print(f"session_http={session.status_code}")
            if session.status_code != 200:
                return 1
            status = client.get("/api/v1/status")
            if status.status_code != 200:
                report_http_failure("status", status)
                return 1
            data = status.json()
            worker = data.get("worker", {})
            print("worker_ready=" + str(worker.get("ready"))
                  + " context_count=" + str(worker.get("context_count"))
                  + " page_count=" + str(worker.get("page_count"))
                  + " sources_count=" + str(data.get("sources_count")))
            auth = client.get("/api/v1/capabilities/gemini-auth/status")
            if auth.status_code == 200:
                result = auth.json()
                print("auth=" + str(result.get("status")) + " reason=" + str(result.get("reason_code")))
            else:
                report_http_failure("auth", auth)
                return 1
        return 0

    started_at = time.monotonic()
    with httpx.Client(base_url=BASE_URL, trust_env=False, timeout=30) as client:
        session = client.get("/api/v1/operator/session")
        if session.status_code != 200:
            report_http_failure("session", session)
            return 1
        csrf = session.json().get("csrf_token")
        if not isinstance(csrf, str) or not csrf:
            print("csrf_unavailable")
            return 1
        headers = {"X-CSRF-Token": csrf}

        status_response = client.get("/api/v1/status")
        if status_response.status_code != 200:
            report_http_failure("status", status_response)
            return 1
        status_data = status_response.json()
        worker = status_data.get("worker", {})
        if (worker.get("ready") is not True or worker.get("context_count") != 1
                or worker.get("page_count") != 1 or status_data.get("sources_count") != 1):
            print("preflight_not_ready worker_ready=" + str(worker.get("ready"))
                  + " context_count=" + str(worker.get("context_count"))
                  + " page_count=" + str(worker.get("page_count"))
                  + " sources_count=" + str(status_data.get("sources_count")))
            return 2

        tasks_response = client.get("/api/v1/tasks")
        if tasks_response.status_code != 200:
            report_http_failure("tasks", tasks_response)
            return 2
        tasks = tasks_response.json().get("tasks", [])
        task_counts = collections.Counter(task.get("status", "unknown") for task in tasks)
        active = [task for task in tasks if task.get("status") not in {
            "succeeded", "failed", "cancelled", "expired"}]
        print("preflight_task_counts=" + repr(dict(task_counts))
              + " active_count=" + str(len(active)))
        if active:
            return 2

        payload = {
            "goal": GOAL,
            "goal_language": "ru",
            "allowed_origins": [GEMINI],
            "verbatim_text": PROMPT,
            "execution_mode": "autonomous",
            "workflow": "gemini_prompt_submit",
            "site_capabilities": ["gemini_prompt_submit"],
            "success_conditions": [],
            "idempotency_key": "unicode-session-" + uuid.uuid4().hex,
        }
        if payload["goal"] != GOAL or payload["verbatim_text"] != PROMPT:
            raise RuntimeError("request payload differs from fixed Unicode test literals")

        created = client.post("/api/v1/tasks", headers=headers, json=payload)
        if created.status_code != 200:
            report_http_failure("task_create", created)
            return 3
        record = created.json()
        task_id = record.get("task_id")
        spec = record.get("spec", {})
        if not isinstance(task_id, str):
            print("task_id_unavailable")
            return 3
        stored_goal = spec.get("goal", "")
        stored_prompt = spec.get("verbatim_text", "")
        print("task_id=" + task_id + " status=" + str(record.get("status"))
              + " goal_counts=" + repr(codepoint_counts(stored_goal))
              + " prompt_counts=" + repr(codepoint_counts(stored_prompt))
              + " goal_exact=" + str(stored_goal == GOAL)
              + " prompt_exact=" + str(stored_prompt == PROMPT), flush=True)
        if stored_goal != GOAL or stored_prompt != PROMPT:
            print("stored_text_mismatch; no success claim", flush=True)
            return 4

        event_cursor = 0
        seen: set[str] = set()
        sync_started = sync_completed = refreshed_at = terminal_at = None
        sync_counts = None
        terminal = None
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            events_response = client.get("/api/v1/tasks/" + task_id + "/events",
                                         params={"after_event_id": event_cursor})
            if events_response.status_code == 200:
                for event in events_response.json().get("events", []):
                    event_cursor = max(event_cursor, event.get("event_id", event_cursor))
                    event_type = event.get("event_type")
                    payload_event = event.get("payload") or {}
                    if event_type == "cookie.sync_started" and event_type not in seen:
                        sync_started = time.monotonic()
                        print("sync_started origins_count=" + str(payload_event.get("origins_count")), flush=True)
                        seen.add(event_type)
                    elif event_type == "cookie.sync_completed" and event_type not in seen:
                        sync_completed = time.monotonic()
                        sync_counts = payload_event.get("counts") or {}
                        print("sync_completed total=" + str(sync_counts.get("total"))
                              + " applied=" + str(sync_counts.get("applied"))
                              + " failed=" + str(sync_counts.get("failed")), flush=True)
                        seen.add(event_type)
                    elif event_type == "cookie.sync_failed" and event_type not in seen:
                        print("sync_failed code=" + str(payload_event.get("code")), flush=True)
                        seen.add(event_type)
                    elif event_type == "cookie.session_refreshed" and event_type not in seen:
                        refreshed_at = time.monotonic()
                        print("session_refreshed origin=" + str(payload_event.get("origin")), flush=True)
                        seen.add(event_type)
            task_response = client.get("/api/v1/tasks/" + task_id)
            if task_response.status_code == 200:
                terminal = task_response.json()
                if terminal.get("status") in {
                    "succeeded", "waiting_human", "waiting_approval", "recovering",
                    "failed", "cancelled", "expired",
                }:
                    terminal_at = time.monotonic()
                    break
            time.sleep(0.4)

        if terminal is None:
            print("task_poll_timeout", flush=True)
            return 5
        result = terminal.get("result") or {}
        error = terminal.get("error") or {}
        print("terminal_status=" + str(terminal.get("status"))
              + " phase=" + str(terminal.get("phase"))
              + " error_code=" + str(error.get("code"))
              + " submitted=" + str(result.get("submitted"))
              + " submit_method=" + str(result.get("submit_method"))
              + " enter_dispatched=" + str(result.get("enter_dispatched"))
              + " input_verified=" + str(result.get("input_verified"))
              + " model_calls=" + str(result.get("model_calls"))
              + " response_verified=" + str(result.get("response_verified")), flush=True)
        if sync_started is not None and sync_completed is not None:
            print("sync_duration_ms=" + str(round((sync_completed-sync_started)*1000)), flush=True)
        if sync_completed is not None and refreshed_at is not None:
            print("sync_to_refresh_ms=" + str(round((refreshed_at-sync_completed)*1000)), flush=True)
        if refreshed_at is not None and terminal_at is not None:
            print("refresh_to_terminal_ms=" + str(round((terminal_at-refreshed_at)*1000)), flush=True)
        if terminal_at is not None:
            print("total_elapsed_ms=" + str(round((terminal_at-started_at)*1000)), flush=True)

        events_response = client.get("/api/v1/tasks/" + task_id + "/events")
        applied = []
        if events_response.status_code == 200:
            for event in events_response.json().get("events", []):
                if event.get("event_type") == "action.applied":
                    item = event.get("payload") or {}
                    applied.append((item.get("kind"), item.get("submit_method"), item.get("enter_dispatched")))
        print("applied_action_receipts=" + repr(applied), flush=True)

        auth = client.get("/api/v1/capabilities/gemini-auth/status")
        if auth.status_code == 200:
            auth_data = auth.json()
            print("final_auth=" + str(auth_data.get("status"))
                  + " reason=" + str(auth_data.get("reason_code")), flush=True)
        else:
            report_http_failure("final_auth", auth)

        succeeded = (terminal.get("status") == "succeeded" and result.get("submitted") is True
                     and result.get("submit_method") == "click" and result.get("enter_dispatched") is False
                     and result.get("input_verified") is True and result.get("model_calls") == 0
                     and sync_counts is not None and sync_counts.get("failed") == 0
                     and sync_counts.get("applied") == sync_counts.get("total")
                     and refreshed_at is not None
                     and sum(item[0] == "gemini_submit_prompt" for item in applied) == 1)
        print("integration_success=" + str(succeeded), flush=True)
        return 0 if succeeded else 6


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--send-test", action="store_true",
                        help="run one fixed benign Gemini prompt integration check")
    raise SystemExit(run(parser.parse_args().send_test))
