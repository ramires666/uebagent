import asyncio

import pytest

from worker.app import AgentExecuteBody, BrowserRuntime, ManualExecuteBody, WorkerError
from worker.memory_maintenance import ACTION_THRESHOLD, MIN_ATTEMPT_INTERVAL_SECONDS, MemoryMaintenance


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


class FakeContext:
    def __init__(self, page):
        self.pages = [page]


class FakePage:
    def __init__(self, *, gc=None):
        self.gc = gc
        self.gc_calls = 0

    async def request_gc(self):
        self.gc_calls += 1
        if self.gc:
            await self.gc()

    async def evaluate(self, _script):
        return {"width": 1440, "height": 900}


class FakeAdapter:
    def __init__(self, page, order=None):
        self.page = page
        self.order = order if order is not None else []

    async def screenshot(self, **_kwargs):
        self.order.append("screenshot")
        return b"png"

    async def url(self):
        return "https://readiness-fixture.test/tree"


def make_runtime(tmp_path, *, gc=None):
    order = []
    page = FakePage(gc=gc)
    context = FakeContext(page)
    runtime = BrowserRuntime(tmp_path, backend="camoufox", adapter=FakeAdapter(page, order),
                            page=page, context=context)
    runtime._memory_maintenance = MemoryMaintenance(enabled=True, backend="camoufox")
    for _ in range(ACTION_THRESHOLD):
        runtime._memory_maintenance.record_completed_action()
    return runtime, page, order


def test_default_off_and_non_camoufox_backend_never_enable(monkeypatch):
    monkeypatch.delenv("BROWSER_MEMORY_MAINTENANCE", raising=False)
    assert MemoryMaintenance(backend="camoufox").status()["enabled"] is False
    monkeypatch.setenv("BROWSER_MEMORY_MAINTENANCE", "true")
    assert MemoryMaintenance(backend="camoufox").status()["enabled"] is False
    monkeypatch.setenv("BROWSER_MEMORY_MAINTENANCE", "1")
    assert MemoryMaintenance(backend="chromium").status()["enabled"] is False
    enabled = MemoryMaintenance(backend="camoufox").status()
    assert enabled["enabled"] is True
    assert "BROWSER_MEMORY_MAINTENANCE" not in str(enabled)


@pytest.mark.asyncio
async def test_counts_only_explicit_success_hook_and_threshold_min_interval():
    clock = FakeClock()
    maintenance = MemoryMaintenance(enabled=True, clock=clock)
    for _ in range(ACTION_THRESHOLD - 1):
        maintenance.record_completed_action()
    assert maintenance.due() is False
    maintenance.record_completed_action()
    assert maintenance.due() is True
    calls = []
    async def request_gc():
        calls.append(True)

    assert await maintenance.attempt(request_gc) is True
    assert calls == [True]
    assert maintenance.status()["successes"] == 1
    assert maintenance.status()["completed_actions_since_attempt"] == 0
    for _ in range(ACTION_THRESHOLD):
        maintenance.record_completed_action()
    assert maintenance.due() is False
    clock.value += MIN_ATTEMPT_INTERVAL_SECONDS
    assert maintenance.due() is True


@pytest.mark.asyncio
async def test_agent_counts_only_applied_non_cached_ledger_finish(tmp_path, monkeypatch):
    runtime, _page, _ = make_runtime(tmp_path)
    runtime._memory_maintenance.completed_actions_since_attempt = 0
    runtime.control_owner = "agent"
    runtime.control_epoch = 7
    runtime._last_observation_id = "obs"
    finished = []
    monkeypatch.setattr(runtime, "_check_agent_gate", lambda _epoch: None)
    monkeypatch.setattr(runtime, "_ledger_lookup", lambda *_args: None)
    monkeypatch.setattr(runtime, "_ledger_claim", lambda *_args: None)
    monkeypatch.setattr(runtime, "_ledger_finish", lambda _id, receipt: finished.append(receipt))
    async def action(*_args, **_kwargs):
        return {"applied": True}
    monkeypatch.setattr(runtime, "_execute_action", action)
    body = AgentExecuteBody(action_id="action-1", control_epoch=7, observation_id="obs",
                            action={"kind": "click", "x": 1, "y": 1},
                            allowed_origins=["https://readiness-fixture.test"])
    receipt = await runtime.execute_agent(body)
    assert receipt["outcome"] == "applied"
    assert len(finished) == 1
    assert runtime._memory_maintenance.status()["completed_actions_since_attempt"] == 1

    # A duplicate ledger hit is returned before action execution and does not count.
    monkeypatch.setattr(runtime, "_ledger_lookup", lambda *_args: {"outcome": "applied"})
    cached = await runtime.execute_agent(body)
    assert cached == {"outcome": "applied"}
    assert runtime._memory_maintenance.status()["completed_actions_since_attempt"] == 1


@pytest.mark.asyncio
async def test_unknown_agent_or_manual_action_does_not_count(tmp_path, monkeypatch):
    runtime, _page, _ = make_runtime(tmp_path)
    runtime._memory_maintenance.completed_actions_since_attempt = 0
    runtime.control_owner = "agent"
    runtime.control_epoch = 3
    runtime._last_observation_id = "obs"
    monkeypatch.setattr(runtime, "_check_agent_gate", lambda _epoch: None)
    monkeypatch.setattr(runtime, "_ledger_lookup", lambda *_args: None)
    monkeypatch.setattr(runtime, "_ledger_claim", lambda *_args: None)
    monkeypatch.setattr(runtime, "_ledger_finish", lambda *_args: None)
    async def uncertain(*_args, **_kwargs):
        raise WorkerError("ACTION_OUTCOME_UNKNOWN", "unknown")
    monkeypatch.setattr(runtime, "_execute_action", uncertain)
    body = AgentExecuteBody(action_id="action-2", control_epoch=3, observation_id="obs",
                            action={"kind": "click", "x": 1, "y": 1},
                            allowed_origins=["https://readiness-fixture.test"])
    receipt = await runtime.execute_agent(body)
    assert receipt["outcome"] == "unknown"
    assert runtime._memory_maintenance.status()["completed_actions_since_attempt"] == 0

    runtime.control_owner = "human"
    runtime.control_epoch = 4
    manual = ManualExecuteBody(control_epoch=4, action={"kind": "click", "x": 1, "y": 1},
                               allowed_origins=["https://readiness-fixture.test"])
    receipt = await runtime.execute_manual(manual)
    assert receipt["outcome"] == "unknown"
    assert runtime._memory_maintenance.status()["completed_actions_since_attempt"] == 0

@pytest.mark.asyncio
async def test_observation_runs_gc_once_at_same_page_boundary_before_capture(tmp_path):
    order = []
    async def gc():
        order.append("gc")

    runtime, page, _old_order = make_runtime(tmp_path, gc=gc)
    runtime._adapter.order = order
    before = (runtime.worker_instance_id, runtime.browser_instance_id, runtime.page_id,
              runtime._recovery_generation, runtime.control_epoch)
    observation = await runtime.observe()
    after = (runtime.worker_instance_id, runtime.browser_instance_id, runtime.page_id,
             runtime._recovery_generation, runtime.control_epoch)
    assert page.gc_calls == 1
    assert order == ["gc", "screenshot"]
    assert observation["screenshot_png_b64"]
    assert before == after
    assert runtime._action_in_progress is False
    assert runtime._memory_maintenance.status()["successes"] == 1
    assert runtime.status_base()["memory_maintenance"]["attempts"] == 1


@pytest.mark.asyncio
async def test_gc_failure_disables_instance_without_failing_observation(tmp_path):
    async def fail_gc():
        raise RuntimeError("must-not-leak")

    runtime, page, _ = make_runtime(tmp_path, gc=fail_gc)
    observation = await runtime.observe()
    maintenance = runtime.status_base()["memory_maintenance"]
    assert observation["screenshot_png_b64"]
    assert page.gc_calls == 1
    assert maintenance["enabled"] is False
    assert maintenance["state"] == "disabled_after_failure"
    assert maintenance["failures"] == 1
    assert maintenance["last_error_code"] == "gc_failed"
    assert "must-not-leak" not in str(maintenance)


@pytest.mark.asyncio
async def test_busy_input_skips_due_gc_and_leaves_it_due(tmp_path):
    runtime, page, _ = make_runtime(tmp_path)
    await runtime._input_lock.acquire()
    try:
        await runtime.observe()
    finally:
        runtime._input_lock.release()
    assert page.gc_calls == 0
    assert runtime._memory_maintenance.due() is True


@pytest.mark.asyncio
async def test_gc_timeout_is_bounded_and_disables_instance(tmp_path):
    release = asyncio.Event()
    async def blocked_gc():
        await release.wait()

    runtime, _page, _ = make_runtime(tmp_path, gc=blocked_gc)
    await runtime.observe()
    assert runtime._memory_maintenance.status()["last_error_code"] == "gc_timeout"
    assert runtime._memory_maintenance.status()["enabled"] is False
    assert runtime._action_in_progress is False


@pytest.mark.asyncio
async def test_human_gate_can_preempt_gc_without_page_or_identity_change(tmp_path):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_gc():
        entered.set()
        await release.wait()

    runtime, page, _ = make_runtime(tmp_path, gc=blocked_gc)
    identity = (runtime.worker_instance_id, runtime.browser_instance_id, runtime.page_id,
                runtime._recovery_generation, runtime._page, runtime._context)
    observe_task = asyncio.create_task(runtime.observe())
    await entered.wait()
    before_epoch = runtime.control_epoch
    gate_result = runtime.gate("human")
    assert gate_result["control_epoch"] == before_epoch + 1
    assert runtime._action_in_progress is True
    release.set()
    observation = await observe_task
    after = (runtime.worker_instance_id, runtime.browser_instance_id, runtime.page_id,
             runtime._recovery_generation, runtime._page, runtime._context)
    assert identity == after
    assert observation["control_epoch"] == before_epoch + 1
    assert page.gc_calls == 1
    assert runtime._action_in_progress is False
