import base64
import json

import httpx

from scripts import readiness_memory_probe as probe_module
from scripts.readiness_memory_probe import PHASES, PHASE_SETS, Probe


def test_probe_runs_phases_in_order_and_releases_human_gate():
    state = {"epoch": 0, "owner": "none"}
    calls = []
    png = b"\x89PNG\r\n\x1a\nimage"
    encoded = base64.b64encode(png).decode("ascii")

    def handler(request):
        path = request.url.path
        calls.append((request.method, path))
        if path == "/diagnostics/readiness-fixture":
            return httpx.Response(200, json={"installed": True})
        if path == "/gate":
            body = json.loads(request.read())
            state["epoch"] += 1
            state["owner"] = body["owner"]
            return httpx.Response(200, json={"control_owner": state["owner"], "control_epoch": state["epoch"]})
        if path == "/manual/execute":
            body = json.loads(request.read())
            assert body["control_epoch"] == state["epoch"]
            assert state["owner"] == "human"
            assert body["action"] == {"kind": "navigate", "url": "https://readiness-fixture.test/tree"}
            state["epoch"] += 1
            return httpx.Response(200, json={"outcome": "applied", "control_owner": "human",
                                             "control_epoch": state["epoch"]})
        if path == "/screenshot":
            return httpx.Response(200, content=png)
        if path == "/observe":
            return httpx.Response(200, json={"screenshot_png_b64": encoded,
                "interaction_hints": {"version": 1, "coordinate_space": "normalized_1000", "elements": []}})
        if path == "/status":
            return httpx.Response(200, json={"context_count": 1, "page_count": 1,
                "control_owner": state["owner"], "action_in_progress": False,
                "import_in_progress": False, "resource_violation": False,
                "recovery_needed": False, "recovery_state": "idle"})
        raise AssertionError(path)

    now = [0.0]
    def clock():
        now[0] += 0.1
        return now[0]
    client = httpx.Client(transport=httpx.MockTransport(handler))
    sleeps = []
    runner = Probe(client, "key", phase_seconds=0.15, idle_seconds=5,
                   clock=clock, sleep=sleeps.append)
    try:
        report = runner.run()
    finally:
        client.close()
    assert report["status"] == "DIAGNOSTIC"
    assert [phase["name"] for phase in report["phases"]] == list(PHASES)
    assert [phase["operation_count"] for phase in report["phases"]] == [1, 1, 1]
    assert [path for method, path in calls if path in {"/manual/execute", "/screenshot", "/observe"}] == [
        "/manual/execute", "/manual/execute", "/screenshot", "/observe"]
    assert sleeps == [5, 5]
    assert state["owner"] == "none"


def test_mixed_profile_runs_verified_cycles_at_each_phase_dispatch():
    state = {"epoch": 0, "owner": "none"}
    actions, verifications, observations, gc_requests = [], [], [], []
    png = b"\x89PNG\r\n\x1a\nimage"
    encoded = base64.b64encode(png).decode("ascii")

    def handler(request):
        path = request.url.path
        if path == "/diagnostics/readiness-fixture":
            return httpx.Response(200, json={"installed": True})
        if path == "/gate":
            body = json.loads(request.read())
            state["epoch"] += 1
            state["owner"] = body["owner"]
            return httpx.Response(200, json={"control_owner": state["owner"],
                                             "control_epoch": state["epoch"]})
        if path == "/manual/execute":
            body = json.loads(request.read())
            assert body["control_epoch"] == state["epoch"]
            assert state["owner"] == "human"
            assert body["allowed_origins"] == ["https://readiness-fixture.test"]
            actions.append(body["action"])
            state["epoch"] += 1
            return httpx.Response(200, json={"outcome": "applied", "control_owner": "human",
                                             "control_epoch": state["epoch"]})
        if path == "/verify":
            body = json.loads(request.read())
            verifications.append(body["conditions"])
            assert body["conditions"] == [
                {"kind": "text_present", "text": "Selected: Desktop"},
                {"kind": "element_checked", "selector": "#desktop", "checked": True},
            ]
            return httpx.Response(200, json={"checks": [{"passed": True}, {"passed": True}]})
        if path == "/observe":
            assert state["owner"] == "none"
            observations.append(path)
            return httpx.Response(200, json={"screenshot_png_b64": encoded,
                "interaction_hints": {"version": 1, "coordinate_space": "normalized_1000", "elements": []}})
        if path == "/diagnostics/readiness-memory-gc":
            assert state["owner"] == "none"
            assert json.loads(request.read()) == {}
            gc_requests.append(path)
            return httpx.Response(200, json={"requested": True})
        if path == "/status":
            return httpx.Response(200, json={"context_count": 1, "page_count": 1,
                "control_owner": state["owner"], "action_in_progress": False,
                "import_in_progress": False, "resource_violation": False,
                "recovery_needed": False, "recovery_state": "idle"})
        raise AssertionError(path)

    now = [0.0]
    def clock():
        now[0] += 0.1
        return now[0]
    events = []
    client = httpx.Client(transport=httpx.MockTransport(handler))
    sleeps = []
    runner = Probe(client, "key", phase_seconds=0.15, idle_seconds=5,
                   clock=clock, sleep=sleeps.append,
                   progress=events.append, phase_set="mixed", gc_contrast=True)
    try:
        report = runner.run()
    finally:
        client.close()

    assert report["status"] == "DIAGNOSTIC"
    assert report["phase_set"] == "mixed"
    assert report["gc_contrast"]["requested"] is True
    assert report["gc_contrast"]["error_code"] is None
    assert [phase["name"] for phase in report["phases"]] == list(PHASE_SETS["mixed"])
    assert [phase["operation_count"] for phase in report["phases"]] == [1, 1, 1]
    assert [phase["image_bytes"] for phase in report["phases"]] == [len(png), 0, len(png)]
    assert [action["kind"] for action in actions].count("navigate") == 4  # includes one-time fixture navigation
    assert [action["kind"] for action in actions].count("click") == 4
    assert len(verifications) == 2
    assert len(observations) == 2
    assert gc_requests == ["/diagnostics/readiness-memory-gc"]
    assert state["owner"] == "none"
    assert all(json.loads(event)["phase_set"] == "mixed" for event in events)
    assert [json.loads(event)["event"] for event in events][-2:] == ["gc_start", "gc_end"]
    assert sleeps == [5, 5, 2, 8]


def test_cli_guard_rejection_is_blocked_without_network_or_report(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(probe_module, "validate_environment",
                        lambda **_kwargs: (_ for _ in ()).throw(ValueError("guard")))
    output = tmp_path / "report.json"
    result = probe_module.main(["--key-file", str(tmp_path / "key"), "--output", str(output),
                                "--duration-seconds", "210", "--i-confirm-disposable"])
    assert result == 2
    assert json.loads(capsys.readouterr().out)["status"] == "BLOCKED"
    assert not output.exists()


def test_cli_rejects_duration_outside_fixed_three_phase_budget(tmp_path, capsys):
    result = probe_module.main(["--key-file", str(tmp_path / "key"), "--output", str(tmp_path / "report.json"),
                                "--duration-seconds", "200", "--i-confirm-disposable"])
    assert result == 2
    assert json.loads(capsys.readouterr().out)["status"] == "BLOCKED"


def test_cli_selects_mixed_phase_set():
    args = probe_module.parse_args(["--i-confirm-disposable", "--phase-set", "mixed"])
    assert args.phase_set == "mixed"


def test_gc_contrast_runs_once_only_after_mixed_and_records_progress():
    calls, sleeps, events = [], [], []

    def handler(request):
        calls.append((request.method, request.url.path, request.read()))
        return httpx.Response(200, json={"requested": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    runner = Probe(client, "key", phase_seconds=0.15, idle_seconds=5,
                   clock=lambda: 0.0, sleep=sleeps.append, progress=events.append,
                   phase_set="mixed", gc_contrast=True)
    try:
        result = runner.gc_contrast()
    finally:
        client.close()
    assert result["requested"] is True
    assert result["error_code"] is None
    assert calls == [("POST", "/diagnostics/readiness-memory-gc", b"{}")]
    assert sleeps == [2, 8]
    assert [json.loads(event)["event"] for event in events] == ["gc_start", "gc_end"]
    assert all(json.loads(event)["phase_set"] == "mixed" for event in events)


def test_gc_contrast_failure_is_fixed_and_components_cannot_enable_it():
    def handler(_request):
        return httpx.Response(503, json={"detail": {"code": "GC_REQUEST_FAILED", "message": "secret"}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    sleeps = []
    runner = Probe(client, "key", phase_set="mixed", gc_contrast=True,
                   clock=lambda: 0.0, sleep=sleeps.append, progress=lambda _event: None)
    try:
        result = runner.gc_contrast()
    finally:
        client.close()
    assert result["requested"] is False
    assert result["error_code"] == "gc_contrast_failed"
    assert sleeps == [2, 8]
    with httpx.Client(transport=httpx.MockTransport(handler)) as unused:
        try:
            Probe(unused, "key", phase_set="components", gc_contrast=True)
        except ValueError as exc:
            assert str(exc) == "gc_contrast_requires_mixed_phase_set"
        else:
            raise AssertionError("components profile accepted GC contrast")


def test_gc_contrast_skips_when_a_mixed_phase_fails(monkeypatch):
    client = httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={})))
    runner = Probe(client, "key", phase_set="mixed", gc_contrast=True,
                   clock=lambda: 0.0, sleep=lambda _seconds: None,
                   progress=lambda _event: None)
    gc_calls = []
    monkeypatch.setattr(runner, "setup", lambda: None)
    monkeypatch.setattr(runner, "phase", lambda name: {
        "name": name, "error_codes": ["phase_failed"], "operation_count": 0,
    })
    monkeypatch.setattr(runner, "idle_status", lambda: {"control_owner": "none"})
    monkeypatch.setattr(runner, "gc_contrast", lambda: gc_calls.append(True))
    try:
        report = runner.run()
    finally:
        client.close()
    assert report["status"] == "FAIL"
    assert report["gc_contrast"] is None
    assert gc_calls == []
