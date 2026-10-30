from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_observation_hints_guard_and_numeric_median():
    powershell = shutil.which("powershell")
    if powershell is None:
        pytest.skip("Windows PowerShell is unavailable")
    runner = str(ROOT / "scripts" / "run-demoqa-tests.ps1").replace("'", "''")
    harness = rf'''
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile('{runner}', [ref]$tokens, [ref]$errors)
if ($errors.Count) {{ throw 'runner parse failed' }}
foreach ($name in @('Assert-ObservationHintsMode','Get-NumericMedian')) {{
  $fn=$ast.FindAll({{ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }}, $true)[0]
  . ([scriptblock]::Create($fn.Extent.Text))
}}
$common=@{{ Enabled=$true; Protocol='generic'; ProbeOnly=$false; DiagnosticGate=$false; SkipCases=$false; P0Diagnostics=$false; RunCasesAfterReadableCandidate=$false; SoakChecks=$false }}
Assert-ObservationHintsMode @common
$soak=@{{ Enabled=$true; Protocol='generic'; ProbeOnly=$false; DiagnosticGate=$false; SkipCases=$true; P0Diagnostics=$false; RunCasesAfterReadableCandidate=$false; SoakChecks=$true }}
Assert-ObservationHintsMode @soak
$bad=@(
  @{{ Enabled=$true; Protocol='fara'; ProbeOnly=$false; DiagnosticGate=$false; SkipCases=$true; P0Diagnostics=$false; RunCasesAfterReadableCandidate=$false; SoakChecks=$true }},
  @{{ Enabled=$true; Protocol='generic'; ProbeOnly=$false; DiagnosticGate=$false; SkipCases=$true; P0Diagnostics=$false; RunCasesAfterReadableCandidate=$false; SoakChecks=$false }},
  @{{ Enabled=$true; Protocol='generic'; ProbeOnly=$false; DiagnosticGate=$false; SkipCases=$false; P0Diagnostics=$false; RunCasesAfterReadableCandidate=$false; SoakChecks=$true }},
  @{{ Enabled=$true; Protocol='generic'; ProbeOnly=$true; DiagnosticGate=$false; SkipCases=$true; P0Diagnostics=$false; RunCasesAfterReadableCandidate=$false; SoakChecks=$true }}
)
foreach ($paramsMap in $bad) {{ $rejected=$false; try {{ Assert-ObservationHintsMode @paramsMap }} catch {{ $rejected=$true }}; if (-not $rejected) {{ throw 'accepted invalid observation-hints mode' }} }}
if ((Get-NumericMedian -Values @(1,3,5)) -ne 3) {{ throw 'odd median is incorrect' }}
if ((Get-NumericMedian -Values @(1,2,3,4)) -ne 2.5) {{ throw 'even median is incorrect' }}
'''
    result = subprocess.run([powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", harness],
                            text=True, capture_output=True, timeout=15, check=False)
    assert result.returncode == 0, result.stdout + result.stderr