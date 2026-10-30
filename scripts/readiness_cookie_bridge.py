"""Disposable synthetic-source integration for the Chrome-to-Camoufox cookie bridge."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
import shutil
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from browser_mcp.app import TaskCookieImporter
from browser_mcp.chrome import ChromeBroker
from browser_mcp.config import Settings
from browser_mcp.domain import DomainError, TaskSpec, TaskStatus
from browser_mcp.runtime import WorkerClient
from browser_mcp.storage import TaskRepository

ORIGIN = "https://readiness-fixture.test"
EXTENSION_ID = "a" * 32
SYNTHETIC_COOKIE_VALUE = "synthetic-only-cookie-value"


class CookieBridgeGuardError(RuntimeError):
    pass


class SyntheticSocket:
    def __init__(self, broker: ChromeBroker, source_id: str, rows: list[dict]):
        self.broker, self.source_id, self.rows = broker, source_id, rows
        self.requests = 0
        self.returned_count = 0
        self.excluded_unrelated_count = 0

    async def send_json(self, message: dict) -> None:
        self.requests += 1
        if message.get("kind") != "task_cookie_export" or message.get("payload") != {"origins": [ORIGIN]}:
            raise RuntimeError("unexpected source request")
        rows = [row for row in self.rows if row["domain"].lstrip(".") == urlsplit(ORIGIN).hostname]
        self.returned_count = len(rows)
        self.excluded_unrelated_count = len(self.rows) - len(rows)
        self.broker.receive(self.source_id, {"request_id": message["request_id"], "ok": True,
            "data": {"cookies": rows, "allowed_origins": [ORIGIN]}})


def _guard(args: argparse.Namespace) -> None:
    expected = {
        "APP_ENV": "test", "READINESS_DISPOSABLE": "1", "READINESS_RUNTIME_INTEGRATION": "1",
        "READINESS_ACTION_TRACE": "1", "BROWSER_WORKER_URL": "http://browser:7070",
    }
    if not args.i_confirm_disposable or any(os.environ.get(key) != value for key, value in expected.items()):
        raise CookieBridgeGuardError("DISPOSABLE_GUARD")
    if not Path("/.dockerenv").is_file():
        raise CookieBridgeGuardError("DOCKER_REQUIRED")
    if not args.key_file or not Path(args.key_file).is_file() or os.environ.get("WORKER_API_KEY_FILE") != args.key_file:
        raise CookieBridgeGuardError("WORKER_KEY_GUARD")
    if args.worker_url != "http://browser:7070":
        raise CookieBridgeGuardError("WORKER_URL_GUARD")


def _cookie(name: str, value: str, domain: str) -> dict:
    return {"source_key": f"fake-{name}-{domain}", "site_ids": [], "name": name, "domain": domain,
        "path": "/", "store_id": "0", "host_only": True, "secure": True, "http_only": False,
        "same_site": "lax", "expires_at": None, "partition_key": None, "unsupported_reason": None,
        "value": value}


def _worker_idle(state: dict) -> bool:
    return (state.get("browser_started") is True and state.get("control_owner") == "none"
            and state.get("action_in_progress") is False and state.get("import_in_progress") is False
            and state.get("context_count") == 1 and state.get("page_count") == 1
            and state.get("recovery_state") == "idle" and state.get("recovery_needed") is False
            and state.get("resource_violation") is False)


async def _post(client, path: str, body: dict | None = None):
    response = await client.post("http://browser:7070" + path, json=body or {})
    response.raise_for_status()
    return response.json()


async def _get(client, path: str):
    response = await client.get("http://browser:7070" + path)
    response.raise_for_status()
    return response.json()


async def _run(args: argparse.Namespace) -> dict:
    import httpx

    _guard(args)
    name = "readiness_bridge_" + secrets.token_hex(6)
    token = SYNTHETIC_COOKIE_VALUE
    key = Path(args.key_file).read_text(encoding="utf-8").strip()
    transport_client = httpx.AsyncClient(base_url=args.worker_url, headers={"Authorization": f"Bearer {key}"},
                                         timeout=15, trust_env=False)
    settings = Settings(data_dir=Path(tempfile.mkdtemp(prefix="readiness-cookie-")),
                        worker_url=args.worker_url, worker_key_file=args.key_file)
    settings.prepare()
    repository = TaskRepository(settings.data_dir / "tasks.sqlite3")
    worker = WorkerClient(settings)
    importer = TaskCookieImporter(repository, worker)
    broker = ChromeBroker(settings.data_dir, importer, repository=repository)
    report = {"schema": "readiness-cookie-bridge/v1", "status": "FAIL", "source_kind": "synthetic_extension_protocol",
              "fixture_origin": ORIGIN, "negative_source_codes": [], "exported_cookie_count": 0,
              "imported_cookie_count": 0, "unrelated_origin_excluded": False, "fixture_session_ready": False,
              "source_returned_cookie_count": 0, "source_excluded_unrelated_cookie_count": 0,
              "model_calls": 0, "browser_actions": 0, "cookie_values_recorded": False}
    try:
        await _post(transport_client, "/diagnostics/readiness-fixture", {"scenario": "cookie_bridge", "cookie_name": name})
        status = await _get(transport_client, "/status")
        if not _worker_idle(status):
            raise RuntimeError("WORKER_NOT_IDLE")
        record = repository.create(TaskSpec(goal="Import one synthetic fixture session cookie", allowed_origins=[ORIGIN],
                                            idempotency_key="cookie-bridge-" + secrets.token_hex(12)))
        repository.transition(record.task_id, TaskStatus.RUNNING)
        job = "cookiebridge-" + secrets.token_hex(8)

        # Both source-selection failures happen before source requests or worker imports.
        for expected in ("COOKIE_SOURCE_UNAVAILABLE",):
            try:
                await broker.sync_task_cookies(record.task_id, "default", [ORIGIN], job_id=job)
                raise RuntimeError("EXPECTED_SOURCE_REJECTION_MISSING")
            except DomainError as exc:
                if exc.code != expected:
                    raise RuntimeError("SOURCE_GUARD_FAILED") from None
                report["negative_source_codes"].append(exc.code)
        for index in range(2):
            pairing = broker.pairing_code()
            paired = broker.pair(pairing["code"], EXTENSION_ID, f"synthetic-{index}")
            source = broker.authenticate(paired["source_id"], paired["token"], f"chrome-extension://{EXTENSION_ID}")
            source["socket"] = SyntheticSocket(broker, paired["source_id"], [])
            source["last_seen"] = time.monotonic()
        try:
            await broker.sync_task_cookies(record.task_id, "default", [ORIGIN], job_id=job)
            raise RuntimeError("EXPECTED_AMBIGUITY_REJECTION_MISSING")
        except DomainError as exc:
            if exc.code != "COOKIE_SOURCE_AMBIGUOUS":
                raise RuntimeError("SOURCE_GUARD_FAILED") from None
            report["negative_source_codes"].append(exc.code)
        if any(source["socket"].requests for source in broker.sources.values()):
            raise RuntimeError("AMBIGUOUS_SOURCE_CONTACTED")

        broker.sources.clear()
        pairing = broker.pairing_code()
        paired = broker.pair(pairing["code"], EXTENSION_ID, "synthetic-normal-store")
        source = broker.authenticate(paired["source_id"], paired["token"], f"chrome-extension://{EXTENSION_ID}")
        rows = [_cookie(name, token, "readiness-fixture.test"), _cookie("unrelated", "excluded", "elsewhere.test")]
        socket = SyntheticSocket(broker, paired["source_id"], rows)
        source["socket"], source["last_seen"] = socket, time.monotonic()
        gate = await _post(transport_client, "/gate", {"owner": "agent"})
        epoch = gate["control_epoch"]
        observation = await _post(transport_client, "/observe")
        nav_receipt = await _post(transport_client, "/execute", {"action_id": "cookie-bridge-navigate-" + secrets.token_hex(6),
            "control_epoch": epoch, "observation_id": observation["observation_id"],
            "action": {"kind": "navigate", "url": ORIGIN + "/cookie_bridge"},
            "allowed_origins": [ORIGIN], "allow_write": False})
        if nav_receipt.get("outcome") != "applied":
            raise RuntimeError("NAVIGATION_OUTCOME_UNKNOWN")
        report["browser_actions"] = 1
        await _post(transport_client, "/gate", {"owner": "none"})
        verify = await _post(transport_client, "/verify", {"conditions": [{"kind": "element_text_contains",
            "selector": "#session-state", "text": "SESSION_MISSING"}]})
        if verify.get("verified") is not True:
            raise RuntimeError("FIXTURE_BASELINE_FAILED")

        receipt = await broker.sync_task_cookies(record.task_id, "default", [ORIGIN], job_id=job)
        report["exported_cookie_count"] = receipt["total"]
        report["imported_cookie_count"] = receipt["applied"]
        report["source_returned_cookie_count"] = socket.returned_count
        report["source_excluded_unrelated_cookie_count"] = socket.excluded_unrelated_count
        report["unrelated_origin_excluded"] = (socket.requests == 1 and socket.returned_count == 1
            and socket.excluded_unrelated_count == 1 and receipt == {"total": 1, "applied": 1, "failed": 0})
        gate = await _post(transport_client, "/gate", {"owner": "agent"})
        observation = await _post(transport_client, "/observe")
        nav_receipt = await _post(transport_client, "/execute", {"action_id": "cookie-bridge-verify-" + secrets.token_hex(6),
            "control_epoch": gate["control_epoch"], "observation_id": observation["observation_id"],
            "action": {"kind": "navigate", "url": ORIGIN + "/cookie_bridge"},
            "allowed_origins": [ORIGIN], "allow_write": False})
        if nav_receipt.get("outcome") != "applied":
            raise RuntimeError("NAVIGATION_OUTCOME_UNKNOWN")
        report["browser_actions"] = 2
        await _post(transport_client, "/gate", {"owner": "none"})
        verify = await _post(transport_client, "/verify", {"conditions": [{"kind": "element_text_contains",
            "selector": "#session-state", "text": "SESSION_READY"}]})
        report["fixture_session_ready"] = verify.get("verified") is True
        final_state = await _get(transport_client, "/status")
        events = repository.events(record.task_id, 0, 100)
        report["cookie_values_recorded"] = token in json.dumps(events, ensure_ascii=False)
        if (report["negative_source_codes"] != ["COOKIE_SOURCE_UNAVAILABLE", "COOKIE_SOURCE_AMBIGUOUS"]
                or not report["unrelated_origin_excluded"] or not report["fixture_session_ready"]
                or report["cookie_values_recorded"] or not _worker_idle(final_state)):
            raise RuntimeError("INTEGRATION_ASSERTION_FAILED")
        report["status"] = "PASS"
    finally:
        try:
            await _post(transport_client, "/gate", {"owner": "none"})
        except Exception:
            pass
        await broker.close()
        repository.close()
        await transport_client.aclose()
        shutil.rmtree(settings.data_dir, ignore_errors=True)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-url", default="http://browser:7070")
    parser.add_argument("--key-file", default=os.environ.get("WORKER_API_KEY_FILE", ""))
    parser.add_argument("--i-confirm-disposable", action="store_true", required=True)
    args = parser.parse_args()
    try:
        result = asyncio.run(_run(args))
    except CookieBridgeGuardError as exc:
        result = {"schema": "readiness-cookie-bridge/v1", "status": "BLOCKED",
                  "failure_code": str(exc), "source_kind": "synthetic_extension_protocol",
                  "cookie_values_recorded": False, "model_calls": 0}
    except Exception:
        result = {"schema": "readiness-cookie-bridge/v1", "status": "FAIL",
                  "failure_code": "INTEGRATION_FAILED", "source_kind": "synthetic_extension_protocol",
                  "cookie_values_recorded": False, "model_calls": 0}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "PASS" else (2 if result["status"] == "BLOCKED" else 1)


if __name__ == "__main__":
    raise SystemExit(main())
