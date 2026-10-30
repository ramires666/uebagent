import base64
import json

import httpx

from scripts import readiness_memory_probe as probe_module
from scripts.readiness_memory_probe import PHASES, Probe


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
            return httpx.Response(200, json={"outcome": "applied", "control_epoch": state["epoch"]})
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
