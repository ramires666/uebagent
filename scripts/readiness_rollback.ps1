[CmdletBinding()]
param(
    [string]$ProductionControlUrl = 'http://127.0.0.1:8090',
    [string]$ReportPath = ''
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$ComposeFile = Join-Path $Root 'deploy/compose.readiness-rollback.yaml'
$Project = 'browser-mcp-readiness-rollback'
$ProductionProject = 'browser-mcp-local'
$ServiceUrl = 'http://localhost:18081'
$Preflight = Join-Path $PSScriptRoot 'demoqa_preflight.py'
if (-not $ReportPath) { $ReportPath = Join-Path $Root 'runtime/readiness/rollback-results.json' }

$savedEnv = @{}
foreach ($name in @('READINESS_WORKER_KEY', 'READINESS_CONTROL_IMAGE', 'READINESS_BROWSER_IMAGE')) {
    $savedEnv[$name] = [pscustomobject]@{
        Present = Test-Path "Env:$name"
        Value = [Environment]::GetEnvironmentVariable($name, 'Process')
    }
}
$productionIds = @()
$productionWasStopped = $false
$stackWasStarted = $false
$result = [ordered]@{
    schema_version = 1
    started_at_utc = [DateTime]::UtcNow.ToString('o')
    project = $Project
    port = 18081
    phases = [ordered]@{}
    status = 'BLOCKED'
}

function Get-ComposeArgs {
    return @('compose', '-p', $Project, '-f', $ComposeFile)
}

function Invoke-Native {
    param([string]$FilePath, [string[]]$Arguments, [string]$FailureMessage)
    $oldPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = @(& $FilePath @Arguments 2>&1)
        $exitCode = $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $oldPreference }
    if ($exitCode -ne 0) { throw $FailureMessage }
    return $output
}

function Get-ExactProductionContainer {
    param([ValidateSet('control', 'browser')][string]$Service)
    $rows = @(Invoke-Native 'docker' @('ps', '-q', '--filter', "label=com.docker.compose.project=$ProductionProject", '--filter', "label=com.docker.compose.service=$Service") "Could not identify the production $Service container.")
    $ids = @($rows | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ })
    if ($ids.Count -ne 1) { throw "Expected exactly one running production $Service container." }
    return $ids[0]
}

function Get-ImageId {
    param([string]$ContainerOrImage, [switch]$Container)
    $args = if ($Container) { @('inspect', '--format', '{{.Image}}', $ContainerOrImage) }
            else { @('image', 'inspect', '--format', '{{.Id}}', $ContainerOrImage) }
    $rows = Invoke-Native 'docker' $args 'Could not read a Docker image ID.'
    $id = (($rows | ForEach-Object { [string]$_ }) -join '').Trim()
    if ($id -notmatch '^sha256:[0-9a-f]{64}$') { throw 'Docker returned a malformed image ID.' }
    return $id
}

function Get-ComposeImageReference {
    param([string]$ImageId)
    if ($ImageId -notmatch '^sha256:([0-9a-f]{64})$') { throw 'Cannot use a malformed image ID in Compose.' }
    return $Matches[1]
}

function Assert-NoOtherRunningBrowser {
    $rows = Invoke-Native 'docker' @('ps', '--format', '{{.ID}} {{.Label "com.docker.compose.project"}} {{.Label "com.docker.compose.service"}}') 'Could not inspect running browser containers.'
    foreach ($row in $rows) {
        $parts = ([string]$row).Trim() -split '\s+'
        if ($parts.Count -ge 3 -and $parts[2] -eq 'browser' -and $parts[1] -ne $ProductionProject) {
            throw 'Another non-production browser container is running; rollback exercise is unsafe.'
        }
    }
}

function Invoke-Preflight {
    $rows = Invoke-Native 'python' @($Preflight, '--control-url', $ProductionControlUrl, '--allow-suspended-tasks') 'Read-only production preflight failed.'
    $payload = (($rows | ForEach-Object { [string]$_ }) -join '').Trim()
    try { $check = $payload | ConvertFrom-Json -ErrorAction Stop }
    catch { throw 'Read-only production preflight returned invalid evidence.' }
    if ($check.ready -ne $true) { throw 'Read-only production preflight did not confirm an idle worker and allowed task state.' }
    $null = Get-Command Invoke-RestMethod -ErrorAction Stop
    $web = New-Object Microsoft.PowerShell.Commands.WebRequestSession
    $session = Invoke-RestMethod -Uri "$($ProductionControlUrl.TrimEnd('/'))/api/v1/operator/session" -Method Get -WebSession $web -TimeoutSec 5
    if ($session.authenticated -ne $true) { throw 'Production operator session is unavailable for task-state fingerprinting.' }
    $taskPayload = Invoke-RestMethod -Uri "$($ProductionControlUrl.TrimEnd('/'))/api/v1/tasks" -Method Get -WebSession $web -TimeoutSec 5
    if ($taskPayload.tasks -isnot [array]) { throw 'Production task-state response is malformed.' }
    if ($taskPayload.tasks.Count -ne $check.task_count) { throw 'Production task count differs between preflight and task listing.' }
    $taskStatusCounts = [ordered]@{}
    foreach ($task in $taskPayload.tasks) {
        if (-not $task.status) { throw 'Production task-state response lacks a status.' }
        $status = ([string]$task.status).ToLowerInvariant()
        if (-not $taskStatusCounts.Contains($status)) { $taskStatusCounts[$status] = 0 }
        $taskStatusCounts[$status]++
    }
    return [pscustomobject]@{
        ready = $true
        worker_idle = $check.worker_idle
        tasks_idle = $check.tasks_idle
        active_task_statuses = @($check.active_task_statuses)
        suspended_task_count = $check.suspended_task_count
        suspended_task_statuses = @($check.suspended_task_statuses)
        task_count = $check.task_count
        task_status_counts = $taskStatusCounts
    }
}

function Get-PreflightFingerprint {
    param($Summary)
    $counts = [ordered]@{}
    foreach ($status in @($Summary.task_status_counts.Keys | Sort-Object)) {
        $counts[$status] = $Summary.task_status_counts[$status]
    }
    $state = [ordered]@{
        ready = $Summary.ready
        worker_idle = $Summary.worker_idle
        tasks_idle = $Summary.tasks_idle
        active_task_statuses = @($Summary.active_task_statuses | Sort-Object)
        suspended_task_count = $Summary.suspended_task_count
        suspended_task_statuses = @($Summary.suspended_task_statuses | Sort-Object)
        task_count = $Summary.task_count
        task_status_counts = $counts
    }
    $canonical = $state | ConvertTo-Json -Depth 4 -Compress
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { $digest = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($canonical)) }
    finally { $sha.Dispose() }
    return ([BitConverter]::ToString($digest).Replace('-', '')).ToLowerInvariant()
}

function Get-ComposeServiceContainerId {
    param([ValidateSet('control', 'browser')][string]$Service)
    $rows = @(Invoke-Native 'docker' ((Get-ComposeArgs) + @('ps', '-q', $Service)) "Could not identify isolated $Service container.")
    $ids = @($rows | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ })
    if ($ids.Count -ne 1) { throw "Expected exactly one isolated $Service container." }
    return $ids[0]
}

function Start-TestStack {
    param([string]$ControlImage, [string]$BrowserImage)
    $env:READINESS_CONTROL_IMAGE = Get-ComposeImageReference $ControlImage
    $env:READINESS_BROWSER_IMAGE = Get-ComposeImageReference $BrowserImage
    $script:stackWasStarted = $true
    $null = Invoke-Native 'docker' ((Get-ComposeArgs) + @('up', '-d', '--no-build', '--force-recreate')) 'Could not start the isolated rollback stack.'
}

function Stop-TestStack {
    $null = Invoke-Native 'docker' ((Get-ComposeArgs) + @('stop', 'control', 'browser')) 'Could not stop the isolated rollback stack.'
}

function Get-StackEvidence {
    param([string]$Name, [string]$ExpectedControlImage, [string]$ExpectedBrowserImage)
    $deadline = [DateTime]::UtcNow.AddSeconds(90)
    $ready = $false
    $state = $null
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $null = Get-Command Invoke-RestMethod -ErrorAction Stop
            $web = New-Object Microsoft.PowerShell.Commands.WebRequestSession
            $session = Invoke-RestMethod -Uri "$ServiceUrl/api/v1/operator/session" -Method Get -WebSession $web -TimeoutSec 5
            $health = Invoke-RestMethod -Uri "$ServiceUrl/health/ready" -Method Get -WebSession $web -TimeoutSec 5
            $state = Invoke-RestMethod -Uri "$ServiceUrl/api/v1/desktop/state" -Method Get -WebSession $web -TimeoutSec 5
            if ($health.ready -eq $true -and $session.authenticated -eq $true -and
                $state.browser_started -eq $true -and $state.context_count -eq 1 -and $state.page_count -eq 1) {
                $ready = $true
                break
            }
        }
        catch { }
        Start-Sleep -Seconds 2
    }
    if (-not $ready) { throw "Isolated $Name stack failed readiness, UI session, or one-page checks." }

    $digestCode = "from pathlib import Path;import hashlib;p=Path('/app/runtime/worker-state/profiles/default.camoufox-fingerprint.json');print(hashlib.sha256(p.read_bytes()).hexdigest())"
    $rows = Invoke-Native 'docker' ((Get-ComposeArgs) + @('exec', '-T', 'browser', 'python', '-c', $digestCode)) 'Could not read the isolated browser fingerprint digest.'
    $digest = (($rows | ForEach-Object { [string]$_ }) -join '').Trim()
    if ($digest -notmatch '^[0-9a-f]{64}$') { throw 'Isolated browser fingerprint digest is missing or malformed.' }
    $controlId = Get-ComposeServiceContainerId 'control'
    $browserId = Get-ComposeServiceContainerId 'browser'
    $actualControl = Get-ImageId $controlId -Container
    $actualBrowser = Get-ImageId $browserId -Container
    if ($actualControl -ne $ExpectedControlImage -or $actualBrowser -ne $ExpectedBrowserImage) {
        throw "Isolated $Name image IDs do not match the requested image IDs."
    }
    return [ordered]@{
        ready = $true
        operator_session = $true
        browser_started = $true
        context_count = 1
        page_count = 1
        control_image_id = $actualControl
        browser_image_id = $actualBrowser
        profile_fingerprint_sha256 = $digest
    }
}

function Wait-ProductionReady {
    $deadline = [DateTime]::UtcNow.AddSeconds(90)
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $null = Get-Command Invoke-RestMethod -ErrorAction Stop
            $web = New-Object Microsoft.PowerShell.Commands.WebRequestSession
            $session = Invoke-RestMethod -Uri "$($ProductionControlUrl.TrimEnd('/'))/api/v1/operator/session" -Method Get -WebSession $web -TimeoutSec 5
            if ($session.authenticated -ne $true) { throw 'Production operator session is unavailable.' }
            $health = Invoke-RestMethod -Uri "$($ProductionControlUrl.TrimEnd('/'))/health/ready" -Method Get -WebSession $web -TimeoutSec 5
            if ($health.ready -eq $true) {
                $script:productionPostRestorePreflight = Invoke-Preflight
                return $true
            }
        }
        catch { }
        Start-Sleep -Seconds 2
    }
    return $false
}

try {
    # All safety gates are read-only and precede stopping either production container.
    $result.phases.production_preflight = Invoke-Preflight
    $result.production_task_state_before_sha256 = Get-PreflightFingerprint $result.phases.production_preflight
    Assert-NoOtherRunningBrowser
    $controlId = Get-ExactProductionContainer 'control'
    $browserId = Get-ExactProductionContainer 'browser'
    $productionIds = @($controlId, $browserId)
    $baselineControl = Get-ImageId $controlId -Container
    $baselineBrowser = Get-ImageId $browserId -Container
    $candidateControl = Get-ImageId 'browser-mcp-demoqa-control:test'
    $candidateBrowser = Get-ImageId 'browser-mcp-demoqa-browser:test'
    $result.image_ids = [ordered]@{
        baseline_control = $baselineControl
        baseline_browser = $baselineBrowser
        candidate_control = $candidateControl
        candidate_browser = $candidateBrowser
    }

    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    $env:READINESS_WORKER_KEY = [Convert]::ToBase64String($bytes)
    [Array]::Clear($bytes, 0, $bytes.Length)
    $env:READINESS_CONTROL_IMAGE = Get-ComposeImageReference $candidateControl
    $env:READINESS_BROWSER_IMAGE = Get-ComposeImageReference $candidateBrowser
    $null = Invoke-Native 'docker' ((Get-ComposeArgs) + @('config', '--quiet')) 'Rollback Compose configuration validation failed.'

    # Only the exact production containers found by the project/service labels are stopped.
    $productionWasStopped = $true
    $null = Invoke-Native 'docker' (@('stop') + $productionIds) 'Could not stop the exact production container pair.'

    Start-TestStack $candidateControl $candidateBrowser
    $result.phases.candidate_initial = Get-StackEvidence 'candidate_initial' $candidateControl $candidateBrowser

    Stop-TestStack
    Start-TestStack $baselineControl $baselineBrowser
    $result.phases.baseline_rollback = Get-StackEvidence 'baseline_rollback' $baselineControl $baselineBrowser
    if ($result.phases.baseline_rollback.profile_fingerprint_sha256 -ne
        $result.phases.candidate_initial.profile_fingerprint_sha256) {
        throw 'Worker profile fingerprint changed during rollback; shared-volume persistence check failed.'
    }

    Stop-TestStack
    Start-TestStack $candidateControl $candidateBrowser
    $result.phases.candidate_redeploy = Get-StackEvidence 'candidate_redeploy' $candidateControl $candidateBrowser
    if ($result.phases.candidate_redeploy.profile_fingerprint_sha256 -ne
        $result.phases.candidate_initial.profile_fingerprint_sha256) {
        throw 'Worker profile fingerprint changed during candidate redeploy.'
    }
    $result.control_database_marker = 'NOT CHECKED'
    $result.status = 'PASS'
}
catch {
    $result.status = 'FAIL'
    $result.failure = $_.Exception.Message
}
finally {
    $isolatedGone = $true
    if ($stackWasStarted) {
        try { $null = Invoke-Native 'docker' ((Get-ComposeArgs) + @('down')) 'Could not remove isolated rollback containers and network.' }
        catch { $result.status = 'FAIL'; $result.isolated_stack_removed = $false }
        $deadline = [DateTime]::UtcNow.AddSeconds(30)
        while ([DateTime]::UtcNow -lt $deadline) {
            try {
                $remaining = @(Invoke-Native 'docker' @('ps', '-aq', '--filter', "label=com.docker.compose.project=$Project") 'Could not verify isolated container cleanup.')
                $remainingIds = @($remaining | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ })
                if ($remainingIds.Count -eq 0) {
                    $result.isolated_project_containers_removed = $true
                    break
                }
            }
            catch { }
            Start-Sleep -Milliseconds 500
        }
        if ($result.isolated_project_containers_removed -ne $true) {
            $result.isolated_project_containers_removed = $false
            $result.status = 'FAIL'
            try {
                $runningBrowser = @(Invoke-Native 'docker' @('ps', '-q', '--filter', "label=com.docker.compose.project=$Project", '--filter', 'label=com.docker.compose.service=browser') 'Could not verify that the isolated browser stopped.')
                $runningBrowserIds = @($runningBrowser | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ })
                if ($runningBrowserIds.Count -gt 0) { $isolatedGone = $false }
            }
            catch { $isolatedGone = $false }
        }
    }
    if ($productionWasStopped -and $productionIds.Count -eq 2) {
        if ($isolatedGone) {
            try {
                $null = Invoke-Native 'docker' (@('start') + $productionIds) 'Could not restart the exact production container pair.'
            }
            catch { $result.status = 'FAIL'; $result.production_restored = $false }
            if ($result.production_restored -ne $false -and -not (Wait-ProductionReady)) {
                $result.status = 'FAIL'
                $result.production_restored = $false
            }
            elseif ($result.production_restored -ne $false) { $result.production_restored = $true }
            if ($result.production_restored -eq $true -and $script:productionPostRestorePreflight) {
                $result.production_task_state_after_sha256 = Get-PreflightFingerprint $script:productionPostRestorePreflight
                if ($result.production_task_state_after_sha256 -ne $result.production_task_state_before_sha256) {
                    $result.status = 'FAIL'
                    $result.production_state_unchanged = $false
                }
                else { $result.production_state_unchanged = $true }
            }
        }
        else { $result.production_restored = $false }
    }
    foreach ($name in $savedEnv.Keys) {
        if ($savedEnv[$name].Present) { [Environment]::SetEnvironmentVariable($name, $savedEnv[$name].Value, 'Process') }
        else { [Environment]::SetEnvironmentVariable($name, $null, 'Process') }
    }
    $result.finished_at_utc = [DateTime]::UtcNow.ToString('o')
    $parent = Split-Path -Parent $ReportPath
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    $result | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ReportPath -Encoding utf8
}

Write-Output (($result | ConvertTo-Json -Depth 8 -Compress))
if ($result.status -ne 'PASS' -or $result.production_restored -ne $true) { exit 1 }
exit 0
