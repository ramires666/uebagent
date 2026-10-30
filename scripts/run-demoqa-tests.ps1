[CmdletBinding()]
param(
    [string[]]$Cases = @('text_box', 'invalid_email', 'checkbox', 'web_table', 'notify_then_click'),
    [string]$ModelBaseUrl = 'http://host.docker.internal:8080/v1',
    [string]$ModelAlias = 'fara-browser',
    [ValidateSet('generic', 'fara')][string]$ModelProtocol = 'fara',
    [string]$ProductionControlUrl = 'http://127.0.0.1:8090',
    [switch]$ReadinessChecks,
    [switch]$TaskControlChecks,
    [switch]$SkipCases,
    [switch]$AllowSuspendedTasks,
    [switch]$ExtendedComplex,
    [string]$RunLabel = 'v4',
    [switch]$SkipBuild,
    [switch]$ProbeOnly,
    [switch]$RunCasesAfterReadableCandidate,
    [switch]$RemoveTestVolume,
    [switch]$DiagnosticGate,
    [switch]$P0Diagnostics,
    [switch]$RuntimeIntegrationChecks,
    [switch]$LifecycleIntegrationChecks,
    [switch]$CookieBridgeChecks,
    [switch]$SoakChecks,
    [ValidateRange(1, 1200)][int]$SoakSeconds = 900,
    [ValidateSet(2816, 4096)][int]$BrowserMemoryMiB = 2816,
    [switch]$MemoryProbeChecks,
    [switch]$MemoryProbeGcContrast,
    [ValidateSet('components', 'mixed')][string]$MemoryProbePhaseSet = 'components',
    [ValidateRange(180, 300)][int]$MemoryProbeSeconds = 210,
    [switch]$MemoryAttributionChecks,
    [ValidateSet('baseline', 'no_trace', 'no_addons', 'neither')][string]$MemoryAttributionVariant = 'baseline',
    [switch]$MemoryAttributionGcContrast,
    [switch]$MemoryAttributionMemoryReport,
    [switch]$MemoryAttributionPeriodicGc,
    [switch]$MemoryMaintenance,
    [switch]$ObservationHints,
    [string[]]$P0Scenarios = @('checkbox', 'web_table'),
    [ValidateRange(0, 600)][int]$DiagnosticHoldSeconds = 120,
    [ValidatePattern('^[A-Za-z0-9_-]{1,32}$')][string]$ProbeFramePrefix = 'frame',
    [string[]]$FontMatrixVariants = @('baseline', 'arial_normal', 'arial_medium', 'system_normal', 'system_bold', 'monospace')
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$AttributionSeedPath = Join-Path $Root 'runtime/demoqa/attribution-shared-fingerprint.json'
$useAttributionSeed = $MemoryAttributionChecks -or ($SoakChecks -and $MemoryMaintenance)
$ComposeFile = Join-Path $Root 'deploy/compose.demoqa-test.yaml'
$Project = 'browser-mcp-demoqa-test'
$ServiceUrl = 'http://127.0.0.1:18080'
$ResultsPath = Join-Path $Root "runtime/demoqa/results-$RunLabel.jsonl"
$workerKey = $null
$managedDemoqaEnv = @(
    'DEMOQA_MODEL_BASE_URL', 'DEMOQA_MODEL_ALIAS', 'DEMOQA_MODEL_PROTOCOL', 'DEMOQA_BROWSER_MEMORY_LIMIT',
    'DEMOQA_WORKER_KEY', 'DEMOQA_CASES', 'DEMOQA_EXTENDED_COMPLEX', 'DEMOQA_RUN_LABEL',
    'DEMOQA_FRAME_PREFIX', 'DEMOQA_FONT_VARIANTS', 'DEMOQA_DIAGNOSTIC_HOLD_SECONDS',
    'READINESS_ACTION_TRACE', 'READINESS_P0_SCENARIOS', 'BROWSER_OBSERVATION_HINTS',
    'READINESS_SKIP_ACTION_SNAPSHOTS', 'READINESS_DISABLE_DEFAULT_ADDONS', 'BROWSER_MEMORY_MAINTENANCE'
)
$priorDemoqaEnv = @{}
$processEnv = [Environment]::GetEnvironmentVariables('Process')
foreach ($name in $managedDemoqaEnv) {
    $priorDemoqaEnv[$name] = [pscustomobject]@{ Present = $processEnv.Contains($name); Value = [Environment]::GetEnvironmentVariable($name, 'Process') }
}
$stackCleanupNeeded = $false
$productionContainerId = $null
$productionWasRunning = $false
$productionStopAttempted = $false
$productionStopped = $false
$preexistingTestContainerIds = @()
$continueCases = $false
$readinessCheckAttempted = $false
$readinessReportError = $null
$p0ReportAttempted = $false
$p0ReportError = $null
$soakReportAttempted = $false
$soakReportError = $null
$memoryProbeReportAttempted = $false
$memoryProbeReportError = $null
$memoryAttributionReportAttempted = $false
$memoryAttributionReportError = $null
$attributionSeedDigest = $null
$attributionSeedVersion = $null
$attributionSeedMatched = $false
$attributionSeedStage = $null
$resultLines = [System.Collections.Generic.List[string]]::new()
$metricJob = $null
$metricSamples = @()

function Invoke-NativeCaptured {
    param([string]$FilePath, [string[]]$Arguments)
    $previousPreference = $ErrorActionPreference
    $output = @()
    $exitCode = 1
    $ErrorActionPreference = 'Continue'
    try {
        $output = @(& $FilePath @Arguments 2>&1)
        $exitCode = $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $previousPreference }
    return [pscustomobject]@{ ExitCode = $exitCode; Output = @($output) }
}

function Invoke-DockerChecked {
    param([string[]]$Arguments, [string]$FailureMessage)
    $result = Invoke-NativeCaptured -FilePath 'docker' -Arguments $Arguments
    if ($result.ExitCode -ne 0) {
        $details = ($result.Output | ForEach-Object { [string]$_ }) -join [Environment]::NewLine
        throw "$FailureMessage (exit $($result.ExitCode)). $details"
    }
    return ,$result.Output
}

function Get-TestProfileDigest {
    $code = "from pathlib import Path;import hashlib;p=Path('/app/runtime/worker-state/profiles/default.camoufox-fingerprint.json');print(hashlib.sha256(p.read_bytes()).hexdigest())"
    $result = Invoke-NativeCaptured -FilePath 'docker' -Arguments ((Get-ComposeArgs) + @('exec', '-T', 'browser', 'python', '-c', $code))
    if ($result.ExitCode -ne 0) { throw 'Could not read the disposable test fingerprint digest.' }
    $digest = (($result.Output | ForEach-Object { [string]$_ }) -join '').Trim()
    if ($digest -notmatch '^[0-9a-f]{64}$') { throw 'Disposable worker fingerprint digest was missing or malformed.' }
    return $digest
}

function Get-ValidatedAttributionFingerprintSeed {
    param([string]$Path)
    $workspaceRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    if (-not $fullPath.StartsWith($workspaceRoot, [System.StringComparison]::OrdinalIgnoreCase)) { throw 'Attribution fingerprint seed path escaped the workspace.' }
    if (-not (Test-Path -LiteralPath $fullPath -PathType Leaf)) { throw 'Attribution fingerprint seed is missing.' }
    $file = Get-Item -LiteralPath $fullPath -Force
    if (($file.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 -or $file.Length -gt 256KB) { throw 'Attribution fingerprint seed is not a small regular file.' }
    $validator = "import hashlib,json,sys; p=sys.argv[1]; b=open(p,'rb').read(); d=json.loads(b.decode('utf-8')); c=d.get('config'); f=d.get('firefox_user_prefs'); canon=json.dumps({'config':c,'firefox_user_prefs':f},ensure_ascii=False,allow_nan=False,sort_keys=True,separators=(',',':')).encode('utf-8'); allowed={'webgl.enable-webgl2','webgl.force-enabled'}; ok=(len(b)<=262144 and set(d)=={'version','config','firefox_user_prefs','sha256'} and d.get('version')=='0.4.11' and isinstance(c,dict) and isinstance(f,dict) and {'navigator.userAgent','fonts:spacing_seed','canvas:aaOffset'}.issubset(c) and not (set(f)-allowed) and all(type(v) is bool for v in f.values()) and d.get('sha256')==hashlib.sha256(canon).hexdigest()); print(json.dumps({'valid':bool(ok),'version':d.get('version'),'file_sha256':hashlib.sha256(b).hexdigest()}))"
    $check = Invoke-NativeCaptured -FilePath 'python' -Arguments @('-c', $validator, $fullPath)
    if ($check.ExitCode -ne 0) { throw 'Attribution fingerprint seed could not be validated against the worker schema.' }
    try { $result = (($check.Output -join '') | ConvertFrom-Json -ErrorAction Stop) }
    catch { throw 'Attribution fingerprint seed validator returned invalid metadata.' }
    if ($result.valid -ne $true -or $result.version -ne '0.4.11' -or $result.file_sha256 -notmatch '^[0-9a-f]{64}$') {
        throw 'Attribution fingerprint seed did not match the Camoufox 0.4.11 worker schema.'
    }
    return [pscustomobject]@{ Version=[string]$result.version; Digest=[string]$result.file_sha256 }
}

function Save-AttributionWorkerStartupDiagnostic {
    if (-not $MemoryAttributionChecks) { return }
    try {
        $directory = Join-Path (Join-Path $Root 'runtime/demoqa') $RunLabel
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
        $target = Join-Path $directory ('private-worker-startup-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ') + '.log')
        $browserRows = Invoke-NativeCaptured -FilePath 'docker' -Arguments ((Get-ComposeArgs) + @('ps', '-q', 'browser'))
        $browserIds = @($browserRows.Output | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ })
        if ($browserRows.ExitCode -ne 0 -or $browserIds.Count -ne 1) {
            $content = 'Private diagnostic: disposable browser container logs unavailable.'
        }
        else {
            $logs = Invoke-NativeCaptured -FilePath 'docker' -Arguments @('logs', '--tail', '200', $browserIds[0])
            $content = (($logs.Output | ForEach-Object { [string]$_ }) -join [Environment]::NewLine)
            if (-not $content) { $content = 'Private diagnostic: browser emitted no startup log lines.' }
            if ($content.Length -gt 65536) { $content = $content.Substring($content.Length - 65536) }
        }
        [System.IO.File]::WriteAllText($target, $content + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
        Write-Host "Saved private disposable worker startup diagnostic: $target"
    }
    catch { Write-Warning 'Could not preserve the private disposable worker startup diagnostic.' }
}

function Get-ComposeArgs {
    return @('compose', '-p', $Project, '-f', $ComposeFile)
}

function Wait-TestProjectContainersRemoved {
    param(
        [ValidateRange(1, 90)][int]$TimeoutSeconds = 30,
        [string[]]$IgnoreContainerIds = @()
    )
    $ignore = @($IgnoreContainerIds | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ })
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        $remaining = Invoke-NativeCaptured -FilePath 'docker' -Arguments @('ps', '-aq', '--filter', "label=com.docker.compose.project=$Project")
        if ($remaining.ExitCode -eq 0) {
            $ids = @($remaining.Output | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ -and $_ -notin $ignore })
            if ($ids.Count -eq 0) { return $true }
        }
        Start-Sleep -Seconds 1
    }
    return $false
}

function Wait-ProductionReady {
    $null = Get-Command Invoke-RestMethod -ErrorAction Stop
    $deadline = [DateTime]::UtcNow.AddSeconds(90)
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $health = Invoke-RestMethod -Uri "$($ProductionControlUrl.TrimEnd('/'))/health/ready" -Method Get -TimeoutSec 5 -UseBasicParsing
            if ($health.status -eq 'ok' -or $health.ready -eq $true) {
                $checkArgs = @((Join-Path $PSScriptRoot 'demoqa_preflight.py'), '--control-url', $ProductionControlUrl)
                if ($AllowSuspendedTasks) { $checkArgs += '--allow-suspended-tasks' }
                $check = Invoke-NativeCaptured -FilePath 'python' -Arguments $checkArgs
                if ($check.ExitCode -eq 0) {
                    try {
                        $state = ($check.Output -join '') | ConvertFrom-Json -ErrorAction Stop
                        if ($state.ready -eq $true) { return $true }
                    } catch { }
                }
            }
        } catch { }
        Start-Sleep -Seconds 2
    }
    return $false
}

function Save-ViewerScreenshot {
    param([string]$FileName)
    if ($FileName -notmatch '^[a-z0-9_-]+\.png$') { throw 'Invalid screenshot filename.' }
    $screensDirectory = Join-Path (Join-Path $Root 'runtime/demoqa') "$RunLabel/screens"
    New-Item -ItemType Directory -Path $screensDirectory -Force | Out-Null
    $target = Join-Path $screensDirectory $FileName
    $null = Invoke-WebRequest -Uri "$ServiceUrl/api/v1/desktop/screenshot" -Method Get -WebSession $script:DemoqaWebSession -TimeoutSec 15 -UseBasicParsing -OutFile $target -ErrorAction Stop
    $bytes = [System.IO.File]::ReadAllBytes($target)
    if ($bytes.Length -lt 8 -or $bytes.Length -gt 16MB -or $bytes[0] -ne 137 -or
        [System.Text.Encoding]::ASCII.GetString($bytes, 1, 3) -ne 'PNG') {
        Remove-Item -LiteralPath $target -Force -ErrorAction SilentlyContinue
        throw "Viewer screenshot $FileName failed PNG validation."
    }
    Write-Host "Saved screenshot: $target"
}

function Invoke-RunnerStream {
    param([string[]]$RunnerArgs)
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & docker @RunnerArgs 2>&1 | ForEach-Object {
            $line = [string]$_
            Write-Host $line
            if ($line.TrimStart().StartsWith('{')) {
                $resultLines.Add($line)
                try {
                    $row = $line | ConvertFrom-Json -ErrorAction Stop
                    if ($row.scenario -eq 'font_probe_phase' -and $row.phase -eq 'one_second') { Save-ViewerScreenshot 'font-probe-1s.png' }
                    elseif ($row.scenario -eq 'font_probe_phase' -and $row.phase -eq 'five_seconds') { Save-ViewerScreenshot 'font-probe-5s.png' }
                    elseif ($row.screenshot_saved -eq $true -and $row.scenario -is [string]) { Save-ViewerScreenshot "$($row.scenario).png" }
                } catch { throw }
            }
        }
        return $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $previousPreference }
}

function Invoke-HostProbe {
    param([string]$ProbePath)
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & python -u $ProbePath 2>&1 | ForEach-Object {
            $line = [string]$_
            Write-Host $line
            if ($line.TrimStart().StartsWith('{')) { $resultLines.Add($line) }
        }
        return $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $previousPreference }
}

function Copy-DemoqaScreenshots {
    $screensDirectory = Join-Path (Join-Path $Root 'runtime/demoqa') "$RunLabel/screens"
    New-Item -ItemType Directory -Path $screensDirectory -Force | Out-Null
    $screensToCopy = @{}
    $artifactDirectory = '/tmp/demoqa-artifacts'
    foreach ($line in $resultLines) {
        try {
            $row = $line | ConvertFrom-Json -ErrorAction Stop
            if ($row.artifact_directory -is [string] -and
                $row.artifact_directory -in @('/tmp/demoqa-artifacts', '/app/runtime/control-data/demoqa-artifacts')) {
                $artifactDirectory = $row.artifact_directory
            }
            if ($row.screenshot_saved -eq $true -and $row.scenario -is [string]) { $screensToCopy["$($row.scenario).png"] = $true }
            if ($row.screenshot_files -is [array]) {
                foreach ($name in $row.screenshot_files) {
                    if ($name -is [string] -and $name -match '^[a-z0-9_-]+\.png$') { $screensToCopy[$name] = $true }
                }
            }
        } catch { }
    }
    if ($screensToCopy.Count -eq 0) { return }
    $controlQuery = Invoke-NativeCaptured -FilePath 'docker' -Arguments ((Get-ComposeArgs) + @('ps', '-q', 'control'))
    $controlIds = @($controlQuery.Output | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ })
    if ($controlQuery.ExitCode -ne 0 -or $controlIds.Count -ne 1) { throw 'Could not identify the isolated control container before copying screenshots.' }
    foreach ($name in $screensToCopy.Keys) {
        $sourcePath = "$artifactDirectory/$name"
        $source = "$($controlIds[0]):$sourcePath"
        $target = Join-Path $screensDirectory $name
        if (Test-Path -LiteralPath $target -PathType Leaf) { continue }
        $copiedScreen = Invoke-NativeCaptured -FilePath 'docker' -Arguments @('cp', $source, $target)
        if ($copiedScreen.ExitCode -ne 0) {
            $readerCode = 'import base64,pathlib,sys; p=pathlib.Path(sys.argv[1]); print(base64.b64encode(p.read_bytes()).decode("ascii"))'
            $readArgs = @('exec', $controlIds[0], 'python', '-c', $readerCode, $sourcePath)
            $encoded = Invoke-NativeCaptured -FilePath 'docker' -Arguments $readArgs
            if ($encoded.ExitCode -ne 0) { throw "Could not export screenshot $name before test-stack cleanup." }
            try { $bytes = [Convert]::FromBase64String(($encoded.Output -join '').Trim()) }
            catch { throw "Screenshot $name could not be decoded from the isolated container." }
            if ($bytes.Length -lt 8 -or $bytes.Length -gt 16MB -or $bytes[0] -ne 137 -or
                [System.Text.Encoding]::ASCII.GetString($bytes, 1, 3) -ne 'PNG') {
                throw "Screenshot $name failed PNG validation."
            }
            [System.IO.File]::WriteAllBytes($target, $bytes)
        }
        Write-Host "Saved screenshot: $target"
    }
}

function Export-ReadinessReport {
    $controlQuery = Invoke-NativeCaptured -FilePath 'docker' -Arguments ((Get-ComposeArgs) + @('ps', '-q', 'control'))
    $controlIds = @($controlQuery.Output | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ })
    if ($controlQuery.ExitCode -ne 0 -or $controlIds.Count -ne 1) { throw 'Could not identify the disposable control container for readiness evidence.' }

    $destinationDirectory = Join-Path (Join-Path $Root 'runtime/demoqa') $RunLabel
    New-Item -ItemType Directory -Path $destinationDirectory -Force | Out-Null
    $target = Join-Path $destinationDirectory 'readiness-live-report.json'
    $sourcePath = '/app/runtime/control-data/readiness-live-report.json'
    $copy = Invoke-NativeCaptured -FilePath 'docker' -Arguments @('cp', "$($controlIds[0]):$sourcePath", $target)
    if ($copy.ExitCode -ne 0) {
        $readerCode = "import base64,pathlib; print(base64.b64encode(pathlib.Path('/app/runtime/control-data/readiness-live-report.json').read_bytes()).decode('ascii'))"
        $encoded = Invoke-NativeCaptured -FilePath 'docker' -Arguments @('exec', $controlIds[0], 'python', '-c', $readerCode)
        if ($encoded.ExitCode -ne 0) { throw 'Could not export readiness evidence before disposable stack cleanup.' }
        $encodedText = (($encoded.Output | ForEach-Object { [string]$_ }) -join '').Trim()
        if ($encodedText.Length -gt 2MB) { throw 'Readiness evidence exceeded the export size limit.' }
        try { [System.IO.File]::WriteAllBytes($target, [Convert]::FromBase64String($encodedText)) }
        catch { throw 'Readiness evidence export was not valid base64.' }
    }
    $raw = [System.IO.File]::ReadAllText($target)
    if ($raw.Length -gt 1MB) { Remove-Item -LiteralPath $target -Force; throw 'Readiness report exceeded the 1 MiB limit.' }
    try { $report = $raw | ConvertFrom-Json -ErrorAction Stop }
    catch { Remove-Item -LiteralPath $target -Force; throw 'Readiness report was not valid JSON.' }
    if ($report.schema_version -ne 1 -or $report.overall_status -notin @('PASS', 'FAIL', 'BLOCKED')) {
        Remove-Item -LiteralPath $target -Force
        throw 'Readiness report did not match the expected sanitized schema.'
    }
    [System.IO.File]::WriteAllText($target, ($report | ConvertTo-Json -Depth 20) + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
    Write-Host "Saved sanitized readiness report: $target"
}

function Export-P0Report {
    $controlQuery = Invoke-NativeCaptured -FilePath 'docker' -Arguments ((Get-ComposeArgs) + @('ps', '-q', 'control'))
    $controlIds = @($controlQuery.Output | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ })
    if ($controlQuery.ExitCode -ne 0 -or $controlIds.Count -ne 1) { throw 'Could not identify the disposable control container for P0 evidence.' }
    $destinationDirectory = Join-Path (Join-Path $Root 'runtime/demoqa') $RunLabel
    New-Item -ItemType Directory -Path $destinationDirectory -Force | Out-Null
    $target = Join-Path $destinationDirectory 'readiness-p0-report.json'
    $sourcePath = '/app/runtime/control-data/readiness-p0-report.json'
    $copy = Invoke-NativeCaptured -FilePath 'docker' -Arguments @('cp', "$($controlIds[0]):$sourcePath", $target)
    if ($copy.ExitCode -ne 0) {
        Remove-Item -LiteralPath $target -Force -ErrorAction SilentlyContinue
        $readerCode = 'import base64,pathlib; print(base64.b64encode(pathlib.Path(''/app/runtime/control-data/readiness-p0-report.json'').read_bytes()).decode(''ascii''))'
        $encoded = Invoke-NativeCaptured -FilePath 'docker' -Arguments @('exec', $controlIds[0], 'python', '-c', $readerCode)
        if ($encoded.ExitCode -ne 0) { throw 'Could not export P0 evidence before disposable stack cleanup.' }
        $encodedText = (($encoded.Output | ForEach-Object { [string]$_ }) -join '').Trim()
        if ($encodedText.Length -gt 3MB) { throw 'P0 evidence exceeded the export size limit.' }
        try { [System.IO.File]::WriteAllBytes($target, [Convert]::FromBase64String($encodedText)) }
        catch { throw 'P0 evidence export was not valid base64.' }
    }
    $raw = [System.IO.File]::ReadAllText($target)
    if ($raw.Length -gt 2MB) { Remove-Item -LiteralPath $target -Force; throw 'P0 report exceeded the 2 MiB export limit.' }
    try { $report = $raw | ConvertFrom-Json -ErrorAction Stop }
    catch { Remove-Item -LiteralPath $target -Force; throw 'P0 report was not valid JSON.' }
    if ($report.schema -ne 'readiness-p0/v1' -or $report.status -notin @('PASS', 'FAIL', 'BLOCKED', 'IN_PROGRESS') -or
        $report.scenarios.Count -gt 4 -or $report.raw_model_output_recorded -ne $false -or
        $report.typed_values_recorded -ne $false -or $report.hints_enabled -isnot [bool]) {
        Remove-Item -LiteralPath $target -Force
        throw 'P0 report did not match the expected bounded schema.'
    }
    Write-Host "Saved P0 diagnostic report: $target"
}

function Export-SoakReport {
    $controlQuery = Invoke-NativeCaptured -FilePath 'docker' -Arguments ((Get-ComposeArgs) + @('ps', '-q', 'control'))
    $controlIds = @($controlQuery.Output | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ })
    if ($controlQuery.ExitCode -ne 0 -or $controlIds.Count -ne 1) { throw 'Could not identify the disposable control container for soak evidence.' }
    $destinationDirectory = Join-Path (Join-Path $Root 'runtime/demoqa') $RunLabel
    New-Item -ItemType Directory -Path $destinationDirectory -Force | Out-Null
    $target = Join-Path $destinationDirectory 'readiness-soak.json'
    $sourcePath = '/app/runtime/control-data/readiness-soak.json'
    $copy = Invoke-NativeCaptured -FilePath 'docker' -Arguments @('cp', "$($controlIds[0]):$sourcePath", $target)
    if ($copy.ExitCode -ne 0) {
        Remove-Item -LiteralPath $target -Force -ErrorAction SilentlyContinue
        $readerCode = 'import base64,pathlib; print(base64.b64encode(pathlib.Path(''/app/runtime/control-data/readiness-soak.json'').read_bytes()).decode(''ascii''))'
        $encoded = Invoke-NativeCaptured -FilePath 'docker' -Arguments @('exec', $controlIds[0], 'python', '-c', $readerCode)
        if ($encoded.ExitCode -ne 0) { throw 'Could not export soak evidence before disposable stack cleanup.' }
        $encodedText = (($encoded.Output | ForEach-Object { [string]$_ }) -join '').Trim()
        if ($encodedText.Length -gt 2MB) { throw 'Soak evidence exceeded the 2 MiB export limit.' }
        try { [System.IO.File]::WriteAllBytes($target, [Convert]::FromBase64String($encodedText)) }
        catch { throw 'Soak evidence export was not valid base64.' }
    }
    $raw = [System.IO.File]::ReadAllText($target)
    if ($raw.Length -gt 1MB) { Remove-Item -LiteralPath $target -Force; throw 'Soak report exceeded the 1 MiB limit.' }
    try { $report = $raw | ConvertFrom-Json -ErrorAction Stop }
    catch { Remove-Item -LiteralPath $target -Force; throw 'Soak report was not valid JSON.' }
    if ($report.schema -ne 'readiness-soak/v1' -or $report.status -notin @('PASS', 'FAIL', 'BLOCKED') -or
        $report.duration_requested_seconds -isnot [ValueType] -or $report.duration_actual_seconds -isnot [ValueType] -or
        $report.cycles_completed -isnot [ValueType] -or $report.latency -isnot [pscustomobject] -or
        $report.invariants -isnot [pscustomobject]) {
        Remove-Item -LiteralPath $target -Force
        throw 'Soak report did not match the expected bounded schema.'
    }
    if ($MemoryMaintenance) {
        $report | Add-Member -NotePropertyName fingerprint_seed_matched -NotePropertyValue ([bool]$attributionSeedMatched) -Force
        $report | Add-Member -NotePropertyName memory_maintenance_configured -NotePropertyValue $true -Force
    }
    [System.IO.File]::WriteAllText($target, ($report | ConvertTo-Json -Depth 20) + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
    Write-Host "Saved sanitized soak report: $target"
}

function Export-MemoryProbeReport {
    $controlQuery = Invoke-NativeCaptured -FilePath 'docker' -Arguments ((Get-ComposeArgs) + @('ps', '-q', 'control'))
    $controlIds = @($controlQuery.Output | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ })
    if ($controlQuery.ExitCode -ne 0 -or $controlIds.Count -ne 1) { throw 'Could not identify the disposable control container for memory-probe evidence.' }
    $directory = Join-Path (Join-Path $Root 'runtime/demoqa') $RunLabel
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
    $target = Join-Path $directory 'memory-probe.json'
    $sourcePath = '/app/runtime/control-data/readiness-memory-probe.json'
    $copy = Invoke-NativeCaptured -FilePath 'docker' -Arguments @('cp', "$($controlIds[0]):$sourcePath", $target)
    if ($copy.ExitCode -ne 0) {
        Remove-Item -LiteralPath $target -Force -ErrorAction SilentlyContinue
        $readerCode = 'import base64,pathlib; print(base64.b64encode(pathlib.Path(''/app/runtime/control-data/readiness-memory-probe.json'').read_bytes()).decode(''ascii''))'
        $encoded = Invoke-NativeCaptured -FilePath 'docker' -Arguments @('exec', $controlIds[0], 'python', '-c', $readerCode)
        if ($encoded.ExitCode -ne 0) { throw 'Could not export memory-probe evidence before disposable stack cleanup.' }
        $encodedText = (($encoded.Output | ForEach-Object { [string]$_ }) -join '').Trim()
        if ($encodedText.Length -gt 3MB) { throw 'Memory-probe evidence exceeded the 3 MiB export limit.' }
        try { [System.IO.File]::WriteAllBytes($target, [Convert]::FromBase64String($encodedText)) }
        catch { throw 'Memory-probe evidence export was not valid base64.' }
    }
    $raw = [System.IO.File]::ReadAllText($target)
    if ($raw.Length -gt 2MB) { Remove-Item -LiteralPath $target -Force; throw 'Memory-probe report exceeded the 2 MiB limit.' }
    try { $report = $raw | ConvertFrom-Json -ErrorAction Stop }
    catch { Remove-Item -LiteralPath $target -Force; throw 'Memory-probe report was not valid JSON.' }
    if ($report.schema -ne 'readiness-memory-probe/v1' -or $report.status -ne 'DIAGNOSTIC') {
        Remove-Item -LiteralPath $target -Force
        throw 'Memory-probe report did not match the expected diagnostic schema.'
    }
    $hostMetricsPath = Join-Path $directory 'memory-probe-host-metrics.json'
    $processPath = Join-Path $directory 'memory-probe-final-process-rss.json'
    if (Test-Path -LiteralPath $hostMetricsPath -PathType Leaf) {
        $hostMetrics = Get-Content -LiteralPath $hostMetricsPath -Raw | ConvertFrom-Json -ErrorAction Stop
        $report | Add-Member -NotePropertyName host_metrics -NotePropertyValue $hostMetrics -Force
    }
    if (Test-Path -LiteralPath $processPath -PathType Leaf) {
        $processReport = Get-Content -LiteralPath $processPath -Raw | ConvertFrom-Json -ErrorAction Stop
        $report | Add-Member -NotePropertyName final_process_rss -NotePropertyValue $processReport -Force
    }
    [System.IO.File]::WriteAllText($target, ($report | ConvertTo-Json -Depth 20) + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
    Write-Host "Saved bounded memory diagnostic: $target"
}

function Export-MemoryAttributionReport {
    $controlQuery = Invoke-NativeCaptured -FilePath 'docker' -Arguments ((Get-ComposeArgs) + @('ps', '-q', 'control'))
    $controlIds = @($controlQuery.Output | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ })
    if ($controlQuery.ExitCode -ne 0 -or $controlIds.Count -ne 1) { throw 'Could not identify the disposable control container for memory-attribution evidence.' }
    $directory = Join-Path (Join-Path $Root 'runtime/demoqa') $RunLabel
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
    $target = Join-Path $directory 'memory-attribution.json'
    $sourcePath = '/app/runtime/control-data/readiness-memory-attribution.json'
    $copy = Invoke-NativeCaptured -FilePath 'docker' -Arguments @('cp', "$($controlIds[0]):$sourcePath", $target)
    if ($copy.ExitCode -ne 0) {
        Remove-Item -LiteralPath $target -Force -ErrorAction SilentlyContinue
        $readerCode = 'import base64,pathlib; print(base64.b64encode(pathlib.Path(''/app/runtime/control-data/readiness-memory-attribution.json'').read_bytes()).decode(''ascii''))'
        $encoded = Invoke-NativeCaptured -FilePath 'docker' -Arguments @('exec', $controlIds[0], 'python', '-c', $readerCode)
        if ($encoded.ExitCode -ne 0) { throw 'Could not export memory-attribution evidence before disposable stack cleanup.' }
        $encodedText = (($encoded.Output | ForEach-Object { [string]$_ }) -join '').Trim()
        if ($encodedText.Length -gt 4MB) { throw 'Memory-attribution evidence exceeded the export size limit.' }
        try { [System.IO.File]::WriteAllBytes($target, [Convert]::FromBase64String($encodedText)) }
        catch { throw 'Memory-attribution evidence export was not valid base64.' }
    }
    $raw = [System.IO.File]::ReadAllText($target)
    if ($raw.Length -gt 3MB) { Remove-Item -LiteralPath $target -Force; throw 'Memory-attribution report exceeded the 3 MiB limit.' }
    try { $report = $raw | ConvertFrom-Json -ErrorAction Stop }
    catch { Remove-Item -LiteralPath $target -Force; throw 'Memory-attribution report was not valid JSON.' }
    if ($report.schema -ne 'readiness-memory-attribution/v1' -or
        $report.status -notin @('DIAGNOSTIC', 'FAIL', 'BLOCKED') -or
        $report.variant -notin @('baseline', 'no_trace', 'no_addons', 'neither')) {
        Remove-Item -LiteralPath $target -Force
        throw 'Memory-attribution report did not match the expected bounded schema.'
    }
    $report | Add-Member -NotePropertyName fingerprint_seed_matched -NotePropertyValue $attributionSeedMatched -Force
    $report | Add-Member -NotePropertyName fingerprint_version -NotePropertyValue $attributionSeedVersion -Force
    $hostMetricsPath = Join-Path $directory 'memory-attribution-host-metrics.json'
    $processPath = Join-Path $directory 'memory-attribution-final-process-rss.json'
    if (Test-Path -LiteralPath $hostMetricsPath -PathType Leaf) {
        $hostMetrics = Get-Content -LiteralPath $hostMetricsPath -Raw | ConvertFrom-Json -ErrorAction Stop
        $report | Add-Member -NotePropertyName host_metrics -NotePropertyValue $hostMetrics -Force
    }
    if (Test-Path -LiteralPath $processPath -PathType Leaf) {
        $processReport = Get-Content -LiteralPath $processPath -Raw | ConvertFrom-Json -ErrorAction Stop
        $report | Add-Member -NotePropertyName final_process_rss -NotePropertyValue $processReport -Force
    }
    [System.IO.File]::WriteAllText($target, ($report | ConvertTo-Json -Depth 20) + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
    Write-Host "Saved bounded memory-attribution report: $target"
}

function Get-NumericMedian {
    param([double[]]$Values)
    $ordered = @($Values | Sort-Object)
    if ($ordered.Count -eq 0) { throw 'Cannot calculate median without numeric samples.' }
    $middle = [int][Math]::Floor($ordered.Count / 2.0)
    if ($ordered.Count % 2 -eq 1) { return [double]$ordered[$middle] }
    return ([double]$ordered[$middle - 1] + [double]$ordered[$middle]) / 2.0
}

function Get-SanitizedProcessRss {
    param([string]$ContainerId)
    if ($ContainerId -notmatch '^[0-9a-f]{12,64}$') { return [pscustomobject]@{ status='unavailable'; processes=@() } }
    $result = Invoke-NativeCaptured -FilePath 'docker' -Arguments @('exec', $ContainerId, 'ps', '-eo', 'comm=,rss=', '--sort=-rss')
    if ($result.ExitCode -ne 0) { return [pscustomobject]@{ status='unavailable'; processes=@() } }
    $processes = [System.Collections.Generic.List[object]]::new()
    foreach ($line in $result.Output) {
        $trimmed = ([string]$line).Trim()
        if ($trimmed -notmatch '^(?<comm>.+?)\s+(?<rss>\d+)$') { continue }
        $comm = ($Matches.comm -replace '[^A-Za-z0-9_.+ -]', '').Trim()
        if (-not $comm -or $comm.Length -gt 64) { continue }
        $processes.Add([pscustomobject]@{ comm=$comm; rss_kib=[long]$Matches.rss })
        if ($processes.Count -ge 10) { break }
    }
    return [pscustomobject]@{ status=$(if ($processes.Count) { 'available' } else { 'unavailable' }); processes=@($processes) }
}

function Start-BoundedHostMetricSampler {
    param([string]$ContainerId, [int]$DurationSeconds)
    return Start-Job -ArgumentList $ContainerId, $DurationSeconds -ScriptBlock {
        param($containerId, $duration)
        $deadline = [DateTime]::UtcNow.AddSeconds($duration)
        while ([DateTime]::UtcNow -lt $deadline) {
            $stats = & docker stats --no-stream --format '{{json .}}' $containerId 2>$null
            $statsOk = ($LASTEXITCODE -eq 0 -and $stats)
            $statsObj = $null
            if ($statsOk) { try { $statsObj = [string]$stats | ConvertFrom-Json -ErrorAction Stop } catch { $statsOk = $false } }
            $cg = @(& docker exec $containerId cat /sys/fs/cgroup/memory.current /sys/fs/cgroup/memory.max /sys/fs/cgroup/pids.current /sys/fs/cgroup/pids.max /sys/fs/cgroup/memory.events 2>$null)
            $cgOk = ($LASTEXITCODE -eq 0 -and $cg.Count -ge 9)
            $cgObj = $null
            if ($cgOk) {
                try {
                    $eventValues = @{}
                    foreach ($eventLine in $cg[4..($cg.Count - 1)]) {
                        $eventParts = ([string]$eventLine).Trim() -split '\s+'
                        if ($eventParts.Count -eq 2 -and $eventParts[1] -match '^\d+$') { $eventValues[$eventParts[0]] = [long]$eventParts[1] }
                    }
                    if (($cg[0..3] | Where-Object { ([string]$_).Trim() -notmatch '^\d+$' }).Count -eq 0) {
                        $cgObj = [pscustomobject]@{
                            memory_current_bytes=[long]([string]$cg[0]).Trim(); memory_limit_bytes=[long]([string]$cg[1]).Trim()
                            pids_current=[long]([string]$cg[2]).Trim(); pids_limit=[long]([string]$cg[3]).Trim()
                            memory_events=[pscustomobject]$eventValues
                        }
                    } else { $cgOk = $false }
                } catch { $cgOk = $false }
            }
            $memoryStat = @(& docker exec $containerId cat /sys/fs/cgroup/memory.stat 2>$null)
            $memoryStatFields = @{}
            if ($LASTEXITCODE -eq 0) {
                foreach ($statLine in $memoryStat) {
                    $statParts = ([string]$statLine).Trim() -split '\s+'
                    if ($statParts.Count -eq 2 -and $statParts[0] -in @('anon','file','shmem','kernel') -and $statParts[1] -match '^\d+$') {
                        $memoryStatFields[$statParts[0]] = [long]$statParts[1]
                    }
                }
            }
            $processRss = @()
            $psLines = @(& docker exec $containerId ps -eo 'comm=,rss=' --sort=-rss 2>$null)
            if ($LASTEXITCODE -eq 0) {
                foreach ($psLine in ($psLines | Select-Object -First 10)) {
                    $trimmed = ([string]$psLine).Trim()
                    if ($trimmed -match '^(?<comm>.+?)\s+(?<rss>\d+)$') {
                        $comm = ($Matches.comm -replace '[^A-Za-z0-9_.+ -]', '').Trim()
                        if ($comm -and $comm.Length -le 64) { $processRss += [pscustomobject]@{ comm=$comm; rss_kib=[long]$Matches.rss } }
                    }
                }
            }
            $complete = $statsOk -and $cgOk -and $null -ne $cgObj -and
                $cgObj.memory_current_bytes -is [ValueType] -and $cgObj.memory_limit_bytes -is [ValueType] -and
                $cgObj.pids_current -is [ValueType] -and $cgObj.pids_limit -is [ValueType] -and
                $cgObj.memory_events.oom -is [ValueType] -and $cgObj.memory_events.oom_kill -is [ValueType]
            Write-Output ([pscustomobject]@{
                at_utc=[DateTime]::UtcNow.ToString('o'); complete=$complete; docker_stats=$statsObj
                memory_current_bytes=$cgObj.memory_current_bytes; memory_limit_bytes=$cgObj.memory_limit_bytes
                pids_current=$cgObj.pids_current; pids_limit=$cgObj.pids_limit; memory_events=$cgObj.memory_events
                top_process_rss=$processRss; memory_stat=[pscustomobject]@{ anon=$memoryStatFields.anon; file=$memoryStatFields.file; shmem=$memoryStatFields.shmem; kernel=$memoryStatFields.kernel }
            })
            Start-Sleep -Seconds 5
        }
    }
}

function Assert-ObservationHintsMode {
    param(
        [bool]$Enabled,
        [string]$Protocol,
        [bool]$ProbeOnly,
        [bool]$DiagnosticGate,
        [bool]$SkipCases,
        [bool]$P0Diagnostics,
        [bool]$RunCasesAfterReadableCandidate,
        [bool]$SoakChecks,
        [bool]$MemoryProbeChecks = $false,
        [bool]$MemoryAttributionChecks = $false,
        [bool]$MemoryMaintenance = $false
    )
    if (-not $Enabled) { return }
    if ($Protocol -ne 'generic') { throw 'ObservationHints requires the generic model protocol.' }
    if ($SoakChecks -and -not $SkipCases) { throw 'ObservationHints soak requires SkipCases.' }
    if ($MemoryProbeChecks -and -not $SkipCases) { throw 'ObservationHints memory probe requires SkipCases.' }
    if ($MemoryAttributionChecks -and -not $SkipCases) { throw 'ObservationHints memory attribution requires SkipCases.' }
    if ($MemoryMaintenance -and -not $SkipCases) { throw 'MemoryMaintenance requires SkipCases.' }
    if ($ProbeOnly -or $DiagnosticGate -or $RunCasesAfterReadableCandidate -or ($SkipCases -and -not ($SoakChecks -or $MemoryProbeChecks -or $MemoryAttributionChecks))) {
        throw 'ObservationHints requires a supported DemoQA case execution path or an explicitly bounded soak.'
    }
}

function Normalize-DemoqaCases {
    param([string[]]$Cases)
    $allowed = @('text_box', 'invalid_email', 'checkbox', 'web_table', 'radio', 'double_click', 'single_click', 'dynamic', 'notify_then_click')
    $normalized = @($Cases | ForEach-Object { ([string]$_).Split(',') } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    if ($normalized.Count -eq 0 -or @($normalized | Where-Object { $_ -notin $allowed }).Count -gt 0) {
        throw 'Cases contains an unknown or empty scenario name.'
    }
    return ,$normalized
}

try {
    if (-not (Test-Path -LiteralPath $ComposeFile -PathType Leaf)) { throw 'Disposable DemoQA compose file is missing.' }
    if ($RunLabel -notmatch '^[A-Za-z0-9_-]{1,20}$') { throw 'RunLabel may contain only letters, digits, underscores and dashes.' }
    $FontMatrixVariants = @($FontMatrixVariants | ForEach-Object { ([string]$_).Split(',') } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    $allowedFontVariants = @('baseline', 'arial_normal', 'arial_medium', 'system_normal', 'system_bold', 'monospace', 'system_thin100', 'arial_thin100', 'baseline_normal')
    if (@($FontMatrixVariants | Where-Object { $_ -notin $allowedFontVariants }).Count -gt 0) { throw 'FontMatrixVariants contains an unknown style.' }
    if ($useAttributionSeed) {
        if (Test-Path -LiteralPath $AttributionSeedPath -PathType Leaf) {
            $seedInfo = Get-ValidatedAttributionFingerprintSeed -Path $AttributionSeedPath
            $attributionSeedDigest = $seedInfo.Digest
            $attributionSeedVersion = $seedInfo.Version
        }
        elseif ($MemoryAttributionChecks -and $MemoryAttributionVariant -ne 'baseline') {
            throw 'The baseline attribution run must create the shared fingerprint seed before other variants.'
        }
        elseif ($SoakChecks -and $MemoryMaintenance) {
            throw 'MemoryMaintenance soak requires the existing shared attribution fingerprint seed.'
        }
    }
    $Cases = Normalize-DemoqaCases -Cases $Cases
    if ($ProbeOnly -and $TaskControlChecks) { throw 'TaskControlChecks cannot be combined with ProbeOnly.' }
    if ($RunCasesAfterReadableCandidate) {
        if (-not $ProbeOnly -or -not $ExtendedComplex -or ($Cases -join ',') -ne 'checkbox,web_table') {
            throw 'RunCasesAfterReadableCandidate requires ProbeOnly, ExtendedComplex, and exactly checkbox,web_table.'
        }
    }
    if ($P0Diagnostics -and ($ProbeOnly -or $DiagnosticGate -or $ReadinessChecks -or $TaskControlChecks -or $RunCasesAfterReadableCandidate)) {
        throw 'P0Diagnostics cannot be combined with probe, gate, readiness, task-control, or readability-gated modes.'
    }
    if ($RuntimeIntegrationChecks) {
        if (-not $SkipCases) {
            throw 'RuntimeIntegrationChecks requires SkipCases to avoid mixing integration checks with model-driven cases.'
        }
        if ($P0Diagnostics -or $ProbeOnly -or $DiagnosticGate -or $RunCasesAfterReadableCandidate) {
            throw 'RuntimeIntegrationChecks cannot be combined with P0, probe, diagnostic-gate, or readability-gated modes.'
        }
    }
    if ($LifecycleIntegrationChecks -and -not $RuntimeIntegrationChecks) {
        throw 'LifecycleIntegrationChecks requires RuntimeIntegrationChecks and SkipCases.'
    }
    if ($CookieBridgeChecks -and -not $RuntimeIntegrationChecks) {
        throw 'CookieBridgeChecks requires RuntimeIntegrationChecks and SkipCases.'
    }
    if ($MemoryProbeChecks) {
        if (-not $SkipCases) { throw 'MemoryProbeChecks requires SkipCases to avoid model-driven cases.' }
        if ($SoakChecks -or $RuntimeIntegrationChecks -or $LifecycleIntegrationChecks -or $CookieBridgeChecks -or
            $P0Diagnostics -or $ProbeOnly -or $DiagnosticGate -or $RunCasesAfterReadableCandidate -or
            $TaskControlChecks -or $ReadinessChecks) {
            throw 'MemoryProbeChecks must run standalone without soak, model cases, P0, or other integration modes.'
        }
    }
    if ($MemoryAttributionChecks) {
        if (-not $SkipCases) { throw 'MemoryAttributionChecks requires SkipCases to avoid model-driven cases.' }
        if (-not $ObservationHints) { throw 'MemoryAttributionChecks requires the disposable ObservationHints candidate.' }
        if ($ModelProtocol -ne 'generic') { throw 'MemoryAttributionChecks requires the generic model protocol.' }
        if ($SoakChecks -or $MemoryProbeChecks -or $RuntimeIntegrationChecks -or $LifecycleIntegrationChecks -or $CookieBridgeChecks -or
            $P0Diagnostics -or $ProbeOnly -or $DiagnosticGate -or $RunCasesAfterReadableCandidate -or
            $TaskControlChecks -or $ReadinessChecks) {
            throw 'MemoryAttributionChecks must run standalone without soak, other probes, model cases, P0, or integration modes.'
        }
    }
    if ($MemoryMaintenance) {
        if (-not $SoakChecks -or -not $SkipCases -or -not $ObservationHints -or $ModelProtocol -ne 'generic') {
            throw 'MemoryMaintenance requires SoakChecks, SkipCases, ObservationHints, and the generic model protocol.'
        }
        if ($MemoryAttributionChecks -or $MemoryAttributionGcContrast -or $MemoryAttributionMemoryReport -or $MemoryAttributionPeriodicGc -or
            $MemoryAttributionVariant -ne 'baseline' -or $MemoryProbeChecks -or $MemoryProbeGcContrast -or
            $RuntimeIntegrationChecks -or $LifecycleIntegrationChecks -or $CookieBridgeChecks -or
            $P0Diagnostics -or $ProbeOnly -or $DiagnosticGate -or $RunCasesAfterReadableCandidate -or
            $TaskControlChecks -or $ReadinessChecks) {
            throw 'MemoryMaintenance supports only the baseline generic observation-hints soak.'
        }
    }
    elseif ($SoakChecks -and ($MemoryAttributionChecks -or $MemoryAttributionGcContrast -or $MemoryAttributionMemoryReport -or $MemoryAttributionPeriodicGc)) {
        throw 'Memory attribution options cannot be combined with a soak.'
    }
    if ($MemoryAttributionGcContrast -and -not $MemoryAttributionChecks) {
        throw 'MemoryAttributionGcContrast requires MemoryAttributionChecks.'
    }
    if ($MemoryAttributionMemoryReport -and (-not $MemoryAttributionChecks -or $MemoryAttributionVariant -ne 'baseline' -or $MemoryAttributionGcContrast)) {
        throw 'MemoryAttributionMemoryReport requires the baseline attribution variant without GC contrast.'
    }
    if ($MemoryAttributionPeriodicGc -and (-not $MemoryAttributionChecks -or $MemoryAttributionVariant -ne 'baseline' -or $MemoryAttributionGcContrast -or $MemoryAttributionMemoryReport)) {
        throw 'MemoryAttributionPeriodicGc requires the baseline attribution variant without GC contrast or memory report.'
    }
    if ($MemoryProbeGcContrast -and (-not $MemoryProbeChecks -or $MemoryProbePhaseSet -ne 'mixed')) {
        throw 'MemoryProbeGcContrast requires MemoryProbeChecks with MemoryProbePhaseSet mixed.'
    }
    if ($SoakChecks) {
        if (-not $SkipCases) { throw 'SoakChecks requires SkipCases to avoid model-driven cases.' }
        if ($P0Diagnostics -or $ProbeOnly -or $DiagnosticGate -or $RunCasesAfterReadableCandidate -or $RuntimeIntegrationChecks -or $TaskControlChecks -or $ReadinessChecks) {
            throw 'SoakChecks cannot be combined with model cases, probes, diagnostics, readiness, or integration checks.'
        }
    }
    if ($BrowserMemoryMiB -eq 4096 -and (-not $SkipCases -or -not ($SoakChecks -or $MemoryProbeChecks -or $MemoryAttributionChecks))) {
        throw 'BrowserMemoryMiB 4096 requires SkipCases and a bounded soak or memory diagnostic.'
    }
    Assert-ObservationHintsMode -Enabled $ObservationHints -Protocol $ModelProtocol -ProbeOnly $ProbeOnly `
        -DiagnosticGate $DiagnosticGate -SkipCases $SkipCases -P0Diagnostics $P0Diagnostics `
        -RunCasesAfterReadableCandidate $RunCasesAfterReadableCandidate -SoakChecks $SoakChecks `
        -MemoryProbeChecks $MemoryProbeChecks -MemoryAttributionChecks $MemoryAttributionChecks -MemoryMaintenance $MemoryMaintenance
    if ($P0Diagnostics -and ($P0Scenarios.Count -eq 0 -or $P0Scenarios.Count -gt 4 -or @($P0Scenarios | Select-Object -Unique).Count -ne $P0Scenarios.Count)) {
        throw 'P0Scenarios must contain one to four unique allowlisted scenarios.'
    }
    $P0Scenarios = @($P0Scenarios | ForEach-Object { ([string]$_).Split(',') } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    $allowedP0 = @('fixture_tree', 'fixture_form', 'checkbox', 'web_table')
    if ($P0Diagnostics -and ($P0Scenarios.Count -eq 0 -or $P0Scenarios.Count -gt 4 -or
        @($P0Scenarios | Where-Object { $_ -notin $allowedP0 }).Count -gt 0 -or
        @($P0Scenarios | Select-Object -Unique).Count -ne $P0Scenarios.Count)) {
        throw 'P0Scenarios must contain one to four unique allowlisted scenarios.'
    }

    # Validate the exact endpoint and served alias before any Docker command can
    # interrupt the production browser. Compose receives this same validated URL.
    $env:DEMOQA_MODEL_BASE_URL = $ModelBaseUrl
    $env:DEMOQA_MODEL_ALIAS = $ModelAlias
    $env:DEMOQA_MODEL_PROTOCOL = $ModelProtocol
    $env:DEMOQA_BROWSER_MEMORY_LIMIT = ([string]$BrowserMemoryMiB + 'm')
    if (-not $MemoryAttributionChecks) {
        $modelPreflight = Invoke-NativeCaptured -FilePath 'python' -Arguments @((Join-Path $PSScriptRoot 'demoqa_model_preflight.py'), '--base-url', $ModelBaseUrl, '--alias', $ModelAlias)
        if ($modelPreflight.ExitCode -ne 0) { throw 'Configured model endpoint is unavailable or does not serve the required alias; production was left running.' }
    }

    $keyBytes = [byte[]]::new(48)
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($keyBytes) } finally { $rng.Dispose() }
    $workerKey = [Convert]::ToBase64String($keyBytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
    $env:DEMOQA_WORKER_KEY = $workerKey
    $env:DEMOQA_CASES = ($Cases -join ',')
    if ($P0Diagnostics) {
        $env:READINESS_ACTION_TRACE = '1'
        $env:READINESS_P0_SCENARIOS = ($P0Scenarios -join ',')
    }
    if ($RuntimeIntegrationChecks -or $SoakChecks -or $MemoryProbeChecks -or $MemoryAttributionChecks) { $env:READINESS_ACTION_TRACE = '1' }
    $env:BROWSER_MEMORY_MAINTENANCE = if ($MemoryMaintenance) { '1' } else { '0' }
    if ($useAttributionSeed) {
        $env:READINESS_SKIP_ACTION_SNAPSHOTS = if ($MemoryAttributionVariant -in @('no_trace', 'neither')) { '1' } else { '0' }
        $env:READINESS_DISABLE_DEFAULT_ADDONS = if ($MemoryAttributionVariant -in @('no_addons', 'neither')) { '1' } else { '0' }
    }
    else {
        $env:READINESS_SKIP_ACTION_SNAPSHOTS = '0'
        $env:READINESS_DISABLE_DEFAULT_ADDONS = '0'
    }
    if ($ObservationHints) { $env:BROWSER_OBSERVATION_HINTS = '1' }
    else { $env:BROWSER_OBSERVATION_HINTS = '0' }
    if ($ExtendedComplex) { $env:DEMOQA_EXTENDED_COMPLEX = '1' } else { Remove-Item Env:DEMOQA_EXTENDED_COMPLEX -ErrorAction SilentlyContinue }

    # Building the isolated image is safe while production is live and ensures it is ready
    # before the one production browser is paused.
    if (-not $SkipBuild) {
        Invoke-DockerChecked -Arguments ((Get-ComposeArgs) + @('build')) -FailureMessage 'Could not build the isolated DemoQA images' | Out-Null
    }

    # Never run two Camoufox workers at once. Inspect status before stopping the exact
    # production container, and refuse to interrupt any active worker or task.
    $existingTestQuery = Invoke-NativeCaptured -FilePath 'docker' -Arguments @('ps', '-aq', '--filter', "label=com.docker.compose.project=$Project")
    if ($existingTestQuery.ExitCode -ne 0) { throw 'Could not inspect existing disposable DemoQA containers; production was left running.' }
    $preexistingTestContainerIds = @($existingTestQuery.Output | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ })
    $activeTestBrowserQuery = Invoke-NativeCaptured -FilePath 'docker' -Arguments @('ps', '-q', '--filter', "label=com.docker.compose.project=$Project", '--filter', 'label=com.docker.compose.service=browser')
    $activeTestBrowserIds = @($activeTestBrowserQuery.Output | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ })
    if ($activeTestBrowserQuery.ExitCode -ne 0 -or $activeTestBrowserIds.Count -gt 0) {
        throw 'A disposable DemoQA browser is already running; refusing to start another worker.'
    }
    if ($useAttributionSeed) {
        foreach ($existingId in $preexistingTestContainerIds) {
            $existingName = Invoke-NativeCaptured -FilePath 'docker' -Arguments @('inspect', '-f', '{{.Name}}', $existingId)
            if ($existingName.ExitCode -ne 0) { throw 'Could not validate existing disposable project containers before attribution setup.' }
            $nameValue = (($existingName.Output -join '') -replace '^/', '').Trim()
            if ($nameValue -in @('browser-mcp-demoqa-test-browser-1', 'browser-mcp-demoqa-test-control-1')) {
                throw 'A candidate attribution service container already exists; preserving it and refusing to reuse its state.'
            }
        }
        $volumeName = 'browser-mcp-demoqa-test-worker-state'
        $volumeInfo = Invoke-NativeCaptured -FilePath 'docker' -Arguments @('volume', 'inspect', $volumeName)
        if ($volumeInfo.ExitCode -eq 0) {
            try { $volumeRecord = (($volumeInfo.Output -join '') | ConvertFrom-Json -ErrorAction Stop)[0] }
            catch { throw 'Could not validate ownership of the disposable attribution worker volume.' }
            if ($volumeRecord.Labels.'com.docker.compose.project' -ne $Project) { throw 'The existing worker volume is not owned by the disposable DemoQA project.' }
            foreach ($existingId in $preexistingTestContainerIds) {
                $mountsResult = Invoke-NativeCaptured -FilePath 'docker' -Arguments @('inspect', '-f', '{{json .Mounts}}', $existingId)
                if ($mountsResult.ExitCode -ne 0) { throw 'Could not check that preserved legacy containers do not reference the attribution worker volume.' }
                try { $mounts = (($mountsResult.Output -join '') | ConvertFrom-Json -ErrorAction Stop) }
                catch { throw 'Could not validate mounts on preserved legacy containers.' }
                if (@($mounts | Where-Object { $_.Type -eq 'volume' -and $_.Name -eq $volumeName }).Count -gt 0) {
                    throw 'The attribution worker volume is referenced by a preexisting container; preserving the volume and aborting.'
                }
            }
            $removeAttributionVolume = Invoke-NativeCaptured -FilePath 'docker' -Arguments @('volume', 'rm', $volumeName)
            if ($removeAttributionVolume.ExitCode -ne 0) { throw 'Could not reset the verified disposable attribution worker volume.' }
        }
    }

    $productionQuery = Invoke-NativeCaptured -FilePath 'docker' -Arguments @('ps', '-q', '--filter', 'label=com.docker.compose.project=browser-mcp-local', '--filter', 'label=com.docker.compose.service=browser')
    $productionWorkers = $productionQuery.Output
    if ($productionQuery.ExitCode -ne 0) { throw 'Could not inspect running Docker containers; no test services were started.' }
    $productionWorkers = @($productionWorkers | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ })
    if ($productionWorkers.Count -gt 1) { throw 'More than one production browser container is running; refusing to change services.' }
    if ($productionWorkers.Count -eq 1) {
        $productionContainerId = $productionWorkers[0]
        $productionWasRunning = $true
        $preflightArgs = @((Join-Path $PSScriptRoot 'demoqa_preflight.py'), '--control-url', $ProductionControlUrl)
        if ($AllowSuspendedTasks) { $preflightArgs += '--allow-suspended-tasks' }
        $preflight = Invoke-NativeCaptured -FilePath 'python' -Arguments $preflightArgs
        if ($preflight.ExitCode -ne 0) { throw 'Production preflight did not prove that its browser and tasks are idle; production was left running.' }
        try { $preflightState = ($preflight.Output -join '') | ConvertFrom-Json -ErrorAction Stop } catch { throw 'Production preflight returned unreadable state; production was left running.' }
        if ($preflightState.ready -ne $true) { throw 'Production preflight did not prove idle state; production was left running.' }
        $productionStopAttempted = $true
        Invoke-DockerChecked -Arguments @('stop', $productionContainerId) -FailureMessage 'Could not stop the idle production browser container' | Out-Null
        $productionStopped = $true
        $stillRunning = Invoke-NativeCaptured -FilePath 'docker' -Arguments @('ps', '-q', '--filter', "id=$productionContainerId")
        if ($stillRunning.ExitCode -ne 0 -or @($stillRunning.Output | Where-Object { [string]$_ }).Count -gt 0) { throw 'Production browser did not stop cleanly; disposable stack was not started.' }
    }

    # For attribution, create stopped containers first and inject the validated synthetic
    # fingerprint seed into a fresh disposable volume before Camoufox initializes.
    $stackCleanupNeeded = $true
    if ($useAttributionSeed) {
        Invoke-DockerChecked -Arguments ((Get-ComposeArgs) + @('create')) -FailureMessage 'Could not create stopped attribution containers' | Out-Null
        if ($attributionSeedDigest) {
            $stoppedBrowserRows = Invoke-NativeCaptured -FilePath 'docker' -Arguments ((Get-ComposeArgs) + @('ps', '-aq', 'browser'))
            $stoppedBrowserIds = @($stoppedBrowserRows.Output | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ })
            if ($stoppedBrowserRows.ExitCode -ne 0 -or $stoppedBrowserIds.Count -ne 1) { throw 'Could not identify the stopped attribution browser before seed import.' }
            $runningCheck = Invoke-NativeCaptured -FilePath 'docker' -Arguments @('ps', '-q', '--filter', "id=$($stoppedBrowserIds[0])")
            if ($runningCheck.ExitCode -ne 0 -or @($runningCheck.Output | Where-Object { [string]$_ }).Count -ne 0) { throw 'Attribution fingerprint seed import requires a stopped browser container.' }
            $attributionSeedStage = Join-Path (Join-Path $Root 'runtime/demoqa') ('.attribution-seed-stage-' + [Guid]::NewGuid().ToString('N'))
            $stageProfiles = Join-Path $attributionSeedStage 'profiles'
            New-Item -ItemType Directory -Path $stageProfiles -Force | Out-Null
            Copy-Item -LiteralPath $AttributionSeedPath -Destination (Join-Path $stageProfiles 'default.camoufox-fingerprint.json')
            Invoke-DockerChecked -Arguments @('cp', $stageProfiles, "$($stoppedBrowserIds[0]):/app/runtime/worker-state/") -FailureMessage 'Could not inject the validated shared fingerprint into the stopped disposable browser' | Out-Null
            $fixModeCode = "import hashlib,os,stat,sys; from pathlib import Path; root=Path('/seed').resolve(strict=True); p=Path('/seed/profiles/default.camoufox-fingerprint.json'); before=p.lstat(); ok=stat.S_ISREG(before.st_mode) and not stat.S_ISLNK(before.st_mode) and p.resolve(strict=True).is_relative_to(root); digest=hashlib.sha256(p.read_bytes()).hexdigest(); ok=ok and digest==sys.argv[1]; os.chmod(p,0o600,follow_symlinks=False) if ok else None; after=p.lstat(); ok=ok and stat.S_ISREG(after.st_mode) and stat.S_IMODE(after.st_mode)==0o600 and hashlib.sha256(p.read_bytes()).hexdigest()==sys.argv[1]; print('SEED_MODE_OK' if ok else 'SEED_MODE_REJECTED'); raise SystemExit(0 if ok else 1)"
            $mountSpec = 'type=volume,source=' + $volumeName + ',target=/seed'
            $modeFix = Invoke-NativeCaptured -FilePath 'docker' -Arguments @('run', '--rm', '--network', 'none', '--read-only', '--mount', $mountSpec, '--entrypoint', 'python', 'browser-mcp-demoqa-browser:test', '-c', $fixModeCode, $attributionSeedDigest)
            if ($modeFix.ExitCode -ne 0 -or (($modeFix.Output -join '').Trim() -ne 'SEED_MODE_OK')) {
                throw 'Could not apply and verify the required private mode on the shared fingerprint in the disposable volume.'
            }
        }
    }
    # Mark cleanup before `up`: compose can partially create the browser even when it fails.
    Invoke-DockerChecked -Arguments ((Get-ComposeArgs) + @('up', '-d', '--no-build')) -FailureMessage 'Could not start the disposable DemoQA stack' | Out-Null

    # Create the local UI cookie session first; otherwise the auth middleware can return a
    # browser redirect instead of JSON and obscure whether the test service is ready.
    $null = Get-Command Invoke-RestMethod -ErrorAction Stop
    $web = [Microsoft.PowerShell.Commands.WebRequestSession]::new()
    $uiReady = $false
    $deadline = [DateTime]::UtcNow.AddSeconds(90)
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $session = Invoke-RestMethod -Uri "$ServiceUrl/api/v1/operator/session" -Method Get -WebSession $web -TimeoutSec 3
            if ($session.authenticated -eq $true) { $uiReady = $true; break }
        } catch { Start-Sleep -Seconds 2 }
    }
    if (-not $uiReady) {
        Save-AttributionWorkerStartupDiagnostic
        throw 'Disposable local UI session did not become ready within 90 seconds.'
    }
    $script:DemoqaWebSession = $web

    $healthReady = $false
    $deadline = [DateTime]::UtcNow.AddSeconds(60)
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $health = Invoke-RestMethod -Uri "$ServiceUrl/health/ready" -Method Get -WebSession $web -TimeoutSec 3
            if ($health.status -eq 'ok' -or $health.ready -eq $true) { $healthReady = $true; break }
        } catch { Start-Sleep -Seconds 2 }
    }
    if (-not $healthReady) {
        Save-AttributionWorkerStartupDiagnostic
        throw 'Disposable control service health did not become ready within 60 seconds.'
    }

    if ($ReadinessChecks) {
        $readinessArgs = (Get-ComposeArgs) + @(
            'exec', '-T', '-e', 'APP_ENV=test', '-e', 'READINESS_DISPOSABLE=1',
            'control', 'python', '/app/scripts/readiness_live.py',
            '--url', 'http://127.0.0.1:8080', '--origin', 'http://localhost:18080',
            '--allow-disposable-internal', '--i-confirm-disposable',
            '--output', '/app/runtime/control-data/readiness-live-report.json'
        )
        $readinessCheckAttempted = $true
        $readinessExit = Invoke-RunnerStream -RunnerArgs $readinessArgs
        if ($readinessExit -ne 0) { throw "Disposable readiness checks failed (exit $readinessExit)." }
    }

    $profileDigestBefore = Get-TestProfileDigest
    if ($useAttributionSeed) {
        if (-not $attributionSeedDigest) {
            if ($MemoryMaintenance) { throw 'MemoryMaintenance cannot create a new fingerprint seed; expected the existing shared seed.' }
            New-Item -ItemType Directory -Path (Split-Path -Parent $AttributionSeedPath) -Force | Out-Null
            if (Test-Path -LiteralPath $AttributionSeedPath) { throw 'The shared attribution fingerprint seed appeared concurrently; refusing to overwrite it.' }
            $browserRows = Invoke-NativeCaptured -FilePath 'docker' -Arguments ((Get-ComposeArgs) + @('ps', '-q', 'browser'))
            $browserIds = @($browserRows.Output | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ })
            if ($browserRows.ExitCode -ne 0 -or $browserIds.Count -ne 1) { throw 'Could not identify the running browser to establish the first attribution seed.' }
            $seedCopy = Invoke-NativeCaptured -FilePath 'docker' -Arguments @('cp', "$($browserIds[0]):/app/runtime/worker-state/profiles/default.camoufox-fingerprint.json", $AttributionSeedPath)
            if ($seedCopy.ExitCode -ne 0) { throw 'Could not create the first shared attribution fingerprint seed from the disposable baseline.' }
            $seedInfo = Get-ValidatedAttributionFingerprintSeed -Path $AttributionSeedPath
            $attributionSeedDigest = $seedInfo.Digest
            $attributionSeedVersion = $seedInfo.Version
        }
        if ($profileDigestBefore -ne $attributionSeedDigest) { throw 'Disposable browser fingerprint does not match the shared attribution seed; workload was not started.' }
        $attributionSeedMatched = $true
        $seedRow = [pscustomobject]@{ scenario='attribution_fingerprint_seed'; status='verified'; matched=$true; version=$attributionSeedVersion }
        $resultLines.Add(($seedRow | ConvertTo-Json -Compress))
        Write-Host ($seedRow | ConvertTo-Json -Compress)
    }

    if ($ProbeOnly) {
        $gateDir = Join-Path (Join-Path $Root 'runtime/demoqa') $RunLabel
        New-Item -ItemType Directory -Path $gateDir -Force | Out-Null
        $readableGate = Join-Path $gateDir 'run-readable-cases'
        Remove-Item -LiteralPath $readableGate -Force -ErrorAction SilentlyContinue
        $env:DEMOQA_RUN_LABEL = $RunLabel
        $env:DEMOQA_FRAME_PREFIX = $ProbeFramePrefix
        $env:DEMOQA_FONT_VARIANTS = ($FontMatrixVariants -join ',')
        $env:DEMOQA_DIAGNOSTIC_HOLD_SECONDS = [string]$DiagnosticHoldSeconds
        $probePath = Join-Path $PSScriptRoot 'demoqa_live_probe.py'
        $probeExit = Invoke-HostProbe -ProbePath $probePath
        Remove-Item Env:DEMOQA_RUN_LABEL -ErrorAction SilentlyContinue
        Remove-Item Env:DEMOQA_FRAME_PREFIX -ErrorAction SilentlyContinue
        Remove-Item Env:DEMOQA_FONT_VARIANTS -ErrorAction SilentlyContinue
        Remove-Item Env:DEMOQA_DIAGNOSTIC_HOLD_SECONDS -ErrorAction SilentlyContinue
        if ($probeExit -ne 0) { throw "Read-only live probe failed (exit $probeExit)." }
        if ($RunCasesAfterReadableCandidate) {
            if (Test-Path -LiteralPath $readableGate -PathType Leaf) {
                Remove-Item -LiteralPath $readableGate -Force
                $runnerArgs = (Get-ComposeArgs) + @('exec', '-T', '-e', 'DEMOQA_CASES=checkbox,web_table', '-e', 'DEMOQA_EXTENDED_COMPLEX=1', 'control', 'python', '/app/scripts/test_demoqa.py')
                $testExitCode = Invoke-RunnerStream -RunnerArgs $runnerArgs
                Copy-DemoqaScreenshots
                if ($testExitCode -ne 0) { throw "Readability-gated DemoQA runner failed (exit $testExitCode)." }
            }
            else {
                $resultLines.Add('{"scenario":"font_readability_gate","status":"not_signaled","model_cases_run":false}')
                Write-Host 'Natural-font review gate was not signaled; checkbox and web_table were not run.'
            }
        }
    }
    elseif ($DiagnosticGate) {
        $diagnosticArgs = (Get-ComposeArgs) + @('exec', '-T', '-e', 'DEMOQA_DIAGNOSTIC_ONLY=1', 'control', 'python', '/app/scripts/test_demoqa.py')
        $diagnosticExit = Invoke-RunnerStream -RunnerArgs $diagnosticArgs
        if ($diagnosticExit -ne 0) { throw "Diagnostic probe failed (exit $diagnosticExit)." }
        Copy-DemoqaScreenshots
        Write-Host "Font probe is live at $ServiceUrl/desktop; waiting up to $DiagnosticHoldSeconds seconds for review before starting model cases."
        $gateDir = Join-Path (Join-Path $Root 'runtime/demoqa') $RunLabel
        New-Item -ItemType Directory -Path $gateDir -Force | Out-Null
        $gateFile = Join-Path $gateDir 'continue-cases'
        $continueCases = $false
        $reviewDeadline = [DateTime]::UtcNow.AddSeconds($DiagnosticHoldSeconds)
        while ([DateTime]::UtcNow -lt $reviewDeadline) {
            if (Test-Path -LiteralPath $gateFile -PathType Leaf) {
                Remove-Item -LiteralPath $gateFile -Force
                $continueCases = $true
                break
            }
            Start-Sleep -Seconds 1
        }
        if (-not $continueCases) {
            $resultLines.Add('{"scenario":"font_gate","status":"review_timeout","model_cases_run":false}')
            Write-Host 'No diagnostic continuation signal arrived; no model cases were run.'
        }
    }

    if ($P0Diagnostics) {
        $hintFlag = if ($ObservationHints) { '1' } else { '0' }
        $p0Args = (Get-ComposeArgs) + @('exec', '-T', '-e', 'APP_ENV=test', '-e', 'READINESS_ACTION_TRACE=1',
            '-e', "READINESS_P0_SCENARIOS=$($P0Scenarios -join ',')", '-e', "BROWSER_OBSERVATION_HINTS=$hintFlag",
            '-e', 'READINESS_P0_OUTPUT=/app/runtime/control-data/readiness-p0-report.json',
            'control', 'python', '/app/scripts/readiness_p0.py')
        $p0ReportAttempted = $true
        $p0Exit = Invoke-RunnerStream -RunnerArgs $p0Args
        if ($p0Exit -ne 0) { throw "P0 diagnostic runner failed (exit $p0Exit)." }
    }

    if (-not $P0Diagnostics -and -not $ProbeOnly -and -not $SkipCases -and (-not $DiagnosticGate -or $continueCases)) {
        $runnerArgs = (Get-ComposeArgs) + @('exec', '-T', '-e', "DEMOQA_CASES=$($Cases -join ',')")
        if ($ExtendedComplex) { $runnerArgs += @('-e', 'DEMOQA_EXTENDED_COMPLEX=1') }
        $runnerArgs += @('control', 'python', '/app/scripts/test_demoqa.py')
        $testExitCode = Invoke-RunnerStream -RunnerArgs $runnerArgs
        Copy-DemoqaScreenshots
        if ($testExitCode -ne 0) { throw "DemoQA scenario runner failed (exit $testExitCode)." }
    }
    elseif ($SkipCases -and -not $ProbeOnly) {
        $resultLines.Add('{"scenario":"demoqa_cases","status":"skipped_by_request"}')
    }

    if ($TaskControlChecks) {
        $taskControlArgs = (Get-ComposeArgs) + @(
            'exec', '-T', '-e', 'APP_ENV=test', '-e', 'READINESS_DISPOSABLE=1',
            'control', 'python', '/app/scripts/readiness_task_control.py',
            '--i-confirm-disposable', '--output', '/app/runtime/control-data/readiness-task-control.json'
        )
        $taskControlExit = Invoke-RunnerStream -RunnerArgs $taskControlArgs
        if ($taskControlExit -ne 0) { throw "Disposable task control checks failed (exit $taskControlExit)." }
    }

    if ($RuntimeIntegrationChecks) {
        $runtimeIntegrationArgs = (Get-ComposeArgs) + @(
            'exec', '-T', '-e', 'APP_ENV=test', '-e', 'READINESS_DISPOSABLE=1',
            '-e', 'READINESS_RUNTIME_INTEGRATION=1', '-e', 'READINESS_ACTION_TRACE=1',
            '-e', 'BROWSER_WORKER_URL=http://browser:7070',
            'control', 'python', '/app/scripts/readiness_runtime_integration.py', '--i-confirm-disposable'
        )
        $runtimeIntegrationExit = Invoke-RunnerStream -RunnerArgs $runtimeIntegrationArgs

        $overlayIntegrationArgs = (Get-ComposeArgs) + @(
            'exec', '-T', '-e', 'APP_ENV=test', '-e', 'READINESS_DISPOSABLE=1',
            '-e', 'READINESS_RUNTIME_INTEGRATION=1', '-e', 'READINESS_ACTION_TRACE=1',
            '-e', 'BROWSER_WORKER_URL=http://browser:7070',
            'control', 'python', '/app/scripts/readiness_overlay_integration.py'
        )
        $overlayIntegrationExit = Invoke-RunnerStream -RunnerArgs $overlayIntegrationArgs
        $lifecycleIntegrationExit = 0
        if ($LifecycleIntegrationChecks) {
            $lifecycleIntegrationArgs = (Get-ComposeArgs) + @(
                'exec', '-T', '-e', 'APP_ENV=test', '-e', 'READINESS_DISPOSABLE=1',
                '-e', 'READINESS_RUNTIME_INTEGRATION=1', '-e', 'READINESS_ACTION_TRACE=1',
                '-e', 'BROWSER_WORKER_URL=http://browser:7070',
                'control', 'python', '/app/scripts/readiness_lifecycle_integration.py', '--i-confirm-disposable'
            )
            $lifecycleIntegrationExit = Invoke-RunnerStream -RunnerArgs $lifecycleIntegrationArgs
        }
        $cookieBridgeExit = 0
        if ($CookieBridgeChecks) {
            $cookieBridgeArgs = (Get-ComposeArgs) + @(
                'exec', '-T', '-e', 'APP_ENV=test', '-e', 'READINESS_DISPOSABLE=1',
                '-e', 'READINESS_RUNTIME_INTEGRATION=1', '-e', 'READINESS_ACTION_TRACE=1',
                '-e', 'BROWSER_WORKER_URL=http://browser:7070',
                '-e', 'WORKER_API_KEY_FILE=/run/demoqa-secrets/key',
                'control', 'python', '/app/scripts/readiness_cookie_bridge.py',
                '--worker-url', 'http://browser:7070', '--key-file', '/run/demoqa-secrets/key',
                '--i-confirm-disposable'
            )
            $cookieBridgeExit = Invoke-RunnerStream -RunnerArgs $cookieBridgeArgs
        }
        if ($runtimeIntegrationExit -ne 0 -or $overlayIntegrationExit -ne 0 -or
            $lifecycleIntegrationExit -ne 0 -or $cookieBridgeExit -ne 0) {
            throw "Disposable runtime integration checks failed (runtime $runtimeIntegrationExit, overlay $overlayIntegrationExit, lifecycle $lifecycleIntegrationExit, cookie bridge $cookieBridgeExit)."
        }
    }

    if ($SoakChecks) {
        $soakArgs = (Get-ComposeArgs) + @(
            'exec', '-T', '-e', 'APP_ENV=test', '-e', 'READINESS_DISPOSABLE=1',
            '-e', 'READINESS_RUNTIME_INTEGRATION=1', '-e', 'READINESS_ACTION_TRACE=1',
            '-e', 'BROWSER_WORKER_URL=http://browser:7070',
            '-e', ('BROWSER_OBSERVATION_HINTS=' + $(if ($ObservationHints) { '1' } else { '0' })),
            'control', 'python', '/app/scripts/readiness_soak.py',
            '--url', 'http://browser:7070', '--control-url', 'http://localhost:8080',
            '--key-file', '/run/demoqa-secrets/key', '--duration-seconds', [string]$SoakSeconds,
            '--output', '/app/runtime/control-data/readiness-soak.json', '--i-confirm-disposable'
        )
        $browserIdRows = Invoke-NativeCaptured -FilePath 'docker' -Arguments ((Get-ComposeArgs) + @('ps', '-q', 'browser'))
        $browserIds = @($browserIdRows.Output | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ })
        if ($browserIdRows.ExitCode -ne 0 -or $browserIds.Count -ne 1) { throw 'Could not identify the single disposable browser for resource sampling.' }
        $metricJob = Start-BoundedHostMetricSampler -ContainerId $browserIds[0] -DurationSeconds $SoakSeconds
        $soakReportAttempted = $true
        $metricPath = Join-Path (Join-Path $Root 'runtime/demoqa') "$RunLabel/soak-host-metrics.json"
        $metricDirectory = Split-Path -Parent $metricPath
        New-Item -ItemType Directory -Path $metricDirectory -Force | Out-Null
        try {
            $soakExit = Invoke-RunnerStream -RunnerArgs $soakArgs
        }
        finally {
            if ($metricJob.State -eq 'Running') { Stop-Job -Job $metricJob -ErrorAction SilentlyContinue }
            $metricSamples = @(Receive-Job -Job $metricJob -ErrorAction SilentlyContinue)
            Remove-Job -Job $metricJob -Force -ErrorAction SilentlyContinue
            $metricJob = $null
            $partialMetricReport = [pscustomobject]@{ schema='readiness-soak-host-metrics/v1'; samples=$metricSamples; sample_count=$metricSamples.Count; resource_gate_pass=$false }
            [System.IO.File]::WriteAllText($metricPath, ($partialMetricReport | ConvertTo-Json -Depth 12) + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
            $processSnapshot = Get-SanitizedProcessRss -ContainerId $browserIds[0]
            $processSnapshotPath = Join-Path $metricDirectory 'soak-final-process-rss.json'
            $processReport = [pscustomobject]@{ schema='readiness-soak-process-rss/v1'; captured_at_utc=[DateTime]::UtcNow.ToString('o'); source='disposable-browser-container'; status=$processSnapshot.status; processes=$processSnapshot.processes }
            [System.IO.File]::WriteAllText($processSnapshotPath, ($processReport | ConvertTo-Json -Depth 6) + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
        }
        $completeSamples = @($metricSamples | Where-Object { $_.complete -eq $true })
        $minSamples = [Math]::Max(2, [Math]::Ceiling($SoakSeconds / 15.0))
        $sampleCoverageSeconds = 0.0
        if ($completeSamples.Count -ge 2) {
            $sampleTimes = @($completeSamples | ForEach-Object { [DateTime]::Parse($_.at_utc).ToUniversalTime() })
            $sampleCoverageSeconds = ($sampleTimes[-1] - $sampleTimes[0]).TotalSeconds
        }
        $memoryLimit = 0L; $pidsLimit = 0L; $peakMemory = 0L; $peakPids = 0L
        $oomDelta = $null; $memoryGrowthBytes = $null; $metricsComplete = $false
        $memoryPercent = $null; $pidsPercent = $null; $growthPercentLimit = $null
        if ($completeSamples.Count -ge 2) {
            $memoryLimit = [long]$completeSamples[0].memory_limit_bytes
            $pidsLimit = [long]$completeSamples[0].pids_limit
            $peakMemory = [long](($completeSamples | Measure-Object -Property memory_current_bytes -Maximum).Maximum)
            $peakPids = [long](($completeSamples | Measure-Object -Property pids_current -Maximum).Maximum)
            $firstEvents = $completeSamples[0].memory_events; $lastEvents = $completeSamples[-1].memory_events
            $oomDelta = [pscustomobject]@{
                oom=[long]$lastEvents.oom - [long]$firstEvents.oom
                oom_kill=[long]$lastEvents.oom_kill - [long]$firstEvents.oom_kill
            }
            $thirdCount = [Math]::Max(1, [Math]::Ceiling($completeSamples.Count / 3.0))
            $firstThird = @($completeSamples | Select-Object -First $thirdCount)
            $lastThird = @($completeSamples | Select-Object -Last $thirdCount)
            $firstMedian = Get-NumericMedian -Values @($firstThird | ForEach-Object { [double]$_.memory_current_bytes })
            $lastMedian = Get-NumericMedian -Values @($lastThird | ForEach-Object { [double]$_.memory_current_bytes })
            $memoryGrowthBytes = [long]($lastMedian - $firstMedian)
            if ($memoryLimit -gt 0 -and $pidsLimit -gt 0) {
                $memoryPercent = [Math]::Round(100.0 * $peakMemory / $memoryLimit, 2)
                $pidsPercent = [Math]::Round(100.0 * $peakPids / $pidsLimit, 2)
                $growthPercentLimit = [Math]::Round(100.0 * $memoryGrowthBytes / $memoryLimit, 2)
                $metricsComplete = $completeSamples.Count -eq $metricSamples.Count -and $completeSamples.Count -ge $minSamples -and
                    $sampleCoverageSeconds -ge [Math]::Max(0, $SoakSeconds - 15) -and
                    $memoryLimit -gt 0 -and $pidsLimit -gt 0 -and $memoryPercent -lt 90 -and $pidsPercent -lt 90 -and
                    $oomDelta.oom -eq 0 -and $oomDelta.oom_kill -eq 0 -and $growthPercentLimit -le 10
            }
        }
        $metricReport = [pscustomobject]@{
            schema='readiness-soak-host-metrics/v1'; samples=$metricSamples
            sample_count=$metricSamples.Count; sample_coverage_seconds=[Math]::Round($sampleCoverageSeconds,2)
            thresholds=[pscustomobject]@{ minimum_samples=$minSamples; max_memory_and_pids_percent=90; max_memory_growth_percent_of_limit=10; oom_delta=0 }
            measured=[pscustomobject]@{ memory_limit_bytes=$memoryLimit; pids_limit=$pidsLimit; peak_memory_bytes=$peakMemory; peak_pids=$peakPids; memory_peak_percent=$memoryPercent; pids_peak_percent=$pidsPercent; memory_growth_bytes=$memoryGrowthBytes; memory_growth_percent_of_limit=$growthPercentLimit; oom_delta=$oomDelta }
            resource_gate_pass=$metricsComplete
        }
        [System.IO.File]::WriteAllText($metricPath, ($metricReport | ConvertTo-Json -Depth 12) + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
        if ($soakExit -ne 0) { throw "Disposable bounded soak failed functionally (exit $soakExit); host resource evidence was preserved separately." }
        if (-not $metricsComplete) { throw 'Soak resource evidence is incomplete or crossed a predeclared memory, PID, OOM, or growth threshold.' }
    }
    if ($MemoryProbeChecks) {
        $probeArgs = (Get-ComposeArgs) + @(
            'exec', '-T', '-e', 'APP_ENV=test', '-e', 'READINESS_DISPOSABLE=1',
            '-e', 'READINESS_RUNTIME_INTEGRATION=1', '-e', 'READINESS_ACTION_TRACE=1',
            '-e', 'BROWSER_WORKER_URL=http://browser:7070',
            '-e', ('BROWSER_OBSERVATION_HINTS=' + $(if ($ObservationHints) { '1' } else { '0' })),
            'control', 'python', '-m', 'scripts.readiness_memory_probe',
            '--url', 'http://browser:7070', '--control-url', 'http://localhost:8080',
            '--key-file', '/run/demoqa-secrets/key', '--duration-seconds', [string]$MemoryProbeSeconds,
            '--phase-set', $MemoryProbePhaseSet,
            '--output', '/app/runtime/control-data/readiness-memory-probe.json', '--i-confirm-disposable'
        )
        if ($MemoryProbeGcContrast) { $probeArgs += '--gc-contrast' }
        $browserIdRows = Invoke-NativeCaptured -FilePath 'docker' -Arguments ((Get-ComposeArgs) + @('ps', '-q', 'browser'))
        $browserIds = @($browserIdRows.Output | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ })
        if ($browserIdRows.ExitCode -ne 0 -or $browserIds.Count -ne 1) { throw 'Could not identify the single disposable browser for memory-probe metrics.' }
        $metricJob = Start-BoundedHostMetricSampler -ContainerId $browserIds[0] -DurationSeconds $MemoryProbeSeconds
        $memoryProbeReportAttempted = $true
        $probeExit = 1
        $probeDirectory = Join-Path (Join-Path $Root 'runtime/demoqa') $RunLabel
        New-Item -ItemType Directory -Path $probeDirectory -Force | Out-Null
        try {
            $probeExit = Invoke-RunnerStream -RunnerArgs $probeArgs
        }
        finally {
            if ($metricJob.State -eq 'Running') { Stop-Job -Job $metricJob -ErrorAction SilentlyContinue }
            $metricSamples = @(Receive-Job -Job $metricJob -ErrorAction SilentlyContinue)
            Remove-Job -Job $metricJob -Force -ErrorAction SilentlyContinue
            $metricJob = $null
            $completeProbeSamples = @($metricSamples | Where-Object { $_.complete -eq $true })
            $probePeakMemory = $null; $probePeakPids = $null; $probeMemoryLimit = $null; $probePidsLimit = $null
            $probeOomDelta = $null
            if ($completeProbeSamples.Count -ge 2) {
                $probePeakMemory = [long](($completeProbeSamples | Measure-Object -Property memory_current_bytes -Maximum).Maximum)
                $probePeakPids = [long](($completeProbeSamples | Measure-Object -Property pids_current -Maximum).Maximum)
                $probeMemoryLimit = [long]$completeProbeSamples[0].memory_limit_bytes
                $probePidsLimit = [long]$completeProbeSamples[0].pids_limit
                $firstEvents = $completeProbeSamples[0].memory_events; $lastEvents = $completeProbeSamples[-1].memory_events
                $probeOomDelta = [pscustomobject]@{ oom=([long]$lastEvents.oom - [long]$firstEvents.oom); oom_kill=([long]$lastEvents.oom_kill - [long]$firstEvents.oom_kill) }
            }
            $metricPath = Join-Path $probeDirectory 'memory-probe-host-metrics.json'
            $probeMetricReport = [pscustomobject]@{
                schema='readiness-memory-probe-host-metrics/v1'; samples=$metricSamples; sample_count=$metricSamples.Count
                complete_sample_count=$completeProbeSamples.Count
                measured=[pscustomobject]@{ memory_limit_bytes=$probeMemoryLimit; pids_limit=$probePidsLimit; peak_memory_bytes=$probePeakMemory; peak_pids=$probePeakPids; oom_delta=$probeOomDelta }
            }
            [System.IO.File]::WriteAllText($metricPath, ($probeMetricReport | ConvertTo-Json -Depth 12) + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
            $processSnapshot = Get-SanitizedProcessRss -ContainerId $browserIds[0]
            $processSnapshotPath = Join-Path $probeDirectory 'memory-probe-final-process-rss.json'
            $processReport = [pscustomobject]@{ schema='readiness-soak-process-rss/v1'; captured_at_utc=[DateTime]::UtcNow.ToString('o'); source='disposable-browser-container'; status=$processSnapshot.status; processes=$processSnapshot.processes }
            [System.IO.File]::WriteAllText($processSnapshotPath, ($processReport | ConvertTo-Json -Depth 6) + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
        }
        if ($probeExit -ne 0) { throw "Disposable bounded memory diagnostic failed (exit $probeExit); phase and host metrics were preserved." }
    }
    if ($MemoryAttributionChecks) {
        $attributionArgs = (Get-ComposeArgs) + @(
            'exec', '-T', '-e', 'APP_ENV=test', '-e', 'READINESS_DISPOSABLE=1',
            '-e', 'READINESS_RUNTIME_INTEGRATION=1', '-e', 'READINESS_ACTION_TRACE=1',
            '-e', 'BROWSER_WORKER_URL=http://browser:7070',
            '-e', ('BROWSER_OBSERVATION_HINTS=' + $(if ($ObservationHints) { '1' } else { '0' })),
            '-e', ('READINESS_SKIP_ACTION_SNAPSHOTS=' + $(if ($MemoryAttributionVariant -in @('no_trace', 'neither')) { '1' } else { '0' })),
            '-e', ('READINESS_DISABLE_DEFAULT_ADDONS=' + $(if ($MemoryAttributionVariant -in @('no_addons', 'neither')) { '1' } else { '0' })),
            'control', 'python', '-m', 'scripts.readiness_memory_attribution',
            '--url', 'http://browser:7070', '--control-url', 'http://localhost:8080',
            '--key-file', '/run/demoqa-secrets/key', '--variant', $MemoryAttributionVariant,
            '--output', '/app/runtime/control-data/readiness-memory-attribution.json', '--i-confirm-disposable'
        )
        if ($MemoryAttributionGcContrast) { $attributionArgs += '--gc-contrast' }
        if ($MemoryAttributionMemoryReport) { $attributionArgs += '--memory-report' }
        if ($MemoryAttributionPeriodicGc) { $attributionArgs += '--periodic-gc' }
        $browserIdRows = Invoke-NativeCaptured -FilePath 'docker' -Arguments ((Get-ComposeArgs) + @('ps', '-q', 'browser'))
        $browserIds = @($browserIdRows.Output | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ })
        if ($browserIdRows.ExitCode -ne 0 -or $browserIds.Count -ne 1) { throw 'Could not identify the single disposable browser for memory-attribution metrics.' }
        $metricJob = Start-BoundedHostMetricSampler -ContainerId $browserIds[0] -DurationSeconds 300
        $memoryAttributionReportAttempted = $true
        $attributionExit = 1
        $attributionDirectory = Join-Path (Join-Path $Root 'runtime/demoqa') $RunLabel
        New-Item -ItemType Directory -Path $attributionDirectory -Force | Out-Null
        try { $attributionExit = Invoke-RunnerStream -RunnerArgs $attributionArgs }
        finally {
            if ($metricJob.State -eq 'Running') { Stop-Job -Job $metricJob -ErrorAction SilentlyContinue }
            $metricSamples = @(Receive-Job -Job $metricJob -ErrorAction SilentlyContinue)
            Remove-Job -Job $metricJob -Force -ErrorAction SilentlyContinue
            $metricJob = $null
            $completeSamples = @($metricSamples | Where-Object { $_.complete -eq $true })
            $safeSamples = @($metricSamples | ForEach-Object {
                $event = $_.memory_events
                $selectedEvents = [ordered]@{}
                foreach ($eventName in @('low','high','max','oom','oom_kill','oom_group_kill')) {
                    if ($event -and $null -ne $event.$eventName) { $selectedEvents[$eventName] = [long]$event.$eventName }
                }
                [pscustomobject]@{
                    at_utc=$_.at_utc; complete=($_.complete -eq $true)
                    memory_current_bytes=$_.memory_current_bytes; memory_limit_bytes=$_.memory_limit_bytes
                    pids_current=$_.pids_current; pids_limit=$_.pids_limit
                    memory_events=[pscustomobject]$selectedEvents
                    memory_stat=[pscustomobject]@{ anon=$_.memory_stat.anon; file=$_.memory_stat.file; shmem=$_.memory_stat.shmem; kernel=$_.memory_stat.kernel }
                    top_process_rss=@($_.top_process_rss | ForEach-Object { if ($_.comm -match '^[A-Za-z0-9_.+ -]{1,64}$' -and $_.rss_kib -is [ValueType]) { [pscustomobject]@{comm=$_.comm;rss_kib=[long]$_.rss_kib} } })
                }
            })
            $attributionMetricReport = [pscustomobject]@{
                schema='readiness-memory-attribution-host-metrics/v1'; samples=$safeSamples; sample_count=$safeSamples.Count
                complete_sample_count=$completeSamples.Count
                measured=[pscustomobject]@{
                    memory_limit_bytes=$(if ($completeSamples.Count) { [long]$completeSamples[0].memory_limit_bytes } else { $null })
                    pids_limit=$(if ($completeSamples.Count) { [long]$completeSamples[0].pids_limit } else { $null })
                    peak_memory_bytes=$(if ($completeSamples.Count) { [long](($completeSamples | Measure-Object -Property memory_current_bytes -Maximum).Maximum) } else { $null })
                    peak_pids=$(if ($completeSamples.Count) { [long](($completeSamples | Measure-Object -Property pids_current -Maximum).Maximum) } else { $null })
                }
            }
            $metricPath = Join-Path $attributionDirectory 'memory-attribution-host-metrics.json'
            [System.IO.File]::WriteAllText($metricPath, ($attributionMetricReport | ConvertTo-Json -Depth 12) + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
            $processSnapshot = Get-SanitizedProcessRss -ContainerId $browserIds[0]
            $processPath = Join-Path $attributionDirectory 'memory-attribution-final-process-rss.json'
            $processReport = [pscustomobject]@{ schema='readiness-soak-process-rss/v1'; captured_at_utc=[DateTime]::UtcNow.ToString('o'); source='disposable-browser-container'; status=$processSnapshot.status; processes=$processSnapshot.processes }
            [System.IO.File]::WriteAllText($processPath, ($processReport | ConvertTo-Json -Depth 6) + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
        }
        if ($attributionExit -ne 0) { throw "Disposable memory-attribution diagnostic failed (exit $attributionExit); lifecycle and host metrics were preserved." }
    }
    $profileDigestAfter = Get-TestProfileDigest
    if ($useAttributionSeed) {
        $attributionSeedMatched = $attributionSeedMatched -and ($profileDigestAfter -eq $attributionSeedDigest)
        $profileDigestRow = [pscustomobject]@{ scenario='attribution_fingerprint_seed'; status='verified_after_workload'; matched=$attributionSeedMatched; version=$attributionSeedVersion }
    }
    else {
        $profileDigestRow = [pscustomobject]@{ scenario = 'test_profile_fingerprint'; status = 'read_only'; sha256_before = $profileDigestBefore; sha256_after = $profileDigestAfter; unchanged = ($profileDigestBefore -eq $profileDigestAfter) }
    }
    $profileDigestJson = $profileDigestRow | ConvertTo-Json -Compress
    $resultLines.Add($profileDigestJson)
    Write-Host $profileDigestJson
    if ($profileDigestBefore -ne $profileDigestAfter) { throw 'Disposable worker fingerprint changed during the run; same-profile comparison is invalid.' }
}
finally {
    if ($metricJob -and $metricJob.State -eq 'Running') { Stop-Job -Job $metricJob -ErrorAction SilentlyContinue; Remove-Job -Job $metricJob -Force -ErrorAction SilentlyContinue }
    $cleanupError = $null
    if ($readinessCheckAttempted) {
        try { Export-ReadinessReport }
        catch {
            $readinessReportError = 'Could not export readiness evidence before disposable stack cleanup.'
            $resultLines.Add('{"scenario":"readiness_live","status":"report_export_failed"}')
        }
    }
    if ($p0ReportAttempted) {
        try { Export-P0Report }
        catch {
            $p0ReportError = 'Could not export P0 evidence before disposable stack cleanup.'
            $resultLines.Add('{"scenario":"readiness_p0","status":"report_export_failed"}')
        }
    }
    if ($soakReportAttempted) {
        try { Export-SoakReport }
        catch {
            $soakReportError = 'Could not export soak evidence before disposable stack cleanup.'
            $resultLines.Add('{"scenario":"readiness_soak","status":"report_export_failed"}')
        }
    }
    if ($memoryProbeReportAttempted) {
        try { Export-MemoryProbeReport }
        catch {
            $memoryProbeReportError = 'Could not export memory-probe evidence before disposable stack cleanup.'
            $resultLines.Add('{"scenario":"readiness_memory_probe","status":"report_export_failed"}')
        }
    }
    if ($memoryAttributionReportAttempted) {
        try { Export-MemoryAttributionReport }
        catch {
            $memoryAttributionReportError = 'Could not export memory-attribution evidence before disposable stack cleanup.'
            $resultLines.Add('{"scenario":"readiness_memory_attribution","status":"report_export_failed"}')
        }
    }
    if ($stackCleanupNeeded) {
        # Scope cleanup to this project's name; the stack uses no persistent volumes.
        $downArgs = (Get-ComposeArgs) + @('down')
        if (-not $MemoryAttributionChecks) { $downArgs += '--remove-orphans' }
        $down = Invoke-NativeCaptured -FilePath 'docker' -Arguments $downArgs
        if ($down.ExitCode -ne 0) {
            $retryDown = Invoke-NativeCaptured -FilePath 'docker' -Arguments $downArgs
        }
        $containersRemoved = Wait-TestProjectContainersRemoved -TimeoutSeconds 30 -IgnoreContainerIds $preexistingTestContainerIds
        if (-not $containersRemoved) {
            # Retry only this disposable compose project, then allow Docker time to
            # finish asynchronous stops before deciding whether production may resume.
            $retryDown = Invoke-NativeCaptured -FilePath 'docker' -Arguments $downArgs
            $containersRemoved = Wait-TestProjectContainersRemoved -TimeoutSeconds 15 -IgnoreContainerIds $preexistingTestContainerIds
        }
        if (-not $containersRemoved) {
            $testBrowserResult = Invoke-NativeCaptured -FilePath 'docker' -Arguments @('ps', '-aq', '--filter', "label=com.docker.compose.project=$Project", '--filter', 'label=com.docker.compose.service=browser')
            $testBrowserIds = @($testBrowserResult.Output | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ -and $_ -notin $preexistingTestContainerIds })
            if ($testBrowserResult.ExitCode -eq 0 -and $testBrowserIds.Count -gt 0) {
                $stopTest = Invoke-NativeCaptured -FilePath 'docker' -Arguments (@('stop') + $testBrowserIds)
            }
            $cleanupError = 'Could not verify removal of disposable DemoQA containers; production restore is blocked until cleanup is confirmed.'
        }
    }
    if ($productionWasRunning -and $productionStopAttempted) {
        $restoreError = $null
        $testWorkers = Invoke-NativeCaptured -FilePath 'docker' -Arguments @('ps', '-q', '--filter', "label=com.docker.compose.project=$Project", '--filter', 'label=com.docker.compose.service=browser')
        if ($testWorkers.ExitCode -ne 0 -or @($testWorkers.Output | Where-Object { [string]$_ }).Count -gt 0) {
            $restoreError = 'Test browser is still running; production will not be restarted concurrently.'
        }
        else {
            $currentProduction = Invoke-NativeCaptured -FilePath 'docker' -Arguments @('ps', '-q', '--filter', "id=$productionContainerId")
            if ($currentProduction.ExitCode -ne 0) { $restoreError = 'Could not verify production browser state before restore.' }
            else {
                if (@($currentProduction.Output | Where-Object { [string]$_ }).Count -eq 0) {
                    $startProduction = Invoke-NativeCaptured -FilePath 'docker' -Arguments @('start', $productionContainerId)
                    if ($startProduction.ExitCode -ne 0) { $restoreError = 'Could not restore the production browser container.' }
                }
                if (-not $restoreError) {
                    $verifiedProduction = Invoke-NativeCaptured -FilePath 'docker' -Arguments @('ps', '-q', '--filter', "id=$productionContainerId")
                    if ($verifiedProduction.ExitCode -ne 0 -or @($verifiedProduction.Output | Where-Object { [string]$_ }).Count -ne 1) {
                        $restoreError = 'Production browser did not return to its original running state.'
                    }
                    elseif (-not (Wait-ProductionReady)) {
                        $restoreError = 'Production browser is running, but health/ready and idle worker state did not recover.'
                    }
                }
            }
        }
        if ($restoreError) {
            if ($cleanupError) { $cleanupError = "$cleanupError $restoreError" }
            else { $cleanupError = $restoreError }
        }
    }
    if ($RemoveTestVolume) {
        $volumeName = 'browser-mcp-demoqa-test-worker-state'
        $volumeInfo = Invoke-NativeCaptured -FilePath 'docker' -Arguments @('volume', 'inspect', $volumeName)
        if ($volumeInfo.ExitCode -eq 0) {
            try { $volumeRecord = (($volumeInfo.Output -join '') | ConvertFrom-Json -ErrorAction Stop)[0] }
            catch { $cleanupError = 'Could not verify the project label before removing the test worker volume.' }
            if (-not $cleanupError -and $volumeRecord.Labels.'com.docker.compose.project' -ne $Project) {
                $cleanupError = 'The named volume is not owned by this disposable DemoQA project; it was kept.'
            }
            if (-not $cleanupError) {
                $removedVolume = Invoke-NativeCaptured -FilePath 'docker' -Arguments @('volume', 'rm', $volumeName)
                if ($removedVolume.ExitCode -ne 0) { $cleanupError = 'Could not remove the verified disposable test worker volume.' }
            }
        }
    }
    if ($resultLines.Count -gt 0) {
        $resultsDirectory = Split-Path -Parent $ResultsPath
        New-Item -ItemType Directory -Path $resultsDirectory -Force | Out-Null
        [System.IO.File]::WriteAllLines($ResultsPath, $resultLines, [System.Text.UTF8Encoding]::new($false))
    }
    Remove-Item Env:DEMOQA_WORKER_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:DEMOQA_CASES -ErrorAction SilentlyContinue
    Remove-Item Env:DEMOQA_EXTENDED_COMPLEX -ErrorAction SilentlyContinue
    Remove-Item Env:DEMOQA_FRAME_PREFIX -ErrorAction SilentlyContinue
    Remove-Item Env:DEMOQA_FONT_VARIANTS -ErrorAction SilentlyContinue
    Remove-Item Env:DEMOQA_DIAGNOSTIC_HOLD_SECONDS -ErrorAction SilentlyContinue
    Remove-Item Env:READINESS_ACTION_TRACE -ErrorAction SilentlyContinue
    Remove-Item Env:READINESS_P0_SCENARIOS -ErrorAction SilentlyContinue
    Remove-Item Env:BROWSER_OBSERVATION_HINTS -ErrorAction SilentlyContinue
    Remove-Item Env:READINESS_SKIP_ACTION_SNAPSHOTS -ErrorAction SilentlyContinue
    Remove-Item Env:READINESS_DISABLE_DEFAULT_ADDONS -ErrorAction SilentlyContinue
    Remove-Item Env:BROWSER_MEMORY_MAINTENANCE -ErrorAction SilentlyContinue
    if ($attributionSeedStage) {
        $stageFullPath = [System.IO.Path]::GetFullPath($attributionSeedStage)
        $demoqaRoot = [System.IO.Path]::GetFullPath((Join-Path $Root 'runtime/demoqa')).TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
        if ($stageFullPath.StartsWith($demoqaRoot, [System.StringComparison]::OrdinalIgnoreCase) -and (Split-Path -Leaf $stageFullPath) -match '^\.attribution-seed-stage-[0-9a-f]{32}$') {
            Remove-Item -LiteralPath $stageFullPath -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    foreach ($name in $managedDemoqaEnv) {
        if ($priorDemoqaEnv[$name].Present) {
            [Environment]::SetEnvironmentVariable($name, $priorDemoqaEnv[$name].Value, 'Process')
        }
        else {
            [Environment]::SetEnvironmentVariable($name, $null, 'Process')
        }
    }
    $workerKey = $null
    if ($cleanupError -and $readinessReportError) { throw "$cleanupError $readinessReportError" }
    if ($cleanupError) { throw $cleanupError }
    if ($readinessReportError) { throw $readinessReportError }
    if ($p0ReportError) { throw $p0ReportError }
    if ($soakReportError) { throw $soakReportError }
    if ($memoryProbeReportError) { throw $memoryProbeReportError }
}

Write-Host "Saved sanitized test output to $ResultsPath"
