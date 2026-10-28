from __future__ import annotations

import pytest
from pydantic import ValidationError

from browser_mcp.domain import DomainError, TaskSpec, TaskStatus
from browser_mcp.storage import TaskRepository


def spec(key: str = "request-1", **updates: object) -> TaskSpec:
    values: dict[str, object] = {
        "goal": "Find the item and report its price",
        "allowed_origins": ["https://shop.example"],
        "idempotency_key": key,
    }
    values.update(updates)
    return TaskSpec.model_validate(values)


def test_spec_validates_origins_and_profile_ids() -> None:
    normalized = spec(allowed_origins=["HTTPS://Shop.Example:443", "https://[2001:0db8::1]:8443"])
    assert normalized.allowed_origins == ["https://shop.example", "https://[2001:db8::1]:8443"]
    for bad_origin in ("http://shop.example", "https://user@shop.example", "https://shop.example/path", "https://shop.example?x=1", "https://shop.example#frag"):
        with pytest.raises(ValidationError):
            spec(allowed_origins=[bad_origin])
    for bad_profile in ("../default", "a/b", "..", ""):
        with pytest.raises(ValidationError):
            spec(profile_id=bad_profile)


def test_sync_cookies_defaults_on_and_requires_a_boolean() -> None:
    assert spec().sync_cookies is True
    assert spec(sync_cookies=False).sync_cookies is False
    for invalid in ("false", 0, 1, None):
        with pytest.raises(ValidationError):
            spec(sync_cookies=invalid)


def test_create_idempotency_is_scoped_to_principal_and_canonical_spec(tmp_path) -> None:
    repo = TaskRepository(tmp_path / "tasks.sqlite")
    first = repo.create(spec(), principal_id="alice")
    same = repo.create(spec(), principal_id="alice")
    assert same.task_id == first.task_id
    assert repo.create(spec(), principal_id="bob").task_id != first.task_id
    with pytest.raises(DomainError) as err:
        repo.create(spec(goal="Different goal"), principal_id="alice")
    assert err.value.code == "IDEMPOTENCY_CONFLICT"
    repo.close()


def test_transitions_versions_and_step_budget_are_durable(tmp_path) -> None:
    path = tmp_path / "tasks.sqlite"
    repo = TaskRepository(path)
    created = repo.create(spec())
    running = repo.transition(created.task_id, TaskStatus.RUNNING, expected_version=created.version)
    assert running.status == TaskStatus.RUNNING and running.version == created.version + 1
    stepped = repo.increment_step(created.task_id)
    assert stepped.step_count == 1 and stepped.version == running.version + 1
    with pytest.raises(DomainError) as conflict:
        repo.transition(created.task_id, TaskStatus.PAUSED, expected_version=running.version)
    assert conflict.value.code == "VERSION_CONFLICT"
    with pytest.raises(DomainError) as invalid:
        repo.transition(created.task_id, TaskStatus.CANCELLED, expected_version=stepped.version)
    assert invalid.value.code == "INVALID_TRANSITION"
    paused = repo.transition(created.task_id, TaskStatus.PAUSE_REQUESTED, expected_version=stepped.version, phase="stopping")
    paused = repo.transition(created.task_id, TaskStatus.PAUSED, expected_version=paused.version)
    repo.close()

    reopened = TaskRepository(path)
    restored = reopened.get(created.task_id)
    assert restored.status == TaskStatus.PAUSED
    assert restored.step_count == 1
    assert restored.phase == "stopping"
    timeline = reopened.events(created.task_id)
    assert [event["event_type"] for event in timeline] == [
        "task.created", "task.status_changed", "task.step_counted", "task.status_changed", "task.status_changed"
    ]
    assert [event["event_id"] for event in timeline] == sorted(event["event_id"] for event in timeline)
    reopened.close()


def test_event_payload_redacts_secret_fields_and_list_is_principal_scoped(tmp_path) -> None:
    repo = TaskRepository(tmp_path / "tasks.sqlite")
    record = repo.create(spec(), principal_id="alice")
    repo.create(spec("request-2"), principal_id="bob")
    event = repo.add_event(record.task_id, "observation", {"title": "Item", "password": "do-not-store", "nested": {"api_key": "private"}})
    assert event["payload"] == {"title": "Item", "password": "[REDACTED]", "nested": {"api_key": "[REDACTED]"}}
    assert [row.principal_id for row in repo.list("alice")] == ["alice"]
    assert len(repo.list("bob")) == 1
    repo.close()
