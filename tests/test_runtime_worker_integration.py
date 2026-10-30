from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
SRC = ROOT / "src"
for path in (SCRIPTS, SRC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from readiness_runtime_integration import (
    INTEGRATION_FLAG,
    WORKER_URL,
    IntegrationGuardError,
    run_integration,
    validate_environment,
)


def test_runtime_integration_requires_explicit_disposable_worker(tmp_path):
    key_file = tmp_path / "worker-key"
    key_file.write_text("synthetic-worker-key", encoding="utf-8")
    valid = {
        "APP_ENV": "test", "READINESS_DISPOSABLE": "1", INTEGRATION_FLAG: "1",
        "BROWSER_WORKER_URL": WORKER_URL, "WORKER_API_KEY_FILE": str(key_file),
    }
    assert validate_environment(confirmed=True, environ=valid, docker_marker=True) == key_file
    for changed, expected in (
        (dict(valid, APP_ENV="production"), "disposable_test_markers_required"),
        (dict(valid, READINESS_DISPOSABLE="0"), "disposable_test_markers_required"),
        (dict(valid, **{INTEGRATION_FLAG: "0"}), "runtime_integration_opt_in_required"),
        (dict(valid, BROWSER_WORKER_URL="http://localhost:7070"), "worker_url_must_be_exact_disposable_service"),
    ):
        with pytest.raises(IntegrationGuardError, match=expected):
            validate_environment(confirmed=True, environ=changed, docker_marker=True)
    with pytest.raises(IntegrationGuardError, match="explicit_disposable_confirmation_required"):
        validate_environment(confirmed=False, environ=valid, docker_marker=True)
    with pytest.raises(IntegrationGuardError, match="container_only_execution_required"):
        validate_environment(confirmed=True, environ=valid, docker_marker=False)


@pytest.mark.asyncio
async def test_real_worker_taskrunner_inflight_cancel_isolated_opt_in():
    if os.environ.get(INTEGRATION_FLAG) != "1":
        pytest.skip("real worker integration requires explicit disposable-stack opt-in")
    report = await run_integration(confirmed=True)
    assert report["status"] == "PASS"
    assert report["queued_cancel"] == "CANCELLED"
    assert report["inflight_cancel"] == "RECOVERING"
    assert report["inflight_action_observed"] is True
    assert report["action_planned_count"] == 1
    assert report["action_resolved_count"] == 0
    assert report["scripted_model_calls"] == 1
    assert report["worker_ready_idle_after"] is True
    assert report["worker_identity_unchanged"] is True
