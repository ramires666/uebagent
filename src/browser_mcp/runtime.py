"""Single-profile execution coordinator; Windows inference and browser RPC stay separate."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlsplit

import httpx

from .config import Settings
from .domain import DomainError, TaskSpec, TaskStatus
from .model_client import ModelClient, ModelError, ModelSettings
from .storage import TaskRepository


TERMINAL = {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.EXPIRED}
GOOGLE_SEARCH_ORIGIN = "https://www.google.com"
HISTORY_ITEMS = 12
HISTORY_ITEM_BYTES = 3 * 1024
VERBATIM_TEXT_TOKEN = "__VERBATIM_TEXT__"
VERBATIM_TEXT_FEEDBACK = "Use the exact supplied text; rephrasing is not allowed."
KEY_CHORD_FEEDBACK = "The previous keyboard action had an invalid key chord. Use unique modifiers and at most one physical key. Do not repeat a multi-key sequence."
TASK_EXPIRED_MESSAGE = "This task's time limit has passed. Use Repeat to edit and create a new task."
GEMINI_PROMPT_ORIGIN = "https://gemini.google.com"
GEMINI_PROMPT_APP_URL = "https://gemini.google.com/app"
GEMINI_PRE_SUBMIT_ERRORS = {
    "DRAFT_NOT_EMPTY", "PROMPT_TEXT_MISMATCH", "CAPABILITY_UNAVAILABLE", "POLICY_DENIED",
    "AUTH_REQUIRED", "AUTH_STATE_UNKNOWN",
}
GEMINI_AUTH_STATUS_PATH = "/capabilities/gemini-auth/status"
GEMINI_AUTH_REASON_CODES = {
    "ACCOUNT_CONTROLS_PRESENT", "SIGN_IN_CONTROL_PRESENT", "PROOF_INCOMPLETE",
    "PROBE_UNAVAILABLE", "NOT_GEMINI",
}
WORKER_RECOVERY_STATES = {"idle", "recovering", "backoff", "exhausted", "blocked"}
WORKER_RECOVERY_OUTCOMES = {"recovered", "already_ready", "deferred", "blocked", "failed"}
WORKER_RECOVERY_REASONS = {
    "healthy", "canonical_page_closed", "browser_disconnected", "operation_in_progress",
    "resource_violation", "egress_unavailable", "recovery_in_progress", "backoff",
    "attempts_exhausted", "teardown_unconfirmed", "launch_failed", "not_recoverable",
    "context_close_failed", "context_close_timeout", "driver_stop_failed",
    "driver_stop_timeout", "pending_page_close", "browser_process_alive",
}
WORKER_RECOVERABLE_REASONS = {"canonical_page_closed", "browser_disconnected"}
WORKER_INFLIGHT_FIELDS = {
    "action_in_progress", "import_in_progress", "input_in_progress", "observe_in_progress",
    "capture_in_progress",
}
WORKER_RECOVERY_WAIT_SECONDS = 30.0
TASK_ACTIVITY_STAGES = {
    "checking_dependencies", "syncing_cookies", "recovering_browser", "observing",
    "thinking", "executing", "verifying",
}


class WorkerClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self._transport = transport

    async def call(self, method: str, path: str, data: dict | None = None, raw: bool = False) -> Any:
        if not self.settings.worker_url:
            raise DomainError("WORKER_NOT_CONFIGURED", "Не задан BROWSER_WORKER_URL")
        key = self.settings.read_secret(self.settings.worker_key_file)
        if not key:
            raise DomainError("WORKER_NOT_CONFIGURED", "Не задан ключ browser worker")
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(15, connect=3), trust_env=False,
                                         transport=self._transport) as client:
                response = await client.request(method, self.settings.worker_url.rstrip("/")+path,
                                                headers={"Authorization": f"Bearer {key}"}, json=data)
                if not response.is_success:
                    try:
                        body = response.json()
                    except (ValueError, UnicodeDecodeError):
                        body = {}
                    error = body.get("error") if isinstance(body, dict) else None
                    if not isinstance(error, dict) and isinstance(body, dict):
                        error = body.get("detail")
                    if not isinstance(error, dict):
                        error = {}
                    code = error.get("code")
                    message = error.get("message")
                    raise DomainError(
                        code if isinstance(code, str) and code else "WORKER_UNAVAILABLE",
                        message if isinstance(message, str) and message else "Browser worker rejected the request",
                    )
                return response.content if raw else response.json()
        except httpx.HTTPError as exc:
            raise DomainError("WORKER_UNAVAILABLE", "Нет связи с browser worker; исход отправленного действия может быть неизвестен") from exc

    async def health(self) -> dict:
        try:
            result = await self.call("GET", "/health")
            return {"configured": True, **result}
        except DomainError as exc:
            return {"configured": bool(self.settings.worker_url), "ready": False, "detail": exc.message}

    async def state(self) -> dict:
        return await self.call("GET", "/status")

    async def recover(self) -> dict:
        result = await self.call("POST", "/recover", {})
        if not isinstance(result, dict):
            raise DomainError("WORKER_UNAVAILABLE", "Browser recovery returned an invalid response")
        state = result.get("recovery_state")
        outcome = result.get("outcome")
        if (not isinstance(outcome, str) or outcome not in WORKER_RECOVERY_OUTCOMES
                or not isinstance(state, str) or state not in WORKER_RECOVERY_STATES
                or type(result.get("recovery_needed")) is not bool
                or type(result.get("recovery_generation")) is not int or result["recovery_generation"] < 0
                or type(result.get("recovery_attempts")) is not int or result["recovery_attempts"] < 0
                or type(result.get("browser_started")) is not bool
                or type(result.get("context_count")) is not int or result["context_count"] < 0
                or type(result.get("page_count")) is not int or result["page_count"] < 0):
            raise DomainError("WORKER_UNAVAILABLE", "Browser recovery returned an invalid response")
        retry_after = result.get("recovery_retry_after_seconds")
        if (retry_after is not None and
                (type(retry_after) not in {int, float} or not 0 <= retry_after <= WORKER_RECOVERY_WAIT_SECONDS)):
            raise DomainError("WORKER_UNAVAILABLE", "Browser recovery returned an invalid response")
        reason = result.get("recovery_reason")
        if reason is not None and (not isinstance(reason, str) or reason not in WORKER_RECOVERY_REASONS):
            raise DomainError("WORKER_UNAVAILABLE", "Browser recovery returned an invalid response")
        return result

    async def gate(self, owner: str) -> dict:
        return await self.call("POST", "/gate", {"owner": owner})

    async def can_import(self) -> bool:
        state = await self.state()
        return state.get("control_owner") == "none" and not state.get("action_in_progress") and not state.get("import_in_progress")

    async def import_cookies(self, job_id: str, records: list[dict]) -> dict:
        return await self.call("POST", "/cookies/import", {"job_id": job_id, "cookies": records})


class TaskRunner:
    def __init__(self, settings: Settings, repository: TaskRepository, worker: Any = None, model: Any = None):
        self.settings, self.repository = settings, repository
        self.worker = worker or WorkerClient(settings)
        prompt_path = Path(settings.model_prompt_file)
        prompt = prompt_path.read_text(encoding="utf-8") if prompt_path.is_file() else ""
        self.model = model or ModelClient(ModelSettings(settings.model_base_url, settings.model_alias,
            settings.read_secret(settings.model_key_file), system_prompt=prompt,
            protocol=settings.model_protocol))
        self.jobs: dict[str, asyncio.Task] = {}
        self.histories: dict[str, list] = {}
        self.cookie_sync = None
        self.gemini_auth_wait_seconds = 10.0

    @staticmethod
    def _bounded_history_item(item: dict) -> dict:
        """Retain exact small actions and summarize oversized context safely."""
        encoded = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) <= HISTORY_ITEM_BYTES:
            return item

        def utf8_prefix(value: Any, budget: int) -> str:
            return str(value).encode("utf-8")[:budget].decode("utf-8", errors="ignore")

        if "model_action" in item:
            action = item["model_action"]
            if action.get("kind") == "remember_fact":
                fact = str(item.get("fact", action.get("text", "")))
                fact_prefix = utf8_prefix(fact, 1600)
                return {"fact": fact_prefix, "source": utf8_prefix(item.get("source", ""), 400),
                        "context_truncated": len(fact_prefix) < len(fact)}
            return {
                "model_action": {"kind": utf8_prefix(action.get("kind", "unknown"), 160), "omitted": True},
                "url": utf8_prefix(item.get("url", ""), 400),
                "outcome": utf8_prefix(item.get("outcome", ""), 64),
                "context_truncated": True,
            }
        if "fact" in item:
            fact = str(item.get("fact", ""))
            fact_prefix = utf8_prefix(fact, 1600)
            return {"fact": fact_prefix, "source": utf8_prefix(item.get("source", ""), 400),
                    "context_truncated": len(fact_prefix) < len(fact)}
        return {"context_truncated": True}

    def _remember(self, task_id: str, item: dict) -> None:
        history = self.histories.setdefault(task_id, [])
        retained = self._bounded_history_item(item)
        is_fact = "fact" in retained
        if is_fact:
            # Keep the freshest copy of a fact and make room for facts learned
            # before later browsing actions, while retaining chronological order.
            fact = retained.get("fact")
            history[:] = [old for old in history if old.get("fact") != fact]
        history.append(retained)

        facts = [entry for entry in history if "fact" in entry]
        actions = [entry for entry in history if "fact" not in entry and entry.get("outcome") == "applied"]
        while len(facts) > 4:
            oldest = facts.pop(0)
            history.remove(oldest)
        while len(actions) > 8:
            oldest = actions.pop(0)
            history.remove(oldest)
        del history[:-HISTORY_ITEMS]

    def _discard_task_memory(self, task_id: str) -> None:
        self.histories.pop(task_id, None)
        discard_task = getattr(self.model, "discard_task", None)
        if callable(discard_task):
            discard_task(task_id)

    def _job_done(self, task_id: str, task: asyncio.Task) -> None:
        if self.jobs.get(task_id) is task:
            self.jobs.pop(task_id, None)
        # Retrieving and discarding the exception prevents a completed task
        # from keeping its traceback (and all referenced frames) alive.
        try:
            task.exception()
        except asyncio.CancelledError:
            pass

    async def start(self) -> None:
        for record in self.repository.list():
            if record.status == TaskStatus.QUEUED:
                self.schedule(record.task_id)
            elif record.status not in TERMINAL | {TaskStatus.PAUSED, TaskStatus.WAITING_HUMAN, TaskStatus.WAITING_APPROVAL, TaskStatus.RECOVERING}:
                self.repository.transition(record.task_id, TaskStatus.RECOVERING, phase="restart_recovery")

    async def close(self) -> None:
        tasks = list(self.jobs.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        clear_conversation = getattr(self.model, "clear_conversation", None)
        if callable(clear_conversation):
            clear_conversation()

    def create(self, spec: TaskSpec):
        if spec.profile_id != "default":
            raise DomainError("PROFILE_NOT_FOUND", "В этом выпуске доступен профиль default")
        existing = self.repository.list()
        # Let repository resolve an idempotent retry before checking profile availability.
        repeated = next((x for x in existing if x.spec.idempotency_key == spec.idempotency_key), None)
        if repeated:
            return self.repository.create(spec)
        if any(x.status not in TERMINAL for x in existing):
            raise DomainError("PROFILE_BUSY", "Сначала завершите или отмените текущую задачу профиля")
        if spec.policy_grant_id or spec.secret_refs:
            raise DomainError("CAPABILITY_UNAVAILABLE", "Автоматические grants/secret bindings ещё не включены; используйте ручной ввод")
        record = self.repository.create(spec)
        self.schedule(record.task_id)
        return record

    def schedule(self, task_id: str) -> None:
        previous = self.jobs.get(task_id)
        if previous and not previous.done():
            return
        task = asyncio.create_task(self._run(task_id))
        self.jobs[task_id] = task
        task.add_done_callback(lambda completed, key=task_id: self._job_done(key, completed))

    async def _heartbeat(self, epoch: int) -> None:
        while True:
            try:
                await self.worker.call("POST", "/heartbeat", {"control_epoch": epoch})
            except DomainError:
                return
            await asyncio.sleep(1)

    def _verification_repair(self, record: Any, check: dict) -> str | None:
        conditions = record.spec.success_conditions
        checks = check.get("checks")
        if (check.get("verified") is not False or not conditions or not isinstance(checks, list)
                or len(checks) != len(conditions)):
            return None
        failed, metadata = [], []
        fields = {"url_is": ("url",), "text_present": ("text",), "field_equals": ("selector", "value"),
                  "element_checked": ("selector", "checked"), "element_enabled": ("selector", "enabled"),
                  "element_visible": ("selector", "visible"), "element_text_contains": ("selector", "text")}
        element_kinds = {"element_checked", "element_enabled", "element_visible", "element_text_contains"}
        for index, (condition, result) in enumerate(zip(conditions, checks)):
            kind = condition.get("kind") if isinstance(condition, dict) else None
            if (kind not in fields or not isinstance(result, dict) or result.get("kind") != kind
                    or type(result.get("passed")) is not bool):
                return None
            if kind in element_kinds:
                selector, value_field = condition.get("selector"), fields[kind][1]
                expected = condition.get(value_field)
                if (set(condition) != {"kind", "selector", value_field} or not isinstance(selector, str)
                        or not selector or len(selector) > 256
                        or (not isinstance(expected, str) or not expected or len(expected) > 10_000
                            if value_field == "text" else type(expected) is not bool)):
                    return None
            if result["passed"] is False:
                if kind not in element_kinds and any(not isinstance(condition.get(key), str) for key in fields[kind]):
                    return None
                failed.append({"kind": kind, **{key: condition[key] for key in fields[kind]}})
                metadata.append({"index": index, "kind": kind, "passed": False})
        encoded = json.dumps(failed, ensure_ascii=False, separators=(",", ":"))
        if not failed or len(encoded.encode("utf-8")) > 8192:
            return None
        if not self.repository.request_verification_repair(record.task_id, metadata):
            return None
        callback = getattr(self.model, "record_verification_failure", None)
        if callable(callback):
            callback(failed)
        return "Independent verification found these unmet success conditions: " + encoded + ". Inspect the current page and correct them."

    @staticmethod
    def _is_gemini_app_url(value: Any) -> bool:
        if not isinstance(value, str):
            return False
        try:
            parts = urlsplit(value)
        except ValueError:
            return False
        return (parts.scheme.lower() == "https" and parts.netloc.lower() == "gemini.google.com"
                and parts.path.rstrip("/") == "/app")

    def _gemini_submit_receipt(self, task_id: str) -> tuple[str, dict[str, Any] | None]:
        planned: set[str] = set()
        failed_before_enter: set[str] = set()
        acknowledged: dict[str, Any] = {}
        events = self._task_events(task_id)
        for event in events:
            payload = event.get("payload") or {}
            action_id = payload.get("action_id")
            if payload.get("kind") != "gemini_submit_prompt" or not isinstance(action_id, str):
                continue
            if event.get("event_type") == "action.planned":
                planned.add(action_id)
            elif (event.get("event_type") == "action.failed"
                  and payload.get("submission_not_dispatched") is True
                  and payload.get("code") in GEMINI_PRE_SUBMIT_ERRORS):
                failed_before_enter.add(action_id)
            elif (event.get("event_type") == "action.applied"
                  and payload.get("submission_acknowledged") is True
                  and payload.get("input_verified") is True
                  and payload.get("ack_kind") == "composer_cleared"
                  and ((payload.get("enter_dispatched") is True
                        and payload.get("submit_dispatched") is not True
                        and payload.get("submit_method") is None)
                       or (payload.get("submit_dispatched") is True
                           and payload.get("submit_method") == "click"
                           and payload.get("enter_dispatched") is False))):
                acknowledged[action_id] = payload
        unresolved = planned - set(acknowledged) - failed_before_enter
        if unresolved:
            return "unknown", None
        if len(acknowledged) == 1 and next(iter(acknowledged)) in planned:
            return "acknowledged", next(iter(acknowledged.values()))
        if len(acknowledged) > 1:
            return "unknown", None
        return "none", None

    async def _gemini_expire_if_needed(self, task_id: str, *, release_gate: bool) -> bool:
        record = self.repository.get(task_id)
        if record.status != TaskStatus.RUNNING or not self._task_deadline_elapsed(record):
            return False
        if release_gate:
            try:
                await self.worker.gate("none")
            except DomainError:
                pass
        current = self.repository.get(task_id)
        if current.status == TaskStatus.RUNNING:
            self.repository.transition(task_id, TaskStatus.EXPIRED, expected_version=current.version,
                phase="task_deadline_exceeded",
                error={"code": "TASK_EXPIRED", "message": TASK_EXPIRED_MESSAGE})
        return True

    def _gemini_wait_for_human(self, task_id: str, code: str, message: str) -> None:
        current = self.repository.get(task_id)
        if current.status == TaskStatus.RUNNING:
            self.repository.transition(task_id, TaskStatus.WAITING_HUMAN, expected_version=current.version,
                phase=code.lower(), error={"code": code, "message": message})

    def _task_activity(self, task_id: str, stage: str) -> None:
        if stage not in TASK_ACTIVITY_STAGES:
            return
        try:
            step = self.repository.get(task_id).step_count
        except DomainError:
            step = 0
        self.repository.add_event(task_id, "task.activity", {"stage": stage, "step": step})

    @staticmethod
    def _worker_not_ready_message(recovery_context: dict[str, Any] | None) -> str:
        reason = recovery_context.get("worker_recovery_reason") if isinstance(recovery_context, dict) else None
        if isinstance(reason, str) and reason in WORKER_RECOVERY_REASONS:
            return f"Browser recovery is blocked ({reason}). Review the browser status before resuming."
        return "The browser worker is not ready. Restart it, then resume this task."

    def _task_events(self, task_id: str):
        cursor = 0
        while True:
            page = self.repository.events(task_id, after_event_id=cursor, limit=500)
            if not page:
                return
            yield from page
            if len(page) < 500:
                return
            cursor = page[-1]["event_id"]

    def _gemini_session_refresh_completed(self, task_id: str,
                                          recovery_context: dict[str, Any] | None = None) -> bool:
        job_id = self._cookie_sync_job_id(task_id, recovery_context)
        return any(event.get("event_type") == "cookie.session_refreshed"
                   and (event.get("payload") or {}).get("job_id") == job_id
                   and (event.get("payload") or {}).get("origin") == GEMINI_PROMPT_ORIGIN
                   for event in self._task_events(task_id))

    @staticmethod
    def _worker_cookie_scope(health: dict[str, Any]) -> dict[str, Any] | None:
        instance_id = health.get("browser_instance_id") if isinstance(health, dict) else None
        generation = health.get("recovery_generation") if isinstance(health, dict) else None
        if (not isinstance(instance_id, str) or not 1 <= len(instance_id) <= 128
                or type(generation) is not int or generation < 0):
            return None
        return {"browser_instance_id": instance_id, "recovery_generation": generation}

    @staticmethod
    def _cookie_scope_event_fields(scope: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(scope, dict):
            return {}
        instance_id = scope.get("browser_instance_id")
        generation = scope.get("recovery_generation")
        if (not isinstance(instance_id, str) or type(generation) is not int or generation < 0):
            return {}
        return {
            "browser_instance_id_hash": hashlib.sha256(instance_id.encode("utf-8")).hexdigest()[:16],
            "recovery_generation": generation,
        }

    @staticmethod
    def _remember_worker_cookie_scope(context: dict[str, Any], health: dict[str, Any]) -> None:
        scope = TaskRunner._worker_cookie_scope(health)
        if scope is None:
            return
        prior = context.get("worker_scope")
        if isinstance(prior, dict) and prior != scope:
            context["recovery_occurred"] = True
        context["worker_scope"] = scope

    def _cookie_sync_job_id(self, task_id: str, recovery_context: dict[str, Any] | None = None) -> str:
        base = f"taskcookie-{task_id}"
        scope = recovery_context.get("worker_scope") if isinstance(recovery_context, dict) else None
        fields = self._cookie_scope_event_fields(scope)
        if not fields:
            return base
        events = self._task_events(task_id)
        completed = [event for event in events if event.get("event_type") == "cookie.sync_completed"
                     and isinstance(event.get("payload"), dict)
                     and isinstance(event["payload"].get("job_id"), str)
                     and (event["payload"]["job_id"] == base or event["payload"]["job_id"].startswith(base + "-r"))]
        if not completed:
            return base
        latest = max(completed, key=lambda event: event.get("event_id", 0))
        payload = latest["payload"]
        if (payload.get("browser_instance_id_hash") is None
                or payload.get("recovery_generation") is None):
            # A legacy completion has no browser incarnation to compare with
            # the current worker. Once typed scope metadata is available,
            # import once for this incarnation instead of assuming that the
            # old receipt covers its session cookies.
            if recovery_context.get("recovery_occurred") is True or fields:
                return f"{base}-r{fields['recovery_generation']}-{fields['browser_instance_id_hash']}"
            return base  # Legacy receipts retain their original idempotency key.
        if (payload.get("browser_instance_id_hash") == fields["browser_instance_id_hash"]
                and payload.get("recovery_generation") == fields["recovery_generation"]):
            return payload["job_id"]
        return f"{base}-r{fields['recovery_generation']}-{fields['browser_instance_id_hash']}"

    def _gemini_cookie_sync_followed_by_navigation(self, task_id: str,
                                                    recovery_context: dict[str, Any] | None = None) -> bool:
        job_id = self._cookie_sync_job_id(task_id, recovery_context)
        events = list(self._task_events(task_id))
        completed_ids = [event.get("event_id") for event in events
                         if event.get("event_type") == "cookie.sync_completed"
                         and (event.get("payload") or {}).get("job_id") == job_id
                         and type(event.get("event_id")) is int]
        if not completed_ids:
            return False
        completed_id = max(completed_ids)
        return any(event.get("event_type") == "action.applied"
                   and (event.get("payload") or {}).get("kind") == "navigate"
                   and type(event.get("event_id")) is int and event["event_id"] > completed_id
                   for event in events)

    def _gemini_session_refresh_action_id(self, task_id: str,
                                         recovery_context: dict[str, Any] | None = None) -> str:
        events = self._task_events(task_id)
        safe_failures = sum(1 for event in events
            if event.get("event_type") == "action.failed"
            and (event.get("payload") or {}).get("kind") == "gemini_refresh_session"
            and (event.get("payload") or {}).get("code") in {"DRAFT_NOT_EMPTY", "CAPABILITY_UNAVAILABLE"})
        job_id = self._cookie_sync_job_id(task_id, recovery_context)
        base = f"taskcookie-{task_id}"
        suffix = "" if job_id == base else f"-{hashlib.sha256(job_id.encode('utf-8')).hexdigest()[:12]}"
        return f"gemini-refresh-{task_id}{suffix}-{safe_failures}"

    async def _gemini_refresh_after_cookie_sync(self, task_id: str,
                                                observation: dict[str, Any],
                                                recovery_context: dict[str, Any] | None = None) -> dict[str, Any] | None:
        record = self.repository.get(task_id)
        job_id = self._cookie_sync_job_id(task_id, recovery_context)
        if (not record.spec.sync_cookies or not self._cookie_sync_completed(task_id, recovery_context)
                or self._gemini_session_refresh_completed(task_id, recovery_context)):
            return observation
        if not isinstance(observation.get("url"), str) or self.origin(observation["url"]) != GEMINI_PROMPT_ORIGIN:
            return observation
        if self._gemini_cookie_sync_followed_by_navigation(task_id, recovery_context):
            self.repository.add_event(task_id, "cookie.session_refreshed", {
                "job_id": job_id, "origin": GEMINI_PROMPT_ORIGIN,
            })
            return observation
        result = await self._gemini_fast_dispatch(task_id, observation, {"kind": "gemini_refresh_session"},
            allow_write=False, action_id_override=self._gemini_session_refresh_action_id(task_id, recovery_context))
        if result is None:
            return None
        if result.get("origin") != GEMINI_PROMPT_ORIGIN or result.get("session_refreshed") is not True:
            raise DomainError("ACTION_OUTCOME_UNKNOWN", "Gemini session refresh was not confirmed; inspect the browser before retrying.")
        self.repository.add_event(task_id, "cookie.session_refreshed", {
            "job_id": job_id, "origin": GEMINI_PROMPT_ORIGIN,
        })
        self._task_activity(task_id, "observing")
        refreshed = await self.worker.call("POST", "/observe", {})
        if not isinstance(refreshed, dict) or not isinstance(refreshed.get("url"), str):
            raise DomainError("ACTION_OUTCOME_UNKNOWN", "Gemini page state after refresh could not be confirmed.")
        if self.origin(refreshed["url"]) != GEMINI_PROMPT_ORIGIN:
            raise DomainError("POLICY_DENIED", "Gemini session refresh did not remain on the allowed origin.")
        return refreshed

    async def _gemini_require_authentication(self, task_id: str, *, wait_seconds: float | None = None) -> bool:
        self._task_activity(task_id, "verifying")
        if wait_seconds is None:
            wait_seconds = self.gemini_auth_wait_seconds
        deadline = time.monotonic() + max(0.0, wait_seconds)
        record = self.repository.get(task_id)
        deadline = min(deadline, self._record_created_monotonic(record) + record.spec.limits.timeout_s)
        status = "unknown"
        reason_code = "PROBE_UNAVAILABLE"
        while True:
            if await self._gemini_expire_if_needed(task_id, release_gate=True):
                return False
            try:
                raw = await self.worker.call("GET", GEMINI_AUTH_STATUS_PATH)
            except DomainError:
                raw = None
            valid = (isinstance(raw, dict) and set(raw) == {"status", "reason_code"}
                     and raw.get("reason_code") in GEMINI_AUTH_REASON_CODES)
            status = raw.get("status") if valid else "unknown"
            reason_code = raw.get("reason_code") if valid else "PROBE_UNAVAILABLE"
            if status == "signed_in" and reason_code == "ACCOUNT_CONTROLS_PRESENT":
                return True
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
        if status == "signed_out" and reason_code == "SIGN_IN_CONTROL_PRESENT":
            code = "AUTH_REQUIRED"
            message = "Sign in to Gemini in the browser, then resume this task."
        else:
            code = "AUTH_STATE_UNKNOWN"
            message = "Gemini sign-in state could not be confirmed. Check the browser, then resume this task."
        try:
            await self.worker.gate("none")
        except DomainError:
            pass
        self._gemini_wait_for_human(task_id, code, message)
        return False

    def _cookie_sync_completed(self, task_id: str,
                               recovery_context: dict[str, Any] | None = None) -> bool:
        job_id = self._cookie_sync_job_id(task_id, recovery_context)
        for event in self._task_events(task_id):
            payload = event.get("payload") or {}
            counts = payload.get("counts")
            if (event.get("event_type") == "cookie.sync_completed" and payload.get("job_id") == job_id
                    and isinstance(counts, dict)
                    and all(type(counts.get(key)) is int and 0 <= counts[key] <= 500
                            for key in ("total", "applied", "failed"))
                    and counts["failed"] == 0 and counts["applied"] == counts["total"]):
                return True
        return False

    def _cookie_sync_wait_for_human(self, task_id: str, code: str, message: str) -> None:
        current = self.repository.get(task_id)
        if current.status == TaskStatus.RUNNING:
            self.repository.transition(task_id, TaskStatus.WAITING_HUMAN, expected_version=current.version,
                phase="cookie_sync_required", error={"code": code, "message": message})

    def _unresolved_action_dispatch(self, task_id: str) -> bool:
        pending: dict[str, str | None] = {}
        for event in self._task_events(task_id):
            payload = event.get("payload") or {}
            action_id = payload.get("action_id")
            if event.get("event_type") == "action.planned":
                if isinstance(action_id, str):
                    pending[action_id] = payload.get("kind") if isinstance(payload.get("kind"), str) else None
            elif event.get("event_type") in {"action.applied", "action.failed"}:
                if isinstance(action_id, str):
                    pending.pop(action_id, None)
                    continue
                # Older generic action receipts omitted action_id. They are
                # trustworthy only when exactly one outstanding plan has the
                # same kind, since execution is serialized within a task.
                kind = payload.get("kind")
                candidates = [key for key, planned_kind in pending.items() if planned_kind == kind]
                if isinstance(kind, str) and len(candidates) == 1:
                    pending.pop(candidates[0], None)
        return bool(pending)

    @staticmethod
    def _worker_recovery_metadata(health: dict[str, Any]) -> tuple[bool, str | None]:
        """Return typed browser recovery eligibility; never infer it from detail text."""
        if not isinstance(health, dict):
            return False, None
        state = health.get("recovery_state")
        reason = health.get("recovery_reason")
        if (health.get("recovery_needed") is not True
                or not isinstance(state, str) or state not in WORKER_RECOVERY_STATES
                or not isinstance(reason, str) or reason not in WORKER_RECOVERY_REASONS
                or type(health.get("recovery_generation")) is not int or health["recovery_generation"] < 0
                or type(health.get("recovery_attempts")) is not int or health["recovery_attempts"] < 0
                or health.get("resource_violation") is not False or health.get("egress_ready") is not True):
            return False, None
        watchdog_owned_reason = reason in {
            "launch_failed", "teardown_unconfirmed", "recovery_in_progress", "backoff",
        }
        eligible = (state == "idle" and reason in WORKER_RECOVERABLE_REASONS) or (
            state in {"recovering", "backoff"}
            and (reason in WORKER_RECOVERABLE_REASONS or watchdog_owned_reason))
        return eligible, reason

    async def _ensure_worker_ready(self, task_id: str, recovery_context: dict[str, Any] | None = None,
                                   *, allow_agent_owner: bool = False) -> bool:
        """Try one typed, bounded recovery and return only after readiness is re-proven."""
        context = recovery_context if recovery_context is not None else {"attempted": False}
        if await self._gemini_expire_if_needed(task_id, release_gate=False):
            return False
        try:
            health = await self.worker.health()
        except DomainError:
            return False
        if isinstance(health, dict) and health.get("ready") is True:
            self._remember_worker_cookie_scope(context, health)
            return not await self._gemini_expire_if_needed(task_id, release_gate=False)
        eligible, reason = self._worker_recovery_metadata(health)
        if reason is not None:
            context["worker_recovery_reason"] = reason
        if not eligible or self._unresolved_action_dispatch(task_id):
            return False
        try:
            initial_state = await self.worker.state()
        except DomainError:
            return False
        if not self._worker_recovery_state_safe(initial_state, allow_agent_owner=allow_agent_owner):
            return False

        deadline = time.monotonic() + WORKER_RECOVERY_WAIT_SECONDS
        current = health
        current_state = health.get("recovery_state")
        watchdog_started = current_state in {"recovering", "backoff"}
        watchdog_started_at = time.monotonic()
        if watchdog_started:
            self._task_activity(task_id, "recovering_browser")
            self.repository.add_event(task_id, "browser.recovery_started", {
                "reason": reason, "generation": health.get("recovery_generation"),
                "source": "watchdog",
            })
        if current_state in {"recovering", "backoff"}:
            while time.monotonic() < deadline:
                await asyncio.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
                if await self._gemini_expire_if_needed(task_id, release_gate=False):
                    return False
                try:
                    current = await self.worker.health()
                except DomainError:
                    # The watchdog may have stopped the worker between its
                    # recovery response and the next health poll. Keep the
                    # existing bounded wait; never issue another recovery
                    # request or infer readiness from a transport failure.
                    continue
                if isinstance(current, dict) and current.get("ready") is True:
                    try:
                        state = await self.worker.state()
                    except DomainError:
                        return False
                    if not self._worker_post_recovery_state_ready(state):
                        return False
                    if await self._gemini_expire_if_needed(task_id, release_gate=False):
                        return False
                    self._remember_worker_cookie_scope(context, current)
                    self.repository.add_event(task_id, "browser.recovery_completed", {
                        "reason": current.get("recovery_reason") or reason,
                        "outcome": "watchdog_recovered",
                        "generation": current.get("recovery_generation"),
                        "duration_ms": self._recovery_duration_ms(watchdog_started_at),
                        "source": "watchdog",
                    })
                    return True
                ok, next_reason = self._worker_recovery_metadata(current)
                if not ok:
                    return False
                current_state = current.get("recovery_state")
                if current_state in {"blocked", "exhausted"}:
                    return False
                if current_state == "idle":
                    if next_reason not in WORKER_RECOVERABLE_REASONS:
                        return False
                    reason = next_reason
                    break
            else:
                if watchdog_started:
                    self.repository.add_event(task_id, "browser.recovery_failed", {
                        "reason": reason, "outcome": "watchdog_timeout",
                        "generation": current.get("recovery_generation"),
                        "duration_ms": self._recovery_duration_ms(watchdog_started_at),
                        "source": "watchdog",
                    })
                return False

        if (context.get("attempted") or current_state != "idle"
                or reason not in WORKER_RECOVERABLE_REASONS):
            return False
        try:
            state = await self.worker.state()
        except DomainError:
            return False
        if not self._worker_recovery_state_safe(state, allow_agent_owner=allow_agent_owner):
            return False

        context["attempted"] = True
        started = time.monotonic()
        generation = health.get("recovery_generation")
        self._task_activity(task_id, "recovering_browser")
        self.repository.add_event(task_id, "browser.recovery_started", {
            "reason": reason, "generation": generation,
        })
        try:
            response = await self.worker.recover()
        except (DomainError, AttributeError):
            self.repository.add_event(task_id, "browser.recovery_failed", {
                "reason": reason, "outcome": "request_failed", "generation": generation,
                "duration_ms": self._recovery_duration_ms(started),
            })
            return False
        outcome = response["outcome"]
        result_generation = response["recovery_generation"]
        if outcome not in {"recovered", "already_ready"}:
            self.repository.add_event(task_id, "browser.recovery_failed", {
                "reason": response.get("recovery_reason") or reason, "outcome": outcome,
                "generation": result_generation,
                "duration_ms": self._recovery_duration_ms(started),
            })
            return False

        while time.monotonic() < deadline:
            try:
                current = await self.worker.health()
            except DomainError:
                current = {}
            if isinstance(current, dict) and current.get("ready") is True:
                try:
                    state = await self.worker.state()
                except DomainError:
                    state = {}
                if self._worker_post_recovery_state_ready(state):
                    if await self._gemini_expire_if_needed(task_id, release_gate=False):
                        return False
                    self._remember_worker_cookie_scope(context, current)
                    self.repository.add_event(task_id, "browser.recovery_completed", {
                        "reason": response.get("recovery_reason") or reason, "outcome": outcome,
                        "generation": result_generation,
                        "duration_ms": self._recovery_duration_ms(started),
                    })
                    return True
            await asyncio.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
            if await self._gemini_expire_if_needed(task_id, release_gate=False):
                return False
        self.repository.add_event(task_id, "browser.recovery_failed", {
            "reason": response.get("recovery_reason") or reason, "outcome": "not_ready",
            "generation": result_generation,
            "duration_ms": self._recovery_duration_ms(started),
        })
        return False

    @staticmethod
    def _recovery_duration_ms(started: float) -> int:
        return max(0, min(60_000, round((time.monotonic() - started) * 1000)))

    @staticmethod
    def _worker_recovery_state_safe(state: dict[str, Any], *, allow_agent_owner: bool) -> bool:
        if (not isinstance(state, dict) or state.get("resource_violation") is not False
                or state.get("egress_ready") is not True
                or state.get("action_in_progress") is not False
                or state.get("import_in_progress") is not False
                or any(state.get(key) is True for key in WORKER_INFLIGHT_FIELDS)):
            return False
        owner = state.get("control_owner")
        return owner == "none" or (allow_agent_owner and owner == "agent")

    @staticmethod
    def _worker_post_recovery_state_ready(state: dict[str, Any]) -> bool:
        return (isinstance(state, dict) and state.get("browser_started") is True
                and type(state.get("context_count")) is int and state.get("context_count") == 1
                and type(state.get("page_count")) is int and state.get("page_count") == 1
                and state.get("resource_violation") is False and state.get("egress_ready") is True
                and state.get("control_owner") == "none"
                and state.get("action_in_progress") is False and state.get("import_in_progress") is False)

    async def _release_agent_lease_if_owned(self, recovery_context: dict[str, Any]) -> None:
        if recovery_context.get("agent_lease") is not True:
            return
        try:
            state = await self.worker.state()
        except DomainError:
            return
        if (not isinstance(state, dict) or state.get("control_owner") != "agent"
                or any(state.get(key) is True for key in WORKER_INFLIGHT_FIELDS)):
            return
        try:
            await self.worker.gate("none")
        except DomainError:
            return
        recovery_context["agent_lease"] = False

    async def _resync_cookies_after_scope_change(self, task_id: str,
                                                prior_scope: dict[str, Any] | None,
                                                recovery_context: dict[str, Any]) -> bool:
        current_scope = recovery_context.get("worker_scope")
        if prior_scope == current_scope:
            return True
        record = self.repository.get(task_id)
        if record.spec.sync_cookies is False:
            return True
        return await self._sync_task_cookies(task_id, record, recovery_context)

    async def _sync_task_cookies(self, task_id: str, record: Any,
                                 recovery_context: dict[str, Any] | None = None) -> bool:
        if record.spec.sync_cookies is False or self._cookie_sync_completed(task_id, recovery_context):
            return True
        if self._task_deadline_elapsed(record):
            current = self.repository.get(task_id)
            if current.status == TaskStatus.RUNNING:
                self.repository.transition(task_id, TaskStatus.EXPIRED, expected_version=current.version,
                    phase="task_deadline_exceeded",
                    error={"code": "TASK_EXPIRED", "message": TASK_EXPIRED_MESSAGE})
            return False
        callback = self.cookie_sync
        if not callable(callback):
            self._cookie_sync_wait_for_human(task_id, "COOKIE_SYNC_FAILED",
                "Chrome cookie synchronization is unavailable. Connect the paired Chrome source, then resume this task.")
            return False
        remaining = record.spec.limits.timeout_s - max(0.0, time.monotonic() - self._record_created_monotonic(record))
        if remaining <= 0:
            current = self.repository.get(task_id)
            if current.status == TaskStatus.RUNNING:
                self.repository.transition(task_id, TaskStatus.EXPIRED, expected_version=current.version,
                    phase="task_deadline_exceeded",
                    error={"code": "TASK_EXPIRED", "message": TASK_EXPIRED_MESSAGE})
            return False
        job_id = self._cookie_sync_job_id(task_id, recovery_context)
        scope = recovery_context.get("worker_scope") if isinstance(recovery_context, dict) else None
        scope_fields = self._cookie_scope_event_fields(scope)
        self._task_activity(task_id, "syncing_cookies")
        self.repository.add_event(task_id, "cookie.sync_started", {
            "job_id": job_id, "origins_count": len(record.spec.allowed_origins), **scope_fields,
        })
        try:
            kwargs: dict[str, Any] = {}
            try:
                parameters = inspect.signature(callback).parameters
                accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values())
            except (TypeError, ValueError):
                parameters, accepts_kwargs = {}, False
            if accepts_kwargs or "job_id" in parameters:
                kwargs["job_id"] = job_id
            if isinstance(scope, dict):
                if accepts_kwargs or "browser_instance_id" in parameters:
                    kwargs["browser_instance_id"] = scope.get("browser_instance_id")
                if accepts_kwargs or "recovery_generation" in parameters:
                    kwargs["recovery_generation"] = scope.get("recovery_generation")
            counts = await asyncio.wait_for(callback(task_id, record.spec.profile_id,
                list(record.spec.allowed_origins), **kwargs), timeout=remaining)
        except asyncio.TimeoutError:
            current = self.repository.get(task_id)
            if current.status == TaskStatus.RUNNING:
                self.repository.transition(task_id, TaskStatus.RECOVERING, expected_version=current.version,
                    phase="cookie_sync_outcome_unknown", error={"code": "ACTION_OUTCOME_UNKNOWN",
                    "message": "Cookie synchronization may have started. Review the browser before retrying."})
            return False
        except DomainError as exc:
            if self._cookie_sync_completed(task_id, recovery_context):
                return True
            if exc.code == "ACTION_OUTCOME_UNKNOWN":
                current = self.repository.get(task_id)
                if current.status == TaskStatus.RUNNING:
                    self.repository.transition(task_id, TaskStatus.RECOVERING, expected_version=current.version,
                        phase="cookie_sync_outcome_unknown", error={"code": "ACTION_OUTCOME_UNKNOWN",
                        "message": "Cookie synchronization may have started. Review the browser before retrying."})
                return False
            allowed_codes = {"CHROME_PERMISSION_REQUIRED", "COOKIE_SOURCE_UNAVAILABLE", "COOKIE_SOURCE_AMBIGUOUS",
                             "COOKIE_SYNC_UNSUPPORTED", "COOKIE_SYNC_FAILED"}
            code = exc.code if exc.code in allowed_codes else "COOKIE_SYNC_FAILED"
            self._cookie_sync_wait_for_human(task_id, code,
                "Chrome cookies could not be synchronized. Restore the paired Chrome connection or permission, then resume this task.")
            return False
        except Exception:
            if self._cookie_sync_completed(task_id, recovery_context):
                return True
            self._cookie_sync_wait_for_human(task_id, "COOKIE_SYNC_FAILED",
                "Chrome cookies could not be synchronized. Restore the paired Chrome connection or permission, then resume this task.")
            return False
        if not isinstance(counts, dict):
            self._cookie_sync_wait_for_human(task_id, "COOKIE_SYNC_FAILED",
                "Chrome cookie synchronization returned an invalid receipt. Review the source, then resume this task.")
            return False
        total, applied, failed = (counts.get("total"), counts.get("applied"), counts.get("failed"))
        if (any(type(value) is not int or not 0 <= value <= 500 for value in (total, applied, failed))
                or failed != 0 or applied != total):
            self._cookie_sync_wait_for_human(task_id, "COOKIE_SYNC_FAILED",
                "Chrome cookie synchronization was incomplete. Review the source, then resume this task.")
            return False
        if not self._cookie_sync_completed(task_id, recovery_context):
            self.repository.add_event(task_id, "cookie.sync_completed", {
                "job_id": job_id, "origins_count": len(record.spec.allowed_origins),
                "counts": {"total": total, "applied": applied, "failed": failed},
                **scope_fields,
            })
        latest = self.repository.get(task_id)
        if latest.status != TaskStatus.RUNNING:
            return False
        if self._task_deadline_elapsed(latest):
            self.repository.transition(task_id, TaskStatus.EXPIRED, expected_version=latest.version,
                phase="task_deadline_exceeded",
                error={"code": "TASK_EXPIRED", "message": TASK_EXPIRED_MESSAGE})
            return False
        return True

    @staticmethod
    def _record_created_monotonic(record: Any) -> float:
        from datetime import UTC, datetime
        created = datetime.fromisoformat(record.created_at.replace("Z", "+00:00"))
        return time.monotonic() - max(0.0, (datetime.now(UTC) - created).total_seconds())

    async def _gemini_fast_dispatch(self, task_id: str, observation: dict[str, Any], action: dict[str, Any],
                                   *, capability: dict[str, str] | None = None, allow_write: bool = True,
                                   action_id_override: str | None = None) -> dict[str, Any] | None:
        record = self.repository.get(task_id)
        if record.status != TaskStatus.RUNNING:
            return None
        if await self._gemini_expire_if_needed(task_id, release_gate=True):
            return None
        if record.step_count >= record.spec.limits.max_steps:
            raise DomainError("STEP_LIMIT_EXCEEDED", "task step limit has been reached")
        if not isinstance(observation.get("control_epoch"), int) or not isinstance(observation.get("observation_id"), str):
            raise DomainError("BROWSER_UNAVAILABLE", "browser observation is missing its control lease")
        action_id = action_id_override or secrets.token_urlsafe(16)
        command: dict[str, Any] = {
            "action_id": action_id,
            "control_epoch": observation["control_epoch"],
            "observation_id": observation["observation_id"],
            "action": action,
            "allowed_origins": [GEMINI_PROMPT_ORIGIN],
            "allow_write": allow_write,
        }
        if capability is not None:
            command["site_capability"] = capability
        self._task_activity(task_id, "executing")
        self.repository.add_event(task_id, "action.planned", {"action_id": action_id, "kind": action["kind"]})
        try:
            receipt = await self.worker.call("POST", "/execute", command)
        except DomainError as exc:
            if action["kind"] == "gemini_refresh_session" and exc.code in {
                    "DRAFT_NOT_EMPTY", "CAPABILITY_UNAVAILABLE", "POLICY_DENIED"}:
                self.repository.add_event(task_id, "action.failed", {
                    "action_id": action_id, "kind": action["kind"], "code": exc.code,
                })
                raise
            if action["kind"] == "gemini_submit_prompt" and exc.code in GEMINI_PRE_SUBMIT_ERRORS:
                self.repository.add_event(task_id, "action.failed", {
                    "action_id": action_id, "kind": action["kind"], "code": exc.code,
                    "submission_not_dispatched": True,
                })
                raise
            if exc.code == "WORKER_UNAVAILABLE":
                raise DomainError("ACTION_OUTCOME_UNKNOWN", "The browser did not confirm the outcome; inspect it before retrying.") from exc
            raise
        if not isinstance(receipt, dict) or receipt.get("outcome") != "applied":
            if (action["kind"] == "gemini_refresh_session" and isinstance(receipt, dict)
                    and receipt.get("outcome") == "rejected"
                    and receipt.get("error") in {"DRAFT_NOT_EMPTY", "CAPABILITY_UNAVAILABLE", "POLICY_DENIED"}):
                code = receipt["error"]
                self.repository.add_event(task_id, "action.failed", {
                    "action_id": action_id, "kind": action["kind"], "code": code,
                })
                raise DomainError(code, "Gemini page was not refreshed; inspect its draft or modal state.")
            raise DomainError("ACTION_OUTCOME_UNKNOWN", "The browser did not confirm the outcome; inspect it before retrying.")
        receipt_result = receipt.get("result")
        applied_event: dict[str, Any] = {"action_id": action_id, "kind": action["kind"]}
        if action["kind"] == "gemini_refresh_session":
            if (not isinstance(receipt_result, dict) or set(receipt_result) != {"origin", "session_refreshed"}
                    or receipt_result.get("origin") != GEMINI_PROMPT_ORIGIN
                    or receipt_result.get("session_refreshed") is not True):
                raise DomainError("ACTION_OUTCOME_UNKNOWN", "Gemini session refresh outcome was not confirmed; inspect the browser before retrying.")
        elif action["kind"] == "gemini_submit_prompt":
            legacy_enter = isinstance(receipt_result, dict) and receipt_result.get("enter_dispatched") is True
            native_click = (isinstance(receipt_result, dict)
                            and receipt_result.get("submit_dispatched") is True
                            and receipt_result.get("submit_method") == "click"
                            and receipt_result.get("enter_dispatched") is False)
            if (not isinstance(receipt_result, dict)
                    or receipt_result.get("submitted") is not True
                    or not (legacy_enter or native_click)
                    or receipt_result.get("submission_acknowledged") is not True
                    or receipt_result.get("input_verified") is not True
                    or receipt_result.get("ack_kind") != "composer_cleared"):
                raise DomainError("ACTION_OUTCOME_UNKNOWN", "Gemini did not confirm an exact prompt submission; inspect it before retrying.")
            applied_event.update({"submission_acknowledged": True, "input_verified": True,
                                  "ack_kind": "composer_cleared", "timings_ms": self._safe_gemini_timings(receipt_result)})
            if native_click:
                applied_event.update({"submit_dispatched": True, "submit_method": "click", "enter_dispatched": False})
            else:
                applied_event["enter_dispatched"] = True
        self.repository.add_event(task_id, "action.applied", applied_event)
        latest = self.repository.get(task_id)
        if latest.status == TaskStatus.RUNNING:
            self.repository.increment_step(task_id)
        if await self._gemini_expire_if_needed(task_id, release_gate=True):
            return None
        return receipt_result if isinstance(receipt_result, dict) else {}

    @staticmethod
    def _safe_gemini_timings(receipt_result: dict[str, Any]) -> dict[str, int | float]:
        raw = receipt_result.get("timings_ms")
        if not isinstance(raw, dict):
            return {}
        allowed = {"preflight", "insert", "verify", "submit", "ack", "total"}
        return {key: value for key, value in raw.items()
                if key in allowed and type(value) in {int, float} and 0 <= value <= 60_000}

    async def _observe_with_recovery(self, task_id: str, recovery_context: dict[str, Any],
                                     heartbeat: asyncio.Task | None = None) -> tuple[dict[str, Any], asyncio.Task | None]:
        reacquire = False
        prior_scope = recovery_context.get("worker_scope")
        try:
            self._task_activity(task_id, "observing")
            observation = await self.worker.call("POST", "/observe", {})
            expected_epoch = recovery_context.get("control_epoch")
            if (isinstance(observation, dict) and type(expected_epoch) is int
                    and observation.get("control_epoch") != expected_epoch):
                if self._unresolved_action_dispatch(task_id):
                    raise DomainError("ACTION_OUTCOME_UNKNOWN",
                        "A previous browser action may still be in progress; inspect the browser before retrying.")
                if not await self._ensure_worker_ready(task_id, recovery_context, allow_agent_owner=True):
                    raise DomainError("BROWSER_UNAVAILABLE", "The browser is not ready after its control lease changed.")
                if not await self._resync_cookies_after_scope_change(task_id, prior_scope, recovery_context):
                    raise DomainError("COOKIE_SYNC_FAILED", "Cookies could not be restored after browser recovery.")
                reacquire = True
        except DomainError as exc:
            if exc.code not in {"WORKER_UNAVAILABLE", "BROWSER_UNAVAILABLE"}:
                raise
            if self._unresolved_action_dispatch(task_id):
                raise DomainError("ACTION_OUTCOME_UNKNOWN",
                    "A previous browser action may still be in progress; inspect the browser before retrying.") from exc
            if not await self._ensure_worker_ready(task_id, recovery_context, allow_agent_owner=True):
                raise
            if not await self._resync_cookies_after_scope_change(task_id, prior_scope, recovery_context):
                raise DomainError("COOKIE_SYNC_FAILED", "Cookies could not be restored after browser recovery.") from exc
            reacquire = True
        if not reacquire:
            return observation, heartbeat
        try:
            state = await self.worker.state()
        except DomainError as exc:
            raise DomainError("WORKER_UNAVAILABLE", "The browser state could not be confirmed after recovery.") from exc
        if state.get("control_owner") == "human":
            raise DomainError("CONTROL_OWNED_BY_HUMAN", "Return browser control before resuming the task.")
        if not self._worker_recovery_state_safe(state, allow_agent_owner=True):
            raise DomainError("BROWSER_UNAVAILABLE", "Browser control is busy; check the browser before continuing.")
        if await self._gemini_expire_if_needed(task_id, release_gate=False):
            raise DomainError("TASK_EXPIRED", TASK_EXPIRED_MESSAGE)
        if heartbeat is not None:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        try:
            gate = await self.worker.gate("agent")
        except DomainError as exc:
            raise DomainError("BROWSER_UNAVAILABLE", "Browser control could not be reacquired after recovery.") from exc
        epoch = gate.get("control_epoch") if isinstance(gate, dict) else None
        if type(epoch) is not int:
            raise DomainError("BROWSER_UNAVAILABLE", "The browser did not provide a control lease after recovery.")
        recovery_context["control_epoch"] = epoch
        recovery_context["agent_lease"] = True
        heartbeat = asyncio.create_task(self._heartbeat(epoch))
        self._task_activity(task_id, "observing")
        observation = await self.worker.call("POST", "/observe", {})
        if not isinstance(observation, dict) or observation.get("control_epoch") != epoch:
            raise DomainError("BROWSER_UNAVAILABLE", "The browser observation does not match the active control lease.")
        return observation, heartbeat

    async def _run_gemini_prompt_submit(self, task_id: str,
                                        recovery_context: dict[str, Any] | None = None) -> None:
        started = time.monotonic()
        record = self.repository.get(task_id)
        receipt_state, saved_ack = self._gemini_submit_receipt(task_id)
        if receipt_state == "unknown":
            current = self.repository.get(task_id)
            if current.status == TaskStatus.RUNNING:
                self.repository.transition(task_id, TaskStatus.RECOVERING, expected_version=current.version,
                    phase="action_outcome_unknown", error={
                        "code": "ACTION_OUTCOME_UNKNOWN",
                        "message": "A previous prompt submission may have started; inspect the browser before retrying.",
                    })
            return
        if receipt_state == "acknowledged":
            if record.status == TaskStatus.RUNNING:
                latest = self.repository.get(task_id)
                if latest.status == TaskStatus.RUNNING:
                    self.repository.transition(task_id, TaskStatus.SUCCEEDED, expected_version=latest.version,
                        phase="submitted", result={
                            "submitted": True, "submission_acknowledged": True,
                            "enter_dispatched": saved_ack.get("enter_dispatched") is True,
                            **({"submit_dispatched": True, "submit_method": "click"}
                               if saved_ack.get("submit_dispatched") is True else {}),
                            "input_verified": True,
                            "response_verified": False, "model_calls": 0,
                            "ack_kind": "composer_cleared",
                            "timings_ms": self._safe_gemini_timings(saved_ack or {}),
                            "recovered_from_receipt": True,
                        })
            return
        if await self._gemini_expire_if_needed(task_id, release_gate=False):
            return
        record = self.repository.get(task_id)
        capability = self.repository.capability(task_id, "gemini_prompt_submit")
        if capability is None:
            self._gemini_wait_for_human(task_id, "CAPABILITY_UNAVAILABLE",
                "This task does not have the Gemini prompt submission capability.")
            return
        recovery_context = recovery_context if recovery_context is not None else {"attempted": False}
        self._task_activity(task_id, "checking_dependencies")
        if not await self._ensure_worker_ready(task_id, recovery_context):
            self._gemini_wait_for_human(task_id, "DEPENDENCIES_NOT_READY",
                self._worker_not_ready_message(recovery_context))
            return
        try:
            state = await self.worker.state()
        except DomainError as exc:
            self._gemini_wait_for_human(task_id, exc.code, exc.message)
            return
        if (state.get("control_owner") == "human" or state.get("action_in_progress") is True
                or state.get("import_in_progress") is True):
            code = "CONTROL_OWNED_BY_HUMAN" if state.get("control_owner") == "human" else "CONTROL_BUSY"
            self._gemini_wait_for_human(task_id, code, "Browser control is currently in use.")
            return
        if state.get("context_count") != 1 or state.get("page_count") != 1 or state.get("resource_violation") is True:
            self._gemini_wait_for_human(task_id, "BROWSER_UNAVAILABLE", "The browser does not have one healthy page ready for this task.")
            return
        if await self._gemini_expire_if_needed(task_id, release_gate=False):
            return
        try:
            gate = await self.worker.gate("agent")
        except DomainError as exc:
            # A failed gate acquisition must not release or overwrite another owner.
            self._gemini_wait_for_human(task_id, exc.code, exc.message)
            return
        control_epoch = gate.get("control_epoch")
        if not isinstance(control_epoch, int):
            raise DomainError("BROWSER_UNAVAILABLE", "The browser did not provide a control lease.")
        recovery_context["control_epoch"] = control_epoch
        recovery_context["agent_lease"] = True
        heartbeat = asyncio.create_task(self._heartbeat(control_epoch))
        try:
            if await self._gemini_expire_if_needed(task_id, release_gate=True):
                return
            observation, heartbeat = await self._observe_with_recovery(task_id, recovery_context, heartbeat)
            if not isinstance(observation, dict):
                raise DomainError("BROWSER_UNAVAILABLE", "The browser returned an invalid observation.")
            observation.pop("screenshot_png_b64", None)
            if await self._gemini_expire_if_needed(task_id, release_gate=True):
                return
            if not self._is_gemini_app_url(observation.get("url")):
                if (isinstance(observation.get("url"), str)
                        and self.origin(observation["url"]) == GEMINI_PROMPT_ORIGIN):
                    observation = await self._gemini_refresh_after_cookie_sync(task_id, observation, recovery_context)
                    if observation is None:
                        return
                navigation_receipt = await self._gemini_fast_dispatch(task_id, observation,
                    {"kind": "navigate", "url": GEMINI_PROMPT_APP_URL})
                if navigation_receipt is None:
                    return
                if await self._gemini_expire_if_needed(task_id, release_gate=True):
                    return
                observation, heartbeat = await self._observe_with_recovery(task_id, recovery_context, heartbeat)
                if not isinstance(observation, dict):
                    raise DomainError("BROWSER_UNAVAILABLE", "The browser returned an invalid observation.")
                observation.pop("screenshot_png_b64", None)
                if not self._is_gemini_app_url(observation.get("url")):
                    raise DomainError("POLICY_DENIED", "The browser did not reach the canonical Gemini application page.")
                if (record.spec.sync_cookies and self._cookie_sync_completed(task_id, recovery_context)
                        and not self._gemini_session_refresh_completed(task_id, recovery_context)):
                    self.repository.add_event(task_id, "cookie.session_refreshed", {
                        "job_id": self._cookie_sync_job_id(task_id, recovery_context),
                        "origin": GEMINI_PROMPT_ORIGIN,
                    })
            else:
                observation = await self._gemini_refresh_after_cookie_sync(task_id, observation, recovery_context)
                if observation is None:
                    return
            if not await self._gemini_require_authentication(task_id):
                return
            if await self._gemini_expire_if_needed(task_id, release_gate=True):
                return
            result = await self._gemini_fast_dispatch(task_id, observation,
                {"kind": "gemini_submit_prompt", "text": record.spec.verbatim_text}, capability=capability)
            if result is None:
                return
            latest = self.repository.get(task_id)
            if latest.status != TaskStatus.RUNNING:
                return
            await self.worker.gate("none")
            latest = self.repository.get(task_id)
            if latest.status != TaskStatus.RUNNING:
                return
            self.repository.transition(task_id, TaskStatus.SUCCEEDED, expected_version=latest.version,
                phase="submitted", result={
                    "submitted": True, "submission_acknowledged": True,
                    **({"submit_dispatched": True, "submit_method": "click"}
                       if result.get("submit_dispatched") is True and result.get("submit_method") == "click" else {}),
                    "enter_dispatched": result.get("enter_dispatched") is True, "input_verified": True,
                    "response_verified": False, "model_calls": 0,
                    "ack_kind": "composer_cleared",
                    "timings_ms": self._safe_gemini_timings(result),
                    "elapsed_s": round(time.monotonic() - started, 3),
                })
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _run(self, task_id: str) -> None:
        heartbeat = None
        recovery_context: dict[str, Any] = {"attempted": False, "agent_lease": False}
        try:
            record = self.repository.get(task_id)
            fresh_run = record.status == TaskStatus.QUEUED
            if fresh_run:
                if self._task_deadline_elapsed(record):
                    self.repository.transition(task_id, TaskStatus.EXPIRED, expected_version=record.version,
                        phase="task_deadline_exceeded",
                        error={"code": "TASK_EXPIRED", "message": TASK_EXPIRED_MESSAGE})
                    return
                self.repository.transition(task_id, TaskStatus.RUNNING,
                    phase="submitting_prompt" if record.spec.workflow == "gemini_prompt_submit" else "checking_dependencies")
            elif record.status != TaskStatus.RUNNING:
                return
            record = self.repository.get(task_id)
            self._task_activity(task_id, "checking_dependencies")
            if (record.spec.workflow != "gemini_prompt_submit"
                    and self._unresolved_action_dispatch(task_id)):
                self.repository.transition(task_id, TaskStatus.RECOVERING, expected_version=record.version,
                    phase="action_outcome_unknown", error={
                        "code": "ACTION_OUTCOME_UNKNOWN",
                        "message": "A previous browser action may have started; inspect the browser before retrying.",
                    })
                return
            # Cookie import needs a live browser control surface. Check that
            # dependency before asking Chrome to export cookies, so a dead
            # canonical page cannot leave sync waiting on a busy import gate.
            # A completed receipt is already idempotent and needs no preflight.
            if record.spec.sync_cookies:
                if not await self._ensure_worker_ready(task_id, recovery_context):
                    self._gemini_wait_for_human(task_id, "DEPENDENCIES_NOT_READY",
                        self._worker_not_ready_message(recovery_context))
                    return
            if not await self._sync_task_cookies(task_id, record, recovery_context):
                return
            record = self.repository.get(task_id)
            if record.status != TaskStatus.RUNNING:
                return
            if record.spec.workflow == "gemini_prompt_submit":
                await self._run_gemini_prompt_submit(task_id, recovery_context)
                return
            self._task_activity(task_id, "checking_dependencies")
            if not await self._ensure_worker_ready(task_id, recovery_context):
                raise DomainError("DEPENDENCIES_NOT_READY", self._worker_not_ready_message(recovery_context))
            gate = await self.worker.gate("agent")
            heartbeat = asyncio.create_task(self._heartbeat(gate["control_epoch"]))
            recovery_context["control_epoch"] = gate["control_epoch"]
            recovery_context["agent_lease"] = True
            started = time.monotonic()
            if GEMINI_PROMPT_ORIGIN in record.spec.allowed_origins:
                initial, heartbeat = await self._observe_with_recovery(task_id, recovery_context, heartbeat)
                if await self._gemini_expire_if_needed(task_id, release_gate=True):
                    return
                initial_url = initial.get("url", "about:blank") if isinstance(initial, dict) else "about:blank"
                if isinstance(initial_url, str) and self.origin(initial_url) == GEMINI_PROMPT_ORIGIN:
                    initial = await self._gemini_refresh_after_cookie_sync(task_id, initial, recovery_context)
                    if initial is None or not await self._gemini_require_authentication(task_id):
                        return
            self._task_activity(task_id, "checking_dependencies")
            model_health = await self.model.health()
            if not model_health.get("ready"):
                raise DomainError("DEPENDENCIES_NOT_READY", "Model and browser worker are not ready.")
            history = self.histories.setdefault(task_id, [])
            begin_task = getattr(self.model, "begin_task", None)
            managed_model = callable(begin_task)
            if managed_model:
                begin_task(task_id)
            resume_verification = (not fresh_run and record.phase == "verification_required"
                                   and isinstance(record.result, dict) and record.result.get("verified") is False)
            pending_feedback = None
            pending_autonomy_feedback = None
            pending_verbatim_feedback = None
            pending_key_chord_feedback = None
            while self.repository.get(task_id).status == TaskStatus.RUNNING:
                record = self.repository.get(task_id)
                # Deadline includes pauses via persisted wall-clock creation time.
                if self._task_deadline_elapsed(record):
                    await self.worker.gate("none")
                    self.repository.transition(task_id, TaskStatus.EXPIRED, phase="task_deadline_exceeded",
                        error={"code": "TASK_EXPIRED", "message": TASK_EXPIRED_MESSAGE})
                    return
                if record.step_count >= record.spec.limits.max_steps:
                    raise DomainError("STEP_LIMIT_EXCEEDED", "Достигнут лимит шагов")
                observation, heartbeat = await self._observe_with_recovery(task_id, recovery_context, heartbeat)
                if await self._gemini_expire_if_needed(task_id, release_gate=True):
                    return
                current_url = observation.get("url", "about:blank")
                if fresh_run:
                    fresh_run = False
                    if current_url not in {"about:blank", ""} and self.origin(current_url) not in record.spec.allowed_origins:
                        try:
                            reset = await self.worker.call("POST", "/view/reset", {
                                "control_epoch": observation["control_epoch"],
                            })
                        except DomainError as exc:
                            if exc.code == "WORKER_UNAVAILABLE":
                                raise DomainError("ACTION_OUTCOME_UNKNOWN", "Initial page reset outcome is unknown; inspect browser state before continuing") from exc
                            raise
                        if reset.get("outcome") != "applied":
                            raise DomainError("ACTION_OUTCOME_UNKNOWN", "Initial page reset was not confirmed; inspect browser state before continuing")
                        observation, heartbeat = await self._observe_with_recovery(task_id, recovery_context, heartbeat)
                        if await self._gemini_expire_if_needed(task_id, release_gate=True):
                            return
                        current_url = observation.get("url", "about:blank")
                if current_url not in {"about:blank", ""} and self.origin(current_url) not in record.spec.allowed_origins:
                    raise DomainError("POLICY_DENIED", "Текущая страница вне разрешённых сайтов задачи")
                if current_url not in {"about:blank", ""} and self.origin(current_url) == GEMINI_PROMPT_ORIGIN:
                    observation = await self._gemini_refresh_after_cookie_sync(task_id, observation, recovery_context)
                    if observation is None:
                        return
                    if not await self._gemini_require_authentication(task_id):
                        return
                if resume_verification:
                    if current_url in {"about:blank", ""}:
                        raise DomainError("POLICY_DENIED", "Verification requires a page on an allowed origin")
                    resume_verification = False
                    self._task_activity(task_id, "verifying")
                    check = await self.worker.call("POST", "/verify", {"conditions": record.spec.success_conditions})
                    if self.repository.get(task_id).status != TaskStatus.RUNNING:
                        return
                    if self._task_deadline_elapsed(record):
                        continue
                    if check.get("verified") is True and record.spec.success_conditions:
                        await self.worker.gate("none")
                        self.repository.transition(task_id, TaskStatus.SUCCEEDED, phase="verified",
                                                   result={"summary": record.result.get("summary", "Готово"),
                                                           "verified": True, "checks": check.get("checks", []),
                                                           "elapsed_s": time.monotonic()-started})
                        return
                    if check.get("verified") is not True:
                        pending_feedback = self._verification_repair(record, check)
                        if pending_feedback is None:
                            await self.worker.gate("none")
                            self.repository.transition(task_id, TaskStatus.WAITING_HUMAN, phase="verification_required",
                                                       result={"summary": record.result.get("summary", "Нужна проверка результата"),
                                                               "verified": False, "checks": check.get("checks", [])})
                            return
                screenshot = base64.b64decode(observation["screenshot_png_b64"], validate=True)
                model_goal = (
                    record.spec.goal
                    + "\nTask allowed origins (exact): " + json.dumps(record.spec.allowed_origins, ensure_ascii=False)
                    + "\nUser-provided data (JSON): " + json.dumps(record.spec.user_inputs, ensure_ascii=False)
                )
                if record.spec.user_inputs:
                    model_goal += (
                        "\nCopy every user-provided literal exactly as written, including spelling, punctuation, and domain. "
                        "Do not correct, substitute, infer, or reformat provided values. Before submitting, compare each entered value with its source literal. "
                        "If verification reports a value mismatch, use that feedback to re-enter the exact source literal and verify it again."
                    )
                if record.spec.verbatim_text is not None:
                    model_goal += (
                        "\nAn exact text payload is supplied. If text must be typed, emit the exact token "
                        + VERBATIM_TEXT_TOKEN
                        + ". Runtime will insert the supplied text unchanged. Do not rephrase, translate, "
                        "transliterate, correct, or reformat it."
                    )
                model_goal += (
                    "\nPreserve names and other user-provided wording exactly when entering them into a page. "
                    "Only transform text when the user explicitly requests that transformation."
                )
                if record.spec.execution_mode == 'autonomous':
                    model_goal += ('\nAct only within the user’s stated task and the allowed origins. '
                                   'Do not take unrelated actions, invent missing required inputs or credentials, '
                                   'or bypass login, CAPTCHA, OTP or other access challenges.')
                if pending_feedback and not callable(getattr(self.model, "record_verification_failure", None)):
                    model_goal += "\n" + pending_feedback
                pending_feedback = None
                if pending_autonomy_feedback:
                    model_goal += "\n" + pending_autonomy_feedback
                pending_autonomy_feedback = None
                if pending_verbatim_feedback:
                    model_goal += "\n" + pending_verbatim_feedback
                pending_verbatim_feedback = None
                if pending_key_chord_feedback:
                    model_goal += "\n" + pending_key_chord_feedback
                pending_key_chord_feedback = None
                if managed_model:
                    try:
                        self._task_activity(task_id, "thinking")
                        action = await self.model.next_action(model_goal, screenshot, history[-12:], current_url=current_url)
                    except ModelError as exc:
                        if exc.category != "invalid_key_chord":
                            raise
                        if self.repository.get(task_id).status != TaskStatus.RUNNING:
                            return
                        current = self.repository.get(task_id)
                        if self._task_deadline_elapsed(current):
                            await self.worker.gate("none")
                            self.repository.transition(task_id, TaskStatus.EXPIRED, expected_version=current.version,
                                phase="task_deadline_exceeded",
                                error={"code": "TASK_EXPIRED", "message": TASK_EXPIRED_MESSAGE})
                            return
                        count = self.repository.record_invalid_key_chord(task_id)
                        if count >= 3:
                            await self.worker.gate("none")
                            self.repository.transition(task_id, TaskStatus.FAILED, phase="model_output_invalid",
                                error={"code": "MODEL_OUTPUT_INVALID",
                                       "message": "The model repeatedly returned an invalid key chord."})
                            return
                        feedback = getattr(self.model, "record_model_output_rejection", None)
                        if callable(feedback):
                            feedback("invalid_key_chord")
                        else:
                            pending_key_chord_feedback = KEY_CHORD_FEEDBACK
                        continue
                else:
                    try:
                        self._task_activity(task_id, "thinking")
                        action = await self.model.next_action(
                            model_goal + "\nCurrent page URL: " + current_url, screenshot, history[-12:])
                    except ModelError as exc:
                        if exc.category != "invalid_key_chord":
                            raise
                        if self.repository.get(task_id).status != TaskStatus.RUNNING:
                            return
                        current = self.repository.get(task_id)
                        if self._task_deadline_elapsed(current):
                            await self.worker.gate("none")
                            self.repository.transition(task_id, TaskStatus.EXPIRED, expected_version=current.version,
                                phase="task_deadline_exceeded",
                                error={"code": "TASK_EXPIRED", "message": TASK_EXPIRED_MESSAGE})
                            return
                        count = self.repository.record_invalid_key_chord(task_id)
                        if count >= 3:
                            await self.worker.gate("none")
                            self.repository.transition(task_id, TaskStatus.FAILED, phase="model_output_invalid",
                                error={"code": "MODEL_OUTPUT_INVALID",
                                       "message": "The model repeatedly returned an invalid key chord."})
                            return
                        pending_key_chord_feedback = KEY_CHORD_FEEDBACK
                        continue
                if self.repository.get(task_id).status != TaskStatus.RUNNING:
                    return
                record = self.repository.get(task_id)
                if self._task_deadline_elapsed(record):
                    await self.worker.gate("none")
                    self.repository.transition(task_id, TaskStatus.EXPIRED, expected_version=record.version,
                        phase="task_deadline_exceeded",
                        error={"code": "TASK_EXPIRED", "message": TASK_EXPIRED_MESSAGE})
                    return
                if action.get("kind") == "search":
                    query = action.get("query")
                    if not isinstance(query, str) or not query.strip() or len(query) > 10_000:
                        raise DomainError("MODEL_OUTPUT_INVALID", "search query must be non-empty and bounded")
                    if GOOGLE_SEARCH_ORIGIN not in record.spec.allowed_origins:
                        raise DomainError("POLICY_DENIED", "web search requires https://www.google.com in this task's allowed_origins")
                    action = {"kind": "navigate", "url": f"{GOOGLE_SEARCH_ORIGIN}/search?q={quote_plus(query, safe='')}"}
                if record.spec.verbatim_text is not None and action.get("kind") == "type_text":
                    scrub_action = getattr(self.model, "record_verbatim_text_action", None)
                    if callable(scrub_action):
                        scrub_action()
                    candidate = action.get("text")
                    if candidate != record.spec.verbatim_text and candidate != VERBATIM_TEXT_TOKEN:
                        count = self.repository.record_verbatim_text_rejection(task_id)
                        if count >= 3:
                            await self.worker.gate("none")
                            self.repository.transition(task_id, TaskStatus.FAILED, phase="verbatim_text_mismatch",
                                error={"code": "VERBATIM_TEXT_MISMATCH", "message": VERBATIM_TEXT_FEEDBACK})
                            return
                        feedback = getattr(self.model, "record_action_rejection", None)
                        if callable(feedback):
                            feedback("verbatim_text_mismatch")
                        else:
                            pending_verbatim_feedback = VERBATIM_TEXT_FEEDBACK
                        continue
                    action = {**action, "text": record.spec.verbatim_text}
                if action["kind"] == "finish":
                    if not record.spec.success_conditions:
                        await self.worker.gate("none")
                        self.repository.transition(task_id, TaskStatus.SUCCEEDED, phase="completed_unverified", result={
                            "summary": action.get("summary", "Готово"), "verified": False, "checks": [],
                            "detail": "Независимые критерии проверки не заданы; результат по сообщению модели.",
                            "sources": [{"url": current_url}], "elapsed_s": time.monotonic()-started})
                        return
                    self._task_activity(task_id, "verifying")
                    check = await self.worker.call("POST", "/verify", {"conditions": record.spec.success_conditions})
                    if self.repository.get(task_id).status != TaskStatus.RUNNING:
                        return
                    if self._task_deadline_elapsed(record):
                        continue
                    if check.get("verified") is True and record.spec.success_conditions:
                        await self.worker.gate("none")
                        self.repository.transition(task_id, TaskStatus.SUCCEEDED, phase="verified", result={
                            "summary": action.get("summary", "Готово"), "verified": True, "checks": check.get("checks", []),
                            "sources": [{"url": current_url}], "elapsed_s": time.monotonic()-started})
                    else:
                        pending_feedback = self._verification_repair(record, check)
                        if pending_feedback is not None:
                            continue
                        await self.worker.gate("none")
                        self.repository.transition(task_id, TaskStatus.WAITING_HUMAN, phase="verification_required", result={
                            "summary": action.get("summary", "Нужна проверка результата"), "verified": False,
                            "checks": check.get("checks", [])})
                    return
                if action["kind"] == "ask_user":
                    if record.spec.execution_mode == 'autonomous':
                        message = action.get('question', 'Нужны данные')
                        total, consecutive = self.repository.autonomy_notice(task_id, str(message))
                        record_outcome = getattr(self.model, 'record_outcome', None)
                        if callable(record_outcome): record_outcome('applied')
                        feedback = getattr(self.model, 'record_autonomy_notice', None)
                        if callable(feedback): feedback()
                        else: pending_autonomy_feedback = 'The user was notified. Continue the authorized task using available information; do not invent missing essential data or bypass authentication or challenges.'
                        if consecutive >= 3 or total >= 8:
                            await self.worker.gate('none')
                            self.repository.transition(task_id, TaskStatus.FAILED, phase='autonomy_no_progress', error={'code': 'HUMAN_INPUT_UNAVAILABLE', 'message': 'Уведомления не помогли продолжить; возможно, нужны данные пользователя.'})
                            return
                        continue
                    await self.worker.gate("none")
                    self.repository.add_event(task_id, "question", {"message": action.get("question", "Нужны данные")})
                    self.repository.transition(task_id, TaskStatus.WAITING_HUMAN, phase="answer_required")
                    return
                if action["kind"] == "remember_fact":
                    self._remember(task_id, {"model_action": dict(action), "outcome": "applied",
                                             "fact": action.get("text", ""), "source": current_url})
                    self.repository.increment_step(task_id)
                    record_outcome = getattr(self.model, "record_outcome", None)
                    if callable(record_outcome):
                        record_outcome("applied")
                    continue
                if action["kind"] == "navigate" and self.origin(action["url"]) not in record.spec.allowed_origins:
                    raise DomainError("POLICY_DENIED", "Модель запросила сайт вне разрешённого списка")
                model_action = dict(action)
                if record.spec.verbatim_text is not None and action.get("kind") == "type_text":
                    # Never retain the immutable source literal in in-memory model history.
                    model_action["text"] = VERBATIM_TEXT_TOKEN
                if action["kind"] in {"click", "mouse_move", "drag"}:
                    action = dict(action)
                    # Fara1.5 official FARA_DISPLAY_SIZE=1000; browser RPC uses viewport pixels.
                    action["x"] = action["x"] * observation["width"] / 1000
                    action["y"] = action["y"] * observation["height"] / 1000
                command = {"action_id": secrets.token_urlsafe(16), "control_epoch": observation["control_epoch"],
                           "observation_id": observation["observation_id"], "action": action,
                           "allowed_origins": record.spec.allowed_origins}
                if record.spec.execution_mode == 'autonomous':
                    command['allow_write'] = True
                if action['kind'] in {'click', 'press_key'}:
                    capability = self.repository.capability(task_id, 'gemini_prompt_submit')
                    if capability:
                        command['site_capability'] = capability
                # Record only kind and identifiers. Typed text is not logged.
                self._task_activity(task_id, "executing")
                self.repository.add_event(task_id, "action.planned", {"action_id": command["action_id"], "kind": action["kind"]})
                try:
                    receipt = await self.worker.call("POST", "/execute", command)
                except DomainError as exc:
                    record_outcome = getattr(self.model, "record_outcome", None)
                    if callable(record_outcome):
                        record_outcome("unknown" if exc.code == "WORKER_UNAVAILABLE" else "failed", exc.code)
                    if (record.spec.execution_mode == 'autonomous' and action['kind'] == 'type_text'
                            and exc.code == 'CAPABILITY_UNAVAILABLE'
                            and exc.message == 'text input requires a focused editable field'):
                        self.repository.add_event(task_id, "action.failed", {
                            "action_id": command["action_id"], "kind": action["kind"], "code": exc.code,
                        })
                        count = self.repository.record_editable_focus_rejection(task_id)
                        if count >= 3:
                            await self.worker.gate('none')
                            self.repository.transition(task_id, TaskStatus.FAILED, phase='action_recovery_exhausted',
                                error={'code': 'EDITABLE_FOCUS_RETRIES_EXHAUSTED', 'message': 'Не удалось установить фокус на поле ввода после нескольких попыток.'})
                            return
                        feedback = getattr(self.model, 'record_action_rejection', None)
                        if callable(feedback): feedback('editable_not_focused')
                        else: pending_autonomy_feedback = 'Typing was not performed: no focused editable field. Inspect the screenshot, focus the intended field, then retry.'
                        continue
                    if exc.code == "WORKER_UNAVAILABLE":
                        raise DomainError("ACTION_OUTCOME_UNKNOWN", "Связь потеряна после отправки действия; сначала проверьте его результат") from exc
                    if exc.code != "ACTION_OUTCOME_UNKNOWN":
                        self.repository.add_event(task_id, "action.failed", {
                            "action_id": command["action_id"], "kind": action["kind"], "code": exc.code,
                        })
                    raise
                if receipt.get("outcome") != "applied":
                    record_outcome = getattr(self.model, "record_outcome", None)
                    if callable(record_outcome):
                        record_outcome("failed", "ACTION_OUTCOME_UNKNOWN")
                    raise DomainError("ACTION_OUTCOME_UNKNOWN", "Результат действия неизвестен; требуется проверка")
                if self.repository.get(task_id).status != TaskStatus.RUNNING:
                    return
                self.repository.increment_step(task_id)
                self.repository.add_event(task_id, 'action.applied', {
                    'action_id': command['action_id'], 'kind': action['kind'],
                })
                self._remember(task_id, {"model_action": model_action, "url": current_url, "outcome": "applied"})
                record_outcome = getattr(self.model, "record_outcome", None)
                if callable(record_outcome):
                    record_outcome("applied")
        except asyncio.CancelledError:
            raise
        except (DomainError, ModelError) as exc:
            current = self.repository.get(task_id)
            if current.status == TaskStatus.RUNNING:
                await self._release_agent_lease_if_owned(recovery_context)
                status = TaskStatus.RECOVERING if exc.code == "ACTION_OUTCOME_UNKNOWN" else TaskStatus.WAITING_APPROVAL if exc.code == "APPROVAL_REQUIRED" else TaskStatus.WAITING_HUMAN
                self.repository.transition(task_id, status, phase=exc.code.lower(), error={"code": exc.code, "message": exc.message})
        except Exception:
            current = self.repository.get(task_id)
            if current.status == TaskStatus.RUNNING:
                self.repository.transition(task_id, TaskStatus.RECOVERING, phase="internal_error", error={"code": "INTERNAL_ERROR", "message": "Выполнение остановлено; требуется проверка состояния"})
        finally:
            if heartbeat:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            try:
                if self.repository.get(task_id).status in TERMINAL:
                    terminal = self.repository.get(task_id)
                    if terminal.spec.workflow == "gemini_prompt_submit":
                        self.histories.pop(task_id, None)
                    else:
                        self._discard_task_memory(task_id)
            except DomainError:
                self._discard_task_memory(task_id)

    @staticmethod
    def origin(url: str) -> str:
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}"

    @staticmethod
    def _task_deadline_elapsed(record: Any) -> bool:
        from datetime import UTC, datetime
        created = datetime.fromisoformat(record.created_at.replace("Z", "+00:00"))
        return (datetime.now(UTC) - created).total_seconds() >= record.spec.limits.timeout_s

    def _has_unresolved_action_dispatch(self, task_id: str) -> bool:
        cursor = 0
        planned: set[str] = set()
        resolved: set[str] = set()
        while True:
            page = self.repository.events(task_id, after_event_id=cursor, limit=1000)
            if not page:
                break
            for event in page:
                payload = event.get("payload") or {}
                action_id = payload.get("action_id") if isinstance(payload, dict) else None
                if not isinstance(action_id, str):
                    continue
                if event.get("event_type") == "action.planned":
                    planned.add(action_id)
                elif event.get("event_type") in {"action.applied", "action.failed"}:
                    resolved.add(action_id)
            cursor = page[-1]["event_id"]
            if len(page) < 1000:
                break
        return bool(planned - resolved)

    async def control(self, task_id: str, operation: str, expected_version: int | None = None):
        allowed_operations = {"pause", "resume", "cancel", "takeover", "release-control", "review-recovery"}
        if operation not in allowed_operations:
            raise DomainError("INVALID_OPERATION", "unsupported task control operation")
        record = self.repository.get(task_id)
        dispatch_was_inflight = operation == "cancel" and self._has_unresolved_action_dispatch(task_id)
        if expected_version is not None and expected_version != record.version:
            raise DomainError("VERSION_CONFLICT", "task version does not match expected_version")
        if (operation == "resume" and record.status in {
                TaskStatus.PAUSED, TaskStatus.WAITING_HUMAN, TaskStatus.WAITING_APPROVAL,
        } and self._task_deadline_elapsed(record)):
            self.repository.transition(task_id, TaskStatus.EXPIRED, expected_version=record.version,
                phase="task_deadline_exceeded",
                error={"code": "TASK_EXPIRED", "message": TASK_EXPIRED_MESSAGE})
            raise DomainError("TASK_EXPIRED", TASK_EXPIRED_MESSAGE)
        if record.status in TERMINAL:
            raise DomainError("INVALID_TRANSITION", "Задача уже завершена")
        if operation == "resume" and record.status == TaskStatus.RECOVERING:
            raise DomainError("RECOVERY_REVIEW_REQUIRED", "Review the browser state and use review-recovery before resuming")
        if operation == "review-recovery":
            if record.status != TaskStatus.RECOVERING:
                raise DomainError("INVALID_TRANSITION", "review-recovery is only valid for a recovering task")
            if expected_version is None:
                raise DomainError("VERSION_REQUIRED", "review-recovery requires the current task version")
            # Explicit review hands control back to the agent gate but leaves
            # the task paused. A separate resume request is still required.
            await self.worker.gate("none")
            return self.repository.transition(task_id, TaskStatus.PAUSED, expected_version=record.version,
                                              phase="recovery_reviewed")
        if record.status == TaskStatus.RECOVERING and operation in {"pause", "release-control"}:
            raise DomainError("RECOVERY_REVIEW_REQUIRED", "Review the browser state before changing recovery status")
        if record.status == TaskStatus.QUEUED and operation != "cancel":
            raise DomainError("INVALID_TRANSITION", "queued tasks can only be cancelled; resume is not needed before execution starts")
        if record.status == TaskStatus.QUEUED and operation == "cancel":
            cancelled = self.repository.transition(task_id, TaskStatus.CANCELLED, expected_version=record.version)
            previous = self.jobs.get(task_id)
            if previous and not previous.done():
                previous.cancel()
                await asyncio.gather(previous, return_exceptions=True)
            self._discard_task_memory(task_id)
            return cancelled
        if record.status == TaskStatus.RECOVERING and operation == "takeover":
            await self.worker.gate("human")
            previous = self.jobs.get(task_id)
            if previous and not previous.done():
                previous.cancel()
                await asyncio.gather(previous, return_exceptions=True)
            # Human inspection is useful during recovery, but taking over does
            # not certify that the unknown action completed safely.
            return record
        if operation == "resume":
            state = await self.worker.state()
            if state.get("control_owner") == "human":
                raise DomainError("CONTROL_OWNED_BY_HUMAN", "Сначала верните ручное управление")
            result = self.repository.transition(task_id, TaskStatus.RUNNING, expected_version=record.version)
            previous = self.jobs.get(task_id)
            if previous and not previous.done():
                previous.cancel()
                await asyncio.gather(previous, return_exceptions=True)
            self.schedule(task_id)
            return result
        if record.status == TaskStatus.RUNNING:
            record = self.repository.transition(task_id, TaskStatus.PAUSE_REQUESTED)
        try:
            await self.worker.gate("human" if operation == "takeover" else "none")
        except DomainError as exc:
            if record.status in {TaskStatus.PAUSE_REQUESTED, TaskStatus.RUNNING}:
                return self.repository.transition(task_id, TaskStatus.RECOVERING, error={"code": "CONTROL_NOT_CONFIRMED", "message": exc.message})
            raise
        previous = self.jobs.get(task_id)
        if previous and not previous.done():
            previous.cancel()
            await asyncio.gather(previous, return_exceptions=True)
        if operation == "cancel":
            # A dispatch may have begun while the gate request yielded. The
            # persisted planned/applied/failed events decide whether its result
            # is still uncertain after the worker has been fenced.
            dispatch_was_inflight = dispatch_was_inflight or self._has_unresolved_action_dispatch(task_id)
        target = (TaskStatus.RECOVERING if dispatch_was_inflight else TaskStatus.CANCELLED) if operation == "cancel" else TaskStatus.WAITING_HUMAN if operation == "takeover" else TaskStatus.PAUSED
        if record.status == target:
            return record
        if target == TaskStatus.RECOVERING:
            result = self.repository.transition(task_id, target, phase="cancel_outcome_unknown",
                error={"code": "ACTION_OUTCOME_UNKNOWN",
                       "message": "Cancel interrupted an already dispatched browser action; inspect browser state before deciding whether to resume."})
        else:
            result = self.repository.transition(task_id, target)
        if target == TaskStatus.CANCELLED:
            self._discard_task_memory(task_id)
        return result
