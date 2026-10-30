from __future__ import annotations

import asyncio
import time

import pytest

from scripts.readiness_cookie_bridge import ORIGIN, SyntheticSocket, _cookie
from src.browser_mcp.chrome import ChromeBroker
from src.browser_mcp.domain import DomainError


class Repository:
    def __init__(self):
        self.items = []

    def events(self, task_id, after_event_id=0, limit=100):
        return self.items[after_event_id:after_event_id + limit]

    def add_event(self, task_id, event_type, payload):
        self.items.append({"event_id": len(self.items) + 1, "event_type": event_type, "payload": payload})


class Importer:
    async def can_import_for_task(self, task_id):
        return True

    async def import_cookies_for_task(self, task_id, job_id, cookies):
        assert len(cookies) == 1
        assert cookies[0]["domain"] == "readiness-fixture.test"
        return {"total": 1, "applied": 1, "failed": 0}


def _paired(broker, socket):
    paired = broker.pair(broker.pairing_code()["code"], "a" * 32, "fixture")
    source = broker.authenticate(paired["source_id"], paired["token"], "chrome-extension://" + "a" * 32)
    source["socket"], source["last_seen"] = socket, time.monotonic()
    return paired["source_id"]


def test_cookie_bridge_protocol_rejects_ambiguous_sources_before_contact(tmp_path):
    async def exercise():
        repo = Repository()
        broker = ChromeBroker(tmp_path, Importer(), repository=repo)
        left = SyntheticSocket(broker, "left", [])
        right = SyntheticSocket(broker, "right", [])
        _paired(broker, left)
        _paired(broker, right)
        with pytest.raises(DomainError) as error:
            await broker.sync_task_cookies("fixture-task", "default", [ORIGIN])
        assert error.value.code == "COOKIE_SOURCE_AMBIGUOUS"
        assert left.requests == right.requests == 0
        await broker.close()

    asyncio.run(exercise())


def test_synthetic_source_exports_only_exact_requested_origin_and_imports_one_cookie(tmp_path):
    async def exercise():
        repo = Repository()
        broker = ChromeBroker(tmp_path, Importer(), repository=repo)
        name = "readiness_bridge_012345abcdef"
        socket = SyntheticSocket(broker, "pending", [
            _cookie(name, "synthetic-only", "readiness-fixture.test"),
            _cookie("unrelated", "must-not-leave-source", "elsewhere.test"),
        ])
        source_id = _paired(broker, socket)
        socket.source_id = source_id
        counts = await broker.sync_task_cookies("fixture-task", "default", [ORIGIN])
        assert counts == {"total": 1, "applied": 1, "failed": 0}
        assert socket.requests == 1
        assert "synthetic-only" not in str(repo.items)
        assert "must-not-leave-source" not in str(repo.items)
        await broker.close()

    asyncio.run(exercise())
