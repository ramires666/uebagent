"""Guarded end-to-end check for false success after a covered target click."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from browser_mcp.config import Settings
from browser_mcp.domain import TaskSpec, TaskStatus
from browser_mcp.runtime import TaskRunner, WorkerClient
from browser_mcp.storage import TaskRepository


WORKER_URL = "http://browser:7070"
FIXTURE_URL = "https://readiness-fixture.test/overlay"
CONDITIONS = [
    {"kind": "element_text_contains", "selector": "#target-count", "text": "1"},
    {"kind": "element_text_contains", "selector": "#overlay-count", "text": "1"},
]


class ScriptedModel:
    """Small deterministic action source; it never contacts a model service."""

    def __init__(self) -> None:
        self.clicked = False

    async def health(self) -> dict[str, bool]:
        return {"ready": True}

    async def next_action(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        if not self.clicked:
            self.clicked = True
            return {"kind": "click", "x": 423.611, "y": 368.889}
        return {"kind": "finish", "summary": "Clicked the requested control."}


class IntegrationFailure(RuntimeError):
    def __init__(self, details: dict[str, Any]):
        self.details = details
        super().__init__("overlay integration contract failed")


async def run_check(data_dir: Path, key_file: Path, worker_url: str = WORKER_URL) -> dict[str, Any]:
    """Use production TaskRunner/WorkerClient against one idle disposable worker."""
    if worker_url != WORKER_URL:
        raise RuntimeError("overlay integration accepts only the fixed disposable worker URL")
    if not key_file.is_file() or not key_file.read_text(encoding="utf-8").strip():
        raise RuntimeError("disposable worker key file is unavailable")

    settings = Settings(data_dir=data_dir, worker_url=worker_url,
                        worker_key_file=str(key_file), model_prompt_file="")
    repository = TaskRepository(data_dir / "tasks.sqlite3")
    worker = WorkerClient(settings)
    runner = TaskRunner(settings, repository, worker=worker, model=ScriptedModel())
    stage = "worker_preflight"
    record = None
    try:
        status = await worker.state()
        if not (status.get("control_owner") == "none" and status.get("browser_started") is True
                and status.get("context_count") == 1 and status.get("page_count") == 1
                and status.get("recovery_state") == "idle" and status.get("action_in_progress") is not True):
            raise RuntimeError("worker must be idle with one browser context and page")
        stage = "fixture_install"
        installed = await worker.call("POST", "/diagnostics/readiness-fixture", {"scenario": "overlay"})
        if installed != {"installed": True, "scenario": "overlay", "path": "/overlay"}:
            raise RuntimeError("overlay fixture installation was not confirmed")
        stage = "fixture_navigation"
        gate = await worker.gate("human")
        try:
            navigation = await worker.call("POST", "/manual/execute", {
                "control_epoch": gate["control_epoch"],
                "allowed_origins": ["https://readiness-fixture.test"],
                "action": {"kind": "navigate", "url": FIXTURE_URL},
            })
            if navigation.get("outcome") != "applied":
                raise RuntimeError("fixture navigation was not confirmed")
        finally:
            await worker.gate("none")

        reset = await worker.call("POST", "/diagnostics/readiness-actions/reset", {})
        if reset.get("reset") is not True:
            raise RuntimeError("action trace reset was not confirmed")
        stage = "baseline_verification"
        baseline = await worker.call("POST", "/verify", {"conditions": CONDITIONS})
        if (baseline.get("verified") is not False or len(baseline.get("checks", [])) != 2
                or baseline["checks"][0].get("passed") is not False
                or baseline["checks"][1].get("passed") is not False):
            raise RuntimeError("overlay fixture did not start with both effects at zero")

        stage = "task_run"
        record = runner.create(TaskSpec(
            sync_cookies=False,
            goal="Click the centered target button on the overlay fixture, then finish.",
            allowed_origins=["https://readiness-fixture.test"],
            success_conditions=[CONDITIONS[0]],
            limits={"timeout_s": 30, "max_steps": 3},
            idempotency_key="overlay-" + uuid.uuid4().hex,
        ))
        await asyncio.wait_for(asyncio.shield(runner.jobs[record.task_id]), timeout=45)
        final = repository.get(record.task_id)
        if final.status != TaskStatus.WAITING_HUMAN or final.status == TaskStatus.SUCCEEDED:
            raise RuntimeError("TaskRunner did not stop honestly after failed target verification")
        if final.phase != "verification_required":
            raise RuntimeError("TaskRunner did not stop in verification_required")
        if final.step_count != 2:
            raise RuntimeError("TaskRunner did not count one dispatched action plus one repair")
        if not final.result or final.result.get("verified") is not False:
            raise RuntimeError("TaskRunner result did not record failed verification")

        stage = "independent_verification"
        verification = await worker.call("POST", "/verify", {"conditions": CONDITIONS})
        checks = verification.get("checks", [])
        if (verification.get("verified") is not False or len(checks) != 2
                or checks[0].get("passed") is not False or checks[1].get("passed") is not True):
            raise RuntimeError("independent verification did not show target=0 and overlay=1")

        stage = "receipt_check"
        events = repository.events(record.task_id)
        applied = [event for event in events if event["event_type"] == "action.applied"]
        planned = [event for event in events if event["event_type"] == "action.planned"]
        repairs = [event for event in events if event["event_type"] == "verification.repair_requested"]
        trace = await worker.call("GET", "/diagnostics/readiness-actions")
        clicks = [item for item in trace.get("records", []) if item.get("kind") == "click"]
        if (len(planned) != 1 or planned[0]["payload"].get("kind") != "click"
                or len(applied) != 1 or applied[0]["payload"].get("kind") != "click"
                or len(repairs) != 1
                or len(clicks) != 1 or clicks[0].get("outcome") != "applied"):
            raise RuntimeError("expected one repair and exactly one worker-confirmed click")

        stage = "worker_idle_check"
        idle = await worker.state()
        if not (idle.get("control_owner") == "none" and idle.get("context_count") == 1
                and idle.get("page_count") == 1 and idle.get("action_in_progress") is not True):
            raise RuntimeError("worker was not idle after the integration check")
        return {
            "status": "PASS",
            "task_status": str(final.status),
            "task_phase": final.phase,
            "task_step_count": final.step_count,
            "target_effect_verified": checks[0]["passed"],
            "overlay_effect_verified": checks[1]["passed"],
            "worker_click_receipts": len(applied),
            "verification_repairs": len(repairs),
            "worker_click_outcomes": [item.get("outcome") for item in clicks],
            "context_count": idle.get("context_count"),
            "page_count": idle.get("page_count"),
        }
    except Exception as exc:
        task = repository.get(record.task_id) if record is not None else None
        events = repository.events(record.task_id) if record is not None else []
        receipt_count = sum(event.get("event_type") == "action.applied" for event in events)
        repair_count = sum(event.get("event_type") == "verification.repair_requested" for event in events)
        error_code = ("WORKER_ERROR" if hasattr(exc, "code") else
                      "TIMEOUT" if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) else
                      "ASSERTION_FAILED" if isinstance(exc, RuntimeError) else "UNEXPECTED_ERROR")
        raise IntegrationFailure({
            "status": "FAIL",
            "failure_stage": stage,
            "failure_code": error_code,
            "task_status": str(task.status) if task else None,
            "task_phase": task.phase if task else None,
            "task_step_count": task.step_count if task else None,
            "worker_action_receipts": receipt_count,
            "verification_repairs": repair_count,
        }) from None
    finally:
        await runner.close()
        repository.close()


def main() -> int:
    if (os.environ.get("APP_ENV", "").lower() != "test"
            or os.environ.get("READINESS_DISPOSABLE") != "1"
            or os.environ.get("READINESS_RUNTIME_INTEGRATION") != "1"
            or os.environ.get("READINESS_ACTION_TRACE") != "1"
            or os.environ.get("BROWSER_WORKER_URL") != WORKER_URL
            or not os.environ.get("WORKER_API_KEY_FILE")):
        print(json.dumps({"status": "BLOCKED", "reason": "explicit disposable integration guards are required"}))
        return 2
    try:
        with tempfile.TemporaryDirectory(prefix="readiness-overlay-") as temporary:
            result = asyncio.run(run_check(Path(temporary), Path(os.environ["WORKER_API_KEY_FILE"])))
    except IntegrationFailure as exc:
        result = exc.details
        print(json.dumps(result, separators=(",", ":")), flush=True)
        return 1
    except Exception:
        result = {"status": "FAIL", "failure_stage": "startup", "failure_code": "UNEXPECTED_ERROR",
                  "task_status": None, "task_phase": None, "task_step_count": None,
                  "worker_action_receipts": 0, "verification_repairs": 0}
        print(json.dumps(result, separators=(",", ":")), flush=True)
        return 1
    print(json.dumps(result, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
