import base64
import json

import httpx
import pytest

from scripts import readiness_memory_attribution as attribution_module
from scripts.readiness_memory_attribution import Attribution, VARIANTS, parse_args


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def now(self):
        value = self.value
        self.value += 0.001
        return value

    def sleep(self, seconds):
        self.value += seconds


def make_handler(*, config=None, time_source=None):
    state = {"epoch": 0, "owner": "none", "human_gate_times": []}
    calls = []
    png = b"\x89PNG\r\n\x1a\nfixture"
    encoded = base64.b64encode(png).decode("ascii")
    config = config or {"trace_snapshots_enabled": True,
                        "default_addons_enabled": True, "backend": "camoufox"}

    def handler(request):
        path = request.url.path
        calls.append((request.method, path))
        if path == "/diagnostics/readiness-memory-config":
            return httpx.Response(200, json=config)
        if path == "/diagnostics/readiness-fixture":
            return httpx.Response(200, json={"installed": True})
        if path == "/gate":
            body = json.loads(request.read())
            if body["owner"] == "human" and time_source is not None:
                state["human_gate_times"].append(time_source.value)
            state["epoch"] += 1
            state["owner"] = body["owner"]
            return httpx.Response(200, json={"control_owner": state["owner"],
                                             "control_epoch": state["epoch"]})
        if path == "/manual/execute":
            body = json.loads(request.read())
            assert state["owner"] == "human"
            assert body["control_epoch"] == state["epoch"]
            assert body["allowed_origins"] == ["https://readiness-fixture.test"]
            state["epoch"] += 1
            return httpx.Response(200, json={"outcome": "applied", "control_owner": "human",
                                             "control_epoch": state["epoch"]})
        if path == "/verify":
            return httpx.Response(200, json={"checks": [{"passed": True}, {"passed": True}]})
        if path == "/observe":
            return httpx.Response(200, json={"screenshot_png_b64": encoded,
                "interaction_hints": {"version": 1, "coordinate_space": "normalized_1000",
                                      "elements": []}})
        if path == "/status":
            return httpx.Response(200, json={
                "context_count": 1, "page_count": 1, "control_owner": state["owner"],
                "action_in_progress": False, "import_in_progress": False,
                "resource_violation": False, "recovery_needed": False, "recovery_state": "idle",
                "worker_instance_id": "worker-id", "browser_instance_id": "browser-id",
                "page_id": "main", "recovery_generation": 3,
            })
        raise AssertionError(path)

    return handler, calls, state, len(png)


def test_variant_mismatch_blocks_before_fixture_or_cycles():
    handler, calls, _state, _png_size = make_handler(config={
        "trace_snapshots_enabled": True, "default_addons_enabled": True, "backend": "camoufox"})
    clock = FakeClock()
    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        report = Attribution(client, "secret-key", "no_trace", clock=clock.now,
                             sleep=clock.sleep, progress=lambda _line: None,
                             _cycle_count=2, _warmup_seconds=0, _settle_seconds=0,
                             _contrast_seconds=0, _deadline_seconds=30).run()
    finally:
        client.close()

    assert report["status"] == "FAIL"
    assert report["cycles_expected"] == 2
    assert report["cycles_completed"] == 0
    assert report["error_codes"] == ["variant_mismatch"]
    assert [path for _method, path in calls] == ["/diagnostics/readiness-memory-config"]
    assert "secret-key" not in json.dumps(report)


def test_fixed_cycle_workload_runs_two_internal_cycles_and_checks_identity():
    clock = FakeClock()
    handler, calls, state, png_size = make_handler(time_source=clock)
    events = []
    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        report = Attribution(client, "key", "baseline", clock=clock.now,
                             sleep=clock.sleep, progress=events.append,
                             _cycle_count=2, _warmup_seconds=0, _settle_seconds=15,
                             _contrast_seconds=10, _operation_interval=0.15,
                             _deadline_seconds=30).run()
    finally:
        client.close()

    assert report["status"] == "DIAGNOSTIC"
    assert report["cycles_expected"] == report["cycles_completed"] == 2
    assert report["image_sizes"] == {"count": 2, "total_bytes": png_size * 2,
                                     "max_bytes": png_size}
    assert report["invariants"] == {
        "configuration_matches": True, "identity_before_present": True,
        "identity_after_present": True, "identity_stable": True,
        "one_context_one_page": True, "recovery_generation_stable": True,
    }
    assert report["gc_control"] is True and report["gc_contrast"] is None
    assert report["settle_seconds"] == pytest.approx(15, abs=0.01)
    assert report["contrast_seconds"] == pytest.approx(10, abs=0.01)
    assert state["owner"] == "none"
    assert [json.loads(event).get("event") for event in events] == [
        "progress", "progress", "workload_end", "settle_end", "contrast_start", "contrast_end"]
    assert sum(path == "/manual/execute" for _method, path in calls) == 7
    cycle_start_gap = state["human_gate_times"][2] - state["human_gate_times"][1]
    assert 0.14 <= cycle_start_gap <= 0.16
    assert "/diagnostics/readiness-memory-gc" not in [path for _method, path in calls]


def test_cli_exposes_four_variants_and_no_custom_cycle_count():
    assert VARIANTS == ("baseline", "no_trace", "no_addons", "neither")
    args = parse_args(["--variant", "baseline", "--i-confirm-disposable"])
    assert args.variant == "baseline"
    with pytest.raises(SystemExit):
        parse_args(["--variant", "baseline", "--cycles", "2", "--i-confirm-disposable"])


def test_variant_expected_flag_pairs_are_a_two_by_two_design():
    assert attribution_module.EXPECTED_FLAGS == {
        "baseline": (True, True), "no_trace": (False, True),
        "no_addons": (True, False), "neither": (False, False),
    }
