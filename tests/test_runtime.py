from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from browser_mcp.config import Settings
from browser_mcp.domain import DomainError, TaskSpec, TaskStatus
from browser_mcp.model_client import ModelClient, ModelError, ModelSettings
from browser_mcp.runtime import GOOGLE_SEARCH_ORIGIN, TaskRunner, WorkerClient
from browser_mcp.storage import TaskRepository


PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode("ascii")


class FakeModel:
    def __init__(self, actions):
        self.actions = list(actions)
        self.goals = []

    async def health(self):
        return {"ready": True}

    async def next_action(self, goal, *_args):
        self.goals.append(goal)
        action = self.actions.pop(0)
        if isinstance(action, Exception):
            raise action
        return action


class FakeWorker:
    def __init__(self, current_url="https://shop.example/"):
        self.control_owner = "none"
        self.control_epoch = 0
        self.calls = []
        self.gates = []
        self.executes = []
        self.observation_count = 0
        self.current_url = current_url
        self.redirect_after_execute = None
        self.state_calls = 0

    async def health(self):
        return {"ready": True}

    async def state(self):
        self.state_calls += 1
        return {"control_owner": self.control_owner}

    async def gate(self, owner):
        self.gates.append(owner)
        self.control_owner = owner
        self.control_epoch += 1
        return {"control_epoch": self.control_epoch, "control_owner": owner}

    async def call(self, method, path, data=None, raw=False):
        self.calls.append((method, path, data))
        if path == "/heartbeat":
            return {"ok": True}
        if path == "/capabilities/gemini-auth/status":
            return {"status": "signed_in", "reason_code": "ACCOUNT_CONTROLS_PRESENT"}
        if path == "/observe":
            self.observation_count += 1
            return {
                "url": self.current_url,
                "width": 1440,
                "height": 900,
                "screenshot_png_b64": PNG_B64,
                "control_epoch": self.control_epoch,
                "observation_id": f"obs-{self.observation_count}",
            }
        if path == "/view/reset":
            assert data == {"control_epoch": self.control_epoch}
            self.current_url = "about:blank"
            return {"outcome": "applied"}
        if path == "/execute":
            self.executes.append(data)
            if self.redirect_after_execute:
                self.current_url = self.redirect_after_execute
            return {"outcome": "applied"}
        if path == "/verify":
            return {"verified": False, "checks": []}
        raise AssertionError(f"unexpected worker call {method} {path}")


@pytest.mark.asyncio
async def test_gemini_task_capability_is_scoped_to_click_and_key(tmp_path):
    runner, repo, worker, record, _ = make_runner(tmp_path, [
        {'kind': 'type_text', 'text': 'synthetic'},
        {'kind': 'click', 'x': 100, 'y': 100},
        {'kind': 'press_key', 'key': 'Enter'},
        {'kind': 'finish', 'summary': 'fixture'},
    ], ['https://gemini.google.com'], current_url='https://gemini.google.com/app',
       execution_mode='supervised')
    grant = repo.grant_capability(record.task_id, 'gemini_prompt_submit', record.version)
    await runner._run(record.task_id)
    commands = worker.executes
    assert [c['action']['kind'] for c in commands] == ['type_text', 'click', 'press_key']
    assert 'site_capability' not in commands[0]
    assert commands[1]['site_capability'] == grant and commands[2]['site_capability'] == grant
    assert all('allow_write' not in c for c in commands)
    await runner.close(); repo.close()


@pytest.mark.asyncio
async def test_approval_and_unknown_outcome_do_not_auto_resume(tmp_path):
    for mode in ('APPROVAL_REQUIRED', 'ACTION_OUTCOME_UNKNOWN'):
        subdir = tmp_path / mode; subdir.mkdir()
        runner, repo, worker, record, _ = make_runner(subdir, [{'kind': 'click', 'x': 100, 'y': 100}], ['https://gemini.google.com'], current_url='https://gemini.google.com/app')
        grant = repo.grant_capability(record.task_id, 'gemini_prompt_submit', record.version)
        original = worker.call
        async def fail(method, path, data=None, raw=False):
            if path == '/execute':
                worker.executes.append(data)
                raise DomainError(mode, 'fixture')
            return await original(method, path, data, raw)
        worker.call = fail
        await runner._run(record.task_id)
        state = repo.get(record.task_id)
        assert state.status == (TaskStatus.WAITING_APPROVAL if mode == 'APPROVAL_REQUIRED' else TaskStatus.RECOVERING)
        assert len(worker.executes) == 1 and worker.executes[0]['site_capability'] == grant
        assert runner.jobs == {}
        await runner.close(); repo.close()


@pytest.mark.asyncio
async def test_autonomous_notice_then_authorized_actions_continue(tmp_path):
    repo = TaskRepository(tmp_path / 'tasks.db')
    worker = FakeWorker()
    model = FakeModel([{'kind': 'ask_user', 'question': 'Need attention token=secret-fixture'},
                       {'kind': 'click', 'x': 100, 'y': 100}, {'kind': 'press_key', 'key': 'Enter'},
                       {'kind': 'finish', 'summary': 'done'}])
    runner = TaskRunner(Settings(data_dir=tmp_path), repo, worker=worker, model=model)
    record = repo.create(TaskSpec(sync_cookies=False, goal='fixture', allowed_origins=['https://shop.example'], execution_mode='autonomous', idempotency_key='autonomy-key-1'))
    await runner._run(record.task_id)
    result = repo.get(record.task_id)
    assert result.status == TaskStatus.SUCCEEDED and result.phase == 'completed_unverified'
    assert result.result['verified'] is False
    assert [x['action']['kind'] for x in worker.executes] == ['click', 'press_key']
    assert all(x['allow_write'] is True for x in worker.executes)
    events = repo.events(record.task_id)
    notices = [x for x in events if x['event_type'] == 'attention.notice']
    assert len(notices) == 1 and 'secret-fixture' not in str(notices)
    assert 'do not invent missing essential data' in model.goals[1]
    await runner.close(); repo.close()


@pytest.mark.asyncio
async def test_autonomy_notice_budget_and_supervised_behavior(tmp_path):
    for mode in ('autonomous', 'supervised'):
        root = tmp_path / mode; root.mkdir(); repo = TaskRepository(root / 'tasks.db'); worker = FakeWorker()
        model = FakeModel([{'kind': 'ask_user', 'question': 'Need information'}] * 4)
        runner = TaskRunner(Settings(data_dir=root), repo, worker=worker, model=model)
        record = repo.create(TaskSpec(sync_cookies=False, goal='fixture', allowed_origins=['https://shop.example'], execution_mode=mode, idempotency_key='autonomy-key-2'))
        await runner._run(record.task_id)
        result = repo.get(record.task_id); events = repo.events(record.task_id)
        if mode == 'autonomous':
            assert result.status == TaskStatus.FAILED and result.phase == 'autonomy_no_progress'
            assert result.error['code'] == 'HUMAN_INPUT_UNAVAILABLE'
            assert len([x for x in events if x['event_type'] == 'attention.notice']) == 3
        else:
            assert result.status == TaskStatus.WAITING_HUMAN
            assert len([x for x in events if x['event_type'] == 'question']) == 1
        assert not worker.executes
        await runner.close(); repo.close()


@pytest.mark.asyncio
async def test_autonomy_notice_budget_persists_across_resume(tmp_path):
    repo = TaskRepository(tmp_path / 'tasks.db')
    record = repo.create(TaskSpec(sync_cookies=False, goal='fixture', allowed_origins=['https://shop.example'], execution_mode='autonomous', idempotency_key='autonomy-key-3'))
    record = repo.transition(record.task_id, TaskStatus.RUNNING)
    repo.autonomy_notice(record.task_id, 'fixture one')
    repo.autonomy_notice(record.task_id, 'fixture two')
    repo.transition(record.task_id, TaskStatus.PAUSE_REQUESTED)
    repo.transition(record.task_id, TaskStatus.PAUSED)
    repo.transition(record.task_id, TaskStatus.RUNNING)
    worker = FakeWorker(); runner = TaskRunner(Settings(data_dir=tmp_path), repo, worker=worker, model=FakeModel([{'kind': 'ask_user', 'question': 'fixture three'}]))
    await runner._run(record.task_id)
    assert repo.get(record.task_id).status == TaskStatus.FAILED and not worker.executes
    await runner.close(); repo.close()


@pytest.mark.asyncio
async def test_autonomy_total_notice_limit_and_unknown_outcome(tmp_path):
    root = tmp_path / 'budget'; root.mkdir(); repo = TaskRepository(root / 'tasks.db'); worker = FakeWorker()
    actions = []
    for _ in range(8): actions.extend([{'kind': 'ask_user', 'question': 'fixture'}, {'kind': 'wait', 'seconds': 0}])
    runner = TaskRunner(Settings(data_dir=root), repo, worker=worker, model=FakeModel(actions))
    record = repo.create(TaskSpec(sync_cookies=False, goal='fixture', allowed_origins=['https://shop.example'], execution_mode='autonomous', idempotency_key='autonomy-key-4'))
    await runner._run(record.task_id)
    assert repo.get(record.task_id).error['code'] == 'HUMAN_INPUT_UNAVAILABLE'
    assert len([x for x in repo.events(record.task_id) if x['event_type'] == 'attention.notice']) == 8
    assert len(worker.executes) == 7
    await runner.close(); repo.close()


@pytest.mark.asyncio
async def test_autonomous_unfocused_input_observes_and_recovers(tmp_path):
    repo = TaskRepository(tmp_path / 'tasks.db'); worker = FakeWorker()
    model = FakeModel([{'kind': 'type_text', 'text': 'fixture'}, {'kind': 'click', 'x': 10, 'y': 10}, {'kind': 'type_text', 'text': 'fixture'}, {'kind': 'finish', 'summary': 'fixture'}])
    runner = TaskRunner(Settings(data_dir=tmp_path), repo, worker=worker, model=model)
    record = repo.create(TaskSpec(sync_cookies=False, goal='fixture', allowed_origins=['https://shop.example'], execution_mode='autonomous', idempotency_key='focus-test-1'))
    original = worker.call; rejected = False
    async def focused(method, path, data=None, raw=False):
        nonlocal rejected
        if path == '/execute' and data['action']['kind'] == 'type_text' and not rejected:
            rejected = True; worker.executes.append(data)
            raise DomainError('CAPABILITY_UNAVAILABLE', 'text input requires a focused editable field')
        return await original(method, path, data, raw)
    worker.call = focused
    await runner._run(record.task_id)
    assert repo.get(record.task_id).status == TaskStatus.SUCCEEDED
    assert [x['action']['kind'] for x in worker.executes] == ['type_text', 'click', 'type_text']
    assert len({x['observation_id'] for x in worker.executes}) == 3
    assert 'Typing was not performed' in model.goals[1]
    events = [x for x in repo.events(record.task_id) if x['event_type'] == 'action.rejected']
    assert len(events) == 1 and events[0]['payload'] == {'code': 'CAPABILITY_UNAVAILABLE', 'reason': 'editable_not_focused'}
    assert repo.get(record.task_id).step_count == 3
    await runner.close(); repo.close()


@pytest.mark.asyncio
async def test_focus_recovery_bounded_and_supervised_remains_guarded(tmp_path):
    for mode in ('autonomous', 'supervised'):
        root = tmp_path / mode; root.mkdir(); repo = TaskRepository(root / 'tasks.db'); worker = FakeWorker()
        model = FakeModel([{'kind': 'type_text', 'text': 'fixture'}] * 4)
        runner = TaskRunner(Settings(data_dir=root), repo, worker=worker, model=model)
        record = repo.create(TaskSpec(sync_cookies=False, goal='fixture', allowed_origins=['https://shop.example'], execution_mode=mode, idempotency_key='focus-test-2'))
        original = worker.call
        async def reject(method, path, data=None, raw=False):
            if path == '/execute':
                worker.executes.append(data)
                raise DomainError('CAPABILITY_UNAVAILABLE', 'text input requires a focused editable field')
            return await original(method, path, data, raw)
        worker.call = reject
        await runner._run(record.task_id)
        result = repo.get(record.task_id)
        if mode == 'autonomous':
            assert result.status == TaskStatus.FAILED and result.phase == 'action_recovery_exhausted'
            assert result.error['code'] == 'EDITABLE_FOCUS_RETRIES_EXHAUSTED'
            assert len(worker.executes) == 3 and result.step_count == 3
        else:
            assert result.status == TaskStatus.WAITING_HUMAN and len(worker.executes) == 1
        await runner.close(); repo.close()



@pytest.mark.asyncio
async def test_autonomy_unknown_outcome_never_retries(tmp_path):
    root = tmp_path / 'unknown'; root.mkdir(); repo = TaskRepository(root / 'tasks.db'); worker = FakeWorker()
    original = worker.call
    async def unknown(method, path, data=None, raw=False):
        if path == '/execute': worker.executes.append(data); return {'outcome': 'unknown'}
        return await original(method, path, data, raw)
    worker.call = unknown
    runner = TaskRunner(Settings(data_dir=root), repo, worker=worker, model=FakeModel([{'kind': 'click', 'x': 10, 'y': 10}]))
    record = repo.create(TaskSpec(sync_cookies=False, goal='fixture', allowed_origins=['https://shop.example'], execution_mode='autonomous', idempotency_key='autonomy-key-5'))
    await runner._run(record.task_id)
    assert repo.get(record.task_id).status == TaskStatus.RECOVERING and len(worker.executes) == 1
    await runner.close(); repo.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(('kind', 'code', 'message'), [
    ('type_text', 'POLICY_DENIED', 'text input requires a focused editable field'),
    ('type_text', 'CAPABILITY_UNAVAILABLE', 'other reason'),
    ('click', 'CAPABILITY_UNAVAILABLE', 'text input requires a focused editable field'),
])
async def test_other_rejections_never_use_focus_recovery(tmp_path, kind, code, message):
    repo = TaskRepository(tmp_path / 'tasks.db'); worker = FakeWorker()
    action = {'kind': 'type_text', 'text': 'fixture'} if kind == 'type_text' else {'kind': 'click', 'x': 10, 'y': 10}
    runner = TaskRunner(Settings(data_dir=tmp_path), repo, worker=worker, model=FakeModel([action]))
    record = repo.create(TaskSpec(sync_cookies=False, goal='fixture', allowed_origins=['https://shop.example'], execution_mode='autonomous', idempotency_key='focus-test-3'))
    original = worker.call
    async def reject(method, path, data=None, raw=False):
        if path == '/execute': worker.executes.append(data); raise DomainError(code, message)
        return await original(method, path, data, raw)
    worker.call = reject
    await runner._run(record.task_id)
    assert repo.get(record.task_id).status == TaskStatus.WAITING_HUMAN
    assert len(worker.executes) == 1 and not any(e['event_type'] == 'action.rejected' for e in repo.events(record.task_id))
    await runner.close(); repo.close()


def make_runner(tmp_path, actions, allowed_origins=None, *, current_url="https://shop.example/", start_running=True, user_inputs=None, execution_mode="autonomous"):
    repo = TaskRepository(tmp_path / "tasks.db")
    settings = Settings(data_dir=tmp_path, worker_url="http://worker")
    worker = FakeWorker(current_url)
    model = FakeModel(actions)
    runner = TaskRunner(settings, repo, worker=worker, model=model)
    spec = TaskSpec(sync_cookies=False, 
        goal="Find the requested item",
        allowed_origins=allowed_origins or ["https://shop.example"],
        user_inputs=user_inputs or {},
        idempotency_key="test-key-0001",
        execution_mode=execution_mode,
    )
    record = repo.create(spec)
    if start_running:
        record = repo.transition(record.task_id, TaskStatus.RUNNING)
    return runner, repo, worker, record, model


@pytest.mark.asyncio
async def test_user_provided_literals_are_explicitly_preserved_in_model_goal(tmp_path):
    runner, repo, _worker, record, model = make_runner(
        tmp_path, [{"kind": "finish", "summary": "Done"}],
        user_inputs={"email": "test@example.test"},
    )
    await runner._run(record.task_id)
    assert 'User-provided data (JSON): {"email": "test@example.test"}' in model.goals[0]
    assert "Copy every user-provided literal exactly as written" in model.goals[0]
    assert "Do not correct, substitute, infer, or reformat provided values" in model.goals[0]
    assert "If verification reports a value mismatch, use that feedback" in model.goals[0]
    await runner.close()
    repo.close()


@pytest.mark.asyncio
async def test_invalid_key_chord_gets_bounded_feedback_before_any_dispatch(tmp_path):
    malformed = ModelError("MODEL_OUTPUT_INVALID", "key chord must contain unique modifiers and at most one physical key",
                           category="invalid_key_chord")
    runner, repo, worker, record, model = make_runner(
        tmp_path, [malformed, {"kind": "click", "x": 100, "y": 100}, {"kind": "finish", "summary": "done"}])
    try:
        await runner._run(record.task_id)
        assert repo.get(record.task_id).status == TaskStatus.SUCCEEDED, repo.get(record.task_id).error
        assert [command["action"]["kind"] for command in worker.executes] == ["click"]
        assert worker.observation_count == 3
        assert "unique modifiers and at most one physical key" in model.goals[1]
        rejected = [event for event in repo.events(record.task_id) if event["event_type"] == "model.output_rejected"]
        assert len(rejected) == 1
        assert rejected[0]["payload"] == {"code": "MODEL_OUTPUT_INVALID", "reason": "invalid_key_chord"}
        assert repo.get(record.task_id).step_count == 2
    finally:
        await runner.close()
        repo.close()


@pytest.mark.asyncio
async def test_repeated_invalid_key_chords_fail_at_three_without_dispatch(tmp_path):
    failures = [ModelError("MODEL_OUTPUT_INVALID", "key chord must contain unique modifiers and at most one physical key",
                           category="invalid_key_chord") for _ in range(3)]
    runner, repo, worker, record, _model = make_runner(tmp_path, failures)
    try:
        await runner._run(record.task_id)
        result = repo.get(record.task_id)
        assert result.status == TaskStatus.FAILED
        assert result.error["code"] == "MODEL_OUTPUT_INVALID"
        assert result.phase == "model_output_invalid"
        assert not worker.executes
        assert result.step_count == 3
        assert len([event for event in repo.events(record.task_id)
                    if event["event_type"] == "model.output_rejected"]) == 3
    finally:
        await runner.close()
        repo.close()


@pytest.mark.asyncio
async def test_managed_model_key_chord_repair_sends_feedback_and_fresh_screenshot(tmp_path):
    actions = [
        {"action": "key", "keys": ["Control", "a", "c"]},
        {"action": "left_click", "coordinate": [100, 100]},
        {"action": "terminate", "answer": "done"},
    ]
    requests = []

    def response(action):
        call = {"name": "computer_use", "arguments": action}
        content = "<tool_call>" + json.dumps(call, ensure_ascii=False) + "</tool_call>"
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": content}}]})

    def handle(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "fara-browser"}]})
        requests.append(json.loads(request.content))
        return response(actions.pop(0))

    model = ModelClient(
        ModelSettings("http://llama.example/v1", alias="fara-browser",
                      system_prompt="Pinned Fara system prompt", protocol="fara"),
        httpx.MockTransport(handle),
    )
    repo = TaskRepository(tmp_path / "tasks.db")
    worker = FakeWorker()
    runner = TaskRunner(Settings(data_dir=tmp_path, worker_url="http://worker"), repo,
                        worker=worker, model=model)
    record = repo.create(TaskSpec(sync_cookies=False, goal="fixture", allowed_origins=["https://shop.example"],
                                  idempotency_key="managed-key-repair-0001"))
    record = repo.transition(record.task_id, TaskStatus.RUNNING)
    try:
        await runner._run(record.task_id)
        assert repo.get(record.task_id).status == TaskStatus.SUCCEEDED, repo.get(record.task_id).error
        assert len(requests) == 3
        assert worker.observation_count == 3
        assert [command["action"]["kind"] for command in worker.executes] == ["click"]
        second_user_parts = [part.get("text", "") for message in requests[1]["messages"]
                             if message.get("role") == "user" and isinstance(message.get("content"), list)
                             for part in message["content"] if isinstance(part, dict)]
        assert any("unique modifiers and at most one physical key" in text for text in second_user_parts)
        assert all("invalid_key_chord" not in str(request) for request in requests)
        assert len([event for event in repo.events(record.task_id)
                    if event["event_type"] == "model.output_rejected"]) == 1
    finally:
        await runner.close()
        repo.close()


@pytest.mark.asyncio
async def test_overdue_waiting_task_expires_before_resume_or_worker_state(tmp_path):
    repo = TaskRepository(tmp_path / "tasks.db")
    worker = FakeWorker()
    model = FakeModel([])
    runner = TaskRunner(Settings(data_dir=tmp_path, worker_url="http://worker"), repo,
                        worker=worker, model=model)
    record = repo.create(TaskSpec(sync_cookies=False, goal="fixture", allowed_origins=["https://shop.example"],
                                  limits={"timeout_s": 30}, idempotency_key="stale-resume-test-001"))
    record = repo.transition(record.task_id, TaskStatus.RUNNING)
    record = repo.transition(record.task_id, TaskStatus.WAITING_HUMAN, phase="model_output_invalid",
                             error={"code": "MODEL_OUTPUT_INVALID", "message": "old error"})
    old_created = (datetime.now(UTC) - timedelta(minutes=20)).isoformat().replace("+00:00", "Z")
    repo._conn.execute("UPDATE tasks SET created_at=? WHERE task_id=?", (old_created, record.task_id))
    try:
        with pytest.raises(DomainError) as stale:
            await runner.control(record.task_id, "resume", record.version - 1)
        assert stale.value.code == "VERSION_CONFLICT"
        unchanged = repo.get(record.task_id)
        assert unchanged.status == TaskStatus.WAITING_HUMAN
        assert unchanged.phase == "model_output_invalid"
        with pytest.raises(DomainError) as error:
            await runner.control(record.task_id, "resume", record.version)
        assert error.value.code == "TASK_EXPIRED"
        expired = repo.get(record.task_id)
        assert expired.status == TaskStatus.EXPIRED
        assert expired.phase == "task_deadline_exceeded"
        assert expired.error == {"code": "TASK_EXPIRED", "message": error.value.message}
        assert worker.state_calls == 0
        assert model.goals == []
    finally:
        await runner.close()
        repo.close()


@pytest.mark.asyncio
async def test_overdue_queued_task_expires_before_starting_dependencies(tmp_path):
    repo = TaskRepository(tmp_path / "tasks.db")
    worker = FakeWorker()
    model = FakeModel([])
    runner = TaskRunner(Settings(data_dir=tmp_path, worker_url="http://worker"), repo,
                        worker=worker, model=model)
    record = repo.create(TaskSpec(sync_cookies=False, goal="fixture", allowed_origins=["https://shop.example"],
                                  limits={"timeout_s": 30}, idempotency_key="stale-queued-test-001"))
    old_created = (datetime.now(UTC) - timedelta(minutes=20)).isoformat().replace("+00:00", "Z")
    repo._conn.execute("UPDATE tasks SET created_at=? WHERE task_id=?", (old_created, record.task_id))
    try:
        await runner._run(record.task_id)
        expired = repo.get(record.task_id)
        assert expired.status == TaskStatus.EXPIRED
        assert expired.phase == "task_deadline_exceeded"
        assert expired.error["code"] == "TASK_EXPIRED"
        assert not worker.gates and worker.state_calls == 0
        assert model.goals == []
    finally:
        await runner.close()
        repo.close()


@pytest.mark.asyncio
async def test_worker_error_detail_decodes_fastapi_code_and_message(tmp_path) -> None:
    key_file = tmp_path / "worker-key.txt"
    key_file.write_text("worker-secret", encoding="utf-8")
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(503, json={"detail": {"code": "EGRESS_NOT_READY", "message": "external route is not ready"}})

    client = WorkerClient(Settings(worker_url="http://worker", worker_key_file=str(key_file)), httpx.MockTransport(handler))
    with pytest.raises(DomainError) as exc:
        await client.call("POST", "/execute", {})
    assert exc.value.code == "EGRESS_NOT_READY"
    assert exc.value.message == "external route is not ready"
    assert seen["auth"] == "Bearer worker-secret"


@pytest.mark.asyncio
async def test_search_is_explicit_https_navigation_only_when_google_is_allowed(tmp_path) -> None:
    actions = [{"kind": "search", "query": "red shoes & socks"}, {"kind": "finish", "summary": "Found results"}]
    runner, repo, worker, record, _model = make_runner(
        tmp_path, actions, ["https://shop.example", GOOGLE_SEARCH_ORIGIN]
    )
    await runner._run(record.task_id)
    navs = [command["action"] for command in worker.executes]
    assert navs == [{"kind": "navigate", "url": "https://www.google.com/search?q=red+shoes+%26+socks"}]
    finished = repo.get(record.task_id)
    assert finished.status == TaskStatus.SUCCEEDED
    assert finished.phase == 'completed_unverified'
    assert finished.result["verified"] is False
    assert not any(path == '/verify' for _, path, _ in worker.calls)
    await runner.close()
    repo.close()


@pytest.mark.asyncio
async def test_search_does_not_add_google_to_task_allowlist(tmp_path) -> None:
    runner, repo, worker, record, _model = make_runner(tmp_path, [{"kind": "search", "query": "weather"}])
    await runner._run(record.task_id)
    assert worker.executes == []
    blocked = repo.get(record.task_id)
    assert blocked.status == TaskStatus.WAITING_HUMAN
    assert blocked.error["code"] == "POLICY_DENIED"
    assert GOOGLE_SEARCH_ORIGIN not in blocked.spec.allowed_origins
    await runner.close()
    repo.close()


@pytest.mark.asyncio
async def test_fresh_queued_task_resets_disallowed_initial_page_before_model_observation(tmp_path) -> None:
    runner, repo, worker, record, model = make_runner(
        tmp_path,
        [{"kind": "finish", "summary": "Ready"}],
        ["https://selenium.dev"],
        current_url="https://example.com/account",
        start_running=False,
    )
    await runner._run(record.task_id)

    resets = [call for call in worker.calls if call[1] == "/view/reset"]
    assert len(resets) == 1
    assert resets[0][2] == {"control_epoch": 1}
    assert worker.observation_count == 2
    assert len(model.goals) == 1
    assert "Current page URL: about:blank" in model.goals[0]
    assert 'Task allowed origins (exact): ["https://selenium.dev"]' in model.goals[0]
    assert repo.get(record.task_id).status == TaskStatus.SUCCEEDED
    await runner.close()
    repo.close()


@pytest.mark.asyncio
async def test_later_redirect_outside_allowlist_fails_without_reset(tmp_path) -> None:
    runner, repo, worker, record, model = make_runner(
        tmp_path,
        [
            {"kind": "click", "x": 20, "y": 30, "button": "left", "count": 1},
            {"kind": "finish", "summary": "Done"},
        ],
        ["https://shop.example"],
    )
    worker.redirect_after_execute = "https://evil.example/redirected"
    await runner._run(record.task_id)

    assert worker.observation_count == 2
    assert len(worker.executes) == 1
    assert len(model.goals) == 1
    assert not [call for call in worker.calls if call[1] == "/view/reset"]
    stopped = repo.get(record.task_id)
    assert stopped.status == TaskStatus.WAITING_HUMAN
    assert stopped.error["code"] == "POLICY_DENIED"
    await runner.close()
    repo.close()


@pytest.mark.asyncio
async def test_queued_controls_and_unknown_operation_fail_before_worker_gate(tmp_path) -> None:
    repo = TaskRepository(tmp_path / "tasks.db")
    worker = FakeWorker()
    runner = TaskRunner(Settings(data_dir=tmp_path, worker_url="http://worker"), repo, worker=worker, model=FakeModel([]))
    queued = repo.create(TaskSpec(sync_cookies=False, goal="Task", allowed_origins=["https://shop.example"], idempotency_key="queued-0001"))

    for operation in ("pause", "takeover"):
        with pytest.raises(DomainError) as exc:
            await runner.control(queued.task_id, operation)
        assert exc.value.code == "INVALID_TRANSITION"
    with pytest.raises(DomainError) as exc:
        await runner.control(queued.task_id, "unexpected")
    assert exc.value.code == "INVALID_OPERATION"
    assert worker.gates == []
    assert repo.get(queued.task_id).status == TaskStatus.QUEUED

    cancelled = await runner.control(queued.task_id, "cancel")
    assert cancelled.status == TaskStatus.CANCELLED
    assert worker.gates == []
    repo.close()


@pytest.mark.asyncio
async def test_recovery_requires_versioned_review_then_separate_resume(tmp_path) -> None:
    repo = TaskRepository(tmp_path / "tasks.db")
    worker = FakeWorker()
    runner = TaskRunner(Settings(data_dir=tmp_path, worker_url="http://worker"), repo, worker=worker, model=FakeModel([]))
    queued = repo.create(TaskSpec(sync_cookies=False, goal="Task", allowed_origins=["https://shop.example"], idempotency_key="recover-0001"))
    running = repo.transition(queued.task_id, TaskStatus.RUNNING)
    recovering = repo.transition(queued.task_id, TaskStatus.RECOVERING)

    with pytest.raises(DomainError) as exc:
        await runner.control(queued.task_id, "resume", recovering.version)
    assert exc.value.code == "RECOVERY_REVIEW_REQUIRED"
    with pytest.raises(DomainError) as exc:
        await runner.control(queued.task_id, "review-recovery")
    assert exc.value.code == "VERSION_REQUIRED"
    assert worker.gates == []

    taken = await runner.control(queued.task_id, "takeover", recovering.version)
    assert taken.status == TaskStatus.RECOVERING
    assert worker.gates == ["human"]

    paused = await runner.control(queued.task_id, "review-recovery", recovering.version)
    assert paused.status == TaskStatus.PAUSED and paused.phase == "recovery_reviewed"
    assert worker.gates == ["human", "none"]
    runner.schedule = lambda task_id: worker.calls.append(("schedule", task_id, None))
    resumed = await runner.control(queued.task_id, "resume", paused.version)
    assert resumed.status == TaskStatus.RUNNING
    assert worker.calls[-1] == ("schedule", queued.task_id, None)
    assert repo.get(queued.task_id).status == TaskStatus.RUNNING
    repo.close()
