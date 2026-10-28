import base64

import pytest

from browser_mcp.config import Settings
from browser_mcp.domain import DomainError, TaskSpec, TaskStatus
from browser_mcp.runtime import TaskRunner
from browser_mcp.storage import TaskRepository


class Model:
    def __init__(self, actions):
        self.actions = list(actions)
        self.feedback = []

    async def health(self):
        return {"ready": True}

    def begin_task(self, task_id):
        pass

    def record_verification_failure(self, failed):
        self.feedback.append(failed)

    async def next_action(self, goal, screenshot, history, *, current_url):
        return self.actions.pop(0)


class Worker:
    def __init__(self, checks, unknown=False):
        self.checks = list(checks)
        self.gates = []
        self.observations = 0
        self.unknown = unknown
        self.current_url = "https://example.org/"

    async def health(self):
        return {"ready": True}

    async def gate(self, owner):
        self.gates.append(owner)
        return {"control_epoch": 1}

    async def state(self):
        return {"control_owner": "none"}

    async def call(self, method, path, data=None):
        if path == "/heartbeat":
            return {}
        if path == "/observe":
            self.observations += 1
            return {"url": self.current_url, "width": 1440, "height": 900,
                    "control_epoch": 1, "observation_id": str(self.observations),
                    "screenshot_png_b64": base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode()}
        if path == "/verify":
            return self.checks.pop(0)
        if path == "/execute":
            if self.unknown:
                raise DomainError("WORKER_UNAVAILABLE", "unknown")
            return {"outcome": "applied"}
        raise AssertionError(path)


def setup(tmp_path, actions, checks, conditions=None, unknown=False):
    repo = TaskRepository(tmp_path / "tasks.db")
    spec = TaskSpec(goal="Fill the form", allowed_origins=["https://example.org"], sync_cookies=False,
                    idempotency_key="repair-test", success_conditions=conditions if conditions is not None else [
                        {"kind": "field_equals", "selector": "#field", "value": "private expected text"}])
    record = repo.create(spec)
    repo.transition(record.task_id, TaskStatus.RUNNING)
    worker, model = Worker(checks, unknown), Model(actions)
    runner = TaskRunner(Settings(data_dir=tmp_path), repo, worker=worker, model=model)
    return repo, record.task_id, worker, model, runner


def check(passed):
    return {"verified": passed, "checks": [{"kind": "field_equals", "passed": passed}]}


@pytest.mark.asyncio
async def test_repair_then_verified(tmp_path):
    repo, task, worker, model, runner = setup(tmp_path, [{"kind": "finish"}, {"kind": "click", "x": 1, "y": 1}, {"kind": "finish"}], [check(False), check(True)])
    await runner._run(task)
    assert repo.get(task).status == TaskStatus.SUCCEEDED
    assert repo.get(task).step_count == 2
    assert worker.observations == 3 and worker.gates == ["agent", "none"]
    assert model.feedback[0][0]["value"] == "private expected text"
    events = repo.events(task)
    assert sum(e["event_type"] == "verification.repair_requested" for e in events) == 1
    assert "private expected text" not in str(events)
    repo.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(('kind', 'field', 'expected'), [
    ('element_checked', 'checked', True), ('element_enabled', 'enabled', False),
    ('element_visible', 'visible', True), ('element_text_contains', 'text', 'synthetic text'),
])
async def test_typed_element_repair_then_verified(tmp_path, kind, field, expected):
    condition = {'kind': kind, 'selector': '#fixture', field: expected}
    failure = {'verified': False, 'checks': [{'kind': kind, 'passed': False}]}
    success = {'verified': True, 'checks': [{'kind': kind, 'passed': True}]}
    repo, task, worker, model, runner = setup(tmp_path, [{'kind': 'finish'}, {'kind': 'click', 'x': 1, 'y': 1}, {'kind': 'finish'}], [failure, success], [condition])
    await runner._run(task)
    assert repo.get(task).status == TaskStatus.SUCCEEDED and repo.get(task).result['verified'] is True
    assert model.feedback == [[condition]] and worker.observations == 3
    events = [e for e in repo.events(task) if e['event_type'] == 'verification.repair_requested']
    assert len(events) == 1 and events[0]['payload'] == {'failed_checks': [{'index': 0, 'kind': kind, 'passed': False}]}
    repo.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('condition', [
    {'kind': 'element_checked', 'selector': '#fixture', 'checked': 1},
    {'kind': 'element_enabled', 'selector': '', 'enabled': True},
    {'kind': 'element_visible', 'selector': 'x' * 257, 'visible': False},
    {'kind': 'element_text_contains', 'selector': '#fixture', 'text': 'x' * 10001},
    {'kind': 'other', 'selector': '#fixture', 'checked': True},
])
async def test_invalid_typed_condition_does_not_spend_repair(tmp_path, condition):
    failure = {'verified': False, 'checks': [{'kind': condition['kind'], 'passed': False}]}
    repo, task, worker, model, runner = setup(tmp_path, [{'kind': 'finish'}], [failure], [condition])
    await runner._run(task)
    assert repo.get(task).status == TaskStatus.WAITING_HUMAN and not model.feedback
    assert not any(e['event_type'] == 'verification.repair_requested' for e in repo.events(task))
    repo.close()


@pytest.mark.asyncio
async def test_typed_element_repair_budget_persists_across_resume(tmp_path):
    condition = {'kind': 'element_checked', 'selector': '#fixture', 'checked': True}
    failure = {'verified': False, 'checks': [{'kind': 'element_checked', 'passed': False}]}
    repo, task, worker, model, runner = setup(tmp_path, [{'kind': 'finish'}, {'kind': 'finish'}], [failure, failure], [condition])
    await runner._run(task)
    assert repo.get(task).status == TaskStatus.WAITING_HUMAN
    repo.close()
    repo = TaskRepository(tmp_path / 'tasks.db')
    worker, model = Worker([failure]), Model([])
    runner = TaskRunner(Settings(data_dir=tmp_path), repo, worker=worker, model=model)
    repo.transition(task, TaskStatus.RUNNING)
    await runner._run(task)
    assert repo.get(task).status == TaskStatus.WAITING_HUMAN and not model.feedback
    assert len([e for e in repo.events(task) if e['event_type'] == 'verification.repair_requested']) == 1
    repo.close()


@pytest.mark.asyncio
async def test_second_failure_and_resume_do_not_reset_budget(tmp_path):
    repo, task, worker, model, runner = setup(tmp_path, [{"kind": "finish"}, {"kind": "finish"}], [check(False), check(False)])
    await runner._run(task)
    assert repo.get(task).result["checks"] == check(False)["checks"]
    assert repo.get(task).status == TaskStatus.WAITING_HUMAN
    repo.close()
    repo = TaskRepository(tmp_path / "tasks.db")
    runner = TaskRunner(Settings(data_dir=tmp_path), repo, worker=Worker([check(False)]), model=Model([]))
    repo.transition(task, TaskStatus.RUNNING)
    await runner._run(task)
    assert repo.get(task).status == TaskStatus.WAITING_HUMAN
    assert repo.get(task).step_count == 1
    assert sum(e["event_type"] == "verification.repair_requested" for e in repo.events(task)) == 1
    repo.close()


@pytest.mark.asyncio
async def test_resume_prior_failure_requests_repair(tmp_path):
    repo, task, worker, model, runner = setup(tmp_path, [{"kind": "finish"}], [check(False), check(True)])
    repo.transition(task, TaskStatus.WAITING_HUMAN, phase="verification_required", result={"verified": False})
    repo.transition(task, TaskStatus.RUNNING)
    await runner._run(task)
    assert repo.get(task).status == TaskStatus.SUCCEEDED
    assert model.feedback and repo.get(task).step_count == 1
    repo.close()


@pytest.mark.asyncio
async def test_resume_verification_denies_page_outside_allowed_origins(tmp_path):
    repo, task, worker, model, runner = setup(tmp_path, [], [check(True)])
    repo.transition(task, TaskStatus.WAITING_HUMAN, phase="verification_required", result={"verified": False})
    repo.transition(task, TaskStatus.RUNNING)
    worker.current_url = "https://other.example/"
    await runner._run(task)
    result = repo.get(task)
    assert result.status == TaskStatus.WAITING_HUMAN
    assert result.error["code"] == "POLICY_DENIED"
    assert worker.checks == [check(True)] and not model.feedback
    assert not any(e["event_type"] == "verification.repair_requested" for e in repo.events(task))
    repo.close()


@pytest.mark.asyncio
async def test_empty_conditions_never_verify(tmp_path):
    repo, task, worker, model, runner = setup(tmp_path, [{"kind": "finish"}], [{"verified": True, "checks": []}], [])
    await runner._run(task)
    assert repo.get(task).status == TaskStatus.SUCCEEDED
    assert repo.get(task).phase == 'completed_unverified'
    assert repo.get(task).result['verified'] is False
    assert repo.get(task).result["checks"] == [] and repo.get(task).step_count == 0
    repo.close()


@pytest.mark.asyncio
async def test_unknown_outcome_never_repairs(tmp_path):
    repo, task, worker, model, runner = setup(tmp_path, [{"kind": "click", "x": 1, "y": 1}], [], unknown=True)
    await runner._run(task)
    assert repo.get(task).status == TaskStatus.RECOVERING
    assert not model.feedback
    assert not any(e["event_type"] == "verification.repair_requested" for e in repo.events(task))
    repo.close()

@pytest.mark.asyncio
async def test_feedback_reaches_fresh_managed_conversation():
    import json
    import httpx
    from browser_mcp.model_client import ModelClient, ModelSettings

    captured = []
    def respond(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '<tool_call>{"name":"computer_use","arguments":{"action":"terminate","answer":"done"}}</tool_call>'}}]})
    model = ModelClient(ModelSettings("http://model/v1", system_prompt="prompt"), httpx.MockTransport(respond))
    model.begin_task("restarted")
    model.record_verification_failure([{"kind": "field_equals", "selector": "#field", "value": "42"}])
    await model.next_action("Original goal", b"\x89PNG\r\n\x1a\nfixture", current_url="https://example.org/")
    content = captured[0]["messages"][1]["content"][0]["text"]
    assert "Original goal" in content and '"value":"42"' in content
    assert "Independent verification" in content and "previous action" not in content


@pytest.mark.asyncio
async def test_oversized_feedback_does_not_spend_repair(tmp_path):
    conditions = [{"kind": "field_equals", "selector": "#field", "value": "x" * 9000}]
    repo, task, worker, model, runner = setup(tmp_path, [{"kind": "finish"}], [check(False)], conditions)
    await runner._run(task)
    assert repo.get(task).status == TaskStatus.WAITING_HUMAN
    assert repo.get(task).step_count == 0 and not model.feedback
    assert not any(e["event_type"] == "verification.repair_requested" for e in repo.events(task))
    repo.close()

