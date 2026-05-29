<#
.SYNOPSIS
    Install the learn-ai host-daemon as a Windows service via NSSM.

.DESCRIPTION
    Per PRD-A § 16.4 Resolution / PR-A: the host_daemon owns the
    per-strategy live-run subprocess and needs to auto-start on
    Windows boot so an unattended reboot during a paper-week doesn't
    require manual operator intervention before market open.

    The script:
      - Verifies NSSM is available on PATH (refusing to silently
        install nothing).
      - Resolves the repo root, Python interpreter, and live-runs
        root the daemon will use.
      - Refuses to overwrite an existing service unless -Force is
        passed; -Force performs uninstall + reinstall.
      - Installs the service to run as the current user with
        auto-start on boot and on-failure restart with 10s backoff.
      - Routes stdout / stderr to <repo>/PythonDataService/artifacts/
        host_daemon_service.log.

    Run from an elevated PowerShell prompt.

.PARAMETER ServiceName
    Windows service name to install. Defaults to learn-ai-host-daemon.

.PARAMETER RepoRoot
    Absolute path to the learn-ai repo root. Defaults to the parent
    of the directory holding this script.

.PARAMETER PythonExe
    Absolute path to the Python interpreter (typically a venv). If
    omitted, resolves <RepoRoot>/PythonDataService/.venv/Scripts/python.exe.

.PARAMETER Port
    Loopback port the daemon binds to. Defaults to 8765 (the daemon's
    own default).

.PARAMETER Force
    If set, uninstall any existing service of the same name before
    installing.

.EXAMPLE
    pwsh -File install-host-daemon-service.ps1

.EXAMPLE
    pwsh -File install-host-daemon-service.ps1 -Force -Port 8765
#>

[CmdletBinding()]
param(
    [string]$ServiceName = 'learn-ai-host-daemon',
    [string]$RepoRoot,
    [string]$PythonExe,
    [int]$Port = 8765,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

# ---------- Resolve paths ----------

if (-not $RepoRoot) {
    # The script lives at <repo>/PythonDataService/scripts/.
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..' '..')).Path
}
$RepoRoot = (Resolve-Path $RepoRoot).Path
Write-Host "[install-host-daemon] RepoRoot: $RepoRoot"

if (-not $PythonExe) {
    $PythonExe = Join-Path $RepoRoot 'PythonDataService' '.venv' 'Scripts' 'python.exe'
}
if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "Python interpreter not found at $PythonExe. Pass -PythonExe explicitly or create the venv at PythonDataService/.venv first."
}
Write-Host "[install-host-daemon] PythonExe: $PythonExe"

$WorkingDirectory = Join-Path $RepoRoot 'PythonDataService'
$LiveRunsRoot = Join-Path $WorkingDirectory 'artifacts' 'live_runs'
$LogDirectory = Join-Path $WorkingDirectory 'artifacts'
$StdoutLog = Join-Path $LogDirectory 'host_daemon_service.out.log'
$StderrLog = Join-Path $LogDirectory 'host_daemon_service.err.log'

if (-not (Test-Path -LiteralPath $LogDirectory)) {
    New-Item -ItemType Directory -Path $LogDirectory | Out-Null
}

# ---------- Locate NSSM ----------

$nssm = Get-Command -Name nssm -ErrorAction SilentlyContinue
if (-not $nssm) {
    throw "nssm not found on PATH. Install from https://nssm.cc/ (chocolatey: 'choco install nssm') and re-run."
}
$NssmExe = $nssm.Source
Write-Host "[install-host-daemon] nssm: $NssmExe"

# ---------- Handle existing service ----------

$existing = & $NssmExe status $ServiceName 2>$null
if ($LASTEXITCODE -eq 0) {
    if (-not $Force) {
        throw "Service '$ServiceName' already exists. Re-run with -Force to uninstall + reinstall."
    }
    Write-Host "[install-host-daemon] Existing service found; uninstalling (-Force)."
    & $NssmExe stop $ServiceName confirm 2>$null | Out-Null
    & $NssmExe remove $ServiceName confirm | Out-Null
}

# ---------- Install ----------

$arguments = @(
    '-m', 'app.engine.live.host_daemon',
    '--port', "$Port",
    '--repo-root', "$RepoRoot",
    '--live-runs-root', "$LiveRunsRoot"
) -join ' '

Write-Host "[install-host-daemon] Installing service '$ServiceName'..."
& $NssmExe install $ServiceName $PythonExe $arguments | Out-Null

# Working dir + env so the python module path resolves the PythonDataService package.
& $NssmExe set $ServiceName AppDirectory $WorkingDirectory | Out-Null
& $NssmExe set $ServiceName AppEnvironmentExtra "PYTHONPATH=$WorkingDirectory" | Out-Null

# Boot policy: auto-start. Restart-on-failure with 10s backoff per
# the PRD's "Restart=on-failure with 10s back-off" spec.
& $NssmExe set $ServiceName Start SERVICE_AUTO_START | Out-Null
& $NssmExe set $ServiceName AppExit Default Restart | Out-Null
& $NssmExe set $ServiceName AppRestartDelay 10000 | Out-Null
& $NssmExe set $ServiceName AppThrottle 10000 | Out-Null

# Route stdout/stderr to rotating files. NSSM rotates by size when
# AppRotateBytes is set; 10 MB keeps logs bounded over a paper-week.
& $NssmExe set $ServiceName AppStdout $StdoutLog | Out-Null
& $NssmExe set $ServiceName AppStderr $StderrLog | Out-Null
& $NssmExe set $ServiceName AppRotateFiles 1 | Out-Null
& $NssmExe set $ServiceName AppRotateOnline 1 | Out-Null
& $NssmExe set $ServiceName AppRotateBytes 10485760 | Out-Null

Write-Host "[install-host-daemon] Installed."
Write-Host "[install-host-daemon] To start now:   nssm start $ServiceName"
Write-Host "[install-host-daemon] To check state: nssm status $ServiceName"
Write-Host "[install-host-daemon] Logs:           $StdoutLog / $StderrLog"
Write-Host "[install-host-daemon] To uninstall:   nssm remove $ServiceName confirm"
