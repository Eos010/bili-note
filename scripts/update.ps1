# Update a source checkout: pull, reinstall requirements, then restart manually.
# Refuses to run on a dirty working tree so local edits are never mixed into a pull.

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Info($m) { Write-Host "[update] $m" -ForegroundColor Cyan }
function Die($m) { Write-Host "[error] $m" -ForegroundColor Red; exit 1 }

if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Die "git not found" }
$dirty = git status --porcelain
if ($dirty) {
    Write-Host $dirty
    Die "Working tree has local changes. Commit or stash them before updating."
}

Info "Pulling latest ..."
git pull --ff-only
if ($LASTEXITCODE -ne 0) { Die "git pull failed" }

$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPy)) { Die "Run install.bat first (no .venv found)." }

Info "Reinstalling requirements ..."
& $VenvPy -m pip install --disable-pip-version-check -r (Join-Path $Root "requirements.txt") `
    -i "https://pypi.tuna.tsinghua.edu.cn/simple"
if ($LASTEXITCODE -ne 0) {
    & $VenvPy -m pip install --disable-pip-version-check -r (Join-Path $Root "requirements.txt")
}
if ($LASTEXITCODE -ne 0) { Die "pip install failed" }

$ver = & $VenvPy -c "from app import __version__; print(__version__)"
Info ("Updated to bili-note " + $ver)
Info "Now run stop.bat, then start.bat."
