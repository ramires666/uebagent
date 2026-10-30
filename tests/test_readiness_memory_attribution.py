import base64
import json

import httpx
import pytest

from scripts import readiness_memory_attribution as attribution_module
from scripts.readiness_memory_attribution import Attribution, VARIANTS, parse_args


class FakeClock:
    def __init__(self):
        self.value = 0.0
        self.sleeps = []

    def now(self):
        value = self.value
        self.value += 0.001
        return value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds


def make_handler(*, config=None, time_source=None, memory_report=None, gc_receipt=None):
    state = {"epoch": 0, "owner": "none", "human_gate_times": [],
             "memory_report_timeout": None, "periodic_gc_timeout": None}
    calls = []
    png = b"\x89PNG\r\n\x1a\nfixture"
    encoded = base64.b64encode(png).decode("ascii")
    config = config or {"trace_snapshots_enabled": True,
                        "default_addons_enabled": True, "backend": "camoufox"}
    memory_report = memory_report or {
        "schema": "readiness-memory-report/v1", "intrusive": True,
        "page_restored": True,
        "processes": [{"kind": "main", "metrics": {"resident": 1048576,
                                                          "heap_allocated": 524288}}],
    }
    gc_receipt = {"requested": True} if gc_receipt is None else gc_receipt

    def handler(request):
        path = request.url.path
        calls.append((request.method, path))
        if path == "/diagnostics/readiness-memory-config":
            return httpx.Response(200, json=config)
        if path == "/diagnostics/readiness-memory-report":
            assert request.method == "POST"
            assert json.loads(request.read()) == {}
            state["memory_report_timeout"] = request.extensions.get("timeout", {}).get("read")
            return httpx.Response(200, json=memory_report)
        if path == "/diagnostics/readiness-memory-gc":
            assert request.method == "POST"
            assert json.loads(request.read()) == {}
            state["periodic_gc_timeout"] = request.extensions.get("timeout", {}).get("read")
            return httpx.Response(200, json=gc_receipt)
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
    assert "/diagnostics/readiness-memory-report" not in [path for _method, path in calls]


def test_memory_report_calls_once_after_workload_and_exports_only_allowlisted_metrics():
    clock = FakeClock()
    report_data = {
        "schema": "readiness-memory-report/v1", "intrusive": True,
        "page_restored": True,
        "processes": [
            {"kind": "main", "metrics": {"resident": 2**20, "heap_allocated": 2**18}},
            {"kind": "web_content", "metrics": {"explicit": 2**19, "images": 4096}},
        ],
    }
    handler, calls, state, _png_size = make_handler(time_source=clock, memory_report=report_data)
    events = []
    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        report = Attribution(client, "key", "baseline", memory_report=True,
                             clock=clock.now, sleep=clock.sleep, progress=events.append,
                             _cycle_count=2, _warmup_seconds=0, _settle_seconds=15,
                             _operation_interval=0.15, _deadline_seconds=40).run()
    finally:
        client.close()

    paths = [path for _method, path in calls]
    assert report["status"] == "DIAGNOSTIC"
    assert paths.count("/diagnostics/readiness-memory-report") == 1
    assert paths.index("/diagnostics/readiness-memory-report") > max(
        i for i, path in enumerate(paths) if path == "/observe")
    event_names = [json.loads(event).get("event") for event in events]
    assert event_names.index("workload_end") < event_names.index("memory_report_start")
    assert event_names.index("memory_report_end") < event_names.index("settle_end")
    assert report["memory_report_enabled"] is True
    assert report["memory_report_requested"] is True
    assert report["memory_report_intrusive"] is True
    assert report["memory_report"] == report_data
    assert report["memory_report_started_at_utc"]
    assert report["memory_report_ended_at_utc"]
    assert "key" not in json.dumps(report)
    assert state["memory_report_timeout"] == 25.0


def test_memory_report_bad_metrics_fails_with_fixed_code():
    clock = FakeClock()
    invalid_report = {
        "schema": "readiness-memory-report/v1", "intrusive": True,
        "page_restored": True,
        "processes": [{"kind": "main", "metrics": {"resident": 1024**4 + 1}}],
    }
    handler, calls, _state, _png_size = make_handler(memory_report=invalid_report)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        report = Attribution(client, "key", "baseline", memory_report=True,
                             clock=clock.now, sleep=clock.sleep,
                             progress=lambda _event: None, _cycle_count=2,
                             _warmup_seconds=0, _settle_seconds=0,
                             _operation_interval=0.15, _deadline_seconds=40).run()
    finally:
        client.close()
    assert report["status"] == "FAIL"
    assert report["error_codes"] == ["memory_report_invalid"]
    assert report["cycles_completed"] == 2
    assert report["memory_report"] is None
    assert report["failed_stage"] == "memory_report"
    assert [path for _method, path in calls].count("/diagnostics/readiness-memory-report") == 1


def test_memory_report_guard_allows_only_baseline_without_gc(tmp_path, monkeypatch, capsys):
    with pytest.raises(ValueError, match="memory_report_requires_baseline_without_gc"):
        Attribution(httpx.Client(), "key", "no_trace", memory_report=True)
    with pytest.raises(ValueError, match="memory_report_requires_baseline_without_gc"):
        Attribution(httpx.Client(), "key", "baseline", memory_report=True, gc_contrast=True)

    monkeypatch.setattr(attribution_module, "validate_environment",
                        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("guard must run first")))
    result = attribution_module.main([
        "--variant", "no_addons", "--memory-report", "--i-confirm-disposable",
        "--key-file", str(tmp_path / "key"), "--output", str(tmp_path / "report.json"),
    ])
    assert result == 2
    assert json.loads(capsys.readouterr().out)["status"] == "BLOCKED"

    result = attribution_module.main([
        "--variant", "baseline", "--gc-contrast", "--memory-report",
        "--i-confirm-disposable", "--key-file", str(tmp_path / "key"),
        "--output", str(tmp_path / "other.json"),
    ])
    assert result == 2
    assert json.loads(capsys.readouterr().out)["status"] == "BLOCKED"
    result = attribution_module.main([
        "--variant", "baseline", "--periodic-gc", "--gc-contrast",
        "--i-confirm-disposable", "--key-file", str(tmp_path / "key"),
        "--output", str(tmp_path / "periodic-gc-contrast.json"),
    ])
    assert result == 2
    assert json.loads(capsys.readouterr().out)["status"] == "BLOCKED"
    result = attribution_module.main([
        "--variant", "baseline", "--periodic-gc", "--memory-report",
        "--i-confirm-disposable", "--key-file", str(tmp_path / "key"),
        "--output", str(tmp_path / "periodic-gc-memory-report.json"),
    ])
    assert result == 2
    assert json.loads(capsys.readouterr().out)["status"] == "BLOCKED"
    incompatible = parse_args(["--variant", "baseline", "--gc-contrast", "--memory-report",
                               "--i-confirm-disposable"])
    assert incompatible.memory_report and incompatible.gc_contrast


def test_cli_exposes_four_variants_and_no_custom_cycle_count():
    assert VARIANTS == ("baseline", "no_trace", "no_addons", "neither")
    args = parse_args(["--variant", "baseline", "--i-confirm-disposable"])
    assert args.variant == "baseline"
    assert args.memory_report is False
    assert args.periodic_gc is False
    with pytest.raises(SystemExit):
        parse_args(["--variant", "baseline", "--cycles", "2", "--i-confirm-disposable"])
    with pytest.raises(SystemExit):
        parse_args(["--variant", "baseline", "--periodic-gc-interval", "2",
                    "--i-confirm-disposable"])


def test_variant_expected_flag_pairs_are_a_two_by_two_design():
    assert attribution_module.EXPECTED_FLAGS == {
        "baseline": (True, True), "no_trace": (False, True),
        "no_addons": (True, False), "neither": (False, False),
    }


def test_periodic_gc_requests_once_at_fixed_internal_interval_without_extra_idle():
    clock = FakeClock()
    handler, calls, state, _png_size = make_handler(time_source=clock)
    events = []
    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        report = Attribution(client, "key", "baseline", periodic_gc=True,
                             clock=clock.now, sleep=clock.sleep, progress=events.append,
                             _cycle_count=2, _periodic_gc_interval=2, _warmup_seconds=0,
                             _settle_seconds=15, _operation_interval=0.15,
                             _deadline_seconds=40).run()
    finally:
        client.close()

    gc_calls = [(method, path) for method, path in calls
                if path == "/diagnostics/readiness-memory-gc"]
    assert report["status"] == "DIAGNOSTIC"
    assert report["cycles_completed"] == 2
    assert gc_calls == [("POST", "/diagnostics/readiness-memory-gc")]
    assert report["periodic_gc"]["enabled"] is True
    assert report["periodic_gc"]["interval_cycles"] == 2
    assert report["periodic_gc"]["request_count"] == 1
    request = report["periodic_gc"]["requests"][0]
    assert request["after_cycle"] == 2 and request["requested"] is True
    assert request["error_code"] is None
    assert request["started_at_utc"] and request["ended_at_utc"]
    assert request["elapsed_ms"] >= 0
    assert state["periodic_gc_timeout"] == 5.0
    assert clock.sleeps[-3:] == [15, 2, 8]
    event_names = [json.loads(event)["event"] for event in events]
    assert event_names.index("periodic_gc_end") < event_names.index("workload_end")


def test_periodic_gc_bad_receipt_fails_at_fixed_stage_and_modes_are_exclusive():
    with pytest.raises(ValueError, match="periodic_gc_requires_baseline_without_other_diagnostics"):
        Attribution(httpx.Client(), "key", "baseline", periodic_gc=True, gc_contrast=True)
    with pytest.raises(ValueError, match="periodic_gc_requires_baseline_without_other_diagnostics"):
        Attribution(httpx.Client(), "key", "baseline", periodic_gc=True, memory_report=True)
    with pytest.raises(ValueError, match="periodic_gc_requires_baseline_without_other_diagnostics"):
        Attribution(httpx.Client(), "key", "no_trace", periodic_gc=True)

    clock = FakeClock()
    handler, calls, _state, _png_size = make_handler(time_source=clock, gc_receipt={"requested": 1})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        report = Attribution(client, "key", "baseline", periodic_gc=True,
                             clock=clock.now, sleep=clock.sleep, progress=lambda _event: None,
                             _cycle_count=2, _periodic_gc_interval=2, _warmup_seconds=0,
                             _settle_seconds=0, _operation_interval=0.15,
                             _deadline_seconds=40).run()
    finally:
        client.close()
    assert report["status"] == "FAIL"
    assert report["error_codes"] == ["periodic_gc_invalid_receipt"]
    assert report["failed_stage"] == "periodic_gc"
    assert report["periodic_gc"]["request_count"] == 1
    assert report["periodic_gc"]["requests"][0]["requested"] is False
    assert [path for _method, path in calls].count("/diagnostics/readiness-memory-gc") == 1
