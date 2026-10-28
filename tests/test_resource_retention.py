from __future__ import annotations

import asyncio
import inspect
import json
import time

import pytest

from browser_mcp.auth import AuthService, MAX_ATTEMPT_PEERS, MAX_SESSIONS
from browser_mcp.chrome import (
    MAX_BACKGROUND,
    MAX_CATALOGS,
    MAX_IMPORT_RECEIPTS,
    MAX_PENDING,
    ChromeBroker,
    digest,
)
from browser_mcp.config import Settings
from browser_mcp.domain import DomainError, TaskSpec, TaskStatus
from browser_mcp.runtime import HISTORY_ITEM_BYTES, HISTORY_ITEMS, TaskRunner
from browser_mcp.model_client import _history_messages
from browser_mcp.storage import TaskRepository


class _Model:
    async def health(self):
        return {"ready": True}

    async def next_action(self, *_args):
        return {"kind": "finish", "summary": "done"}


class _Worker:
    async def health(self):
        return {"ready": True}

    async def gate(self, owner):
        return {"control_epoch": 1, "control_owner": owner}

    async def call(self, _method, path, _data=None, raw=False):
        if path == "/heartbeat":
            return {"ok": True}
        if path == "/observe":
            return {"url": "https://shop.example/", "control_epoch": 1, "observation_id": "obs",
                    "width": 1000, "height": 800, "screenshot_png_b64": "aQ=="}
        if path == "/verify":
            return {"verified": True, "checks": []}
        raise AssertionError(path)


def _runner(tmp_path):
    repo = TaskRepository(tmp_path / "tasks.db")
    runner = TaskRunner(Settings(data_dir=tmp_path), repo, worker=_Worker(), model=_Model())
    return runner, repo


@pytest.mark.asyncio
async def test_taskrunner_releases_completed_jobs_and_terminal_history(tmp_path):
    runner, repo = _runner(tmp_path)

    async def immediate(_task_id):
        return None

    run = runner._run
    runner._run = immediate
    for index in range(100):
        runner.schedule(f"job-{index}")
    await asyncio.gather(*list(runner.jobs.values()))
    await asyncio.sleep(0)
    assert runner.jobs == {}
    runner._run = run

    record = repo.create(TaskSpec(goal="Finish", allowed_origins=["https://shop.example"],
                                  success_conditions=[{"kind": "url_is", "url": "https://shop.example/"}],
                                  idempotency_key="terminal-0001", sync_cookies=False))
    repo.transition(record.task_id, TaskStatus.RUNNING)
    runner.histories[record.task_id] = [{"old": "context"}]
    await runner._run(record.task_id)
    assert repo.get(record.task_id).status == TaskStatus.SUCCEEDED
    assert record.task_id not in runner.histories
    await runner.close()
    repo.close()


def test_history_retention_is_bounded_and_does_not_replay_truncated_actions(tmp_path):
    runner, repo = _runner(tmp_path)
    for index in range(40):
        runner._remember("paused-task", {"fact": f"fact-{index}"})
    assert len(runner.histories["paused-task"]) == 4
    assert runner.histories["paused-task"][0] == {"fact": "fact-36"}

    small = {"model_action": {"kind": "click", "x": 10}, "url": "https://shop.example/", "outcome": "applied"}
    runner._remember("paused-task", small)
    assert runner.histories["paused-task"][-1] == small
    large = {"model_action": {"kind": "type", "text": "x" * (HISTORY_ITEM_BYTES * 4)},
             "url": "https://shop.example/", "outcome": "applied"}
    runner._remember("paused-task", large)
    retained = runner.histories["paused-task"][-1]
    assert retained["context_truncated"] is True
    assert retained["model_action"] == {"kind": "type", "omitted": True}
    assert "text" not in retained["model_action"]
    repo.close()


def test_history_keeps_four_facts_and_replays_memorize_actions_after_many_clicks(tmp_path):
    runner, repo = _runner(tmp_path)
    task_id = "long-task"
    for index in range(4):
        text = f"Fact {index}: the site displays a distinct identifier."
        runner._remember(task_id, {"model_action": {"kind": "remember_fact", "text": text},
                                  "outcome": "applied", "fact": text, "source": "https://shop.example/"})
    for index in range(12):
        runner._remember(task_id, {"model_action": {"kind": "click", "x": index, "y": 20,
                                                      "button": "left", "count": 1},
                                  "url": "https://shop.example/", "outcome": "applied"})

    history = runner.histories[task_id]
    facts = [entry for entry in history if "fact" in entry]
    actions = [entry for entry in history if entry.get("outcome") == "applied" and "fact" not in entry]
    assert len(history) == HISTORY_ITEMS == 12
    assert [entry["fact"] for entry in facts] == [f"Fact {index}: the site displays a distinct identifier."
                                                   for index in range(4)]
    assert all(entry["model_action"]["kind"] == "remember_fact" for entry in facts)
    assert len(actions) == 8
    assert len(json.dumps(history, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= 36 * 1024
    messages = _history_messages(history)
    remembered = [message["content"] for message in messages if message["role"] == "assistant"
                  and "pause_and_memorize_fact" in message["content"]]
    assert len(remembered) == 4
    repo.close()


@pytest.mark.asyncio
async def test_chrome_broker_rejects_background_overload_without_leaking_coroutine(tmp_path):
    broker = ChromeBroker(tmp_path, importer=object())
    event = asyncio.Event()
    for _ in range(MAX_BACKGROUND):
        broker.spawn(event.wait())
    overflow = event.wait()
    with pytest.raises(DomainError) as error:
        broker.spawn(overflow)
    assert error.value.code == "RESOURCE_LIMIT"
    assert inspect.getcoroutinestate(overflow) == inspect.CORO_CLOSED
    await broker.close()


@pytest.mark.asyncio
async def test_chrome_broker_bounds_pending_calls_and_coalesces_site_refresh(tmp_path):
    broker = ChromeBroker(tmp_path, importer=object())
    source = {"source_id": "source", "socket": object(), "inventory": None}
    broker.sources["source"] = source
    for index in range(MAX_PENDING):
        broker.pending[str(index)] = ("source", asyncio.get_running_loop().create_future())
    with pytest.raises(DomainError) as error:
        await broker.ask("source", "open_sites", {})
    assert error.value.code == "RESOURCE_LIMIT"
    assert len(broker.pending) == MAX_PENDING
    for _, future in broker.pending.values():
        future.cancel()
    broker.pending.clear()

    source["inventory"] = {"status": "pending", "discovery_revision": "same", "sites": []}
    first = broker.refresh_sites("source")
    second = broker.refresh_sites("source")
    assert first == second == {"request_id": "same"}
    assert not broker.background


def test_chrome_catalogs_expire_cookie_metadata_and_active_catalogs_are_not_evicted(tmp_path):
    broker = ChromeBroker(tmp_path, importer=object(), catalog_ttl_s=1)
    now = time.monotonic()
    broker.catalogs["expired"] = {"catalog_id": "expired", "source_id": "source", "revision": "r",
                                   "discovery_revision": "d", "status": "ready", "sites": [{"secret": "site"}],
                                   "cookies": [{"name": "sid", "source_key": "private"}], "created_at": now - 5}
    public = broker.get_catalog("expired")
    assert public["status"] == "expired"
    assert public["cookies"] == []
    assert "sites" not in public

    broker.sources["source"] = {"source_id": "source", "inventory": {"status": "ready",
        "discovery_revision": "d", "sites": [{"site_id": "site", "origin": "https://example.com",
        "store_id": "1", "tab_count": 1}]}}
    for index in range(MAX_CATALOGS):
        broker.catalogs[f"active-{index}"] = {"status": "pending", "created_at": now, "sites": [], "cookies": []}
    with pytest.raises(DomainError) as error:
        broker.catalog("source", ["site"], "d")
    assert error.value.code == "CATALOG_LIMIT"


def test_import_receipt_limit_preserves_idempotency_keys(tmp_path):
    broker = ChromeBroker(tmp_path, importer=object())
    broker.import_keys = {f"key-{index}": ("fingerprint", "job") for index in range(MAX_IMPORT_RECEIPTS)}
    selection = {"source_id": "source", "discovery_revision": "d", "catalog_id": "c", "catalog_revision": "r",
                 "selected_site_ids": ["site"], "selected_cookie_ids": ["cookie"],
                 "conflict_mode": "replace_selected_matches", "idempotency_key": "new-key-0001"}
    with pytest.raises(DomainError) as error:
        broker.import_job("default", selection)
    assert error.value.code == "IMPORT_RECEIPT_LIMIT"
    assert len(broker.import_keys) == MAX_IMPORT_RECEIPTS


def test_import_idempotency_receipt_replays_without_spawning_another_import(tmp_path):
    broker = ChromeBroker(tmp_path, importer=object())
    selection = {"source_id": "source", "discovery_revision": "d", "catalog_id": "c", "catalog_revision": "r",
                 "selected_site_ids": ["site"], "selected_cookie_ids": ["cookie"],
                 "conflict_mode": "replace_selected_matches", "idempotency_key": "repeat-key-0001"}
    fingerprint = digest({"profile": "default", "selection": selection})
    receipt = {"job_id": "original", "profile_id": "default", "status": "done"}
    broker.import_keys[selection["idempotency_key"]] = (fingerprint, "original")
    broker.imports["original"] = receipt
    assert broker.import_job("default", selection) is receipt
    assert broker.background == set()

    changed = {**selection, "selected_site_ids": ["other"]}
    with pytest.raises(DomainError) as error:
        broker.import_job("default", changed)
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    assert len(broker.import_keys) == 1


def test_auth_service_caps_live_sessions_and_login_attempt_peers():
    auth = AuthService("operator-secret")
    now = time.monotonic()
    auth.sessions = {f"session-{index}": type("Session", (), {"expires_at": now + 100, "csrf_token": "x", "local": False, "last_access": now})()
                     for index in range(MAX_SESSIONS)}
    with pytest.raises(DomainError) as error:
        auth.create_session()
    assert error.value.code == "SESSION_LIMIT"
    assert len(auth.sessions) == MAX_SESSIONS
    with pytest.raises(DomainError) as error:
        auth.create_session(local=True)
    assert error.value.code == "SESSION_LIMIT"
    assert len(auth.sessions) == MAX_SESSIONS

    auth.sessions = {f"local-{index}": type("Session", (), {"expires_at": now + 100, "csrf_token": "x", "local": True, "last_access": now + index})()
                     for index in range(MAX_SESSIONS)}
    auth.create_session(local=True)
    assert len(auth.sessions) == MAX_SESSIONS
    assert "local-0" not in auth.sessions
    assert "local-63" in auth.sessions

    auth.attempts = {f"peer-{index}": [now] for index in range(MAX_ATTEMPT_PEERS)}
    with pytest.raises(DomainError) as error:
        auth.login("wrong", "new-peer")
    assert error.value.code == "RATE_LIMITED"
    assert len(auth.attempts) == MAX_ATTEMPT_PEERS
