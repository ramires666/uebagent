from __future__ import annotations

from pathlib import Path
import http.server
import json
import os
import shutil
import subprocess
import threading

import yaml
import pytest


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy" / "compose.readiness-rollback.yaml"
SCRIPT = ROOT / "scripts" / "readiness_rollback.ps1"


def test_rollback_compose_uses_only_project_scoped_persistent_volumes_and_loopback_port():
    document = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    assert document["name"] == "browser-mcp-readiness-rollback"
    assert document["services"]["control"]["ports"] == ["127.0.0.1:18081:8080"]
    assert document["services"]["control"]["environment"]["BROWSER_MCP_ORIGIN"] == "http://localhost:18081"
    assert document["services"]["control"]["environment"]["MODEL_BASE_URL"] == "http://host.docker.internal:8080/v1"
    assert document["services"]["control"]["environment"]["MODEL_ALIAS"] == "auto"
    assert document["services"]["control"]["environment"]["MODEL_PROTOCOL"] == "generic"
    assert document["services"]["browser"]["environment"]["BROWSER_HEADLESS"] == "false"

    declared = document["volumes"]
    assert declared["control_state"]["name"] == "browser-mcp-readiness-rollback-control-state"
    assert declared["worker_state"]["name"] == "browser-mcp-readiness-rollback-worker-state"
    assert all("browser-mcp-local" not in row["name"] and "demoqa-test" not in row["name"]
               for row in declared.values())
    for service in document["services"].values():
        assert "build" not in service
        for mount in service.get("volumes", []):
            assert not mount.startswith((".", "/", "..")), "compose must not mount host paths"
    assert document["services"]["browser"]["volumes"] == ["worker_state:/app/runtime/worker-state"]
    assert document["services"]["control"]["volumes"] == ["control_state:/app/runtime/control-data"]
    assert document["services"]["control"]["tmpfs"] == ["/run/readiness-secrets:mode=0700"]


def test_rollback_script_runs_read_only_guards_before_stopping_exact_production_pair():
    script = SCRIPT.read_text(encoding="utf-8")
    main = script.index("# All safety gates are read-only")
    preflight = script.index("$result.phases.production_preflight = Invoke-Preflight", main)
    other_browser = script.index("Assert-NoOtherRunningBrowser", preflight)
    capture_images = script.index("$baselineControl = Get-ImageId", other_browser)
    stop_production = script.index("Invoke-Native 'docker' (@('stop') + $productionIds)", capture_images)
    assert main < preflight < other_browser < capture_images < stop_production
    assert "'--allow-suspended-tasks'" in script
    assert "$parts[1] -ne $ProductionProject" in script
    assert "Get-ExactProductionContainer 'control'" in script
    assert "Get-ExactProductionContainer 'browser'" in script


def test_rollback_script_reuses_volumes_and_never_removes_them_or_starts_production_with_test_stack():
    script = SCRIPT.read_text(encoding="utf-8")
    assert "@('down')" in script
    assert "@('down', '-v')" not in script
    assert "if ($isolatedGone)" in script
    assert "@('start') + $productionIds" in script
    assert "READINESS_WORKER_KEY" in script
    assert "RandomNumberGenerator" in script
    assert "profile_fingerprint_sha256" in script
    assert "production_task_state_before_sha256" in script
    assert "production_task_state_after_sha256" in script
    assert "task_status_counts = $taskStatusCounts" in script
    assert "taskPayload.tasks.Count -ne $check.task_count" in script
    assert "Write-Output (($result | ConvertTo-Json -Depth 8 -Compress))" in script


def _write_shims(directory: Path, *, ready: bool, multiple_control_ids: bool) -> Path:
    directory.mkdir()
    python = directory / "python.cmd"
    python.write_text(
        "@echo off\r\n"
        + f'echo {{"ready":{str(ready).lower()},"worker_idle":{str(ready).lower()},"tasks_idle":{str(ready).lower()},"active_task_statuses":[],"suspended_task_count":0,"suspended_task_statuses":[],"task_count":0}}\r\n'
        + "exit /b 0\r\n",
        encoding="ascii",
    )
    docker = directory / "docker.cmd"
    if multiple_control_ids:
        docker.write_text(
            "@echo off\r\n"
            '>>"%ROLLBACK_DOCKER_LOG%" echo %*\r\n'
            'echo %* | findstr /C:"com.docker.compose.service=control" >nul\r\n'
            "if not errorlevel 1 (\r\n  echo production-control-a\r\n  echo production-control-b\r\n)\r\n"
            "exit /b 0\r\n",
            encoding="ascii",
        )
    else:
        docker.write_text(
            "@echo off\r\n"
            '>>"%ROLLBACK_DOCKER_LOG%" echo %*\r\n'
            "exit /b 0\r\n",
            encoding="ascii",
        )
    return directory


def _powershell() -> str:
    executable = shutil.which("powershell.exe") or shutil.which("powershell")
    if not executable:
        pytest.skip("Windows PowerShell is required for the command-shim safety test")
    return executable


def _run_script(shims: Path, tmp_path: Path, *, production_url: str) -> subprocess.CompletedProcess:
    environment = os.environ.copy()
    environment["PATH"] = str(shims) + os.pathsep + environment.get("PATH", "")
    environment["ROLLBACK_DOCKER_LOG"] = str(tmp_path / "docker-calls.log")
    return subprocess.run(
        [
            _powershell(), "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT),
            "-ProductionControlUrl", production_url, "-ReportPath", str(tmp_path / "rollback-report.json"),
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_preflight_failure_uses_shim_and_never_stops_or_starts_containers(tmp_path):
    shims = _write_shims(tmp_path / "shims", ready=False, multiple_control_ids=False)
    result = _run_script(shims, tmp_path, production_url="http://127.0.0.1:8090")
    calls = (tmp_path / "docker-calls.log").read_text(encoding="ascii") if (tmp_path / "docker-calls.log").exists() else ""
    assert result.returncode == 1
    assert "stop" not in calls and "up" not in calls
    report = json.loads((tmp_path / "rollback-report.json").read_text(encoding="utf-8-sig"))
    assert report["status"] == "FAIL"


def test_multiple_production_container_ids_are_rejected_before_stop_or_up(tmp_path):
    shims = _write_shims(tmp_path / "shims", ready=True, multiple_control_ids=True)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            payload = {"authenticated": True} if self.path == "/api/v1/operator/session" else {"tasks": []}
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", "operator_session=synthetic; Path=/")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        result = _run_script(shims, tmp_path, production_url=base)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    log = tmp_path / "docker-calls.log"
    assert log.exists(), f"PowerShell exited before Docker shim invocation: stdout={result.stdout!r} stderr={result.stderr!r}"
    calls = log.read_text(encoding="ascii")
    assert result.returncode == 1
    assert "service=control" in calls
    assert "stop" not in calls and "up" not in calls
    report = json.loads((tmp_path / "rollback-report.json").read_text(encoding="utf-8-sig"))
    assert report["status"] == "FAIL"

