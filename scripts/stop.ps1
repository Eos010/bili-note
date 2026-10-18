# Stop a running bili-note instance.
# Reads instance.json (pid/port) and verifies the process really is python/bili-note
# before killing it; falls back to ports 8001-8010.

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$inst = Join-Path $Root "instance.json"
$killed = $false

if (Test-Path $inst) {
    try {
        $info = Get-Content -Raw $inst | ConvertFrom-Json
        $proc = Get-Process -Id $info.pid -ErrorAction Stop
        if ($proc.ProcessName -match "python|bili-note") {
            Stop-Process -Id $info.pid -Force -ErrorAction Stop
            Write-Host ("Stopped bili-note PID " + $info.pid + " (port " + $info.port + ")")
            $killed = $true
        } else {
            Write-Host ("Stale instance.json: PID " + $info.pid + " is " + $proc.ProcessName + ", not bili-note.")
        }
    } catch {
        Write-Host "instance.json is stale; falling back to port scan."
    }
}

if (-not $killed) {
    foreach ($port in 8001..8010) {
        $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
        foreach ($c in $conns) {
            $proc = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue
            if ($proc -and $proc.ProcessName -match "python|bili-note") {
                Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
                Write-Host ("Stopped listener on port " + $port + " PID " + $c.OwningProcess)
                $killed = $true
            }
        }
    }
}

Remove-Item $inst -Force -ErrorAction SilentlyContinue
if (-not $killed) { Write-Host "bili-note is not running." }
