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
    [switch]$ObservationHints,
    [string[]]$P0Scenarios = @('checkbox', 'web_table'),
    [ValidateRange(0, 600)][int]$DiagnosticHoldSeconds = 120,
    [ValidatePattern('^[A-Za-z0-9_-]{1,32}$')][string]$ProbeFramePrefix = 'frame',
    [string[]]$FontMatrixVariants = @('baseline', 'arial_normal', 'arial_medium', 'system_normal', 'system_bold', 'monospace')
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$ComposeFile = Join-Path $Root 'deploy/compose.demoqa-test.yaml'
$Project = 'browser-mcp-demoqa-test'
$ServiceUrl = 'http://127.0.0.1:18080'
$ResultsPath = Join-Path $Root "runtime/demoqa/results-$RunLabel.jsonl"
$workerKey = $null
$managedDemoqaEnv = @(
    'DEMOQA_MODEL_BASE_URL', 'DEMOQA_MODEL_ALIAS', 'DEMOQA_MODEL_PROTOCOL', 'DEMOQA_BROWSER_MEMORY_LIMIT',
    'DEMOQA_WORKER_KEY', 'DEMOQA_CASES', 'DEMOQA_EXTENDED_COMPLEX', 'DEMOQA_RUN_LABEL',
    'DEMOQA_FRAME_PREFIX', 'DEMOQA_FONT_VARIANTS', 'DEMOQA_DIAGNOSTIC_HOLD_SECONDS',
    'READINESS_ACTION_TRACE', 'READINESS_P0_SCENARIOS', 'BROWSER_OBSERVATION_HINTS'
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
$resultLines = [System.Collections.Generic.List[string]]::new()

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

try {
    if (-not (Test-Path -LiteralPath $ComposeFile -PathType Leaf)) { throw 'Disposable DemoQA compose file is missing.' }
    if ($RunLabel -notmatch '^[A-Za-z0-9_-]{1,20}$') { throw 'RunLabel may contain only letters, digits, underscores and dashes.' }
    $FontMatrixVariants = @($FontMatrixVariants | ForEach-Object { ([string]$_).Split(',') } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    $allowedFontVariants = @('baseline', 'arial_normal', 'arial_medium', 'system_normal', 'system_bold', 'monospace', 'system_thin100', 'arial_thin100', 'baseline_normal')
    if (@($FontMatrixVariants | Where-Object { $_ -notin $allowedFontVariants }).Count -gt 0) { throw 'FontMatrixVariants contains an unknown style.' }
    $allowed = @('text_box', 'invalid_email', 'checkbox', 'web_table', 'radio', 'double_click', 'single_click', 'dynamic', 'notify_then_click')
    $Cases = @($Cases | ForEach-Object { ([string]$_).Split(',') } | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    if ($Cases.Count -eq 0 -or @($Cases | Where-Object { $_ -notin $allowed }).Count -gt 0) { throw 'Cases contains an unknown or empty scenario name.' }
    if ($ProbeOnly -and $TaskControlChecks) { throw 'TaskControlChecks cannot be combined with ProbeOnly.' }
    if ($RunCasesAfterReadableCandidate) {
        if (-not $ProbeOnly -or -not $ExtendedComplex -or ($Cases -join ',') -ne 'checkbox,web_table') {
            throw 'RunCasesAfterReadableCandidate requires ProbeOnly, ExtendedComplex, and exactly checkbox,web_table.'
        }
    }
    if ($P0Diagnostics -and ($ProbeOnly -or $DiagnosticGate -or $ReadinessChecks -or $TaskControlChecks -or $RunCasesAfterReadableCandidate)) {
        throw 'P0Diagnostics cannot be combined with probe, gate, readiness, task-control, or readability-gated modes.'
    }
    if ($ObservationHints -and -not $P0Diagnostics) {
        throw 'ObservationHints is currently available only with P0Diagnostics.'
    }
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
    $env:DEMOQA_BROWSER_MEMORY_LIMIT = '2816m'
    $modelPreflight = Invoke-NativeCaptured -FilePath 'python' -Arguments @((Join-Path $PSScriptRoot 'demoqa_model_preflight.py'), '--base-url', $ModelBaseUrl, '--alias', $ModelAlias)
    if ($modelPreflight.ExitCode -ne 0) { throw 'Configured model endpoint is unavailable or does not serve the required alias; production was left running.' }

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

    # Mark cleanup before `up`: compose can partially create the browser even when it fails.
    $stackCleanupNeeded = $true
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
    if (-not $uiReady) { throw 'Disposable local UI session did not become ready within 90 seconds.' }
    $script:DemoqaWebSession = $web

    $healthReady = $false
    $deadline = [DateTime]::UtcNow.AddSeconds(60)
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $health = Invoke-RestMethod -Uri "$ServiceUrl/health/ready" -Method Get -WebSession $web -TimeoutSec 3
            if ($health.status -eq 'ok' -or $health.ready -eq $true) { $healthReady = $true; break }
        } catch { Start-Sleep -Seconds 2 }
    }
    if (-not $healthReady) { throw 'Disposable control service health did not become ready within 60 seconds.' }

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

    $profileDigestAfter = Get-TestProfileDigest
    $profileDigestRow = [pscustomobject]@{ scenario = 'test_profile_fingerprint'; status = 'read_only'; sha256_before = $profileDigestBefore; sha256_after = $profileDigestAfter; unchanged = ($profileDigestBefore -eq $profileDigestAfter) }
    $profileDigestJson = $profileDigestRow | ConvertTo-Json -Compress
    $resultLines.Add($profileDigestJson)
    Write-Host $profileDigestJson
    if ($profileDigestBefore -ne $profileDigestAfter) { throw 'Disposable worker fingerprint changed during the run; same-profile comparison is invalid.' }
}
finally {
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
    if ($stackCleanupNeeded) {
        # Scope cleanup to this project's name; the stack uses no persistent volumes.
        $downArgs = (Get-ComposeArgs) + @('down', '--remove-orphans')
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
}

Write-Host "Saved sanitized test output to $ResultsPath"
