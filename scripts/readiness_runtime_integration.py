"""Guarded TaskRunner cancellation integration against one disposable worker."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from browser_mcp.config import Settings
from browser_mcp.domain import TaskSpec, TaskStatus
from browser_mcp.runtime import TaskRunner, WorkerClient
from browser_mcp.storage import TaskRepository


WORKER_URL = "http://browser:7070"
INTEGRATION_FLAG = "READINESS_RUNTIME_INTEGRATION"
WAIT_SECONDS = 9.0
INFLIGHT_TIMEOUT_SECONDS = 5.0
IDLE_TIMEOUT_SECONDS = 20.0


class IntegrationGuardError(RuntimeError):
    pass


def validate_environment(*, confirmed: bool, environ: dict[str, str] | None = None,
                         docker_marker: bool | None = None) -> Path:
    env = os.environ if environ is None else environ
    if not confirmed:
        raise IntegrationGuardError("explicit_disposable_confirmation_required")
    if env.get("APP_ENV", "").lower() != "test" or env.get("READINESS_DISPOSABLE") != "1":
        raise IntegrationGuardError("disposable_test_markers_required")
    if env.get(INTEGRATION_FLAG) != "1":
        raise IntegrationGuardError("runtime_integration_opt_in_required")
    if env.get("BROWSER_WORKER_URL") != WORKER_URL:
        raise IntegrationGuardError("worker_url_must_be_exact_disposable_service")
    in_container = Path("/.dockerenv").is_file() if docker_marker is None else docker_marker
    if not in_container:
        raise IntegrationGuardError("container_only_execution_required")
    key_file = Path(env.get("WORKER_API_KEY_FILE", ""))
    if not key_file.is_file():
        raise IntegrationGuardError("worker_key_file_missing")
    return key_file


class ScriptedModel:
    """Deterministic, local model substitute; the second action must never run."""

    def __init__(self) -> None:
        self.calls = 0

    async def health(self) -> dict:
        return {"ready": True}

    async def next_action(self, *_args, **_kwargs) -> dict:
        self.calls += 1
        if self.calls == 1:
            return {"kind": "wait", "seconds": WAIT_SECONDS}
        return {"kind": "click", "x": 10, "y": 10}


def _assert_idle(state: dict, *, expected_identity: tuple[object, object, object] | None = None) -> None:
    if (state.get("browser_started") is not True or state.get("control_owner") != "none"
            or state.get("action_in_progress") is not False
            or state.get("import_in_progress") is not False or state.get("context_count") != 1
            or state.get("page_count") != 1 or state.get("resource_violation") is not False
            or state.get("recovery_needed") is not False or state.get("recovery_state") != "idle"):
        raise AssertionError("worker did not satisfy ready/idle/one-page invariants")
    if expected_identity is not None:
        identity = (state.get("browser_instance_id"), state.get("page_id"), state.get("recovery_generation"))
        if identity != expected_identity:
            raise AssertionError("worker browser/page identity changed during cancellation")


async def _wait_for_worker_state(worker: WorkerClient, predicate, timeout: float, step: str) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = await worker.state()
        if predicate(last):
            return last
        await asyncio.sleep(0.1)
    raise AssertionError(f"worker state wait timed out: {step}")


async def run_integration(*, confirmed: bool = True,
                          environ: dict[str, str] | None = None) -> dict:
    key_file = validate_environment(confirmed=confirmed, environ=environ)
    with tempfile.TemporaryDirectory(prefix="readiness-runtime-cancel-") as scratch:
        root = Path(scratch)
        settings = Settings(
            data_dir=root / "data", model_prompt_file="", model_protocol="generic",
            worker_url=WORKER_URL, worker_key_file=str(key_file), test_mode=True,
        )
        repository = TaskRepository(root / "tasks.sqlite")
        worker = WorkerClient(settings)
        model = ScriptedModel()
        runner = TaskRunner(settings, repository, worker=worker, model=model)
        task_started = False
        try:
            before = await worker.state()
            _assert_idle(before)
            if (await worker.health()).get("ready") is not True:
                raise AssertionError("disposable worker health is not ready")
            identity = (before.get("browser_instance_id"), before.get("page_id"),
                        before.get("recovery_generation"))

            # Verify the real repository/TaskRunner queued-cancel path has no dispatch.
            queued = repository.create(TaskSpec(
                sync_cookies=False, goal="Cancel before dispatch", allowed_origins=["https://example.com"],
                idempotency_key="queued-" + uuid.uuid4().hex,
                limits={"timeout_s": 60, "max_steps": 3},
            ))
            cancelled_queued = await runner.control(queued.task_id, "cancel", queued.version)
            if cancelled_queued.status != TaskStatus.CANCELLED:
                raise AssertionError("queued cancellation did not become CANCELLED")
            if any(event["event_type"] == "action.planned" for event in repository.events(queued.task_id)):
                raise AssertionError("queued cancellation dispatched an action")
            _assert_idle(await worker.state(), expected_identity=identity)

            record = runner.create(TaskSpec(
                sync_cookies=False, goal="Exercise one bounded in-flight wait, then stop before any next action.",
                allowed_origins=["https://example.com"],
                idempotency_key="inflight-" + uuid.uuid4().hex,
                limits={"timeout_s": 60, "max_steps": 3},
            ))
            task_started = True
            planned_deadline = time.monotonic() + INFLIGHT_TIMEOUT_SECONDS
            planned = []
            while time.monotonic() < planned_deadline:
                planned = [event for event in repository.events(record.task_id)
                           if event["event_type"] == "action.planned"]
                if planned:
                    break
                await asyncio.sleep(0.05)
            if len(planned) != 1 or planned[0]["payload"].get("kind") != "wait":
                raise AssertionError("the scripted first wait action was not planned")

            await _wait_for_worker_state(
                worker, lambda state: state.get("action_in_progress") is True
                and state.get("control_owner") == "agent", INFLIGHT_TIMEOUT_SECONDS, "execute_in_flight",
            )
            if repository.get(record.task_id).status != TaskStatus.RUNNING:
                raise AssertionError("task left RUNNING before in-flight cancellation")

            current = repository.get(record.task_id)
            cancelled = await runner.control(record.task_id, "cancel", current.version)
            if (cancelled.status != TaskStatus.RECOVERING or cancelled.phase != "cancel_outcome_unknown"
                    or not isinstance(cancelled.error, dict)
                    or cancelled.error.get("code") != "ACTION_OUTCOME_UNKNOWN"):
                raise AssertionError("in-flight cancel did not preserve the unknown outcome as RECOVERING")

            journal = repository.events(record.task_id)
            planned_after = [event for event in journal if event["event_type"] == "action.planned"]
            resolved = [event for event in journal
                        if event["event_type"] in {"action.applied", "action.failed"}]
            if len(planned_after) != 1 or resolved or model.calls != 1:
                raise AssertionError("cancellation allowed a receipt, second dispatch, or model continuation")
            status_events = [event for event in journal if event["event_type"] == "task.status_changed"]
            if not any(event["payload"].get("to") == "recovering"
                       and event["payload"].get("phase") == "cancel_outcome_unknown"
                       for event in status_events):
                raise AssertionError("event journal did not persist the unknown cancellation transition")

            await _wait_for_worker_state(
                worker, lambda state: state.get("browser_started") is True and state.get("control_owner") == "none"
                and state.get("action_in_progress") is False and state.get("recovery_state") == "idle",
                IDLE_TIMEOUT_SECONDS, "worker_idle_after_cancel",
            )
            after = await worker.state()
            _assert_idle(after, expected_identity=identity)
            if (await worker.health()).get("ready") is not True:
                raise AssertionError("worker health did not return to ready")
            final = repository.get(record.task_id)
            if final.status != TaskStatus.RECOVERING or final.error.get("code") != "ACTION_OUTCOME_UNKNOWN":
                raise AssertionError("task left persisted recovery review without an explicit operator action")
            return {
                "status": "PASS", "queued_cancel": "CANCELLED", "inflight_cancel": "RECOVERING",
                "inflight_action_observed": True, "action_kind": "wait",
                "action_planned_count": len(planned_after), "action_resolved_count": len(resolved),
                "scripted_model_calls": model.calls, "worker_ready_idle_after": True,
                "worker_context_count": after.get("context_count"), "worker_page_count": after.get("page_count"),
                "worker_identity_unchanged": True,
            }
        finally:
            await runner.close()
            if task_started:
                try:
                    current_state = await worker.state()
                    if (current_state.get("control_owner") != "none"
                            or current_state.get("action_in_progress") is True):
                        await worker.gate("none")
                        await _wait_for_worker_state(
                            worker, lambda state: state.get("browser_started") is True
                            and state.get("control_owner") == "none"
                            and state.get("action_in_progress") is False
                            and state.get("recovery_state") == "idle",
                            IDLE_TIMEOUT_SECONDS, "cleanup_worker_idle",
                        )
                except Exception:
                    # The command reports the primary failure; the disposable wait itself is <= 9s.
                    pass
            repository.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--i-confirm-disposable", action="store_true",
                        help="confirm this integration test may use only the disposable worker")
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(run_integration(confirmed=args.i_confirm_disposable))
    except Exception as exc:
        report = {"status": "FAIL", "error_type": type(exc).__name__}
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
