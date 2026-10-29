from __future__ import annotations

import base64

import httpx
import pytest

from browser_mcp.config import Settings
from browser_mcp.domain import DomainError, TaskSpec, TaskStatus
from browser_mcp.runtime import TaskRunner, WorkerClient
from browser_mcp.storage import TaskRepository
from worker.app import BrowserRuntime


PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode("ascii")


class RecoveryModel:
    def __init__(self, actions):
        self.actions = list(actions)
        self.calls = 0

    async def health(self):
        return {"ready": True}

    async def next_action(self, *_args):
        self.calls += 1
        action = self.actions.pop(0)
        if isinstance(action, Exception):
            raise action
        return action


class RecoveryWorker:
    def __init__(self, *, needs_recovery=False, owner="none", import_busy=False):
        self.needs_recovery = needs_recovery
        self.owner = owner
        self.import_busy = import_busy
        self.order = []
        self.gates = []
        self.executes = []
        self.observations = 0
        self.control_epoch = 0
        self.current_url = "about:blank"

    async def health(self):
        return {
            "ready": not self.needs_recovery,
            "recovery_needed": self.needs_recovery,
            "recovery_state": "idle" if self.needs_recovery else "idle",
            "recovery_reason": "canonical_page_closed" if self.needs_recovery else "healthy",
            "recovery_generation": 1 if not self.needs_recovery else 0,
            "recovery_attempts": 0,
            "recovery_retry_after_seconds": None,
            "resource_violation": False, "egress_ready": True,
            "browser_started": not self.needs_recovery,
            "context_count": 0 if self.needs_recovery else 1,
            "page_count": 0 if self.needs_recovery else 1,
        }

    async def recover(self):
        self.order.append("recover")
        if self.owner != "none" or self.import_busy:
            return {
                "outcome": "deferred", "recovery_state": "blocked",
                "recovery_reason": "control_busy", "recovery_needed": True,
                "recovery_generation": 0, "recovery_attempts": 0,
                "recovery_retry_after_seconds": None, "browser_started": False,
                "context_count": 1, "page_count": 1,
            }
        self.needs_recovery = False
        return {
            "outcome": "recovered", "recovery_state": "idle",
            "recovery_reason": "canonical_page_closed", "recovery_needed": False,
            "recovery_generation": 1, "recovery_attempts": 1,
            "recovery_retry_after_seconds": None, "browser_started": True,
            "context_count": 1, "page_count": 1,
        }

    async def state(self):
        return {
            "control_owner": self.owner, "action_in_progress": False,
            "import_in_progress": self.import_busy, "context_count": 1,
            "page_count": 1, "resource_violation": False, "egress_ready": True,
            "browser_started": not self.needs_recovery,
            "recovery_needed": self.needs_recovery,
            "recovery_state": "idle",
            "recovery_reason": "canonical_page_closed" if self.needs_recovery else "healthy",
            "recovery_generation": 1 if not self.needs_recovery else 0,
            "recovery_attempts": 0, "recovery_retry_after_seconds": None,
        }

    async def gate(self, owner):
        self.order.append(f"gate:{owner}")
        self.gates.append(owner)
        self.owner = owner
        self.control_epoch += 1
        return {"control_epoch": self.control_epoch, "control_owner": owner}

    async def call(self, method, path, data=None, raw=False):
        if path == "/heartbeat":
            return {"ok": True}
        if path == "/observe":
            self.observations += 1
            return {
                "url": self.current_url, "width": 1440, "height": 900,
                "screenshot_png_b64": PNG_B64, "control_epoch": self.control_epoch,
                "observation_id": f"observation-{self.observations}",
            }
        if path == "/execute":
            self.order.append("execute")
            self.executes.append(data)
            return {"outcome": "applied"}
        raise AssertionError(f"unexpected worker call {method} {path}")


def make_runner(tmp_path, worker, actions):
    repository = TaskRepository(tmp_path / "tasks.db")
    model = RecoveryModel(actions)
    runner = TaskRunner(Settings(data_dir=tmp_path), repository, worker=worker, model=model)
    return runner, repository, model


@pytest.mark.asyncio
async def test_worker_client_recover_uses_authenticated_typed_endpoint(tmp_path):
    key_file = tmp_path / "worker-key"
    key_file.write_text("worker-secret", encoding="utf-8")
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["authorization"] = request.headers.get("Authorization")
        seen["body"] = request.read().decode("utf-8")
        return httpx.Response(200, json={
            "outcome": "recovered", "recovery_state": "idle", "recovery_reason": "canonical_page_closed",
            "recovery_needed": False, "recovery_generation": 2, "recovery_attempts": 1,
            "recovery_retry_after_seconds": None, "browser_started": True,
            "context_count": 1, "page_count": 1,
        })

    client = WorkerClient(
        Settings(worker_url="http://worker", worker_key_file=str(key_file)),
        httpx.MockTransport(handler),
    )
    result = await client.recover()
    assert seen == {
        "method": "POST", "path": "/recover", "authorization": "Bearer worker-secret", "body": "{}",
    }
    assert result["outcome"] == "recovered"
    assert result["recovery_generation"] == 2
    assert result["context_count"] == result["page_count"] == 1


@pytest.mark.asyncio
async def test_runner_recovers_before_first_agent_gate_or_action(tmp_path):
    worker = RecoveryWorker(needs_recovery=True)
    runner, repository, model = make_runner(tmp_path, worker, [
        {"kind": "click", "x": 10, "y": 10}, {"kind": "finish", "summary": "done"},
    ])
    record = repository.create(TaskSpec(
        sync_cookies=False, goal="fixture", allowed_origins=["https://shop.example"],
        idempotency_key="safe-recovery-001",
    ))

    await runner._run(record.task_id)

    assert worker.order.index("recover") < worker.order.index("gate:agent") < worker.order.index("execute")
    assert len(worker.executes) == 1
    assert model.calls == 2
    assert repository.get(record.task_id).status == TaskStatus.SUCCEEDED
    await runner.close()
    repository.close()


@pytest.mark.asyncio
async def test_unresolved_generic_dispatch_resume_does_not_model_gate_or_replay(tmp_path):
    worker = RecoveryWorker(needs_recovery=True)
    runner, repository, model = make_runner(tmp_path, worker, [
        {"kind": "click", "x": 10, "y": 10},
    ])
    record = repository.create(TaskSpec(
        sync_cookies=False, goal="fixture", allowed_origins=["https://shop.example"],
        idempotency_key="unknown-dispatch-001",
    ))
    running = repository.transition(record.task_id, TaskStatus.RUNNING)
    repository.add_event(record.task_id, "action.planned", {
        "action_id": "already-dispatched-action", "kind": "click",
    })

    await runner._run(record.task_id)

    final = repository.get(record.task_id)
    assert final.status == TaskStatus.RECOVERING
    assert final.error["code"] == "ACTION_OUTCOME_UNKNOWN"
    assert model.calls == 0
    assert worker.gates == []
    assert worker.executes == []
    assert "recover" not in worker.order
    assert repository.get(record.task_id).version > running.version
    await runner.close()
    repository.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_state", ["human_owner", "cookie_import"])
async def test_recovery_never_takes_over_human_or_active_import(tmp_path, blocked_state):
    worker = RecoveryWorker(
        needs_recovery=True,
        owner="human" if blocked_state == "human_owner" else "none",
        import_busy=blocked_state == "cookie_import",
    )
    runner, repository, model = make_runner(tmp_path, worker, [
        {"kind": "click", "x": 10, "y": 10},
    ])
    record = repository.create(TaskSpec(
        sync_cookies=False, goal="fixture", allowed_origins=["https://shop.example"],
        idempotency_key=f"blocked-recovery-{blocked_state}",
    ))

    await runner._run(record.task_id)

    assert worker.gates == []
    assert worker.executes == []
    assert model.calls == 0
    assert "recover" not in worker.order
    assert repository.get(record.task_id).status != TaskStatus.SUCCEEDED
    await runner.close()
    repository.close()


@pytest.mark.asyncio
async def test_started_worker_ledger_receipt_survives_runtime_recreation(tmp_path):
    side_effects = []
    first = BrowserRuntime(tmp_path, backend="chromium", test_mode=True)
    first._open_ledger()
    fingerprint = "same-command-fingerprint"
    assert first._ledger_claim("dispatched-once", fingerprint) is None
    side_effects.append("physical click")
    first._db.close()  # Simulate a crash after dispatch and before receipt commit.
    first._db = None

    restarted = BrowserRuntime(tmp_path, backend="chromium", test_mode=True)
    restarted._open_ledger()
    receipt = restarted._ledger_lookup("dispatched-once", fingerprint)
    replay_receipt = restarted._ledger_claim("dispatched-once", fingerprint)

    assert receipt == replay_receipt == {
        "outcome": "unknown", "action_id": "dispatched-once", "error": "INTERRUPTED_AFTER_START",
    }
    assert side_effects == ["physical click"]
    restarted._db.close()
    restarted._db = None
