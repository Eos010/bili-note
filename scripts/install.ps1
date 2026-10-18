# bili-note source install (Windows PowerShell 5.1+)
#
# This is the *developer* path. Normal users should use the portable zip:
# unzip to a writable folder, double-click bili-note.exe. No Python/Git needed.
#
# What this script does:
#   1. finds Python 3.10-3.12 (installs 3.12 via winget if needed);
#   2. creates .venv and installs requirements (Tsinghua mirror, fallback PyPI);
#   3. if an NVIDIA GPU is present, installs cublas and copies its DLLs for ctranslate2;
#   4. finds/installs ffmpeg (PATH -> winget -> gyan.dev zip into tools\ffmpeg);
#   5. creates config.json from config.example.json;
#   6. does NOT download any ASR model.
#
# Safe to re-run.

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Mirror = "https://pypi.tuna.tsinghua.edu.cn/simple"
$Log = Join-Path $Root "install.log"
Start-Transcript -Path $Log -Append | Out-Null

function Info($m) { Write-Host "[install] $m" -ForegroundColor Cyan }
function Warn($m) { Write-Host "[warn] $m" -ForegroundColor Yellow }
function Die($m) { Write-Host "[error] $m" -ForegroundColor Red; Stop-Transcript | Out-Null; exit 1 }

function Find-Python {
    $candidates = @(
        @{ Exe = "py"; Args = @("-3.12") },
        @{ Exe = "py"; Args = @("-3.11") },
        @{ Exe = "py"; Args = @("-3.10") },
        @{ Exe = "python"; Args = @() }
    )
    foreach ($c in $candidates) {
        if (-not (Get-Command $c.Exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $out = & $c.Exe @($c.Args) -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
        } catch { continue }
        if ($out -match '^3\.(10|11|12)$') { return $c }
    }
    return $null
}

Info "bili-note source install"
$py = Find-Python
if (-not $py) {
    Warn "Python 3.10-3.12 not found. Trying winget..."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        & winget install -e --id Python.Python.3.12 --scope user --silent `
            --accept-package-agreements --accept-source-agreements
        $py = Find-Python
    }
}
if (-not $py) {
    Warn "Could not install Python automatically. Opening python.org..."
    Start-Process "https://www.python.org/downloads/windows/"
    Die "Install Python 3.12 (check 'Add python.exe to PATH'), then run install.bat again."
}
Info ("Python: " + $py.Exe + " " + ($py.Args -join " "))

$Venv = Join-Path $Root ".venv"
$VenvPy = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $VenvPy)) {
    Info "Creating .venv ..."
    & $py.Exe @($py.Args) -m venv $Venv
    if ($LASTEXITCODE -ne 0) { Die "venv creation failed" }
}
if (-not (Test-Path $VenvPy)) { Die ".venv\Scripts\python.exe was not created" }

$script:PipOk = $true
function Pip-Run([string[]]$PipArgs) {
    & $VenvPy -m pip @PipArgs -i $Mirror
    if ($LASTEXITCODE -ne 0) {
        Warn "Mirror failed; retrying with official PyPI..."
        & $VenvPy -m pip @PipArgs
    }
    $script:PipOk = ($LASTEXITCODE -eq 0)
}

Info "Upgrading pip ..."
& $VenvPy -m pip install --upgrade pip -i $Mirror *> $null
Info "Installing requirements.txt ..."
Pip-Run @("install", "--disable-pip-version-check", "-r", (Join-Path $Root "requirements.txt"))
if (-not $script:PipOk) { Die "pip install failed; see install.log" }

$nvidia = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if ($nvidia) {
    Info "NVIDIA GPU detected; installing CUDA runtime (cublas) ..."
    Pip-Run @("install", "--disable-pip-version-check", "-r", (Join-Path $Root "requirements-gpu.txt"))
    if (-not $script:PipOk) {
        Warn "GPU runtime install failed; CPU transcription still works."
    } else {
        $cublas = Join-Path $Venv "Lib\site-packages\nvidia\cublas\bin"
        $ct2 = Join-Path $Venv "Lib\site-packages\ctranslate2"
        if ((Test-Path $cublas) -and (Test-Path $ct2)) {
            # If bili-note is running, loaded DLLs are locked; same size means already in place.
            foreach ($dll in Get-ChildItem $cublas -Filter "*.dll" -ErrorAction SilentlyContinue) {
                $destDll = Join-Path $ct2 $dll.Name
                if ((Test-Path $destDll) -and ((Get-Item $destDll).Length -eq $dll.Length)) { continue }
                try {
                    Copy-Item $dll.FullName $destDll -Force -ErrorAction Stop
                } catch {
                    Warn ("Could not copy " + $dll.Name + " (is bili-note running?): " + $_.Exception.Message)
                }
            }
            Info "cublas DLLs are in ctranslate2."
        }
        Info "Probing CUDA (subprocess) ..."
        $probe = & $VenvPy -m app.probe 2>&1 | Select-Object -Last 1
        if ($probe) { Info ("CUDA probe: " + $probe) }
    }
}

function Has-Ffmpeg {
    if (Get-Command ffmpeg -ErrorAction SilentlyContinue) { return $true }
    if (Test-Path (Join-Path $Root "tools\ffmpeg\bin\ffmpeg.exe")) { return $true }
    return $false
}

if (-not (Has-Ffmpeg)) {
    Info "ffmpeg not found; trying winget ..."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        & winget install -e --id Gyan.FFmpeg --scope user --silent `
            --accept-package-agreements --accept-source-agreements
    }
}
if (-not (Has-Ffmpeg)) {
    Info "Downloading ffmpeg essentials into tools\ffmpeg ..."
    $tools = Join-Path $Root "tools"
    $zip = Join-Path $tools "ffmpeg.zip"
    New-Item -ItemType Directory -Force $tools | Out-Null
    try {
        Invoke-WebRequest -Uri "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" `
            -OutFile $zip -UseBasicParsing
    } catch {
        Warn ("ffmpeg download failed: " + $_.Exception.Message)
    }
    if (Test-Path $zip) {
        $tmp = Join-Path $tools "ffmpeg-tmp"
        Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        try {
            Expand-Archive $zip -DestinationPath $tmp -Force
            $exe = Get-ChildItem $tmp -Recurse -Filter ffmpeg.exe | Select-Object -First 1
            if ($exe) {
                $dest = Join-Path $tools "ffmpeg\bin"
                New-Item -ItemType Directory -Force $dest | Out-Null
                Copy-Item (Join-Path $exe.DirectoryName "*") $dest -Force
                Info ("ffmpeg installed to " + $dest)
            }
        } catch {
            Warn ("ffmpeg unzip failed: " + $_.Exception.Message)
        }
        Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        Remove-Item $zip -Force -ErrorAction SilentlyContinue
    }
}
if (-not (Has-Ffmpeg)) {
    Warn "ffmpeg is still missing. Downloading videos will fail until it is installed."
}

$cfg = Join-Path $Root "config.json"
if (-not (Test-Path $cfg)) {
    Copy-Item (Join-Path $Root "config.example.json") $cfg
    Info "config.json created from config.example.json (no API key yet)."
}

Write-Host ""
Info "Done."
Info "Next: double-click start.bat"
Info "ASR models are NOT downloaded by this script. Open the app and use the first-run wizard."
Info ("Log: " + $Log)
Stop-Transcript | Out-Null
