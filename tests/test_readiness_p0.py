from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from readiness_p0 import SCENARIOS, _validate_internal_urls, main as p0_main
from readiness_p0_fixture import fixture_html


def test_fixed_scenario_contract_keeps_all_web_table_fields_and_real_checkbox_role():
    web_table = SCENARIOS["web_table"]
    assert "salary 12345, department Testing" in web_table["goal"]
    expected = {"Testperson", "Example", "test@example.com", "30", "12345", "Testing"}
    assert {condition["text"] for condition in web_table["conditions"]} == expected
    checkbox = SCENARIOS["checkbox"]["conditions"][0]
    assert checkbox == {
        "kind": "element_checked",
        "selector": '[role="checkbox"][aria-label="Select Desktop"]',
        "checked": True,
    }


def test_runner_rejects_non_disposable_service_urls():
    assert _validate_internal_urls("http://localhost:8080", "http://browser:7070") == (
        "http://localhost:8080", "http://browser:7070"
    )
    for control, worker in (
        ("http://localhost:8080?token=secret", "http://browser:7070"),
        ("http://user:secret@localhost:8080", "http://browser:7070"),
        ("http://example.test:8080", "http://browser:7070"),
        ("http://localhost:8080", "http://browser:7070/path"),
    ):
        with pytest.raises(RuntimeError):
            _validate_internal_urls(control, worker)


def test_invalid_scenarios_are_rejected_before_credentials_are_read(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("READINESS_ACTION_TRACE", "1")
    monkeypatch.setenv("DEMOQA_CONTROL_URL", "http://localhost:8080")
    monkeypatch.setenv("BROWSER_WORKER_URL", "http://browser:7070")
    monkeypatch.setenv("READINESS_P0_SCENARIOS", "unknown")
    monkeypatch.setattr(Path, "read_text", lambda *args, **kwargs: pytest.fail("credentials must not be read"))
    with pytest.raises(RuntimeError, match="known scenarios"):
        p0_main()


def test_unknown_fixture_is_rejected():
    with pytest.raises(ValueError):
        fixture_html("arbitrary")


def test_worker_idle_predicate_covers_recovery_and_resource_state():
    from readiness_p0 import _worker_idle

    healthy = {"browser_started": True, "control_owner": "none", "action_in_progress": False,
               "import_in_progress": False, "context_count": 1, "page_count": 1,
               "recovery_state": "idle", "recovery_needed": False, "resource_violation": False}
    assert _worker_idle(healthy)
    for key, value in (("import_in_progress", True), ("recovery_state", "backoff"),
                       ("recovery_needed", True), ("resource_violation", True), ("page_count", 2)):
        assert not _worker_idle({**healthy, key: value})


def test_fixture_behavior_in_offline_headless_chromium():
    playwright_async = pytest.importorskip("playwright.async_api", reason="offline fixture checks need Playwright")

    async def exercise() -> None:
        playwright = await playwright_async.async_playwright().start()
        try:
            try:
                browser = await playwright.chromium.launch(headless=True)
            except Exception as exc:
                if "Executable doesn't exist" in str(exc):
                    pytest.skip("Playwright Chromium is not installed")
                raise
            context = await browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
            page = await context.new_page()
            requested: list[str] = []

            async def block_network(route):
                requested.append(route.request.url)
                await route.abort()

            await page.route("**/*", block_network)
            await page.set_content(fixture_html("tree"), wait_until="load")
            assert await page.locator(".expander").get_attribute("aria-expanded") == "false"
            assert not await page.locator("#desktop").is_visible()
            await page.get_by_role("button", name="Expand Projects").click()
            assert await page.locator("#desktop").is_visible()
            await page.get_by_label("Desktop").check()
            assert await page.locator("#desktop").is_checked()
            assert await page.locator(".expander").get_attribute("aria-expanded") == "true"
            assert await page.locator("#result").inner_text() == "Selected: Desktop"
            await page.get_by_label("Desktop").uncheck()
            assert not await page.locator("#desktop").is_checked()
            assert await page.locator("#result").inner_text() == ""

            await page.set_content(fixture_html("form"), wait_until="load")
            values = {"First name": "Testperson", "Last name": "Example", "Email": "test@example.com",
                      "Age": "30", "Salary": "12345", "Department": "Testing"}
            for label, value in values.items():
                await page.get_by_label(label).fill(value)
            await page.get_by_role("button", name="Save row").click()
            assert await page.locator("#result").inner_text() == "Testperson | Example | test@example.com | 30 | 12345 | Testing"
            assert await page.evaluate("window.__fixtureSubmitCount") == 1
            assert page.url == "about:blank"
            assert requested == []
            await context.close()
            await browser.close()
        finally:
            await playwright.stop()

    asyncio.run(exercise())


def test_powershell_p0_export_uses_base64_fallback_when_docker_cp_fails(tmp_path):
    powershell = shutil.which("powershell")
    if powershell is None:
        pytest.skip("Windows PowerShell is unavailable")
    report = {"schema": "readiness-p0/v1", "status": "BLOCKED", "scenarios": [],
              "raw_model_output_recorded": False, "typed_values_recorded": False}
    env = os.environ.copy()
    env["P0_REPORT_BASE64"] = base64.b64encode(json.dumps(report).encode()).decode()
    runner = str(ROOT / "scripts" / "run-demoqa-tests.ps1").replace("'", "''")
    output_root = str(tmp_path).replace("'", "''")
    harness = rf'''
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile('{runner}', [ref]$tokens, [ref]$errors)
if ($errors.Count) {{ throw 'runner parse failed' }}
$fn=$ast.FindAll({{ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Export-P0Report' }}, $true)[0]
. ([scriptblock]::Create($fn.Extent.Text))
function Get-ComposeArgs {{ return @('compose','-p','test','-f','fixture') }}
function Invoke-NativeCaptured {{
  param([string]$FilePath,[string[]]$Arguments)
  if ($Arguments -contains 'ps') {{ return [pscustomobject]@{{ ExitCode=0; Output=@('fake-control-id') }} }}
  if ($Arguments -contains 'cp') {{ return [pscustomobject]@{{ ExitCode=1; Output=@() }} }}
  if ($Arguments -contains 'exec') {{ return [pscustomobject]@{{ ExitCode=0; Output=@($env:P0_REPORT_BASE64) }} }}
  throw 'unexpected native command'
}}
$Root='{output_root}'; $RunLabel='offline'; Export-P0Report
'''
    result = subprocess.run([powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", harness],
                            text=True, capture_output=True, env=env, timeout=15, check=False)
    assert result.returncode == 0, result.stderr
    saved = json.loads((tmp_path / "runtime" / "demoqa" / "offline" / "readiness-p0-report.json").read_text())
    assert saved == report
