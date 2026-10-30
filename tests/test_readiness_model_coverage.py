import sys
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import readiness_model_coverage as coverage


def test_two_distinct_sites_and_no_demoqa_selection_or_ebay_typing():
    dqa = coverage.SCENARIOS["demoqa_checkbox_readonly"]
    ebay = coverage.SCENARIOS["ebay_guest_search"]
    assert dqa["origin"] != ebay["origin"]
    assert dqa["conditions"][-1] == {
        "kind": "element_checked", "selector": '[role="checkbox"][aria-label="Select Home"]', "checked": False}
    assert dqa["negative_conditions"] == [{"kind": "text_present", "text": "You have selected"}]
    assert "Do not sign in" in ebay["goal"] and "do not type" in ebay["goal"].lower()
    assert "https://www.ebay.com/sch/i.html?_nkw=notebook" in ebay["goal"]


def _run_fake_ebay(urls, guest_on_home=True, trace=None, challenge=False):
    class FakeClient:
        owned_task_id = None

        def __init__(self):
            self.urls = list(urls)
            self.current = urls[0]
            self.cancelled = False
            self.preflight = True
            self.task_status = "running"

        def ensure_no_active_tasks(self):
            pass

        def worker_status(self):
            if self.preflight:
                self.preflight = False
                return {"url": self.current}
            self.current = self.urls.pop(0) if len(self.urls) > 1 else self.urls[0]
            return {"url": self.current}

        def mcp(self, name, args):
            if name == "browser_task_start":
                return {"task_id": "12345678-1234-1234-1234-123456789abc", "status": "running"}
            if name == "browser_task_status":
                status = self.task_status
                if self.current == coverage.EBAY + "/sch/i.html?_nkw=notebook" and len(self.urls) == 1:
                    status = self.task_status = "succeeded"
                return {"task_id": args["task_id"], "status": status,
                        "phase": "verified" if status == "succeeded" else "running",
                        "result": {"verified": status == "succeeded"}}
            if name == "browser_task_cancel":
                self.cancelled = True
                self.task_status = "cancelled"
                return {"status": "cancelled"}
            raise AssertionError(name)

        def worker_verify(self, conditions):
            flags = []
            for condition in conditions:
                if condition["kind"] == "url_is":
                    flags.append(self.current == condition["url"])
                elif condition["kind"] == "text_present":
                    text = condition["text"]
                    if text in coverage.CHALLENGES:
                        flags.append(challenge)
                    elif text == "Sign in or register":
                        flags.append(guest_on_home and self.current == coverage.EBAY + "/")
                    elif text == "notebook":
                        flags.append(self.current.endswith("?_nkw=notebook"))
                    else:
                        flags.append(False)
                elif condition["kind"] == "element_visible":
                    flags.append("/itm/" in self.current and condition["selector"] == "h1.x-item-title__mainTitle")
            return {"checks": [{"passed": flag} for flag in flags]}

    client = FakeClient()
    trace = trace or ["navigate", "navigate", "click", "history_back", "finish"]
    with patch.object(coverage, "validate_worker"), patch.object(
            coverage, "action_trace", return_value=(trace, True, "type_text" in trace)):
        result = coverage.run_one(client, "ebay_guest_search", coverage.SCENARIOS["ebay_guest_search"],
                                  timeout_s=1, sleep=lambda _: None)
    return result, client


def test_ebay_guest_marker_must_be_seen_on_home_before_first_search_visit():
    home, search, item, search = [coverage.EBAY + path for path in (
        "/", "/sch/i.html?_nkw=notebook", "/itm/123", "/sch/i.html?_nkw=notebook")]
    result, _ = _run_fake_ebay([home, search, item, search], guest_on_home=False)
    assert result["signed_out_verified_before_search"] is False
    assert result["status"] == "FAIL"


def test_ebay_requires_observed_item_then_back_not_search_only():
    home = coverage.EBAY + "/"
    search = coverage.EBAY + "/sch/i.html?_nkw=notebook"
    result, _ = _run_fake_ebay([home, search, search])
    assert result["milestone_independently_verified"] is True
    assert result["item_independently_verified"] is False
    assert result["status"] == "FAIL"


def test_ebay_passes_only_with_public_item_observation_and_back_trace():
    home = coverage.EBAY + "/"
    search = coverage.EBAY + "/sch/i.html?_nkw=notebook"
    item = coverage.EBAY + "/itm/123"
    result, _ = _run_fake_ebay([home, search, item, search])
    assert result["signed_out_verified_before_search"] is True
    assert result["item_independently_verified"] is True
    assert result["history_back_to_results_verified"] is True
    assert result["status"] == "PASS"
    assert result["task_id"] == "12345678-1234-1234-1234-123456789abc"
    assert result["limits"] == {"max_steps": 16, "timeout_s": 1}
    assert result["control_origin"] == "http://localhost:18081"
    assert result["prompt_provenance"] == "scripts/readiness_model_coverage.py:SCENARIOS"
    assert result["started_at"].endswith("+00:00") and result["finished_at"].endswith("+00:00")


def test_ebay_type_text_trace_fails_even_when_other_evidence_passes():
    home = coverage.EBAY + "/"
    search = coverage.EBAY + "/sch/i.html?_nkw=notebook"
    item = coverage.EBAY + "/itm/123"
    result, _ = _run_fake_ebay([home, search, item, search], trace=[
        "navigate", "navigate", "type_text", "click", "history_back", "finish"])
    assert result["read_only_actions_only"] is False
    assert result["status"] == "FAIL"


def test_challenge_cancels_owned_task_and_never_returns_pass():
    home = coverage.EBAY + "/"
    result, client = _run_fake_ebay([home, home], challenge=True)
    assert client.cancelled is True
    assert result["challenge_detected"] is True
    assert result["status"] == "BLOCKED"


def test_classification_requires_phase_milestone_final_and_expected_trace():
    args = dict(scenario="ebay_guest_search", task_status="succeeded", phase="verified",
                verified=True, milestone=True, final_ok=True,
                actions=["navigate", "click", "history_back"], challenge=False,
                error_code=None, presearch_guest=True, item_observed=True,
                history_back_verified=True)
    assert coverage.classify(**args) == "PASS"
    assert coverage.classify(**{**args, "phase": "running"}) == "FAIL"
    assert coverage.classify(**{**args, "milestone": False}) == "FAIL"
    assert coverage.classify(**{**args, "final_ok": False}) == "FAIL"
    assert coverage.classify(**{**args, "error_code": "UNKNOWN_TASK_ERROR"}) == "FAIL"
    assert coverage.classify(**{**args, "challenge": True}) == "BLOCKED"
    assert coverage.classify(**{**args, "actions": ["navigate", "type_text", "click", "history_back"]}) == "FAIL"


def test_waiting_human_is_cancelled_and_confirmed_before_next_scenario():
    class TaskClient:
        owned_task_id = None
        status = "waiting_human"
        cancellations = 0

        def mcp(self, name, args):
            if name == "browser_task_status":
                return {"status": self.status}
            if name == "browser_task_cancel":
                self.cancellations += 1
                self.status = "cancelled"
                return {"status": "cancelled"}
            raise AssertionError(name)

    client = TaskClient()
    starts = []

    def runner(current, name, spec):
        if name == "demoqa_checkbox_readonly":
            current.owned_task_id = "task-one"
            return {"scenario": name, "status": "PARTIAL", "task_status": "waiting_human"}
        assert current.owned_task_id is None
        starts.append(name)
        current.owned_task_id = "task-two"
        current.status = "succeeded"
        return {"scenario": name, "status": "PASS", "task_status": "succeeded"}

    rows = coverage.run_scenarios(client, list(coverage.SCENARIOS), runner=runner)
    assert len(rows) == 2
    assert rows[0]["task_status"] == "waiting_human"
    assert client.cancellations == 1
    assert starts == ["ebay_guest_search"]
    assert client.owned_task_id is None


def test_second_scenario_exception_keeps_first_result():
    class EmptyClient:
        owned_task_id = None

    calls = []

    def runner(client, name, spec):
        calls.append(name)
        if name == "ebay_guest_search":
            raise RuntimeError("private details must not leak")
        return {"scenario": name, "status": "PARTIAL", "evidence": "first preserved"}

    rows = coverage.run_scenarios(EmptyClient(), list(coverage.SCENARIOS), runner=runner)
    assert len(rows) == 2
    assert rows[0]["evidence"] == "first preserved"
    assert rows[1] == {"scenario": "ebay_guest_search", "status": "FAIL",
                       "error_code": "scenario_failed"}
    assert calls == list(coverage.SCENARIOS)
