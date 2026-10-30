from __future__ import annotations

from scripts.demoqa_task_spec import browser_task_start_payload, result_exit_code


def test_public_synthetic_task_does_not_request_production_cookie_sync():
    conditions = [{"kind": "element_text_contains", "selector": "#output", "text": "Test Person"}]
    payload = browser_task_start_payload(
        goal="Fill the synthetic public form",
        origin="https://demoqa.com",
        idempotency_key="demoqa-contract-0001",
        timeout_s=120,
        max_steps=30,
        success_conditions=conditions,
    )

    assert payload["sync_cookies"] is False
    assert payload["allowed_origins"] == ["https://demoqa.com"]
    assert payload["success_conditions"] == conditions
    assert payload["limits"] == {"timeout_s": 120, "max_steps": 30}
    assert not {"cookies", "cookie_values", "session_data"}.intersection(payload)


def test_runner_exit_code_fails_for_empty_or_nonpassing_scenario_results():
    assert result_exit_code([{"status": "passed"}, {"status": "passed"}]) == 0
    assert result_exit_code([]) == 1
    assert result_exit_code([{"status": "passed"}, {"status": "failed"}]) == 1
    assert result_exit_code([{"status": "inconclusive"}]) == 1
