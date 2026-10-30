from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess

import httpx
import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "demoqa_model_preflight.py"
spec = importlib.util.spec_from_file_location("demoqa_model_preflight", MODULE_PATH)
assert spec and spec.loader
model_preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(model_preflight)

PREFLIGHT_PATH = ROOT / "scripts" / "demoqa_preflight.py"
preflight_spec = importlib.util.spec_from_file_location("demoqa_preflight", PREFLIGHT_PATH)
assert preflight_spec and preflight_spec.loader
production_preflight = importlib.util.module_from_spec(preflight_spec)
preflight_spec.loader.exec_module(production_preflight)


def test_model_preflight_accepts_selected_alias_and_checks_v1_models_path():
    requested: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, json={"data": [{"id": "other"}, {"id": "fara-browser"}]})

    assert model_preflight.check_model(
        "http://host.docker.internal:8080/v1/", "fara-browser", transport=httpx.MockTransport(respond)
    )
    assert requested == ["http://127.0.0.1:8080/v1/models"]


def test_model_preflight_accepts_auto_only_for_a_single_model_and_path_aliases():
    path_alias = r"W:\LAMA\Bonsai-demo\models\model 27B.gguf"
    body = {"data": [{"id": path_alias}]}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    assert model_preflight.check_model("http://localhost:8080/v1", "auto", transport=transport)
    assert model_preflight.check_model("http://localhost:8080/v1", path_alias, transport=transport)

    multiple = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"data": [{"id": path_alias}, {"id": "second"}]})
    )
    assert not model_preflight.check_model("http://localhost:8080/v1", "auto", transport=multiple)


def test_model_preflight_preserves_ipv6_brackets_and_port():
    assert model_preflight._model_url("http://[::1]:8080/v1") == "http://[::1]:8080/v1/models"


@pytest.mark.parametrize(
    "body",
    [
        {"data": [{"id": "other"}]},
        {"data": []},
        {"data": "invalid"},
        {"data": [{"id": "fara-browser"}, None]},
        {"data": [{"id": "fara-browser\n"}]},
    ],
)
def test_model_preflight_rejects_missing_or_invalid_served_alias(body):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    assert not model_preflight.check_model("http://localhost:8080/v1", "fara-browser", transport=transport)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://localhost:8080/v1",
        "http://user:secret@localhost:8080/v1",
        "http://localhost:8080/v1?token=secret",
        "http://localhost:8080/api",
    ],
)
def test_model_preflight_rejects_unsafe_or_wrong_endpoint_without_request(url):
    transport = httpx.MockTransport(lambda request: pytest.fail("invalid endpoint must not make a request"))
    assert not model_preflight.check_model(url, "fara-browser", transport=transport)


def test_disposable_compose_defaults_to_start_fara_port_and_accepts_override():
    compose = (ROOT / "deploy" / "compose.demoqa-test.yaml").read_text(encoding="utf-8")
    assert "${DEMOQA_MODEL_BASE_URL:-http://host.docker.internal:8080/v1}" in compose
    assert "8081" not in compose
    assert "${DEMOQA_MODEL_ALIAS:-fara-browser}" in compose
    assert "${DEMOQA_MODEL_PROTOCOL:-fara}" in compose
    assert "${DEMOQA_BROWSER_MEMORY_LIMIT:-2816m}" in compose


def test_runner_model_mismatch_exits_before_any_production_docker_action(tmp_path):
    powershell = shutil.which("powershell")
    if powershell is None:
        pytest.skip("Windows PowerShell is unavailable")

    docker_log = tmp_path / "docker-calls.txt"
    shim = tmp_path / "docker.cmd"
    shim.write_text(
        '@echo off\r\necho %*>>"%DOCKER_STUB_LOG%"\r\nexit /b 0\r\n', encoding="ascii"
    )
    env = os.environ.copy()
    env["PATH"] = str(tmp_path) + os.pathsep + env.get("PATH", "")
    env["DOCKER_STUB_LOG"] = str(docker_log)
    env["DEMOQA_MODEL_BASE_URL"] = "http://inherited.invalid:8080/v1"

    result = subprocess.run(
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "scripts" / "run-demoqa-tests.ps1"),
            "-ModelBaseUrl",
            "http://127.0.0.1:1/v1",
            "-SkipBuild",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert result.returncode != 0
    assert "production was left running" in (result.stdout + result.stderr).lower()
    calls = docker_log.read_text(encoding="ascii") if docker_log.exists() else ""
    assert "stop" not in calls.lower()
    assert "up" not in calls.lower()
    assert env["DEMOQA_MODEL_BASE_URL"] == "http://inherited.invalid:8080/v1"


@pytest.mark.parametrize(
    ("docker_ids", "expected"),
    [
        (["old-fixture-a", "old-fixture-b"], "True"),
        (["old-fixture-a", "new-run-browser"], "False"),
    ],
)
def test_container_cleanup_wait_ignores_only_preexisting_ids(tmp_path, docker_ids, expected):
    powershell = shutil.which("powershell")
    if powershell is None:
        pytest.skip("Windows PowerShell is unavailable")

    shim = tmp_path / "docker.cmd"
    shim.write_text(
        "@echo off\r\n" + "".join(f"echo {container_id}\r\n" for container_id in docker_ids) + "exit /b 0\r\n",
        encoding="ascii",
    )
    env = os.environ.copy()
    env["PATH"] = str(tmp_path) + os.pathsep + env.get("PATH", "")
    runner = ROOT / "scripts" / "run-demoqa-tests.ps1"
    command = rf'''
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile('{runner}', [ref]$tokens, [ref]$errors)
if ($errors.Count) {{ throw 'runner parse failed' }}
$defs=$ast.FindAll({{ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -in @('Invoke-NativeCaptured','Wait-TestProjectContainersRemoved') }}, $true)
foreach($def in $defs) {{ . ([scriptblock]::Create($def.Extent.Text)) }}
$Project='browser-mcp-demoqa-test'
Wait-TestProjectContainersRemoved -TimeoutSeconds 1 -IgnoreContainerIds @('old-fixture-a','old-fixture-b')
'''
    result = subprocess.run(
        [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == expected


def test_probe_only_rejects_task_control_checks_before_docker_actions(tmp_path):
    powershell = shutil.which("powershell")
    if powershell is None:
        pytest.skip("Windows PowerShell is unavailable")

    docker_log = tmp_path / "docker-calls.txt"
    shim = tmp_path / "docker.cmd"
    shim.write_text('@echo off\r\necho %*>>"%DOCKER_STUB_LOG%"\r\nexit /b 0\r\n', encoding="ascii")
    env = os.environ.copy()
    env["PATH"] = str(tmp_path) + os.pathsep + env.get("PATH", "")
    env["DOCKER_STUB_LOG"] = str(docker_log)
    result = subprocess.run(
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "scripts" / "run-demoqa-tests.ps1"),
            "-ProbeOnly",
            "-TaskControlChecks",
            "-SkipBuild",
            "-ModelBaseUrl",
            "http://127.0.0.1:1/v1",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert result.returncode != 0
    assert "cannot be combined with probeonly" in (result.stdout + result.stderr).lower()
    assert not docker_log.exists()


def test_fresh_powershell_loads_webrequestsession_before_type_creation():
    powershell = shutil.which("powershell")
    if powershell is None:
        pytest.skip("Windows PowerShell is unavailable")
    result = subprocess.run(
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "Get-Command Invoke-RestMethod -ErrorAction Stop | Out-Null; "
            "[void]([Microsoft.PowerShell.Commands.WebRequestSession]::new())",
        ],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _preflight_fixture(*, status: str = "cancelled", **state_overrides):
    state = {
        "available": True,
        "browser_started": True,
        "control_owner": "none",
        "action_in_progress": False,
        "import_in_progress": False,
        "context_count": 1,
        "page_count": 1,
        "recovery_state": "idle",
        "recovery_needed": False,
        "resource_violation": False,
        **state_overrides,
    }
    payloads = {
        "/api/v1/operator/session": {"authenticated": True},
        "/api/v1/desktop/state": state,
        "/api/v1/tasks": {"tasks": [{"status": status}] if status else []},
    }
    return httpx.MockTransport(
        lambda request: httpx.Response(200, json=payloads[request.url.path])
    )


def test_production_preflight_uses_control_port_8090_and_accepts_safe_idle_state():
    assert production_preflight.DEFAULT_CONTROL_URL == "http://127.0.0.1:8090"
    result = production_preflight.evaluate(
        production_preflight.DEFAULT_CONTROL_URL,
        transport=_preflight_fixture(status="expired"),
    )
    assert result["ready"] is True


@pytest.mark.parametrize("status", ["queued", "running", "pause_requested", "paused", "waiting_human", "waiting_approval", "recovering"])
def test_production_preflight_blocks_every_nonterminal_task(status):
    result = production_preflight.evaluate("http://127.0.0.1:8090", transport=_preflight_fixture(status=status))
    assert result["ready"] is False
    assert result["active_task_statuses"] == [status]


@pytest.mark.parametrize("status", ["paused", "waiting_human", "waiting_approval"])
def test_production_preflight_requires_explicit_flag_for_suspended_tasks(status):
    transport = _preflight_fixture(status=status)
    default_result = production_preflight.evaluate("http://127.0.0.1:8090", transport=transport)
    assert default_result["ready"] is False
    allowed_result = production_preflight.evaluate(
        "http://127.0.0.1:8090", allow_suspended_tasks=True, transport=transport
    )
    assert allowed_result["ready"] is True
    assert allowed_result["suspended_task_count"] == 1
    assert allowed_result["suspended_task_statuses"] == [status]


@pytest.mark.parametrize("status", ["queued", "running", "pause_requested", "recovering", "new_unknown_status"])
def test_explicit_suspended_task_flag_never_allows_active_or_unknown_status(status):
    result = production_preflight.evaluate(
        "http://127.0.0.1:8090", allow_suspended_tasks=True, transport=_preflight_fixture(status=status)
    )
    assert result["ready"] is False
    assert result["active_task_statuses"] == [status]


@pytest.mark.parametrize(
    "overrides",
    [
        {"available": False},
        {"recovery_state": "backoff"},
        {"recovery_needed": True},
        {"resource_violation": True},
        {"control_owner": "human"},
        {"action_in_progress": True},
        {"import_in_progress": True},
    ],
)
def test_production_preflight_blocks_unready_or_unsafe_worker(overrides):
    result = production_preflight.evaluate("http://127.0.0.1:8090", transport=_preflight_fixture(status="", **overrides))
    assert result["ready"] is False
    assert result["worker_idle"] is False


def test_production_preflight_rejects_missing_auth_before_reading_state():
    requested = []

    def respond(request):
        requested.append(request.url.path)
        if request.url.path == "/api/v1/operator/session":
            return httpx.Response(200, json={"authenticated": False})
        pytest.fail("protected state must not be read before local session authorization")

    result = production_preflight.evaluate("http://127.0.0.1:8090", transport=httpx.MockTransport(respond))
    assert result == {"ready": False, "reason": "operator_session_unavailable"}
    assert requested == ["/api/v1/operator/session"]


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080",
        "https://127.0.0.1:8090",
        "http://example.test:8090",
        "http://user:password@127.0.0.1:8090",
        "http://127.0.0.1:8090/api",
    ],
)
def test_production_preflight_rejects_wrong_or_remote_control_target_without_request(url):
    transport = httpx.MockTransport(lambda request: pytest.fail("rejected target must not make a request"))
    result = production_preflight.evaluate(url, transport=transport)
    assert result == {"ready": False, "reason": "control_url_rejected"}
