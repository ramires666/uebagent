"""Guarded disposable-worker checks for uncertain action replies, restart, and profile ownership."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from browser_mcp.config import Settings
from browser_mcp.domain import DomainError, TaskSpec, TaskStatus
from browser_mcp.runtime import TaskRunner, WorkerClient
from browser_mcp.storage import TaskRepository

WORKER_URL = "http://browser:7070"
FIXTURE_URL = "https://readiness-fixture.test/overlay"
_FAILURE_STAGE = "startup"


class IntegrationGuardError(RuntimeError):
    pass


def validate_environment(*, confirmed: bool, environ: dict[str, str] | None = None,
                         docker_marker: bool | None = None) -> Path:
    env = os.environ if environ is None else environ
    if not confirmed:
        raise IntegrationGuardError("explicit_disposable_confirmation_required")
    if env.get("APP_ENV", "").lower() != "test" or env.get("READINESS_DISPOSABLE") != "1":
        raise IntegrationGuardError("disposable_test_markers_required")
    if env.get("READINESS_RUNTIME_INTEGRATION") != "1" or env.get("READINESS_ACTION_TRACE") != "1":
        raise IntegrationGuardError("integration_and_action_trace_opt_in_required")
    if env.get("BROWSER_WORKER_URL") != WORKER_URL:
        raise IntegrationGuardError("worker_url_must_be_exact_disposable_service")
    if not (Path("/.dockerenv").is_file() if docker_marker is None else docker_marker):
        raise IntegrationGuardError("container_only_execution_required")
    key = Path(env.get("WORKER_API_KEY_FILE", ""))
    if not key.is_file() or not key.read_text(encoding="utf-8").strip():
        raise IntegrationGuardError("worker_key_file_missing")
    return key


class ScriptedModel:
    """Local deterministic model; wait provides a bounded concurrency window."""
    def __init__(self, *, wait_seconds: float = 3.0):
        self.calls = 0
        self.wait_seconds = wait_seconds

    async def health(self) -> dict[str, bool]:
        return {"ready": True}

    async def next_action(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        return {"kind": "click", "x": 423.611, "y": 368.889} if self.calls == 1 else {"kind": "finish", "summary": "done"}


class InFlightModel(ScriptedModel):
    async def next_action(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        return {"kind": "wait", "seconds": 4} if self.calls == 1 else {"kind": "finish", "summary": "done"}


class DropFirstExecuteReply(httpx.AsyncBaseTransport):
    """Forward normally, then simulate a lost response once; never retry upstream."""
    def __init__(self, upstream_handler=None):
        self.upstream_handler = upstream_handler
        self.execute_forward_count = 0
        self.dropped = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        transport = httpx.MockTransport(self.upstream_handler) if self.upstream_handler else None
        async with httpx.AsyncClient(transport=transport, trust_env=False) as upstream:
            response = await upstream.send(request)
            content = await response.aread()
            materialized = httpx.Response(status_code=response.status_code, headers=response.headers,
                                          content=content, request=request)
        if request.url.path == "/execute":
            self.execute_forward_count += 1
            if not self.dropped:
                self.dropped = True
                raise httpx.ReadTimeout("simulated response loss after worker completed request", request=request)
        return materialized

    async def aclose(self) -> None:
        # WorkerClient closes its short-lived AsyncClient after every call.
        # The proxy itself is deliberately reusable across those calls.
        return None


class HoldFirstExecuteReply(httpx.AsyncBaseTransport):
    """Complete one real execute upstream, then hold its response until cancelled/released."""
    def __init__(self, upstream_handler=None):
        self.upstream_handler = upstream_handler
        self.execute_forward_count = 0
        self.forwarded = asyncio.Event()
        self.release = asyncio.Event()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        transport = httpx.MockTransport(self.upstream_handler) if self.upstream_handler else None
        async with httpx.AsyncClient(transport=transport, trust_env=False) as upstream:
            response = await upstream.send(request)
            content = await response.aread()
            materialized = httpx.Response(status_code=response.status_code, headers=response.headers,
                                          content=content, request=request)
        if request.url.path == "/execute":
            self.execute_forward_count += 1
            self.forwarded.set()
            await asyncio.wait_for(self.release.wait(), timeout=10)
        return materialized

    async def aclose(self) -> None:
        # WorkerClient closes its short-lived AsyncClient after every call.
        return None


def _spec(label: str) -> TaskSpec:
    return TaskSpec(sync_cookies=False, goal=label,
                    allowed_origins=["https://readiness-fixture.test"],
                    limits={"timeout_s": 30, "max_steps": 3},
                    idempotency_key=label + "-" + uuid.uuid4().hex)


async def _until(predicate, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return bool(predicate())


async def run_integration(key_file: Path) -> dict[str, Any]:
    global _FAILURE_STAGE
    with tempfile.TemporaryDirectory(prefix="readiness-lifecycle-") as temp:
        root = Path(temp)
        settings = Settings(data_dir=root, worker_url=WORKER_URL,
                            worker_key_file=str(key_file), model_prompt_file="", test_mode=True)
        db_path = root / "tasks.sqlite3"
        repo = TaskRepository(db_path)
        proxy = DropFirstExecuteReply()
        worker = WorkerClient(settings, transport=proxy)
        model = ScriptedModel()
        runner = TaskRunner(settings, repo, worker=worker, model=model)
        restart_runner = None
        restart_repo = None
        held_runner = None
        held_repo = None
        held_restart_runner = None
        held_restart_repo = None
        held_proxy = None
        busy_runner = None
        busy_repo = None
        try:
            _FAILURE_STAGE = "worker_preflight"
            state = await worker.state()
            if not (state.get("control_owner") == "none" and state.get("browser_started") is True
                    and state.get("context_count") == 1 and state.get("page_count") == 1
                    and state.get("recovery_state") == "idle"):
                raise AssertionError("worker_not_ready_idle_single_page")

            # Exercise the real worker busy gate while one TaskRunner dispatch is in flight.
            _FAILURE_STAGE = "busy_ownership"
            busy_repo = TaskRepository(root / "busy.sqlite3")
            busy_runner = TaskRunner(settings, busy_repo, worker=WorkerClient(settings), model=InFlightModel())
            busy_first = busy_runner.create(_spec("Hold one worker action briefly for ownership check."))
            became_busy = False
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                busy_state = await worker.state()
                if busy_state.get("action_in_progress") is True and busy_state.get("control_owner") == "agent":
                    became_busy = True
                    break
                await asyncio.sleep(0.05)
            if not became_busy:
                raise AssertionError("worker_never_entered_agent_owned_busy_state")
            try:
                busy_runner.create(_spec("Concurrent task must be rejected while the worker is owned."))
                concurrent_rejected = False
            except DomainError as exc:
                if exc.code != "PROFILE_BUSY":
                    raise
                concurrent_rejected = True
            if not concurrent_rejected:
                raise AssertionError("concurrent_profile_task_was_accepted")
            busy_current = busy_repo.get(busy_first.task_id)
            await busy_runner.control(busy_first.task_id, "cancel", busy_current.version)
            if busy_repo.get(busy_first.task_id).status != TaskStatus.RECOVERING:
                raise AssertionError("inflight_owner_cancel_did_not_require_review")
            await busy_runner.close()
            busy_runner = None
            busy_repo.close()
            busy_repo = None
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                state = await worker.state()
                if state.get("control_owner") == "none" and state.get("action_in_progress") is False:
                    break
                await asyncio.sleep(0.05)
            else:
                raise AssertionError("worker_did_not_release_after_busy_ownership_check")

            _FAILURE_STAGE = "fixture_install_navigation"
            installed = await worker.call("POST", "/diagnostics/readiness-fixture", {"scenario": "overlay"})
            if installed != {"installed": True, "scenario": "overlay", "path": "/overlay"}:
                raise AssertionError("fixture_install_failed")
            gate = await worker.gate("human")
            try:
                nav = await worker.call("POST", "/manual/execute", {
                    "control_epoch": gate["control_epoch"],
                    "allowed_origins": ["https://readiness-fixture.test"],
                    "action": {"kind": "navigate", "url": FIXTURE_URL},
                })
                if nav.get("outcome") != "applied":
                    raise AssertionError("fixture_navigation_failed")
            finally:
                await worker.gate("none")
            await worker.call("POST", "/diagnostics/readiness-actions/reset", {})

            # Simulate process loss after a real browser effect but before the reply reaches TaskRunner.
            _FAILURE_STAGE = "running_inflight_restart"
            held_repo = TaskRepository(root / "held.sqlite3")
            held_proxy = HoldFirstExecuteReply()
            held_worker = WorkerClient(settings, transport=held_proxy)
            held_runner = TaskRunner(settings, held_repo, worker=held_worker, model=ScriptedModel())
            held_record = held_runner.create(_spec("Click once while the worker response is held."))
            if not await _until(lambda: held_proxy.forwarded.is_set(), timeout=10):
                raise AssertionError("held_response_worker_dispatch_timeout")
            held_live = held_repo.get(held_record.task_id)
            held_events = held_repo.events(held_record.task_id)
            held_planned = [e for e in held_events if e["event_type"] == "action.planned"]
            held_applied = [e for e in held_events if e["event_type"] == "action.applied"]
            if held_live.status != TaskStatus.RUNNING or len(held_planned) != 1 or held_applied:
                raise AssertionError("held_response_did_not_preserve_running_inflight_boundary")
            await held_runner.close()  # Cancel local work without issuing runner.control or changing task state.
            held_runner = None
            held_repo.close()
            held_repo = None
            held_restart_repo = TaskRepository(root / "held.sqlite3")
            held_restart_runner = TaskRunner(settings, held_restart_repo, worker=WorkerClient(settings),
                                             model=ScriptedModel())
            await held_restart_runner.start()
            held_restarted = held_restart_repo.get(held_record.task_id)
            if held_restarted.status != TaskStatus.RECOVERING or held_restarted.phase != "restart_recovery":
                raise AssertionError("running_task_restart_not_moved_to_recovery")
            if held_restart_runner.jobs:
                raise AssertionError("running_inflight_task_redispatched_after_restart")
            held_after_events = held_restart_repo.events(held_record.task_id)
            if (len([e for e in held_after_events if e["event_type"] == "action.planned"]) != 1
                    or any(e["event_type"] == "action.applied" for e in held_after_events)
                    or held_proxy.execute_forward_count != 1):
                raise AssertionError("running_task_restart_replayed_or_resolved_action")
            held_trace = await worker.call("GET", "/diagnostics/readiness-actions")
            held_clicks = [x for x in held_trace.get("records", []) if x.get("kind") == "click"]
            held_effects = await worker.call("POST", "/verify", {"conditions": [
                {"kind": "element_text_contains", "selector": "#target-count", "text": "1"},
                {"kind": "element_text_contains", "selector": "#overlay-count", "text": "1"},
            ]})
            held_checks = held_effects.get("checks", [])
            if (len(held_clicks) != 1 or held_clicks[0].get("outcome") != "applied"
                    or len(held_checks) != 2 or held_checks[0].get("passed") is not False
                    or held_checks[1].get("passed") is not True):
                raise AssertionError("running_restart_independent_effect_check_failed")
            await worker.gate("none")
            held_worker_state = await worker.state()
            if (held_worker_state.get("control_owner") != "none"
                    or held_worker_state.get("action_in_progress") is not False
                    or held_worker_state.get("context_count") != 1
                    or held_worker_state.get("page_count") != 1):
                raise AssertionError("worker_not_idle_after_running_task_restart")
            await held_restart_runner.close()
            held_restart_runner = None
            held_restart_repo.close()
            held_restart_repo = None
            held_proxy.release.set()

            # Reinstall/reset the static fixture to isolate the lost-response scenario.
            _FAILURE_STAGE = "uncertain_response"
            installed = await worker.call("POST", "/diagnostics/readiness-fixture", {"scenario": "overlay"})
            if installed != {"installed": True, "scenario": "overlay", "path": "/overlay"}:
                raise AssertionError("second_fixture_install_failed")
            gate = await worker.gate("human")
            try:
                nav = await worker.call("POST", "/manual/execute", {
                    "control_epoch": gate["control_epoch"],
                    "allowed_origins": ["https://readiness-fixture.test"],
                    "action": {"kind": "navigate", "url": FIXTURE_URL},
                })
                if nav.get("outcome") != "applied":
                    raise AssertionError("second_fixture_navigation_failed")
            finally:
                await worker.gate("none")
            await worker.call("POST", "/diagnostics/readiness-actions/reset", {})

            record = runner.create(_spec("Click the overlay fixture target once and finish."))
            await asyncio.wait_for(asyncio.shield(runner.jobs[record.task_id]), timeout=30)
            current = repo.get(record.task_id)
            trace = await worker.call("GET", "/diagnostics/readiness-actions")
            effects = await worker.call("POST", "/verify", {"conditions": [
                {"kind": "element_text_contains", "selector": "#target-count", "text": "1"},
                {"kind": "element_text_contains", "selector": "#overlay-count", "text": "1"},
            ]})
            if current.status != TaskStatus.RECOVERING or current.phase != "action_outcome_unknown":
                raise AssertionError("uncertain_reply_not_persisted_as_recovering")
            events = repo.events(record.task_id)
            planned = [e for e in events if e["event_type"] == "action.planned"]
            applied = [e for e in events if e["event_type"] == "action.applied"]
            if len(planned) != 1 or applied or model.calls != 1:
                raise AssertionError("uncertain_action_was_replayed_or_resolved")
            clicks = [x for x in trace.get("records", []) if x.get("kind") == "click"]
            checks = effects.get("checks", [])
            if proxy.execute_forward_count != 1 or len(clicks) != 1 or clicks[0].get("outcome") != "applied":
                raise AssertionError("worker_dispatch_or_side_effect_count_invalid")
            if len(checks) != 2 or checks[0].get("passed") is not False or checks[1].get("passed") is not True:
                raise AssertionError("independent_fixture_effect_missing")

            await runner.close()
            repo.close()
            _FAILURE_STAGE = "uncertain_restart"
            restart_repo = TaskRepository(db_path)
            restart_runner = TaskRunner(settings, restart_repo, worker=WorkerClient(settings), model=ScriptedModel())
            await restart_runner.start()
            await asyncio.sleep(0.25)
            restarted = restart_repo.get(record.task_id)
            if restarted.status != TaskStatus.RECOVERING or restarted.phase != "action_outcome_unknown":
                raise AssertionError("restart_changed_review_required_task")
            if restart_runner.jobs or len([e for e in restart_repo.events(record.task_id)
                                           if e["event_type"] == "action.planned"]) != 1:
                raise AssertionError("restart_redispatched_unreviewed_action")

            final_state = await WorkerClient(settings).state()
            if final_state.get("context_count") != 1 or final_state.get("page_count") != 1:
                raise AssertionError("worker_page_or_context_count_changed")
            return {"status": "PASS", "uncertain_response": "RECOVERING", "worker_execute_forward_count": 1,
                    "worker_click_effect_count": len(clicks), "task_action_planned_count": len(planned),
                    "task_action_applied_count": len(applied), "model_calls": model.calls,
                    "restart_preserved_review": True, "restart_redispatch_count": 0,
                    "running_restart_preserved_review": True, "running_restart_redispatch_count": 0,
                    "running_worker_execute_forward_count": held_proxy.execute_forward_count,
                    "concurrent_task_rejected": concurrent_rejected,
                    "worker_context_count": final_state.get("context_count"),
                    "worker_page_count": final_state.get("page_count")}
        finally:
            if restart_runner:
                await restart_runner.close()
            if held_runner:
                await held_runner.close()
            if held_restart_runner:
                await held_restart_runner.close()
            if busy_runner:
                await busy_runner.close()
            if restart_repo:
                restart_repo.close()
            if held_repo:
                held_repo.close()
            if held_restart_repo:
                held_restart_repo.close()
            if busy_repo:
                busy_repo.close()
            try:
                await runner.close()
            except Exception:
                pass
            if not getattr(repo, "_closed", False):
                repo.close()
            await proxy.aclose()
            if held_proxy:
                held_proxy.release.set()
                await held_proxy.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--i-confirm-disposable", action="store_true")
    args = parser.parse_args(argv)
    try:
        key = validate_environment(confirmed=args.i_confirm_disposable)
        report = asyncio.run(run_integration(key))
    except IntegrationGuardError as exc:
        report = {"status": "BLOCKED", "reason": str(exc)}
    except AssertionError as exc:
        code = str(exc)
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,80}", code):
            code = "assertion_failed"
        report = {"status": "FAIL", "failure_stage": _FAILURE_STAGE, "failure_code": code}
    except Exception as exc:
        error_type = type(exc).__name__
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,63}", error_type):
            error_type = "UnexpectedError"
        report = {"status": "FAIL", "failure_stage": _FAILURE_STAGE,
                  "failure_code": "unexpected_error", "error_type": error_type}
    print(json.dumps(report, separators=(",", ":"), ensure_ascii=False))
    return 0 if report.get("status") == "PASS" else (2 if report.get("status") == "BLOCKED" else 1)


if __name__ == "__main__":
    raise SystemExit(main())
