# IBKR connectivity diagnostic. Run from PowerShell in the repo root.
# Prints everything we need to identify which layer is blocking the
# Podman container -> IB Gateway connection. ASCII-only on purpose.

$ErrorActionPreference = "Continue"

Write-Host ""
Write-Host "=== 1. Windows-side WSL bridge IPs (candidate IBKR_HOST values) ==="
Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.InterfaceAlias -match "WSL|vEthernet" } |
    Format-Table InterfaceAlias, IPAddress -AutoSize

Write-Host "=== 2. Is anything listening on 4002 on Windows? ==="
$listeners = netstat -an | Select-String ":4002"
if ($listeners) {
    $listeners | ForEach-Object { Write-Host "  $_" }
} else {
    Write-Host "  NOTHING listening on 4002. IB Gateway not running or on a different port."
}

Write-Host ""
Write-Host "=== 3. Existing IBKR firewall rules ==="
$fw = Get-NetFirewallRule -DisplayName "IBKR*" -ErrorAction SilentlyContinue
if ($fw) {
    $fw | Format-Table DisplayName, Direction, Action, Enabled -AutoSize
} else {
    Write-Host "  No IBKR firewall rules. May or may not be the blocker."
}

Write-Host "=== 4. Container view ==="
Write-Host "  Default gateway from inside container:"
podman exec polygon-data-service sh -c "ip route show default | head -1" 2>&1 | ForEach-Object { Write-Host "    $_" }

Write-Host "  Container's IBKR_HOST env value:"
podman exec polygon-data-service sh -c 'echo "    IBKR_HOST=$IBKR_HOST"' 2>&1

Write-Host ""
Write-Host "=== 5. Reachability tests from inside container ==="
$candidates = Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.InterfaceAlias -match "WSL|vEthernet" } |
    Select-Object -ExpandProperty IPAddress
foreach ($ip in $candidates) {
    $code = podman exec polygon-data-service python -c "import socket; s=socket.socket(); s.settimeout(2); print(s.connect_ex(('$ip', 4002)))" 2>&1
    $verdict = if ($code -eq "0") { "OPEN - use this IP" } elseif ($code -eq "111") { "REFUSED (firewall or Gateway allowlist)" } else { "code $code" }
    Write-Host "  $ip -> $verdict"
}

Write-Host ""
Write-Host "=== 6. Last 15 IBKR-related log lines ==="
podman logs polygon-data-service 2>&1 | Select-String -Pattern "IBKR|broker|ib_async" | Select-Object -Last 15 | ForEach-Object { Write-Host "  $_" }

Write-Host ""
Write-Host "=== Done. Paste the above output back. ==="
