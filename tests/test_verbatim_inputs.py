from __future__ import annotations

import base64

import pytest
from pydantic import ValidationError

from browser_mcp.config import Settings
from browser_mcp.domain import TaskSpec, TaskStatus
from browser_mcp.model_client import ModelClient, ModelSettings
from browser_mcp.runtime import TaskRunner, VERBATIM_TEXT_FEEDBACK, VERBATIM_TEXT_TOKEN
from browser_mcp.storage import TaskRepository


PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode("ascii")


class FakeModel:
    def __init__(self, actions):
        self.actions = list(actions)
        self.goals = []

    async def health(self):
        return {"ready": True}

    async def next_action(self, goal, *_args, **_kwargs):
        self.goals.append(goal)
        return self.actions.pop(0)


class FakeWorker:
    def __init__(self):
        self.control_epoch = 1
        self.observations = 0
        self.executes = []
        self.gates = []

    async def health(self):
        return {"ready": True}

    async def gate(self, owner):
        self.gates.append(owner)
        return {"control_epoch": self.control_epoch, "control_owner": owner}

    async def call(self, method, path, data=None, raw=False):
        if path == "/heartbeat":
            return {"ok": True}
        if path == "/observe":
            self.observations += 1
            return {
                "url": "https://shop.example/",
                "width": 1440,
                "height": 900,
                "screenshot_png_b64": PNG_B64,
                "control_epoch": self.control_epoch,
                "observation_id": f"obs-{self.observations}",
            }
        if path == "/execute":
            self.executes.append(data)
            return {"outcome": "applied"}
        raise AssertionError(f"unexpected worker call {method} {path}")


def setup_runner(tmp_path, actions, *, verbatim_text, idempotency="verbatim-test-0001"):
    repo = TaskRepository(tmp_path / "tasks.db")
    worker = FakeWorker()
    model = FakeModel(actions)
    runner = TaskRunner(Settings(data_dir=tmp_path, worker_url="http://worker"), repo,
                        worker=worker, model=model)
    record = repo.create(TaskSpec(
        goal="Enter the requested text without changing it.",
        allowed_origins=["https://shop.example"],
        sync_cookies=False,
        verbatim_text=verbatim_text,
        idempotency_key=idempotency,
    ))
    record = repo.transition(record.task_id, TaskStatus.RUNNING)
    return runner, repo, worker, model, record


@pytest.mark.asyncio
@pytest.mark.parametrize("action_text", ["__VERBATIM_TEXT__", "Élodie O’Neil\n  exact  "])
async def test_verbatim_payload_is_exactly_dispatched_and_not_retained(tmp_path, action_text):
    source = "Élodie O’Neil\n  exact  "
    runner, repo, worker, model, record = setup_runner(
        tmp_path, [{"kind": "type_text", "text": action_text}, {"kind": "ask_user", "question": "Continue?"}],
        verbatim_text=source,
    )
    try:
        await runner._run(record.task_id)
        assert worker.executes[0]["action"] == {"kind": "type_text", "text": source}
        assert VERBATIM_TEXT_TOKEN in model.goals[0]
        assert model.goals[0].startswith("Enter the requested text without changing it.")
        assert runner.histories[record.task_id][0]["model_action"]["text"] == VERBATIM_TEXT_TOKEN
        events = repo.events(record.task_id)
        assert all(source not in str(event) for event in events)
        assert all(action_text not in str(event) for event in events)
    finally:
        await runner.close()
        repo.close()


@pytest.mark.asyncio
async def test_mismatch_is_rejected_before_dispatch_and_feedback_is_bounded(tmp_path):
    source = "Name: Zoë"
    altered = "Name: Zoe"
    runner, repo, worker, model, record = setup_runner(
        tmp_path, [{"kind": "type_text", "text": altered}, {"kind": "finish", "summary": "done"}],
        verbatim_text=source,
    )
    try:
        await runner._run(record.task_id)
        assert not worker.executes
        assert repo.get(record.task_id).status == TaskStatus.SUCCEEDED
        rejected = [event for event in repo.events(record.task_id) if event["event_type"] == "action.rejected"]
        assert len(rejected) == 1
        assert rejected[0]["payload"] == {
            "code": "VERBATIM_TEXT_MISMATCH", "reason": "verbatim_text_mismatch",
        }
        assert source not in str(repo.events(record.task_id))
        assert altered not in str(repo.events(record.task_id))
        assert VERBATIM_TEXT_FEEDBACK in model.goals[1]
    finally:
        await runner.close()
        repo.close()


@pytest.mark.asyncio
async def test_three_verbatim_rejections_fail_without_dispatch_and_survive_resume(tmp_path):
    source = "A exact name"
    runner, repo, worker, _model, record = setup_runner(
        tmp_path,
        [{"kind": "type_text", "text": "A paraphrased name"},
         {"kind": "type_text", "text": "A paraphrased name"}],
        verbatim_text=source,
    )
    try:
        assert repo.record_verbatim_text_rejection(record.task_id) == 1
        repo.transition(record.task_id, TaskStatus.PAUSE_REQUESTED)
        repo.transition(record.task_id, TaskStatus.PAUSED)
        repo.transition(record.task_id, TaskStatus.RUNNING)
        await runner._run(record.task_id)
        result = repo.get(record.task_id)
        assert result.status == TaskStatus.FAILED
        assert result.error["code"] == "VERBATIM_TEXT_MISMATCH"
        assert not worker.executes
        assert result.step_count == 3
        assert len([e for e in repo.events(record.task_id)
                    if e["event_type"] == "action.rejected"]) == 3
        assert source not in str(repo.events(record.task_id))
        assert "A paraphrased name" not in str(repo.events(record.task_id))
    finally:
        await runner.close()
        repo.close()


def test_verbatim_text_schema_bounds_and_legacy_default():
    base = {
        "goal": "task", "allowed_origins": ["https://shop.example"],
        "idempotency_key": "verbatim-test-0001",
    }
    assert TaskSpec(**base).verbatim_text is None
    assert TaskSpec(**base, verbatim_text="  x\n").verbatim_text == "  x\n"
    with pytest.raises(ValidationError):
        TaskSpec(**base, verbatim_text="")
    with pytest.raises(ValidationError):
        TaskSpec(**base, verbatim_text="x" * 100_001)


def test_managed_model_history_redacts_verbatim_type_action():
    source = "Synthetic private exact input"
    model = ModelClient(ModelSettings("http://127.0.0.1:8080/v1"))
    model._conversation = [{
        "role": "assistant",
        "content": f'<tool_call>{{"name":"computer_use","arguments":{{"action":"type","text":{source!r}}}}}</tool_call>',
    }]
    model.record_verbatim_text_action()
    assert source not in str(model._conversation)
    assert "__VERBATIM_TEXT__" in model._conversation[0]["content"]
