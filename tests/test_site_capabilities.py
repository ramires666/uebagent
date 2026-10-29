import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from browser_mcp.app import create_app
from browser_mcp.config import Settings
from browser_mcp.domain import DomainError, TaskSpec, TaskStatus
from browser_mcp.storage import TaskRepository
from browser_mcp.model_client import ModelClient, ModelSettings


def spec(**overrides):
    values = {'goal': 'fixture', 'allowed_origins': ['https://gemini.google.com'], 'idempotency_key': 'fixture-key-1'}
    return TaskSpec(**(values | overrides))


def test_autonomy_feedback_is_bounded_and_requires_no_invented_data():
    model = ModelClient(ModelSettings(base_url='http://localhost:8081/v1'))
    model.begin_task('fixture')
    model.record_autonomy_notice()
    assert len(model._conversation_feedback.encode('utf-8')) < 8192
    assert 'Do not invent' in model._conversation_feedback


def test_capability_schema_and_stable_legacy_idempotency(tmp_path):
    assert spec().execution_mode == 'autonomous'
    assert spec(execution_mode='supervised').execution_mode == 'supervised'
    with pytest.raises(ValidationError): spec(execution_mode='invalid')
    with pytest.raises(ValidationError): spec(allowed_origins=['https://example.test'], site_capabilities=['gemini_prompt_submit'])
    with pytest.raises(ValidationError): spec(site_capabilities=['gemini_prompt_submit', 'gemini_prompt_submit'])
    with pytest.raises(ValidationError): spec(site_capabilities=['unknown'])
    path = tmp_path / 'tasks.sqlite'
    repo = TaskRepository(path)
    original = repo.create(spec())
    # Simulate a task persisted before the new optional field existed.
    row = json.loads(repo._conn.execute('SELECT spec_json FROM tasks WHERE task_id=?', (original.task_id,)).fetchone()[0])
    row.pop('site_capabilities')
    repo._conn.execute('UPDATE tasks SET spec_json=? WHERE task_id=?', (json.dumps(row), original.task_id))
    assert repo.create(spec()).task_id == original.task_id
    with pytest.raises(DomainError) as exc: repo.create(spec(execution_mode='supervised'))
    assert exc.value.code == 'IDEMPOTENCY_CONFLICT'
    before = repo._conn.execute('SELECT spec_json FROM tasks WHERE task_id=?', (original.task_id,)).fetchone()[0]
    grant = repo.grant_capability(original.task_id, 'gemini_prompt_submit', original.version)
    updated = repo.get(original.task_id)
    assert updated.status == TaskStatus.QUEUED and updated.version == original.version + 1
    assert repo.grant_capability(original.task_id, 'gemini_prompt_submit', updated.version) == grant
    assert repo._conn.execute('SELECT spec_json FROM tasks WHERE task_id=?', (original.task_id,)).fetchone()[0] == before
    repo.close()
    repo = TaskRepository(path)
    assert repo.capability(original.task_id, 'gemini_prompt_submit') == grant
    assert repo.create(spec()).task_id == original.task_id
    assert [e['event_type'] for e in repo.events(original.task_id)].count('task.capability_granted') == 1
    repo.close()


def test_created_capability_and_grant_guards(tmp_path):
    repo = TaskRepository(tmp_path / 'tasks.sqlite')
    created = repo.create(spec(site_capabilities=['gemini_prompt_submit']))
    assert repo.capability(created.task_id, 'gemini_prompt_submit')['kind'] == 'gemini_prompt_submit'
    with pytest.raises(DomainError) as exc: repo.grant_capability(created.task_id, 'gemini_prompt_submit', 99)
    assert exc.value.code == 'VERSION_CONFLICT'
    other = repo.create(spec(allowed_origins=['https://example.test'], idempotency_key='fixture-key-2'))
    with pytest.raises(DomainError) as exc: repo.grant_capability(other.task_id, 'gemini_prompt_submit', other.version)
    assert exc.value.code == 'POLICY_DENIED'
    done = repo.transition(created.task_id, TaskStatus.CANCELLED)
    with pytest.raises(DomainError) as exc: repo.grant_capability(done.task_id, 'gemini_prompt_submit', done.version)
    assert exc.value.code == 'INVALID_TRANSITION'
    repo.close()


def test_grant_endpoint_requires_operator_csrf_and_never_resumes(tmp_path):
    settings = Settings(data_dir=tmp_path, ui_dir=tmp_path / 'none', test_mode=True, operator_token='fixture-token', mcp_token='fixture-mcp')
    with TestClient(create_app(settings)) as client:
        repo = client.app.state.repository
        task = repo.create(spec())
        task = repo.transition(task.task_id, TaskStatus.RUNNING)
        task = repo.transition(task.task_id, TaskStatus.WAITING_APPROVAL)
        url = f'/api/v1/tasks/{task.task_id}/site-capabilities'
        body = {'capability': 'gemini_prompt_submit', 'expected_version': task.version}
        assert client.post(url, json=body).status_code == 401
        csrf = client.post('/api/v1/operator/session', json={'token': 'fixture-token'}).json()['csrf_token']
        assert client.post(url, json=body).status_code == 403
        assert client.post(url, json=body | {'extra': True}, headers={'X-CSRF-Token': csrf}).status_code == 409
        response = client.post(url, json=body, headers={'X-CSRF-Token': csrf})
        assert response.status_code == 200
        assert repo.get(task.task_id).status == TaskStatus.WAITING_APPROVAL
        assert client.post(url, json=body, headers={'X-CSRF-Token': csrf}).status_code == 409
