[CmdletBinding()]
param(
    [string]$ModelPath = 'W:\LAMA\Fara\Fara1.5-9B-Q4_K_M.gguf',
    [string]$MmprojPath = 'W:\LAMA\Fara\mmproj-Fara1.5-9B-bf16.gguf',
    [string]$ApiKeyFile = '',
    [ValidateRange(4096, 65536)]
    [int]$ContextSize = 16384,
    [ValidateRange(1, 16)]
    [int]$Parallel = 1,
    [string]$ListenAddress = '127.0.0.1',
    [ValidateRange(1, 65535)]
    [int]$Port = 8080,
    [switch]$Background,
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
$ExecutablePath = 'W:\LAMA\llama\llama-server.exe'
$ExpectedRevision = '9631 (6e14286ed)'
$ModelAlias = 'fara-browser'
$RuntimePath = Join-Path (Split-Path -Parent $PSScriptRoot) 'runtime'
$StdoutLog = Join-Path $RuntimePath 'llama.stdout.log'
$StderrLog = Join-Path $RuntimePath 'llama.stderr.log'

function Resolve-RequiredFile {
    param([string]$Path, [string]$Description)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Description not found: $Path"
    }
    return (Resolve-Path -LiteralPath $Path).Path
}

function Get-ServerVersion {
    param([string]$Path)
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $lines = & $Path '--version' 2>&1
        $code = $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $previousPreference }
    $text = ($lines | ForEach-Object { [string]$_ }) -join "`n"
    if ($code -ne 0) { throw "llama-server --version failed with exit code $code." }
    if ($text -notmatch '9631\s*\(6e14286ed\)') {
        throw "Unexpected llama-server build. Expected $ExpectedRevision; received: $text"
    }
    return $text.Trim()
}

function Assert-RequiredFlags {
    param([string]$Path)
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $lines = & $Path '--help' 2>&1
        $code = $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $previousPreference }
    $help = ($lines | ForEach-Object { [string]$_ }) -join "`n"
    if ($code -ne 0) { throw "llama-server --help failed with exit code $code." }
    $required = @(
        '--model', '--mmproj', '--device', '--split-mode', '--n-gpu-layers',
        '--ctx-size', '--parallel', '--flash-attn', '--cache-type-k', '--cache-type-v',
        '--batch-size', '--ubatch-size', '--image-min-tokens', '--jinja',
        '--skip-chat-parsing', '--no-ui', '--temp', '--seed', '--host', '--port', '--alias'
    )
    $missing = @()
    foreach ($flag in $required) {
        $escaped = [regex]::Escape($flag)
        if ($help -notmatch "(?m)(^|\s)$escaped(?=\s|$|[=,])") { $missing += $flag }
    }
    if ($missing.Count -gt 0) {
        throw "This llama-server build does not advertise required flag(s): $($missing -join ', ')"
    }
    return $help
}

function ConvertTo-WindowsCommandLineArgument {
    param([AllowEmptyString()][string]$Value)
    $builder = New-Object System.Text.StringBuilder
    [void]$builder.Append('"')
    $slashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq [char]92) {
            $slashes++
            continue
        }
        if ($character -eq [char]34) {
            if ($slashes -gt 0) { [void]$builder.Append(('\' * (2 * $slashes))) }
            [void]$builder.Append('\"')
            $slashes = 0
            continue
        }
        if ($slashes -gt 0) { [void]$builder.Append(('\' * $slashes)); $slashes = 0 }
        [void]$builder.Append($character)
    }
    if ($slashes -gt 0) { [void]$builder.Append(('\' * (2 * $slashes))) }
    [void]$builder.Append('"')
    return $builder.ToString()
}

function Get-EndpointUrl {
    param([string]$Address, [int]$EndpointPort)
    $ip = [System.Net.IPAddress]::Parse($Address)
    $formatted = if ($ip.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetworkV6) { "[$Address]" } else { $Address }
    return "http://${formatted}:$EndpointPort"
}

function Assert-PortAvailable {
    param([int]$EndpointPort)
    $listeners = @()
    $useFallback = $false
    try {
        $listeners = @(Get-NetTCPConnection -LocalPort $EndpointPort -State Listen -ErrorAction Stop)
    }
    catch {
        $useFallback = $true
    }
    if ($useFallback) {
        $matches = @(& netstat.exe -ano -p tcp 2>$null | Where-Object {
            $_ -match "^\s*TCP\s+\S+:$EndpointPort\s+\S+\s+LISTENING\s+\d+\s*$"
        })
        if ($matches.Count -gt 0) {
            throw "Port $EndpointPort already has a TCP listener. Stop or reconfigure that process; this script will not terminate it."
        }
    }
    elseif ($listeners.Count -gt 0) {
        $ids = ($listeners | Select-Object -ExpandProperty OwningProcess -Unique) -join ', '
        throw "Port $EndpointPort already has a TCP listener (PID $ids). Stop or reconfigure it; this script will not terminate it."
    }
}

function Test-Health {
    param([string]$Url, [string]$Key)
    try {
        $headers = @{}
        if (-not [string]::IsNullOrEmpty($Key)) { $headers.Authorization = "Bearer $Key" }
        $response = Invoke-WebRequest -Uri "$Url/health" -Method Get -Headers $headers -TimeoutSec 3 -UseBasicParsing -ErrorAction Stop
        return ([int]$response.StatusCode -eq 200)
    }
    catch { return $false }
}

try {
    $Exe = Resolve-RequiredFile -Path $ExecutablePath -Description 'llama-server executable'
    $Model = Resolve-RequiredFile -Path $ModelPath -Description 'Fara model file'
    $Mmproj = Resolve-RequiredFile -Path $MmprojPath -Description 'Fara multimodal projector'
    $ipAddress = $null
    if (-not [System.Net.IPAddress]::TryParse($ListenAddress, [ref]$ipAddress)) {
        throw 'ListenAddress must be a numeric IPv4 or IPv6 address; hostnames are not accepted.'
    }
    $Url = Get-EndpointUrl -Address $ListenAddress -EndpointPort $Port
    $Version = Get-ServerVersion -Path $Exe
    $ApiKeyValue = ''
    $ResolvedApiKeyFile = ''
    if (-not [string]::IsNullOrWhiteSpace($ApiKeyFile)) {
        $ResolvedApiKeyFile = Resolve-RequiredFile -Path $ApiKeyFile -Description 'API key file'
        $ApiKeyValue = [System.IO.File]::ReadAllText($ResolvedApiKeyFile).Trim()
        if ([string]::IsNullOrWhiteSpace($ApiKeyValue)) { throw "API key file is empty: $ResolvedApiKeyFile" }
    }
    $helpText = Assert-RequiredFlags -Path $Exe
    if ($ResolvedApiKeyFile) {
        if ($helpText -notmatch '(?m)(^|\s)--api-key-file(?=\s|$|[=,])') {
            throw 'This llama-server build does not advertise --api-key-file.'
        }
    }

    $Arguments = @(
        '--model', $Model,
        '--mmproj', $Mmproj,
        '--device', 'CUDA0',
        '--split-mode', 'none',
        '--n-gpu-layers', 'all',
        '--ctx-size', [string]$ContextSize,
        '--parallel', [string]$Parallel,
        '--flash-attn', 'on',
        '--cache-type-k', 'f16',
        '--cache-type-v', 'f16',
        '--batch-size', '1024',
        '--ubatch-size', '256',
        '--image-min-tokens', '1024',
        '--jinja',
        '--skip-chat-parsing',
        '--no-ui',
        '--temp', '0',
        '--seed', '0',
        '--alias', $ModelAlias,
        '--host', $ListenAddress,
        '--port', [string]$Port
    )
    if ($ResolvedApiKeyFile) { $Arguments += @('--api-key-file', $ResolvedApiKeyFile) }
    $DisplayCommand = (@(ConvertTo-WindowsCommandLineArgument $Exe) + @($Arguments | ForEach-Object { ConvertTo-WindowsCommandLineArgument ([string]$_) })) -join ' '
    $Plan = [ordered]@{
        executable = $Exe
        revision = $ExpectedRevision
        server_version = $Version
        model = $Model
        mmproj = $Mmproj
        model_alias = $ModelAlias
        context_size = $ContextSize
        parallel = $Parallel
        endpoint = $Url
        command = $DisplayCommand
    }
    if ($ResolvedApiKeyFile) { $Plan.api_key_file = $ResolvedApiKeyFile }

    if ($CheckOnly) {
        [pscustomobject]$Plan | ConvertTo-Json -Depth 4
        exit 0
    }

    Assert-PortAvailable -EndpointPort $Port
    if (-not $Background) {
        Write-Host "Starting $ModelAlias ($ExpectedRevision) on $Url"
        & $Exe @Arguments
        $ServerExitCode = $LASTEXITCODE
        exit $ServerExitCode
    }

    if (-not (Test-Path -LiteralPath $RuntimePath -PathType Container)) {
        New-Item -ItemType Directory -Path $RuntimePath -Force | Out-Null
    }
    $StartArguments = @($Arguments | ForEach-Object { ConvertTo-WindowsCommandLineArgument ([string]$_) })
    $Process = Start-Process -FilePath $Exe -ArgumentList $StartArguments -WorkingDirectory (Split-Path -Parent $Exe) -WindowStyle Hidden -PassThru -RedirectStandardOutput $StdoutLog -RedirectStandardError $StderrLog
    Write-Host "Started llama-server PID $($Process.Id). Waiting up to 120 seconds for $Url/health."
    Write-Host "Logs: $StdoutLog ; $StderrLog"
    $Deadline = [DateTime]::UtcNow.AddSeconds(120)
    while ([DateTime]::UtcNow -lt $Deadline) {
        $Process.Refresh()
        if ($Process.HasExited) {
            $ExitCode = [int]$Process.ExitCode
            Write-Host "ERROR: llama-server exited during startup with code $ExitCode. Logs: $StdoutLog ; $StderrLog"
            if ($ExitCode -eq 0) { exit 1 }
            exit $ExitCode
        }
        if (Test-Health -Url $Url -Key $ApiKeyValue) {
            Write-Host "llama-server is ready at $Url (PID $($Process.Id))."
            exit 0
        }
        Start-Sleep -Seconds 2
    }
    $Process.Refresh()
    if ($Process.HasExited) {
        $ExitCode = [int]$Process.ExitCode
        Write-Host "ERROR: llama-server exited during startup with code $ExitCode. Logs: $StdoutLog ; $StderrLog"
        if ($ExitCode -eq 0) { exit 1 }
        exit $ExitCode
    }
    Write-Host "ERROR: llama-server is still starting after 120 seconds (PID $($Process.Id)). It was left running. Logs: $StdoutLog ; $StderrLog"
    exit 1
}
catch {
    Write-Host "ERROR: $($_.Exception.Message)"
    exit 1
}
