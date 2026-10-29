[CmdletBinding()]
param(
    [switch]$Direct,
    [switch]$Build,
    [string]$ModelKeyFile = '',
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$BaseCompose = Join-Path $Root 'deploy/compose.yaml'
$DirectCompose = Join-Path $Root 'deploy/compose.direct.yaml'
$ModelCompose = Join-Path $Root 'deploy/compose.model-auth.yaml'
$Bootstrap = Join-Path $Root 'scripts/bootstrap_secrets.py'
$AccessDirectory = Join-Path $Root 'runtime/access'
$ServiceUrl = 'http://localhost:8090'

function Invoke-NativeCaptured {
    param([string]$FilePath, [string[]]$Arguments)
    $previousPreference = $ErrorActionPreference
    $nativeOutput = @()
    $nativeExitCode = 1
    $ErrorActionPreference = 'Continue'
    try {
        $nativeOutput = @(& $FilePath @Arguments 2>&1)
        $nativeExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
    return [pscustomobject]@{
        ExitCode = $nativeExitCode
        Output = @($nativeOutput)
    }
}

function Invoke-DockerChecked {
    param([string[]]$DockerArguments, [string]$FailureMessage)
    $result = Invoke-NativeCaptured -FilePath 'docker' -Arguments $DockerArguments
    if ($result.ExitCode -ne 0) { throw "$FailureMessage (exit $($result.ExitCode))." }
}

function Set-PrivateFileAcl {
    param([string]$Path, [bool]$IsDirectory)
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $sid = $identity.User
    $inheritance = if ($IsDirectory) {
        [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
    } else { [System.Security.AccessControl.InheritanceFlags]::None }
    $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
        $sid,
        [System.Security.AccessControl.FileSystemRights]::FullControl,
        $inheritance,
        [System.Security.AccessControl.PropagationFlags]::None,
        [System.Security.AccessControl.AccessControlType]::Allow
    )
    if ($IsDirectory) {
        $acl = New-Object System.Security.AccessControl.DirectorySecurity
    } else {
        $acl = New-Object System.Security.AccessControl.FileSecurity
    }
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetAccessRule($rule)
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Save-AccessTokens {
    $pythonCode = "from pathlib import Path; import sys; root=Path('/app/runtime/control-data'); names=('operator-token.txt','mcp-token.txt'); values=[(root/name).read_text(encoding='utf-8').strip() for name in names]; sys.stdout.write('\n'.join(values)+'\n')"
    $captureArgs = $script:ComposeArguments + @('exec', '-T', 'control', 'python', '-c', $pythonCode)
    $captureResult = Invoke-NativeCaptured -FilePath 'docker' -Arguments $captureArgs
    $captured = @($captureResult.Output)
    if ($captureResult.ExitCode -ne 0 -or $captured.Count -ne 2) {
        throw 'Could not retrieve access tokens from the control service. No token value was displayed.'
    }
    $operatorToken = [string]$captured[0]
    $mcpToken = [string]$captured[1]
    if ($operatorToken -notmatch '^[A-Za-z0-9_-]{32,128}$' -or $mcpToken -notmatch '^[A-Za-z0-9_-]{32,128}$') {
        throw 'The control service returned an invalid access-token format. No token value was displayed.'
    }
    if (-not (Test-Path -LiteralPath $AccessDirectory -PathType Container)) {
        New-Item -ItemType Directory -Path $AccessDirectory -Force | Out-Null
    }
    Set-PrivateFileAcl -Path $AccessDirectory -IsDirectory $true
    $encoding = New-Object System.Text.UTF8Encoding($false)
    $operatorPath = Join-Path $AccessDirectory 'operator-token.txt'
    $mcpPath = Join-Path $AccessDirectory 'mcp-token.txt'
    [System.IO.File]::WriteAllText($operatorPath, "$operatorToken`n", $encoding)
    [System.IO.File]::WriteAllText($mcpPath, "$mcpToken`n", $encoding)
    Set-PrivateFileAcl -Path $operatorPath -IsDirectory $false
    Set-PrivateFileAcl -Path $mcpPath -IsDirectory $false
}

try {
    Push-Location $Root
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw 'Docker CLI was not found. Install/start Docker Desktop with the Linux/WSL2 engine.' }
    if (-not (Get-Command python -ErrorAction SilentlyContinue)) { throw 'Python was not found. Python 3 is required for the credential bootstrap helper.' }
    if (-not (Test-Path -LiteralPath $BaseCompose -PathType Leaf)) { throw 'deploy/compose.yaml is missing.' }
    if ($Direct -and -not (Test-Path -LiteralPath $DirectCompose -PathType Leaf)) { throw 'deploy/compose.direct.yaml is missing.' }
    if (-not [string]::IsNullOrWhiteSpace($ModelKeyFile)) {
        if (-not (Test-Path -LiteralPath $ModelKeyFile -PathType Leaf)) { throw 'The selected model API key file does not exist.' }
        $ModelKeyFile = (Resolve-Path -LiteralPath $ModelKeyFile).Path
        if ([string]::IsNullOrWhiteSpace([System.IO.File]::ReadAllText($ModelKeyFile))) { throw 'The selected model API key file is empty.' }
        if (-not (Test-Path -LiteralPath $ModelCompose -PathType Leaf)) { throw 'deploy/compose.model-auth.yaml is missing.' }
    }

    $dockerInfo = Invoke-NativeCaptured -FilePath 'docker' -Arguments @('info', '--format', '{{.OSType}}')
    $osType = @($dockerInfo.Output | ForEach-Object { [string]$_ })
    if ($dockerInfo.ExitCode -ne 0 -or $osType.Count -lt 1 -or $osType[0] -ne 'linux') {
        throw 'Docker Desktop must be running with its Linux/WSL2 engine.'
    }

    $script:ComposeArguments = @('compose', '-f', $BaseCompose)
    if ($Direct) { $script:ComposeArguments += @('-f', $DirectCompose) }
    if (-not [string]::IsNullOrWhiteSpace($ModelKeyFile)) { $script:ComposeArguments += @('-f', $ModelCompose) }

    Invoke-DockerChecked -DockerArguments ($ComposeArguments + @('config', '--quiet')) -FailureMessage 'Docker Compose configuration validation failed'
    if ($CheckOnly) {
        Write-Host 'Configuration is valid. CheckOnly did not create credentials, volumes, or containers.'
        exit 0
    }

    $bootstrapArgs = @($Bootstrap)
    if (-not [string]::IsNullOrWhiteSpace($ModelKeyFile)) { $bootstrapArgs += @('--model-key-file', $ModelKeyFile) }
    $bootstrapResult = Invoke-NativeCaptured -FilePath 'python' -Arguments $bootstrapArgs
    if ($bootstrapResult.ExitCode -ne 0) { throw 'Credential bootstrap failed. Its captured output was suppressed to protect credentials.' }
    foreach ($line in $bootstrapResult.Output) { if ([string]$line -match '^(Prepared worker credentials\.|Prepared worker credentials and the selected model key volume\.)$') { Write-Host $line } }

    $upArgs = $ComposeArguments + @('up', '-d')
    if ($Build) { $upArgs += '--build' }
    Invoke-DockerChecked -DockerArguments $upArgs -FailureMessage 'Docker Compose could not start the services'

    $ready = $false
    $deadline = [DateTime]::UtcNow.AddSeconds(90)
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $response = Invoke-WebRequest -Uri "$ServiceUrl/health/live" -Method Get -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop
            if ([int]$response.StatusCode -eq 200) { $ready = $true; break }
        } catch { }
        Start-Sleep -Seconds 2
    }
    if (-not $ready) { throw "Control service did not become available at $ServiceUrl within 90 seconds. Check 'docker compose logs'." }

    Save-AccessTokens
    Write-Host "Web UI: $ServiceUrl"
    Write-Host "Operator token saved with a current-user-only ACL: $AccessDirectory\operator-token.txt"
    Write-Host "MCP bearer token saved with a current-user-only ACL: $AccessDirectory\mcp-token.txt"
    if ($Direct) {
        Write-Host 'Local web panel opens without manually entering an operator token.'
        Write-Host 'Browser route: direct-development was selected explicitly. No external proxy is active.'
    } else {
        Write-Host 'Browser egress is blocked until an explicit route is configured. Use -Direct for the current direct-development route.'
    }
    if (-not [string]::IsNullOrWhiteSpace($ModelKeyFile)) { Write-Host 'Model API key authentication is enabled from the selected file.' }
    else { Write-Host 'Model API key authentication is disabled.' }
}
catch {
    Write-Host "ERROR: $($_.Exception.Message)"
    exit 1
}
finally {
    Pop-Location -ErrorAction SilentlyContinue
}
