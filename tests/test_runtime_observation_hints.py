from __future__ import annotations

import asyncio
import base64
import struct
import sys
from types import SimpleNamespace
from typing import Any
from pathlib import Path

import pytest

SCRIPTS = str(Path(__file__).resolve().parents[1] / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from browser_mcp.config import Settings
from browser_mcp.domain import TaskSpec, TaskStatus
from browser_mcp.runtime import TaskRunner
from browser_mcp.storage import TaskRepository


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
NO_HINTS = object()
HINTS = {
    "version": 1,
    "coordinate_space": "normalized_1000",
    "elements": [{
        "tag": "button", "role": "button", "label": "Expand Home",
        "center": {"x": 200, "y": 250},
        "bounds": {"x": 150, "y": 200, "width": 100, "height": 100},
        "kind": "expander", "expanded": False,
    }],
}


def png_header(width: int, height: int) -> bytes:
    """Enough PNG structure to preserve its real screenshot dimensions."""
    return (PNG_SIGNATURE + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height)
            + b"\x08\x02\x00\x00\x00\x00\x00\x00\x00")


class FakeWorker:
    def __init__(self, *, hints: dict[str, Any] | None, width: int = 1440, height: int = 900):
        self.hints = hints
        self.width = width
        self.height = height
        self.control_epoch = 0
        self.control_owner = "none"
        self.observation_count = 0
        self.executes: list[dict[str, Any]] = []

    async def health(self):
        return {"ready": True, "worker_instance_id": "worker-fixture", "browser_instance_id": "browser-fixture"}

    async def state(self):
        return {"control_owner": self.control_owner}

    async def gate(self, owner: str):
        self.control_owner = owner
        self.control_epoch += 1
        return {"control_epoch": self.control_epoch, "control_owner": owner}

    async def call(self, method: str, path: str, data=None, raw=False):
        if path == "/heartbeat":
            return {"ok": True}
        if path == "/observe":
            self.observation_count += 1
            response = {
                "url": "https://fixture.example/",
                "width": self.width,
                "height": self.height,
                "screenshot_png_b64": base64.b64encode(png_header(self.width, self.height)).decode("ascii"),
                "control_epoch": self.control_epoch,
                "observation_id": f"obs-{self.observation_count}",
            }
            if self.hints is not None:
                response["interaction_hints"] = self.hints
            return response
        if path == "/execute":
            self.executes.append(data)
            return {"outcome": "applied"}
        if path == "/verify":
            return {"verified": False, "checks": []}
        raise AssertionError(f"unexpected worker path {path}")


class RecordingModel:
    def __init__(self, protocol: str, actions: list[dict[str, Any]]):
        self.settings = SimpleNamespace(protocol=protocol)
        self.actions = list(actions)
        self.received_hints: list[Any] = []

    def begin_task(self, _task_id: str) -> None:
        pass

    def end_task(self) -> None:
        pass

    async def health(self):
        return {"ready": True}

    async def next_action(self, _goal, _screenshot, _history=None, *, current_url=None,
                          observation_hints=NO_HINTS):
        self.received_hints.append(observation_hints)
        return self.actions.pop(0)


class StrictLegacyModel:
    """Legacy generic-looking stub with no settings and a strict old signature."""

    def __init__(self):
        self.actions = [{"kind": "finish", "summary": "done"}]

    async def health(self):
        return {"ready": True}

    async def next_action(self, _goal, _screenshot, _history=None):
        return self.actions.pop(0)


class StrictFaraModel:
    def __init__(self):
        self.settings = SimpleNamespace(protocol="fara")
        self.actions = [{"kind": "finish", "summary": "done"}]

    def begin_task(self, _task_id: str) -> None:
        pass

    def end_task(self) -> None:
        pass

    async def health(self):
        return {"ready": True}

    async def next_action(self, _goal, _screenshot, _history=None, *, current_url=None):
        return self.actions.pop(0)


async def run_task(tmp_path, *, model, worker):
    repo = TaskRepository(tmp_path / "tasks.db")
    runner = TaskRunner(Settings(data_dir=tmp_path, worker_url="http://worker"), repo, worker=worker, model=model)
    record = repo.create(TaskSpec(sync_cookies=False, goal="Use this synthetic fixture", 
                                  allowed_origins=["https://fixture.example"], idempotency_key="hints-fixture-0001"))
    record = repo.transition(record.task_id, TaskStatus.RUNNING)
    await runner._run(record.task_id)
    return runner, repo, worker, model, record.task_id


@pytest.mark.asyncio
async def test_generic_model_receives_only_current_observation_hints_and_keeps_single_coordinate_scale(tmp_path):
    worker = FakeWorker(hints=HINTS)
    model = RecordingModel("generic", [{"kind": "click", "x": 500, "y": 500},
                                        {"kind": "finish", "summary": "done"}])
    runner, repo, worker, model, task_id = await run_task(tmp_path, model=model, worker=worker)
    try:
        assert repo.get(task_id).status == TaskStatus.SUCCEEDED
        assert model.received_hints == [HINTS, HINTS]
        assert len([event for event in repo.events(task_id) if event["event_type"] == "observation.hints_received"]) == 2
        assert worker.executes[0]["action"] == {"kind": "click", "x": 720, "y": 450}
    finally:
        await runner.close()
        repo.close()


@pytest.mark.asyncio
async def test_generic_model_without_worker_hints_keeps_old_call_shape(tmp_path):
    worker = FakeWorker(hints=None)
    model = RecordingModel("generic", [{"kind": "finish", "summary": "done"}])
    runner, repo, _worker, model, task_id = await run_task(tmp_path, model=model, worker=worker)
    try:
        assert repo.get(task_id).status == TaskStatus.SUCCEEDED
        assert model.received_hints == [NO_HINTS]
        assert not [event for event in repo.events(task_id) if event["event_type"] == "observation.hints_received"]
    finally:
        await runner.close()
        repo.close()


@pytest.mark.asyncio
async def test_legacy_strict_model_is_not_given_new_keyword(tmp_path):
    worker = FakeWorker(hints=HINTS)
    model = StrictLegacyModel()
    runner, repo, _worker, _model, task_id = await run_task(tmp_path, model=model, worker=worker)
    try:
        assert repo.get(task_id).status == TaskStatus.SUCCEEDED
        assert not [event for event in repo.events(task_id) if event["event_type"] == "observation.hints_received"]
    finally:
        await runner.close()
        repo.close()


@pytest.mark.asyncio
async def test_fara_model_does_not_receive_generic_observation_hints(tmp_path):
    worker = FakeWorker(hints=HINTS)
    model = StrictFaraModel()
    runner, repo, _worker, _model, task_id = await run_task(tmp_path, model=model, worker=worker)
    try:
        assert repo.get(task_id).status == TaskStatus.SUCCEEDED
        assert not [event for event in repo.events(task_id) if event["event_type"] == "observation.hints_received"]
    finally:
        await runner.close()
        repo.close()


def test_p0_task_event_reader_advances_cursor_across_pages(monkeypatch):
    import scripts.readiness_p0 as p0

    calls = []
    responses = [
        {"events": [{"event_id": value, "event_type": "action.planned", "payload": {"kind": "click"}}
                    for value in range(1, 101)]},
        {"events": [{"event_id": 101, "event_type": "observation.hints_received",
                      "payload": {"control_count": 4}},
                     {"event_id": 102, "event_type": "task.status", "payload": {}}]},
    ]

    def fake_mcp(_client, _headers, _control, name, arguments):
        calls.append((name, arguments))
        return responses.pop(0)

    monkeypatch.setattr(p0, "_mcp", fake_mcp)
    events, truncated = p0._fetch_task_events(object(), {}, "http://localhost:8080", "fixture-task")

    assert [call[1]["after_event_id"] for call in calls] == [0, 100]
    assert all(call[1]["limit"] == 100 for call in calls)
    assert len(events) == 102 and events[-1]["event_id"] == 102
    assert not truncated


def test_p0_task_event_reader_marks_a_hard_event_cap(monkeypatch):
    import scripts.readiness_p0 as p0

    cursor_calls = []

    def full_page(_client, _headers, _control, _name, arguments):
        cursor = arguments["after_event_id"]
        cursor_calls.append(cursor)
        return {"events": [{"event_id": cursor + index + 1, "event_type": "action.planned", "payload": {}}
                            for index in range(100)]}

    monkeypatch.setattr(p0, "_mcp", full_page)
    events, truncated = p0._fetch_task_events(object(), {}, "http://localhost:8080", "fixture-task")

    assert len(events) == 1000
    assert cursor_calls == list(range(0, 1000, 100))
    assert truncated


def test_hints_must_match_screenshot_dimensions_and_bound_contract():
    from browser_mcp.runtime import _current_observation_hints

    screenshot = png_header(1440, 900)
    observation = {"width": 1440, "height": 900, "interaction_hints": HINTS}
    assert _current_observation_hints(observation, screenshot) is HINTS
    assert _current_observation_hints({**observation, "width": 1280}, screenshot) is None
    oversized = {**HINTS, "elements": HINTS["elements"] * 33}
    assert _current_observation_hints({**observation, "interaction_hints": oversized}, screenshot) is None


def test_worker_sanitized_hints_satisfy_model_boundary_including_decimal_viewport_edge():
    from worker.observation_hints import sanitize_hints
    from browser_mcp.model_client import ModelError, _sanitize_observation_hints

    near_edge = {
        "version": 1, "coordinate_space": "normalized_1000", "elements": [{
            "tag": "button", "role": "button", "label": "edge",
            "center": {"x": 999.99, "y": 250},
            "bounds": {"x": 999.5, "y": 200, "width": 0.5, "height": 100},
            "kind": "expander",
        }],
    }
    sanitized = sanitize_hints(near_edge)
    assert len(sanitized["elements"]) == 1
    encoded = _sanitize_observation_hints(sanitized)
    assert '"label":"edge"' in encoded

    outside = {**near_edge, "elements": [{**near_edge["elements"][0],
                                           "bounds": {"x": 999.5, "y": 200,
                                                      "width": 0.50001, "height": 100}}]}
    assert sanitize_hints(outside)["elements"] == []

    # The consumer tolerates only floating-point residue above the viewport edge;
    # centers themselves must remain in normalized [0,1000).
    edge_residue = {**near_edge, "elements": [{**near_edge["elements"][0],
                                                "center": {"x": 999.99, "y": 250},
                                                "bounds": {"x": 999.5, "y": 200,
                                                           "width": 0.5000000000001, "height": 100}}]}
    assert '"label":"edge"' in _sanitize_observation_hints(edge_residue)
    beyond_tolerance = {**edge_residue, "elements": [{**edge_residue["elements"][0],
                                                       "bounds": {"x": 999.5, "y": 200,
                                                                  "width": 0.50001, "height": 100}}]}
    with pytest.raises(ModelError):
        _sanitize_observation_hints(beyond_tolerance)
