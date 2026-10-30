[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [switch]$Apply,
    [switch]$PreserveEvidence,
    [switch]$IncludeArchivedPytestRuns,
    [switch]$TargetedReparseCleanup
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$DoApply = $Apply -and -not $WhatIfPreference

$WorkspaceRoot = 'W:\_python\uebagent'
$MainRoot = $WorkspaceRoot
$ExpectedWorktree = Join-Path $WorkspaceRoot '.worktrees\readiness-2026-10-30'
$ScriptRepoRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot)).TrimEnd('\')
if ([string]::Equals($ScriptRepoRoot, $MainRoot.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)) {
    $RunRoot = $MainRoot
} elseif ([string]::Equals($ScriptRepoRoot, $ExpectedWorktree.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)) {
    $RunRoot = $ExpectedWorktree
} else {
    throw "Run this script only from the main checkout or the readiness worktree."
}
$WorktreeRoot = $ExpectedWorktree
$RepoRoots = @($MainRoot)
if (Test-Path -LiteralPath $ExpectedWorktree -PathType Container) { $RepoRoots += $ExpectedWorktree }
$SensitiveDirectoryNames = @('secrets', 'profiles', 'profile', 'cookies', 'cookie', 'state', 'chrome-profile', 'chrome-profiles')
$ProtectedFileExtensions = @('.pem', '.key', '.p12', '.pfx')

function Test-ProtectedFile([string]$Path) {
    $name = [IO.Path]::GetFileName($Path).ToLowerInvariant()
    $extension = [IO.Path]::GetExtension($Path).ToLowerInvariant()
    return ($ProtectedFileExtensions -contains $extension) -or
        ($name -match '(^|[._-])(cookie|cookies|session|profile|secret|token)([._-]|$)') -or
        ($name -eq '.env')
}

function Get-LocalSha256([string]$Path) {
    $algorithm = [Security.Cryptography.SHA256]::Create()
    $stream = $null
    try {
        $stream = [IO.File]::OpenRead($Path)
        return [BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace('-', '')
    } finally {
        if ($stream) { $stream.Dispose() }
        $algorithm.Dispose()
    }
}

function Test-ReparsePoint([string]$Path) {
    return (([IO.File]::GetAttributes($Path) -band [IO.FileAttributes]::ReparsePoint) -ne 0)
}

function Get-SafeRelative([string]$BasePath, [string]$Path) {
    $base = [IO.Path]::GetFullPath($BasePath).TrimEnd('\')
    $full = [IO.Path]::GetFullPath($Path)
    $prefix = $base + '\'
    if ([string]::Equals($full, $base, [StringComparison]::OrdinalIgnoreCase)) { return '' }
    if (-not $full.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Path escaped approved root: $full (root $base)"
    }
    return $full.Substring($prefix.Length)
}

function Assert-OrdinaryPath([string]$Root, [string]$Path) {
    # $Root is retained at call sites to make intent explicit; containment is limited
    # to the two fixed checkouts and every path component is checked without following links.
    $full = [IO.Path]::GetFullPath($Path)
    $ownerRoot = $null
    foreach ($approved in @($MainRoot, $WorktreeRoot)) {
        try { [void](Get-SafeRelative $approved $full); $ownerRoot = $approved; break } catch { }
    }
    if (-not $ownerRoot) { throw "Path is outside approved project roots: $full" }
    $current = $WorkspaceRoot
    if ((Test-Path -LiteralPath $current) -and (Test-ReparsePoint $current)) { throw "Workspace root is a reparse point: $current" }
    foreach ($part in (Get-SafeRelative $WorkspaceRoot $full).Split([IO.Path]::DirectorySeparatorChar, [StringSplitOptions]::RemoveEmptyEntries)) {
        $current = Join-Path $current $part
        if (Test-Path -LiteralPath $current) {
            if (Test-ReparsePoint $current) { throw "Refusing path with reparse ancestor: $current" }
        }
    }
}

function Assert-NoTrackedFiles([string]$Root, [string]$Path) {
    Assert-OrdinaryPath $Root $Path
    $relative = (Get-SafeRelative $Root $Path).Replace('\', '/')
    $tracked = @(& git -c "safe.directory=$Root" -C $Root ls-files -- $relative 2>$null)
    if ($LASTEXITCODE -ne 0) { throw "Could not verify tracked-file status for $Path; refusing cleanup." }
    if ($tracked.Count -gt 0) { throw "Tracked files are present under cleanup target; refusing: $Path" }
}

function Get-SafeFiles([string]$StartPath) {
    # Walk only ordinary directories. Do not follow junctions, symlinks or other reparse points.
    $stack = [Collections.Generic.Stack[string]]::new()
    Assert-OrdinaryPath $WorkspaceRoot $StartPath
    $stack.Push($StartPath)
    while ($stack.Count -gt 0) {
        $directory = $stack.Pop()
        try { $entries = [IO.Directory]::EnumerateFileSystemEntries($directory) }
        catch [UnauthorizedAccessException] { Write-Warning "Skipped inaccessible directory: $directory"; continue }
        foreach ($entry in $entries) {
            $attributes = [IO.File]::GetAttributes($entry)
            if (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { continue }
            if (($attributes -band [IO.FileAttributes]::Directory) -ne 0) {
                Assert-OrdinaryPath $WorkspaceRoot $entry
                if ($SensitiveDirectoryNames -contains [IO.Path]::GetFileName($entry).ToLowerInvariant()) { continue }
                $stack.Push($entry)
            } else {
                $entry
            }
        }
    }
}

function Get-ReferenceText([string]$Root) {
    $sources = @(
        @{ Path = (Join-Path $Root 'docs'); Extensions = @('.md', '.html') },
        @{ Path = (Join-Path $Root 'scripts'); Extensions = @('.ps1', '.py', '.md', '.html') },
        @{ Path = (Join-Path $Root 'tests'); Extensions = @('.py', '.ps1', '.md', '.html') }
    )
    foreach ($source in $sources) {
        if (-not (Test-Path -LiteralPath $source.Path -PathType Container)) { continue }
        foreach ($file in Get-SafeFiles $source.Path) {
            if ($source.Extensions -notcontains [IO.Path]::GetExtension($file).ToLowerInvariant()) { continue }
            try { [IO.File]::ReadAllText($file) } catch { Write-Warning "Could not inspect reference source $file; cleanup will stop."; throw }
        }
    }
}

function Test-Referenced([string]$CandidateName, [string]$RelativePath, [string[]]$Corpora) {
    $needles = @($CandidateName, $RelativePath.Replace('\', '/')) | Select-Object -Unique
    foreach ($corpus in $Corpora) {
        foreach ($needle in $needles) {
            if ($needle -and $corpus.IndexOf($needle, [StringComparison]::OrdinalIgnoreCase) -ge 0) { return $true }
        }
    }
    return $false
}

function Test-VerifiedArchivedPytestRun([string]$CandidateName) {
    if (-not $IncludeArchivedPytestRuns) { return $false }
    $proofFiles = @{
        'pytest-final-release' = 'pytest-final.xml'
        'pytest-p0-baseline' = 'pytest-p0-baseline.xml'
    }
    if (-not $proofFiles.ContainsKey($CandidateName)) { return $false }
    if (-not (Test-Path -LiteralPath $WorktreeRoot -PathType Container)) { return $false }
    $relativeProof = Join-Path 'runtime\readiness-2026-10-30' $proofFiles[$CandidateName]
    $sourceProof = Join-Path $WorktreeRoot $relativeProof
    $preservedProof = Join-Path $MainRoot $relativeProof
    if (-not (Test-Path -LiteralPath $sourceProof -PathType Leaf) -or -not (Test-Path -LiteralPath $preservedProof -PathType Leaf)) { return $false }
    Assert-OrdinaryPath $WorktreeRoot $sourceProof
    Assert-OrdinaryPath $MainRoot $preservedProof
    $sourceHash = Get-LocalSha256 $sourceProof
    $preservedHash = Get-LocalSha256 $preservedProof
    return ($sourceHash -eq $preservedHash)
}

function Get-RepoCorpora {
    $items = [Collections.Generic.List[string]]::new()
    foreach ($root in $RepoRoots) {
        foreach ($text in (Get-ReferenceText $root)) { $items.Add($text) }
    }
    return ,$items.ToArray()
}

function Get-TreeStats([string]$Path) {
    [long]$bytes = 0
    [long]$files = 0
    foreach ($file in Get-SafeFiles $Path) {
        if (Test-ProtectedFile $file) { continue }
        $bytes += ([IO.FileInfo]::new($file)).Length
        $files++
    }
    return @{ Files = $files; Bytes = $bytes }
}

function Remove-SafeTreeContents([string]$Path) {
    # Files only; directories are removed bottom-up only when empty. Reparse points remain untouched.
    $directories = [Collections.Generic.List[string]]::new()
    $directories.Add($Path)
    $stack = [Collections.Generic.Stack[string]]::new()
    $stack.Push($Path)
    while ($stack.Count -gt 0) {
        $directory = $stack.Pop()
        Assert-OrdinaryPath $WorkspaceRoot $directory
        foreach ($entry in [IO.Directory]::EnumerateFileSystemEntries($directory)) {
            $attributes = [IO.File]::GetAttributes($entry)
            if (($attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { continue }
            if (($attributes -band [IO.FileAttributes]::Directory) -ne 0) {
                Assert-OrdinaryPath $WorkspaceRoot $entry
                if ($SensitiveDirectoryNames -contains [IO.Path]::GetFileName($entry).ToLowerInvariant()) { continue }
                $directories.Add($entry)
                $stack.Push($entry)
            } elseif (Test-ProtectedFile $entry) {
                continue
            } elseif ($DoApply) {
                [void](Get-SafeRelative $Path $entry)
                [IO.File]::Delete($entry)
            }
        }
    }
    if ($DoApply) {
        for ($i = $directories.Count - 1; $i -ge 1; $i--) {
            try { [IO.Directory]::Delete($directories[$i], $false) } catch [IO.IOException] { }
        }
        # Retain the runtime/pytest-* container if a reparse point or other node remains.
        try { [IO.Directory]::Delete($Path, $false) } catch [IO.IOException] { }
    }
}

function Remove-ReparseLinkOnly([string]$Path) {
    # Delete the link object only. Never resolve or recurse into its target.
    if (-not (Test-ReparsePoint $Path)) { throw "Refusing non-reparse path: $Path" }
    $full = [IO.Path]::GetFullPath($Path)
    $runtimeRoot = [IO.Path]::GetFullPath((Join-Path $WorktreeRoot 'runtime')).TrimEnd('\') + '\'
    $relative = Get-SafeRelative $runtimeRoot $full
    $parts = $relative.Split([IO.Path]::DirectorySeparatorChar)
    if ($parts.Count -lt 2 -or -not $parts[0].StartsWith('pytest-', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Reparse link is outside the worktree runtime pytest namespace: $full"
    }
    $attributes = [IO.File]::GetAttributes($full)
    if (($attributes -band [IO.FileAttributes]::Directory) -ne 0) {
        [IO.Directory]::Delete($full, $false)
    } else {
        [IO.File]::Delete($full)
    }
}

function Preserve-ReadinessEvidence {
    if (-not (Test-Path -LiteralPath $WorktreeRoot -PathType Container)) {
        Write-Output "SKIP evidence copy; readiness worktree is absent: $WorktreeRoot"
        return
    }
    $sourceRuntime = Join-Path $WorktreeRoot 'runtime'
    $destRuntime = Join-Path $MainRoot 'runtime'
    $allowedTopNames = @('demoqa', 'readiness', 'readiness-2026-10-30')
    $evidenceExcludedNames = $SensitiveDirectoryNames + @('camoufox', 'worker-state', 'browser-profile', 'browser-profiles', 'downloads')
    $allowedExtensions = @('.json', '.jsonl', '.png', '.xml', '.gif', '.csv')
    foreach ($topName in $allowedTopNames) {
        $source = Join-Path $sourceRuntime $topName
        if (-not (Test-Path -LiteralPath $source -PathType Container)) { continue }
        foreach ($file in Get-SafeFiles $source) {
            Assert-OrdinaryPath $WorktreeRoot $file
            $relative = Get-SafeRelative $sourceRuntime $file
            $parts = $relative.Split([IO.Path]::DirectorySeparatorChar)
            if ($parts | Where-Object { $evidenceExcludedNames -contains $_.ToLowerInvariant() }) { continue }
            if ($allowedExtensions -notcontains [IO.Path]::GetExtension($file).ToLowerInvariant()) { continue }
            $destination = Join-Path $destRuntime $relative
            if (Test-Path -LiteralPath $destination -PathType Leaf) {
                $sourceHash = Get-LocalSha256 $file
                $destHash = Get-LocalSha256 $destination
                if ($sourceHash -ne $destHash) { throw "Evidence collision; review required: $destination" }
                Write-Output "KEEP identical evidence: $destination"
                continue
            }
            Write-Output "$(if ($DoApply) { 'COPY' } else { 'WOULD COPY' }) evidence: $file -> $destination"
            if ($DoApply) {
                $destinationDirectory = Split-Path -Parent $destination
                Assert-OrdinaryPath $MainRoot $destinationDirectory
                [IO.Directory]::CreateDirectory($destinationDirectory) | Out-Null
                [IO.File]::Copy($file, $destination, $false)
            }
        }
    }
}

if ($PreserveEvidence) { Preserve-ReadinessEvidence }

$corpora = Get-RepoCorpora
$targets = [Collections.Generic.List[object]]::new()
foreach ($root in $RepoRoots) {
    foreach ($relative in @('scripts\__pycache__', 'src\browser_mcp\__pycache__', 'tests\__pycache__', 'worker\__pycache__', '.pytest_cache')) {
        $path = Join-Path $root $relative
        if (Test-Path -LiteralPath $path -PathType Container) { $targets.Add(@{ Path = $path; Kind = 'generated cache' }) }
    }
    $runtime = Join-Path $root 'runtime'
    if (-not (Test-Path -LiteralPath $runtime -PathType Container)) { continue }
    foreach ($entry in [IO.Directory]::EnumerateFileSystemEntries($runtime)) {
        $name = [IO.Path]::GetFileName($entry)
        if (-not $name.StartsWith('pytest-', [StringComparison]::OrdinalIgnoreCase)) { continue }
        if (Test-ReparsePoint $entry) { Write-Output "SKIP reparse point: $entry"; continue }
        if (-not ([IO.File]::GetAttributes($entry) -band [IO.FileAttributes]::Directory)) { continue }
        $relative = 'runtime/' + $name
        if (Test-Referenced -CandidateName $name -RelativePath $relative -Corpora $corpora) {
            if (-not (Test-VerifiedArchivedPytestRun $name)) {
                Write-Output "KEEP referenced runtime evidence: $entry"
                continue
            }
            Write-Output "ALLOWLIST verified archived pytest tree: $entry"
        }
        $targets.Add(@{ Path = $entry; Kind = 'unreferenced first-level pytest temp tree' })
    }
}

foreach ($target in $targets) {
    $path = [IO.Path]::GetFullPath($target.Path)
    if ($path.StartsWith($WorktreeRoot, [StringComparison]::OrdinalIgnoreCase)) { Assert-NoTrackedFiles $WorktreeRoot $path }
    else { Assert-NoTrackedFiles $MainRoot $path }
    $stats = Get-TreeStats $path
    Write-Output "$(if ($DoApply) { 'REMOVE' } else { 'WOULD REMOVE' }) $($target.Kind): $path ($($stats.Files) files, $($stats.Bytes) bytes)"
    if ($DoApply) { Remove-SafeTreeContents $path }
}

if ($TargetedReparseCleanup) {
    $pytestRuntime = Join-Path $WorktreeRoot 'runtime'
    $pytestRoots = @()
    if (Test-Path -LiteralPath $pytestRuntime -PathType Container) { $pytestRoots = @([IO.Directory]::EnumerateDirectories($pytestRuntime, 'pytest-*')) }
    foreach ($pytestRoot in $pytestRoots) {
        if (Test-ReparsePoint $pytestRoot) { continue }
        $pytestName = [IO.Path]::GetFileName($pytestRoot)
        if ((Test-Referenced -CandidateName $pytestName -RelativePath ('runtime/' + $pytestName) -Corpora $corpora) -and
            -not (Test-VerifiedArchivedPytestRun $pytestName)) { continue }
        $stack = [Collections.Generic.Stack[string]]::new()
        $stack.Push($pytestRoot)
        while ($stack.Count -gt 0) {
            $directory = $stack.Pop()
            foreach ($entry in [IO.Directory]::EnumerateFileSystemEntries($directory)) {
                if (Test-ReparsePoint $entry) {
                    Write-Output "$(if ($DoApply) { 'REMOVE LINK ONLY' } else { 'WOULD REMOVE LINK ONLY' }): $entry"
                    if ($DoApply) { Remove-ReparseLinkOnly $entry }
                } elseif ([IO.File]::GetAttributes($entry) -band [IO.FileAttributes]::Directory) {
                    $stack.Push($entry)
                }
            }
        }
    }
}

if (-not $DoApply) { Write-Output 'Dry run only. Pass -Apply to perform the listed local filesystem changes.' }
