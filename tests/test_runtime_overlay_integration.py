from __future__ import annotations

import os
from pathlib import Path
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from readiness_overlay_integration import WORKER_URL, run_check


@pytest.mark.asyncio
async def test_task_runner_does_not_report_success_for_overlay_click(tmp_path):
    if not (os.environ.get("APP_ENV", "").lower() == "test"
            and os.environ.get("READINESS_DISPOSABLE") == "1"
            and os.environ.get("READINESS_RUNTIME_INTEGRATION") == "1"
            and os.environ.get("READINESS_ACTION_TRACE") == "1"
            and os.environ.get("BROWSER_WORKER_URL") == WORKER_URL
            and os.environ.get("WORKER_API_KEY_FILE")):
        pytest.skip("requires the explicit disposable overlay integration environment")
    result = await run_check(tmp_path, Path(os.environ["WORKER_API_KEY_FILE"]))
    assert result["status"] == "PASS"
