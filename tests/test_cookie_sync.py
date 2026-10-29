import time

import pytest

from browser_mcp.chrome import ChromeBroker
from browser_mcp.domain import DomainError, TaskSpec
from browser_mcp.storage import TaskRepository


def cookie(**changes):
    record = {
        "source_key": '["0","SID",".example.test","/",false,""]',
        "site_ids": [],
        "name": "SID",
        "domain": ".example.test",
        "path": "/",
        "store_id": "0",
        "host_only": False,
        "secure": True,
        "http_only": True,
        "same_site": "lax",
        "expires_at": None,
        "partition_key": None,
        "unsupported_reason": None,
        "value": "synthetic-cookie-secret",
    }
    return {**record, **changes}


class Importer:
    def __init__(self):
        self.calls = []

    async def can_import_for_task(self, task_id):
        return bool(task_id)

    async def import_cookies_for_task(self, task_id, job_id, cookies):
        self.calls.append((task_id, job_id, len(cookies)))
        return {"total": len(cookies), "applied": len(cookies), "failed": 0}


def setup(tmp_path):
    repository = TaskRepository(tmp_path / "tasks.sqlite3")
    record = repository.create(TaskSpec(
        goal="Check the item",
        allowed_origins=["https://app.example.test"],
        idempotency_key="cookie-sync-test-1",
    ))
    importer = Importer()
    broker = ChromeBroker(tmp_path, importer, repository=repository)
    broker.sources["source"] = {"source_id": "source", "socket": object(), "last_seen": time.monotonic()}
    return broker, importer, repository, record.task_id


@pytest.mark.asyncio
async def test_task_cookie_sync_is_atomic_task_scoped_and_does_not_persist_values(tmp_path):
    broker, importer, repository, task_id = setup(tmp_path)
    requested = []

    async def ask(source_id, kind, payload):
        requested.append((source_id, kind, payload))
        return {"allowed_origins": ["https://app.example.test"], "cookies": [cookie()]}

    broker.ask = ask
    # Push the durable receipt beyond the old first-1,000-events scan window.
    for index in range(1_005):
        repository.add_event(task_id, "task.activity", {"stage": "observing", "step": index})
    counts = await broker.sync_task_cookies(task_id, "default", ["https://app.example.test"])
    assert counts == {"total": 1, "applied": 1, "failed": 0}
    assert requested == [("source", "task_cookie_export", {"origins": ["https://app.example.test"]})]
    assert importer.calls == [(task_id, f"taskcookie-{task_id}", 1)]
    events = repository.events(task_id, after_event_id=1_000, limit=1_000)
    sync_events = [event["event_type"] for event in events if event["event_type"].startswith("cookie.sync_")]
    assert sync_events == ["cookie.sync_started", "cookie.sync_completed"]
    assert next(event["event_id"] for event in events if event["event_type"] == "cookie.sync_completed") > 1_000
    assert "synthetic-cookie-secret" not in str(events)
    assert "https://app.example.test" not in str(events)

    # A repeated callback uses the durable completion event and does not export/import again.
    assert await broker.sync_task_cookies(task_id, "default", ["https://app.example.test"]) == counts
    assert len(requested) == 1 and len(importer.calls) == 1


@pytest.mark.asyncio
async def test_task_cookie_sync_uses_generation_scoped_idempotency_key(tmp_path):
    broker, importer, repository, task_id = setup(tmp_path)
    requested = []
    async def ask(source_id, kind, payload):
        requested.append((source_id, kind, payload))
        return {"allowed_origins": payload["origins"], "cookies": [cookie()]}
    broker.ask = ask
    scope = {"browser_instance_id": "runtime-instance-123", "recovery_generation": 4}
    job_id = f"taskcookie-{task_id}-r4-opaquehash"
    counts = await broker.sync_task_cookies(task_id, "default", ["https://app.example.test"],
        job_id=job_id, **scope)
    assert counts == {"total": 1, "applied": 1, "failed": 0}
    assert importer.calls == [(task_id, job_id, 1)]
    completed = next(event for event in repository.events(task_id)
                     if event["event_type"] == "cookie.sync_completed")
    assert completed["payload"]["job_id"] == job_id
    assert completed["payload"]["recovery_generation"] == 4
    assert len(completed["payload"]["browser_instance_id_hash"]) == 16
    assert scope["browser_instance_id"] not in str(completed)
    # A retry in this same browser incarnation reuses the receipt, while a
    # new recovery generation is allowed to import into its fresh context.
    assert await broker.sync_task_cookies(task_id, "default", ["https://app.example.test"],
        job_id=job_id, **scope) == counts
    next_scope = {"browser_instance_id": "runtime-instance-123", "recovery_generation": 5}
    next_job_id = f"taskcookie-{task_id}-r5-nextopaque"
    assert await broker.sync_task_cookies(task_id, "default", ["https://app.example.test"],
        job_id=next_job_id, **next_scope) == counts
    assert len(importer.calls) == 2
    assert len(requested) == 2
    assert "synthetic-cookie-secret" not in str(repository.events(task_id))


@pytest.mark.asyncio
async def test_task_cookie_sync_rejects_unsupported_or_out_of_scope_exports(tmp_path):
    broker, importer, repository, task_id = setup(tmp_path)

    async def unsupported(source_id, kind, payload):
        return {"allowed_origins": payload["origins"], "cookies": [cookie(partition_key={"topLevelSite": "https://example.test"})]}

    broker.ask = unsupported
    with pytest.raises(DomainError) as error:
        await broker.sync_task_cookies(task_id, "default", ["https://app.example.test"])
    assert error.value.code == "COOKIE_SYNC_UNSUPPORTED"
    assert repository.events(task_id)[-1]["payload"] == {"job_id": f"taskcookie-{task_id}", "code": "COOKIE_SYNC_UNSUPPORTED"}
    assert not importer.calls

    # A fresh task rejects a validly shaped cookie whose domain cannot reach the requested host.
    record = repository.create(TaskSpec(goal="Second", allowed_origins=["https://app.example.test"], idempotency_key="cookie-sync-test-2"))
    task_id = record.task_id
    broker.sources["source"]["last_seen"] = time.monotonic()

    async def outside_scope(source_id, kind, payload):
        return {"allowed_origins": payload["origins"], "cookies": [cookie(domain="other.test")]}

    broker.ask = outside_scope
    with pytest.raises(DomainError) as error:
        await broker.sync_task_cookies(task_id, "default", ["https://app.example.test"])
    assert error.value.code == "COOKIE_SYNC_FAILED"
    assert "synthetic-cookie-secret" not in str(repository.events(task_id))


@pytest.mark.asyncio
async def test_task_cookie_sync_requires_one_live_source_and_accepts_empty_export(tmp_path):
    broker, importer, repository, task_id = setup(tmp_path)
    broker.sources.clear()
    with pytest.raises(DomainError) as error:
        await broker.sync_task_cookies(task_id, "default", ["https://app.example.test"])
    assert error.value.code == "COOKIE_SOURCE_UNAVAILABLE"

    broker.sources["source"] = {"source_id": "source", "socket": object(), "last_seen": time.monotonic()}
    async def empty(source_id, kind, payload):
        return {"allowed_origins": payload["origins"], "cookies": []}
    broker.ask = empty
    assert await broker.sync_task_cookies(task_id, "default", ["https://app.example.test"]) == {
        "total": 0, "applied": 0, "failed": 0,
    }
    assert not importer.calls
    assert repository.events(task_id)[-1]["event_type"] == "cookie.sync_completed"
