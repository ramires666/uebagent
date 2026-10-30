"""Safe task specifications for synthetic, public DemoQA scenarios."""
from __future__ import annotations

from typing import Any


def browser_task_start_payload(
    *,
    goal: str,
    origin: str,
    idempotency_key: str,
    timeout_s: int,
    max_steps: int,
    success_conditions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build an isolated public-site task without importing cookies or session data."""
    return {
        "goal": goal,
        "allowed_origins": [origin],
        "execution_mode": "autonomous",
        "idempotency_key": idempotency_key,
        "limits": {"timeout_s": timeout_s, "max_steps": max_steps},
        "success_conditions": success_conditions,
        "sync_cookies": False,
    }


def result_exit_code(results: list[dict[str, Any]]) -> int:
    """Return success only when every requested scenario passed independently."""
    return 0 if results and all(item.get("status") == "passed" for item in results) else 1
