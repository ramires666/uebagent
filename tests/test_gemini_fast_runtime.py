from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
import time

import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient

from browser_mcp.app import create_app
from browser_mcp.config import Settings
from browser_mcp.domain import DomainError, TaskSpec, TaskStatus
from browser_mcp.runtime import GEMINI_PROMPT_APP_URL, TaskRunner
from browser_mcp.storage import TaskRepository


PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode("ascii")
GEMINI = "https://gemini.google.com"


class NoModelCalls:
    def __init__(self):
        self.calls: list[str] = []

    async def health(self):
        self.calls.append("health")
        raise AssertionError("fast workflow must not call model health")

    async def next_action(self, *_args, **_kwargs):
        self.calls.append("next_action")
        raise AssertionError("fast workflow must not request model actions")

    def begin_task(self, *_args):
        self.calls.append("begin_task")
        raise AssertionError("fast workflow must not initialize model state")

    def discard_task(self, *_args):
        self.calls.append("discard_task")
        raise AssertionError("fast workflow must not call model cleanup")


class FastWorker:
    def __init__(self, *, current_url: str = f"{GEMINI}/app", receipt: dict | None = None,
                 auth_status: dict | None = None, auth_sequence: list[dict] | None = None):
        self.current_url = current_url
        self.auth_status = auth_status or {"status": "signed_in", "reason_code": "ACCOUNT_CONTROLS_PRESENT"}
        self.auth_sequence = list(auth_sequence or [])
        self.receipt = receipt or {
            "outcome": "applied",
            "result": {
                "submitted": True,
                "submit_dispatched": True,
                "submit_method": "click",
                "enter_dispatched": False,
                "submission_acknowledged": True,
                "input_verified": True,
                "ack_kind": "composer_cleared",
                "characters": 29,
                "timings_ms": {"preflight": 1, "insert": 2, "verify": 3, "submit": 4, "ack": 5, "total": 15},
            },
        }
        self.control_owner = "none"
        self.control_epoch = 0
        self.gates: list[str] = []
        self.calls: list[tuple[str, str, dict | None]] = []
        self.executes: list[dict] = []
        self.observations = 0
        self.submit_error: DomainError | None = None

    async def health(self):
        return {"ready": True}

    async def state(self):
        return {"control_owner": self.control_owner, "context_count": 1, "page_count": 1,
                "action_in_progress": False, "import_in_progress": False, "resource_violation": False}

    async def gate(self, owner):
        self.gates.append(owner)
        self.control_owner = owner
        self.control_epoch += 1
        return {"control_epoch": self.control_epoch, "control_owner": owner}

    async def call(self, method, path, data=None, raw=False):
        self.calls.append((method, path, data))
        if path == "/capabilities/gemini-auth/status":
            if self.auth_sequence:
                self.auth_status = self.auth_sequence.pop(0)
            return self.auth_status
        if path == "/heartbeat":
            return {"ok": True}
        if path == "/observe":
            self.observations += 1
            return {"url": self.current_url, "control_epoch": self.control_epoch,
                    "observation_id": f"observation-{self.observations}",
                    "screenshot_png_b64": PNG_B64}
        if path == "/execute":
            self.executes.append(data)
            action = data["action"]
            if action["kind"] == "navigate":
                self.current_url = action["url"]
                return {"outcome": "applied", "result": {"navigated": True}}
            if action["kind"] == "gemini_refresh_session":
                return {"outcome": "applied", "result": {"origin": GEMINI, "session_refreshed": True}}
            if self.submit_error is not None:
                error, self.submit_error = self.submit_error, None
                raise error
            return self.receipt
        raise AssertionError(f"unexpected worker call: {method} {path}")


def fast_spec(**overrides):
    values = {
        "goal": "Submit the supplied prompt to Gemini.",
        "allowed_origins": [GEMINI],
        "idempotency_key": "gemini-fast-test-001",
        "execution_mode": "autonomous",
        "sync_cookies": False,
        "workflow": "gemini_prompt_submit",
        "site_capabilities": ["gemini_prompt_submit"],
        "verbatim_text": "A literal prompt with punctuation: ?!",
    }
    values.update(overrides)
    return TaskSpec(**values)


def make_runner(tmp_path, worker: FastWorker, spec=None):
    repo = TaskRepository(tmp_path / "tasks.sqlite")
    model = NoModelCalls()
    runner = TaskRunner(Settings(data_dir=tmp_path, worker_url="http://worker"), repo,
                        worker=worker, model=model)
    runner.gemini_auth_wait_seconds = 0
    record = repo.create(spec or fast_spec())
    return runner, repo, model, record


def test_unresolved_dispatch_scan_paginates_past_activity_events(tmp_path):
    worker = FastWorker()
    runner, repo, _model, record = make_runner(tmp_path, worker)
    try:
        for index in range(1_005):
            repo.add_event(record.task_id, "task.activity", {"stage": "observing", "step": index})
        repo.add_event(record.task_id, "action.planned", {
            "action_id": "late-unresolved-action", "kind": "click",
        })

        assert runner._unresolved_action_dispatch(record.task_id) is True
    finally:
        repo.close()


def test_blocked_teardown_failure_reason_is_typed_but_not_recoverable(tmp_path):
    runner, repo, _model, record = make_runner(tmp_path, FastWorker())
    try:
        health = {
            "recovery_needed": True,
            "recovery_state": "blocked",
            "recovery_reason": "context_close_failed",
            "recovery_generation": 0,
            "recovery_attempts": 1,
            "resource_violation": False,
            "egress_ready": True,
        }
        eligible, reason = runner._worker_recovery_metadata(health)
        assert eligible is False
        assert reason == "context_close_failed"
        context = {"worker_recovery_reason": reason}
        assert "context_close_failed" in runner._worker_not_ready_message(context)
    finally:
        repo.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("auth_status", "code"), [
    ({"status": "signed_out", "reason_code": "SIGN_IN_CONTROL_PRESENT"}, "AUTH_REQUIRED"),
    ({"status": "unknown", "reason_code": "PROOF_INCOMPLETE"}, "AUTH_STATE_UNKNOWN"),
])
async def test_fast_submit_waits_for_human_before_submit_when_auth_is_not_proven(tmp_path, auth_status, code):
    worker = FastWorker(auth_status=auth_status)
    runner, repo, model, record = make_runner(tmp_path, worker)
    try:
        await runner._run(record.task_id)
        state = repo.get(record.task_id)
        assert state.status == TaskStatus.WAITING_HUMAN
        assert state.error["code"] == code
        assert [cmd["action"]["kind"] for cmd in worker.executes] == []
        assert model.calls == []
        assert not any(event["event_type"] == "action.applied"
                       and event["payload"].get("kind") == "gemini_submit_prompt" for event in repo.events(record.task_id))
        assert any(path == "/capabilities/gemini-auth/status" for _, path, _ in worker.calls)
    finally:
        await runner.close()
        repo.close()


@pytest.mark.asyncio
async def test_task_cookie_sync_runs_before_model_or_browser_work_and_is_persisted(tmp_path):
    worker = FastWorker()
    repo = TaskRepository(tmp_path / "tasks.sqlite")
    order = []
    original_health = worker.health
    async def worker_health():
        order.append("worker_health")
        return await original_health()
    worker.health = worker_health
    class Model:
        async def health(self):
            order.append("model_health")
            return {"ready": True}
        def begin_task(self, *_args):
            return None
        async def next_action(self, *_args, **_kwargs):
            order.append("model_action")
            return {"kind": "finish", "summary": "read-only fixture"}
        def discard_task(self, *_args):
            return None
    model = Model()
    runner = TaskRunner(Settings(data_dir=tmp_path, worker_url="http://worker"), repo,
                        worker=worker, model=model)
    async def cookie_sync(task_id, profile_id, origins):
        order.append("cookie_sync")
        assert profile_id == "default" and origins == [GEMINI]
        return {"total": 0, "applied": 0, "failed": 0}
    runner.cookie_sync = cookie_sync
    record = repo.create(TaskSpec(goal="Inspect Gemini", allowed_origins=[GEMINI],
        sync_cookies=True, idempotency_key="cookie-sync-order-001"))
    try:
        await runner._run(record.task_id)
        assert order[:2] == ["worker_health", "cookie_sync"]
        receipt = [e for e in repo.events(record.task_id) if e["event_type"] == "cookie.sync_completed"]
        assert len(receipt) == 1
        assert receipt[0]["payload"]["job_id"] == f"taskcookie-{record.task_id}"
        assert receipt[0]["payload"]["counts"] == {"total": 0, "applied": 0, "failed": 0}
    finally:
        await runner.close()
        repo.close()


@pytest.mark.asyncio
async def test_fresh_navigation_after_cookie_sync_counts_as_session_refresh(tmp_path):
    worker = FastWorker(current_url="about:blank")
    repo = TaskRepository(tmp_path / "tasks.sqlite")
    class Model:
        def __init__(self):
            self.actions = [
                {"kind": "navigate", "url": f"{GEMINI}/app"},
                {"kind": "finish", "summary": "completed"},
            ]
        async def health(self):
            return {"ready": True}
        def begin_task(self, *_args):
            return None
        async def next_action(self, *_args, **_kwargs):
            return self.actions.pop(0)
        def discard_task(self, *_args):
            return None
    runner = TaskRunner(Settings(data_dir=tmp_path, worker_url="http://worker"), repo,
                        worker=worker, model=Model())
    async def cookie_sync(*_args):
        return {"total": 2, "applied": 2, "failed": 0}
    runner.cookie_sync = cookie_sync
    record = repo.create(TaskSpec(goal="Inspect Gemini", allowed_origins=[GEMINI],
        sync_cookies=True, idempotency_key="cookie-sync-fresh-navigation-001"))
    try:
        await runner._run(record.task_id)
        assert repo.get(record.task_id).status == TaskStatus.SUCCEEDED
        assert [cmd["action"]["kind"] for cmd in worker.executes] == ["navigate"]
        assert any(event["event_type"] == "cookie.session_refreshed"
                   for event in repo.events(record.task_id))
    finally:
        await runner.close()
        repo.close()


@pytest.mark.asyncio
async def test_browser_generation_change_resyncs_cookies_before_reacquiring_agent_lease(tmp_path):
    class ScopeWorker(FastWorker):
        def __init__(self):
            super().__init__(current_url="https://shop.example")
            self.recovery_generation = 0
            self.instance_id = "browser-instance-a"
            self.order = []
        async def health(self):
            return {
                "ready": True, "browser_instance_id": self.instance_id,
                "recovery_generation": self.recovery_generation,
            }
        async def state(self):
            return {
                "control_owner": self.control_owner, "context_count": 1, "page_count": 1,
                "browser_started": True, "action_in_progress": False, "import_in_progress": False,
                "resource_violation": False, "egress_ready": True,
            }
        async def gate(self, owner):
            self.order.append(f"gate:{owner}")
            return await super().gate(owner)
        async def call(self, method, path, data=None, raw=False):
            if path == "/observe" and self.observations == 1:
                self.control_owner = "none"
                self.control_epoch += 1
                self.recovery_generation += 1
                self.observations += 1
                return {"url": self.current_url, "width": 1440, "height": 900,
                        "control_epoch": self.control_epoch,
                        "observation_id": f"watchdog-{self.observations}",
                        "screenshot_png_b64": PNG_B64}
            result = await super().call(method, path, data, raw)
            if path == "/observe":
                result.setdefault("width", 1440)
                result.setdefault("height", 900)
            return result

    class Model:
        def __init__(self):
            self.actions = [{"kind": "click", "x": 10, "y": 10},
                            {"kind": "finish", "summary": "done"}]
        async def health(self):
            return {"ready": True}
        def begin_task(self, *_args):
            return None
        async def next_action(self, *_args, **_kwargs):
            return self.actions.pop(0)
        def discard_task(self, *_args):
            return None

    worker = ScopeWorker()
    repo = TaskRepository(tmp_path / "tasks.sqlite")
    runner = TaskRunner(Settings(data_dir=tmp_path, worker_url="http://worker"), repo,
                        worker=worker, model=Model())
    syncs = []
    async def cookie_sync(task_id, profile_id, origins, *, job_id=None,
                          browser_instance_id=None, recovery_generation=None):
        worker.order.append(f"sync:{recovery_generation}")
        syncs.append((job_id, browser_instance_id, recovery_generation))
        return {"total": 0, "applied": 0, "failed": 0}
    runner.cookie_sync = cookie_sync
    record = repo.create(TaskSpec(goal="Inspect shop", allowed_origins=["https://shop.example"],
        sync_cookies=True, idempotency_key="cookie-recovery-generation-001"))
    # A pre-scope receipt cannot establish which browser incarnation received
    # the session cookies, so the first typed scope must import once again.
    repo.add_event(record.task_id, "cookie.sync_completed", {
        "job_id": f"taskcookie-{record.task_id}",
        "counts": {"total": 0, "applied": 0, "failed": 0},
    })
    try:
        await runner._run(record.task_id)
        assert repo.get(record.task_id).status == TaskStatus.SUCCEEDED, (
            repo.get(record.task_id).error, worker.order, syncs, worker.executes,
            [(event["event_type"], (event.get("payload") or {}).get("kind"))
             for event in repo.events(record.task_id)],
        )
        assert len(syncs) == 2
        assert syncs[0][0].startswith(f"taskcookie-{record.task_id}-r0-")
        assert syncs[0][1:] == ("browser-instance-a", 0)
        assert syncs[1][0].startswith(f"taskcookie-{record.task_id}-r1-")
        assert syncs[1][1:] == ("browser-instance-a", 1)
        assert worker.order.index("sync:1") < worker.order.index("gate:agent", worker.order.index("gate:agent") + 1)
        assert [cmd["action"]["kind"] for cmd in worker.executes] == ["click"]
    finally:
        await runner.close()
        repo.close()


@pytest.mark.asyncio
async def test_fast_workflow_auto_recovers_dead_browser_without_model_calls(tmp_path):
    class RecoverableFastWorker(FastWorker):
        def __init__(self):
            super().__init__()
            self.needs_recovery = True
            self.recover_calls = 0
        async def health(self):
            return {
                "ready": not self.needs_recovery,
                "recovery_needed": self.needs_recovery,
                "recovery_state": "idle",
                "recovery_reason": "canonical_page_closed" if self.needs_recovery else "healthy",
                "recovery_generation": 0 if self.needs_recovery else 1,
                "recovery_attempts": 0,
                "recovery_retry_after_seconds": None,
                "resource_violation": False, "egress_ready": True,
            }
        async def state(self):
            return {
                "control_owner": self.control_owner,
                "context_count": 1, "page_count": 0 if self.needs_recovery else 1,
                "browser_started": not self.needs_recovery,
                "action_in_progress": False, "import_in_progress": False,
                "resource_violation": False, "egress_ready": True,
            }
        async def recover(self):
            self.recover_calls += 1
            self.needs_recovery = False
            return {
                "outcome": "recovered", "recovery_state": "idle",
                "recovery_reason": "canonical_page_closed", "recovery_generation": 1,
                "recovery_attempts": 1, "recovery_retry_after_seconds": None,
                "browser_started": True, "context_count": 1, "page_count": 1,
            }

    worker = RecoverableFastWorker()
    runner, repo, model, record = make_runner(tmp_path, worker, fast_spec(sync_cookies=False))
    try:
        await runner._run(record.task_id)
        assert repo.get(record.task_id).status == TaskStatus.SUCCEEDED
        assert worker.recover_calls == 1
        assert [cmd["action"]["kind"] for cmd in worker.executes] == ["gemini_submit_prompt"]
        assert model.calls == []
        kinds = [event["event_type"] for event in repo.events(record.task_id)]
        assert "browser.recovery_started" in kinds and "browser.recovery_completed" in kinds
    finally:
        await runner.close()
        repo.close()


@pytest.mark.asyncio
async def test_watchdog_recovery_before_observe_discards_stale_epoch_and_reobserves(tmp_path):
    class WatchdogRecoveredWorker(FastWorker):
        def __init__(self):
            super().__init__()
            self.did_watchdog_recover = False
        async def state(self):
            return {
                "control_owner": self.control_owner,
                "context_count": 1, "page_count": 1, "browser_started": True,
                "action_in_progress": False, "import_in_progress": False,
                "resource_violation": False, "egress_ready": True,
            }
        async def call(self, method, path, data=None, raw=False):
            if path == "/observe" and not self.did_watchdog_recover:
                self.did_watchdog_recover = True
                self.control_owner = "none"
                self.control_epoch += 1
                self.observations += 1
                return {"url": self.current_url, "control_epoch": self.control_epoch,
                        "observation_id": f"stale-observation-{self.observations}",
                        "screenshot_png_b64": PNG_B64}
            return await super().call(method, path, data, raw)

    worker = WatchdogRecoveredWorker()
    runner, repo, model, record = make_runner(tmp_path, worker, fast_spec(sync_cookies=False))
    try:
        await runner._run(record.task_id)
        assert repo.get(record.task_id).status == TaskStatus.SUCCEEDED
        assert worker.gates == ["agent", "agent", "none"]
        assert worker.observations == 2
        assert [cmd["action"]["kind"] for cmd in worker.executes] == ["gemini_submit_prompt"]
        assert model.calls == []
    finally:
        await runner.close()
        repo.close()


@pytest.mark.asyncio
async def test_fast_readonly_observe_failure_recovers_without_replaying_action(tmp_path):
    class ObserveFailureWorker(FastWorker):
        def __init__(self):
            super().__init__()
            self.health_calls = 0
            self.recover_calls = 0
            self.needs_recovery = False
            self.fail_first_observe = True
        async def health(self):
            self.health_calls += 1
            if self.health_calls == 2:
                self.needs_recovery = True
            return {
                "ready": not self.needs_recovery,
                "recovery_needed": self.needs_recovery,
                "recovery_state": "idle",
                "recovery_reason": "canonical_page_closed" if self.needs_recovery else "healthy",
                "recovery_generation": 0 if self.needs_recovery else 1,
                "recovery_attempts": 0, "recovery_retry_after_seconds": None,
                "resource_violation": False, "egress_ready": True,
            }
        async def state(self):
            return {
                "control_owner": self.control_owner,
                "context_count": 1, "page_count": 0 if self.needs_recovery else 1,
                "browser_started": not self.needs_recovery,
                "action_in_progress": False, "import_in_progress": False,
                "resource_violation": False, "egress_ready": True,
            }
        async def recover(self):
            self.recover_calls += 1
            self.needs_recovery = False
            self.control_owner = "none"
            return {
                "outcome": "recovered", "recovery_state": "idle",
                "recovery_reason": "canonical_page_closed", "recovery_generation": 1,
                "recovery_attempts": 1, "recovery_retry_after_seconds": None,
                "browser_started": True, "context_count": 1, "page_count": 1,
            }
        async def call(self, method, path, data=None, raw=False):
            if path == "/observe" and self.fail_first_observe:
                self.fail_first_observe = False
                raise DomainError("WORKER_UNAVAILABLE", "safe fixture")
            return await super().call(method, path, data, raw)

    worker = ObserveFailureWorker()
    runner, repo, model, record = make_runner(tmp_path, worker, fast_spec(sync_cookies=False))
    try:
        await runner._run(record.task_id)
        assert repo.get(record.task_id).status == TaskStatus.SUCCEEDED
        assert worker.recover_calls == 1
        assert [cmd["action"]["kind"] for cmd in worker.executes] == ["gemini_submit_prompt"]
        assert model.calls == []
        assert any(event["event_type"] == "browser.recovery_completed"
                   for event in repo.events(record.task_id))
    finally:
        await runner.close()
        repo.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fast_workflow", [False, True])
async def test_unready_worker_fails_before_task_cookie_sync(tmp_path, fast_workflow):
    worker = FastWorker()
    health_calls = []
    async def unready_health():
        health_calls.append(True)
        return {"ready": False, "detail": "safe fixture"}
    worker.health = unready_health
    if fast_workflow:
        spec = fast_spec(sync_cookies=True)
    else:
        spec = TaskSpec(goal="Inspect Gemini", allowed_origins=[GEMINI],
            sync_cookies=True, idempotency_key="cookie-sync-unready-001")
    runner, repo, model, record = make_runner(tmp_path, worker, spec)
    sync_calls = []
    async def cookie_sync(*_args):
        sync_calls.append(True)
        return {"total": 0, "applied": 0, "failed": 0}
    runner.cookie_sync = cookie_sync
    try:
        await runner._run(record.task_id)
        state = repo.get(record.task_id)
        assert state.status == TaskStatus.WAITING_HUMAN
        assert state.phase == "dependencies_not_ready"
        assert state.error["code"] == "DEPENDENCIES_NOT_READY"
        assert health_calls == [True]
        assert sync_calls == []
        assert not any(event["event_type"].startswith("cookie.sync_") for event in repo.events(record.task_id))
        assert worker.calls == [] and worker.gates == [] and worker.executes == []
        assert model.calls == []
    finally:
        await runner.close()
        repo.close()


@pytest.mark.asyncio
async def test_resume_rechecks_worker_before_retrying_task_cookie_sync(tmp_path):
    worker = FastWorker()
    health_results = iter([False, True, True])
    health_calls = []
    async def changing_health():
        health_calls.append(True)
        return {"ready": next(health_results)}
    worker.health = changing_health
    runner, repo, _model, record = make_runner(tmp_path, worker, fast_spec(sync_cookies=True))
    sync_calls = []
    async def cookie_sync(*_args):
        sync_calls.append(True)
        return {"total": 0, "applied": 0, "failed": 0}
    runner.cookie_sync = cookie_sync
    try:
        await runner._run(record.task_id)
        waiting = repo.get(record.task_id)
        assert waiting.status == TaskStatus.WAITING_HUMAN
        assert sync_calls == []

        await runner.control(record.task_id, "resume", waiting.version)
        await runner.jobs[record.task_id]
        assert repo.get(record.task_id).status == TaskStatus.SUCCEEDED
        assert health_calls == [True, True, True]
        assert sync_calls == [True]
        assert len([e for e in repo.events(record.task_id) if e["event_type"] == "cookie.sync_completed"]) == 1
    finally:
        await runner.close()
        repo.close()


@pytest.mark.asyncio
async def test_default_sync_refreshes_existing_gemini_page_before_auth_and_submit(tmp_path):
    worker = FastWorker(auth_sequence=[
        {"status": "unknown", "reason_code": "PROOF_INCOMPLETE"},
        {"status": "unknown", "reason_code": "PROOF_INCOMPLETE"},
        {"status": "signed_in", "reason_code": "ACCOUNT_CONTROLS_PRESENT"},
    ])
    runner, repo, model, record = make_runner(tmp_path, worker, fast_spec(sync_cookies=True))
    runner.gemini_auth_wait_seconds = 1
    sync_calls = []
    async def cookie_sync(task_id, profile_id, origins):
        sync_calls.append((task_id, profile_id, origins))
        return {"total": 28, "applied": 28, "failed": 0}
    runner.cookie_sync = cookie_sync
    try:
        await runner._run(record.task_id)
        state = repo.get(record.task_id)
        assert state.status == TaskStatus.SUCCEEDED
        assert sync_calls == [(record.task_id, "default", [GEMINI])]
        actions = [cmd["action"]["kind"] for cmd in worker.executes]
        assert actions == ["gemini_refresh_session", "gemini_submit_prompt"]
        events = repo.events(record.task_id)
        assert any(e["event_type"] == "cookie.session_refreshed" for e in events)
        assert model.calls == []
    finally:
        await runner.close()
        repo.close()


@pytest.mark.asyncio
async def test_draft_guard_stops_default_sync_before_auth_or_submit(tmp_path):
    worker = FastWorker()
    runner, repo, model, record = make_runner(tmp_path, worker, fast_spec(sync_cookies=True))
    async def cookie_sync(*_args):
        return {"total": 1, "applied": 1, "failed": 0}
    runner.cookie_sync = cookie_sync
    original_call = worker.call
    async def reject_refresh(method, path, data=None, raw=False):
        if path == "/execute" and data and data.get("action", {}).get("kind") == "gemini_refresh_session":
            worker.executes.append(data)
            raise DomainError("DRAFT_NOT_EMPTY", "preserved draft")
        return await original_call(method, path, data, raw)
    worker.call = reject_refresh
    try:
        await runner._run(record.task_id)
        state = repo.get(record.task_id)
        assert state.status == TaskStatus.WAITING_HUMAN
        assert state.error["code"] == "DRAFT_NOT_EMPTY"
        assert [cmd["action"]["kind"] for cmd in worker.executes] == ["gemini_refresh_session"]
        assert not any(path == "/capabilities/gemini-auth/status" for _, path, _ in worker.calls)
        assert model.calls == []
    finally:
        await runner.close()
        repo.close()


@pytest.mark.asyncio
async def test_default_cookie_sync_without_connected_source_waits_before_worker_calls(tmp_path):
    worker = FastWorker()
    runner, repo, model, record = make_runner(tmp_path, worker, fast_spec(sync_cookies=True))
    try:
        await runner._run(record.task_id)
        state = repo.get(record.task_id)
        assert state.status == TaskStatus.WAITING_HUMAN
        assert state.error["code"] == "COOKIE_SYNC_FAILED"
        assert worker.calls == [] and worker.gates == [] and worker.executes == []
        assert model.calls == []
    finally:
        await runner.close()
        repo.close()


@pytest.mark.asyncio
async def test_unknown_cookie_import_recovers_without_automatic_retry(tmp_path):
    worker = FastWorker()
    runner, repo, model, record = make_runner(tmp_path, worker, fast_spec(sync_cookies=True))
    attempts = []
    async def cookie_sync(*_args):
        attempts.append("attempt")
        raise DomainError("ACTION_OUTCOME_UNKNOWN", "unknown")
    runner.cookie_sync = cookie_sync
    try:
        await runner._run(record.task_id)
        assert repo.get(record.task_id).status == TaskStatus.RECOVERING
        await runner._run(record.task_id)
        assert attempts == ["attempt"]
        assert worker.calls == [] and worker.gates == [] and worker.executes == []
        assert model.calls == []
    finally:
        await runner.close()
        repo.close()


@pytest.mark.asyncio
async def test_completed_cookie_sync_receipt_prevents_repeat_copy_on_resume(tmp_path):
    worker = FastWorker()
    runner, repo, model, record = make_runner(tmp_path, worker, fast_spec(sync_cookies=True))
    called = []
    async def cookie_sync(*_args):
        called.append(True)
        return {"total": 1, "applied": 1, "failed": 0}
    runner.cookie_sync = cookie_sync
    repo.add_event(record.task_id, "cookie.sync_completed", {
        "job_id": f"taskcookie-{record.task_id}", "origins_count": 1,
        "counts": {"total": 1, "applied": 1, "failed": 0},
    })
    try:
        await runner._run(record.task_id)
        assert repo.get(record.task_id).status == TaskStatus.SUCCEEDED
        assert called == []
        assert len([e for e in repo.events(record.task_id) if e["event_type"] == "cookie.sync_completed"]) == 1
    finally:
        await runner.close()
        repo.close()


@pytest.mark.asyncio
async def test_cookie_sync_respects_task_deadline_before_and_after_copy(tmp_path):
    from datetime import UTC, datetime, timedelta

    worker = FastWorker()
    runner, repo, model, record = make_runner(tmp_path, worker,
        fast_spec(sync_cookies=True, limits={"timeout_s": 30, "max_steps": 80}))
    attempts = []
    async def never_called(*_args):
        attempts.append("before")
        return {"total": 0, "applied": 0, "failed": 0}
    runner.cookie_sync = never_called
    repo._conn.execute("UPDATE tasks SET created_at=? WHERE task_id=?",
        ((datetime.now(UTC) - timedelta(seconds=31)).isoformat(), record.task_id))
    try:
        await runner._run(record.task_id)
        assert repo.get(record.task_id).status == TaskStatus.EXPIRED
        assert attempts == []
    finally:
        await runner.close()
        repo.close()

    after_dir = tmp_path / "after"
    after_dir.mkdir()
    worker = FastWorker()
    runner, repo, model, record = make_runner(after_dir, worker,
        fast_spec(sync_cookies=True, limits={"timeout_s": 30, "max_steps": 80}))
    async def late_success(task_id, _profile_id, _origins):
        repo._conn.execute("UPDATE tasks SET created_at=? WHERE task_id=?",
            ((datetime.now(UTC) - timedelta(seconds=31)).isoformat(), task_id))
        return {"total": 0, "applied": 0, "failed": 0}
    runner.cookie_sync = late_success
    try:
        await runner._run(record.task_id)
        assert repo.get(record.task_id).status == TaskStatus.EXPIRED
        assert len([e for e in repo.events(record.task_id) if e["event_type"] == "cookie.sync_completed"]) == 1
        assert worker.calls == [] and worker.gates == []
    finally:
        await runner.close()
        repo.close()


@pytest.mark.asyncio
async def test_generic_gemini_task_checks_auth_before_first_model_action(tmp_path):
    class GenericModel:
        def __init__(self):
            self.action_calls = 0
        async def health(self):
            return {"ready": True}
        def begin_task(self, *_args):
            return None
        async def next_action(self, *_args, **_kwargs):
            self.action_calls += 1
            raise AssertionError("Gemini auth preflight must run before model action")
        def discard_task(self, *_args):
            return None

    worker = FastWorker(auth_status={"status": "signed_out", "reason_code": "SIGN_IN_CONTROL_PRESENT"})
    repo = TaskRepository(tmp_path / "tasks.sqlite")
    model = GenericModel()
    runner = TaskRunner(Settings(data_dir=tmp_path, worker_url="http://worker"), repo,
                        worker=worker, model=model)
    runner.gemini_auth_wait_seconds = 0
    record = repo.create(TaskSpec(goal="Inspect Gemini", allowed_origins=[GEMINI],
                                  sync_cookies=False, idempotency_key="generic-gemini-auth-001"))
    try:
        await runner._run(record.task_id)
        state = repo.get(record.task_id)
        assert state.status == TaskStatus.WAITING_HUMAN and state.error["code"] == "AUTH_REQUIRED"
        assert model.action_calls == 0
        assert worker.executes == []
        assert any(path == "/capabilities/gemini-auth/status" for _, path, _ in worker.calls)
    finally:
        await runner.close()
        repo.close()


def test_fast_workflow_schema_is_explicit_and_constrained():
    legacy = TaskSpec(goal="normal", allowed_origins=["https://example.test"], idempotency_key="legacy-agent-01")
    assert legacy.workflow == "agent"

    with pytest.raises(ValidationError, match="autonomous"):
        fast_spec(execution_mode="supervised")
    with pytest.raises(ValidationError, match="verbatim_text"):
        fast_spec(verbatim_text="  \n")
    with pytest.raises(ValidationError, match="exact Gemini origin"):
        fast_spec(allowed_origins=[GEMINI, "https://example.test"])
    with pytest.raises(ValidationError, match="capability"):
        fast_spec(site_capabilities=[])
    with pytest.raises(ValidationError, match="success conditions"):
        fast_spec(success_conditions=[{"kind": "text_present", "text": "fixture"}])
    with pytest.raises(ValidationError):
        fast_spec(workflow="unknown")


def test_mcp_start_exposes_explicit_workflow_and_capability_fields(tmp_path):
    settings = Settings(data_dir=tmp_path, ui_dir=tmp_path / "no-ui", test_mode=True,
                        operator_token="fixture-operator", mcp_token="fixture-mcp")
    headers = {"Accept": "application/json, text/event-stream", "Authorization": "Bearer fixture-mcp"}
    worker = FastWorker()
    model = NoModelCalls()
    with TestClient(create_app(settings, worker=worker, model=model)) as client:
        response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                               headers=headers)
        assert response.status_code == 200
        start = next(tool for tool in response.json()["result"]["tools"]
                     if tool["name"] == "browser_task_start")
        properties = start["inputSchema"]["properties"]
        assert "workflow" in properties
        assert "site_capabilities" in properties
        assert properties["sync_cookies"]["default"] is True
        source = "MCP literal prompt: exact?"
        called = client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "browser_task_start", "arguments": {
                "goal": "Submit the immutable prompt to Gemini.",
                "allowed_origins": [GEMINI],
                "idempotency_key": "mcp-gemini-fast-001",
                "execution_mode": "autonomous",
                    "sync_cookies": False,
                "workflow": "gemini_prompt_submit",
                "site_capabilities": ["gemini_prompt_submit"],
                "verbatim_text": source,
            }},
        }, headers=headers)
        assert called.status_code == 200
        assert not called.json()["result"].get("isError", False)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            records = client.app.state.repository.list()
            if worker.executes and records and records[0].status == TaskStatus.SUCCEEDED:
                break
            time.sleep(0.01)
        assert len(worker.executes) == 1
        assert worker.executes[0]["action"] == {"kind": "gemini_submit_prompt", "text": source}
        task = client.app.state.repository.list()[0]
        assert task.spec.workflow == "gemini_prompt_submit"
        assert task.spec.site_capabilities == ["gemini_prompt_submit"]
        assert task.phase == "submitted"
        assert model.calls == []


@pytest.mark.asyncio
async def test_fast_submit_skips_model_and_sends_one_exact_worker_action(tmp_path):
    source = "A literal prompt with punctuation: ?!"
    runner, repo, model, record = make_runner(tmp_path, FastWorker(), fast_spec(verbatim_text=source))
    try:
        await runner._run(record.task_id)
        result = repo.get(record.task_id)
        assert result.status == TaskStatus.SUCCEEDED
        assert result.phase == "submitted"
        assert result.result == {
            "submitted": True,
            "submission_acknowledged": True,
            "submit_dispatched": True,
            "submit_method": "click",
            "enter_dispatched": False,
            "input_verified": True,
            "response_verified": False,
            "model_calls": 0,
            "ack_kind": "composer_cleared",
            "timings_ms": {"preflight": 1, "insert": 2, "verify": 3, "submit": 4, "ack": 5, "total": 15},
            "elapsed_s": result.result["elapsed_s"],
        }
        assert isinstance(result.result["elapsed_s"], float)
        assert model.calls == []
        assert len([call for call in runner.worker.calls if call[1] == "/observe"]) == 1
        assert len(runner.worker.executes) == 1
        command = runner.worker.executes[0]
        assert command["action"] == {"kind": "gemini_submit_prompt", "text": source}
        assert command["allowed_origins"] == [GEMINI]
        assert command["allow_write"] is True
        assert command["site_capability"] == repo.capability(record.task_id, "gemini_prompt_submit")
        assert "screenshot_png_b64" not in command
        events = repo.events(record.task_id)
        assert all(source not in str(event) for event in events)
        assert [event["payload"]["kind"] for event in events if event["event_type"] == "action.planned"] == ["gemini_submit_prompt"]
    finally:
        await runner.close()
        repo.close()


@pytest.mark.asyncio
async def test_fast_submit_navigates_only_to_canonical_app_then_observes_again(tmp_path):
    worker = FastWorker(current_url="https://example.test/private")
    runner, repo, model, record = make_runner(tmp_path, worker)
    try:
        await runner._run(record.task_id)
        assert repo.get(record.task_id).phase == "submitted"
        assert [command["action"]["kind"] for command in worker.executes] == ["navigate", "gemini_submit_prompt"]
        assert worker.executes[0]["action"]["url"] == GEMINI_PROMPT_APP_URL
        assert worker.executes[1]["action"]["text"] == record.spec.verbatim_text
        assert worker.observations == 2
        assert model.calls == []
    finally:
        await runner.close()
        repo.close()


@pytest.mark.asyncio
async def test_unacknowledged_send_enters_recovery_and_never_retries(tmp_path):
    worker = FastWorker(receipt={"outcome": "unknown", "error": "ACTION_OUTCOME_UNKNOWN"})
    runner, repo, model, record = make_runner(tmp_path, worker)
    try:
        await runner._run(record.task_id)
        state = repo.get(record.task_id)
        assert state.status == TaskStatus.RECOVERING
        assert state.error["code"] == "ACTION_OUTCOME_UNKNOWN"
        assert len(worker.executes) == 1
        assert model.calls == []
        await runner._run(record.task_id)
        assert repo.get(record.task_id).status == TaskStatus.RECOVERING
        assert len(worker.executes) == 1
        assert any(event["event_type"] == "action.planned" for event in repo.events(record.task_id))
        assert not any(event["event_type"] == "action.applied" for event in repo.events(record.task_id))
    finally:
        await runner.close()
        repo.close()


@pytest.mark.asyncio
async def test_fast_workflow_rejects_missing_persisted_capability_before_browser_action(tmp_path):
    runner, repo, model, record = make_runner(tmp_path, FastWorker())
    try:
        repo._conn.execute("DELETE FROM task_capabilities WHERE task_id=?", (record.task_id,))
        await runner._run(record.task_id)
        state = repo.get(record.task_id)
        assert state.status == TaskStatus.WAITING_HUMAN
        assert state.error["code"] == "CAPABILITY_UNAVAILABLE"
        assert runner.worker.executes == []
        assert model.calls == []
    finally:
        await runner.close()
        repo.close()


@pytest.mark.asyncio
async def test_fast_workflow_finishes_from_durable_ack_without_resending(tmp_path):
    runner, repo, model, record = make_runner(tmp_path, FastWorker())
    runner.worker.control_owner = "human"
    try:
        repo.transition(record.task_id, TaskStatus.RUNNING)
        action_id = "action-existing-001"
        repo.add_event(record.task_id, "action.planned", {"action_id": action_id, "kind": "gemini_submit_prompt"})
        repo.add_event(record.task_id, "action.applied", {
            "action_id": action_id, "kind": "gemini_submit_prompt", "submission_acknowledged": True,
            "enter_dispatched": True, "input_verified": True, "ack_kind": "composer_cleared",
        })
        await runner._run(record.task_id)
        result = repo.get(record.task_id)
        assert result.status == TaskStatus.SUCCEEDED and result.phase == "submitted"
        assert result.result["recovered_from_receipt"] is True
        assert result.result["enter_dispatched"] is True
        assert runner.worker.executes == []
        assert runner.worker.gates == []
        assert runner.worker.control_owner == "human"
        assert model.calls == []
    finally:
        await runner.close()
        repo.close()


@pytest.mark.asyncio
async def test_definitive_pre_enter_failure_can_resume_without_ambiguity(tmp_path):
    worker = FastWorker()
    worker.submit_error = DomainError("DRAFT_NOT_EMPTY", "pre-enter guard")
    runner, repo, model, record = make_runner(tmp_path, worker)
    try:
        await runner._run(record.task_id)
        failed = repo.get(record.task_id)
        assert failed.status == TaskStatus.WAITING_HUMAN
        assert failed.error["code"] == "DRAFT_NOT_EMPTY"
        failure_events = [event for event in repo.events(record.task_id) if event["event_type"] == "action.failed"]
        assert failure_events[0]["payload"]["submission_not_dispatched"] is True
        repo.transition(record.task_id, TaskStatus.RUNNING)
        await runner._run(record.task_id)
        result = repo.get(record.task_id)
        assert result.status == TaskStatus.SUCCEEDED and result.phase == "submitted"
        assert len(worker.executes) == 2
        assert [command["action"]["text"] for command in worker.executes] == [
            record.spec.verbatim_text, record.spec.verbatim_text,
        ]
        assert model.calls == []
    finally:
        await runner.close()
        repo.close()


@pytest.mark.asyncio
async def test_late_ack_is_persisted_then_expires_without_retry(tmp_path):
    repo = TaskRepository(tmp_path / "tasks.sqlite")

    class LateAckWorker(FastWorker):
        async def call(self, method, path, data=None, raw=False):
            receipt = await super().call(method, path, data, raw)
            if path == "/execute" and data["action"]["kind"] == "gemini_submit_prompt":
                old = (datetime.now(UTC) - timedelta(seconds=31)).isoformat()
                repo._conn.execute("UPDATE tasks SET created_at=? WHERE task_id=?", (old, record.task_id))
            return receipt

    model = NoModelCalls()
    worker = LateAckWorker()
    runner = TaskRunner(Settings(data_dir=tmp_path, worker_url="http://worker"), repo, worker=worker, model=model)
    runner.gemini_auth_wait_seconds = 0
    record = repo.create(fast_spec(limits={"timeout_s": 30, "max_steps": 80}))
    try:
        await runner._run(record.task_id)
        state = repo.get(record.task_id)
        assert state.status == TaskStatus.EXPIRED
        assert state.error["code"] == "TASK_EXPIRED"
        assert len(worker.executes) == 1
        assert any(event["event_type"] == "action.applied"
                   and event["payload"].get("submission_acknowledged") is True
                   for event in repo.events(record.task_id))
        assert worker.control_owner == "none"
        await runner._run(record.task_id)
        assert len(worker.executes) == 1
        assert model.calls == []
    finally:
        await runner.close()
        repo.close()


@pytest.mark.asyncio
async def test_fast_workflow_does_not_release_human_owned_browser(tmp_path):
    worker = FastWorker()
    worker.control_owner = "human"
    runner, repo, model, record = make_runner(tmp_path, worker)
    try:
        await runner._run(record.task_id)
        state = repo.get(record.task_id)
        assert state.status == TaskStatus.WAITING_HUMAN
        assert state.error["code"] == "CONTROL_OWNED_BY_HUMAN"
        assert worker.gates == []
        assert worker.observations == 0
        assert worker.executes == []
        assert model.calls == []
    finally:
        await runner.close()
        repo.close()
