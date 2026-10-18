# Build the portable Windows package for bili-note V3.
#
#   powershell -ExecutionPolicy Bypass -File scripts\build_portable.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\build_portable.ps1 -NoCuda
#
# Outputs:
#   dist\bili-note\bili-note.exe          (onedir; run it directly)
#   release\bili-note-v3-portable-win64.zip
#   release\bili-note-v3-portable-cpu-win64.zip   (-NoCuda)
#
# The zip is intended to be unzipped to a WRITABLE folder (e.g. D:\bili-note).
# config.json / output / models are created next to bili-note.exe.

param([switch]$NoCuda)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Info($m) { Write-Host "[build] $m" -ForegroundColor Cyan }
function Warn($m) { Write-Host "[warn] $m" -ForegroundColor Yellow }
function Die($m) { Write-Host "[error] $m" -ForegroundColor Red; exit 1 }

$VenvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPy)) { Die "Run install.bat first (no .venv found)." }

Info "Installing build tools ..."
& $VenvPy -m pip install --disable-pip-version-check -r (Join-Path $Root "requirements-build.txt") `
    -i "https://pypi.tuna.tsinghua.edu.cn/simple"
if ($LASTEXITCODE -ne 0) {
    & $VenvPy -m pip install --disable-pip-version-check -r (Join-Path $Root "requirements-build.txt")
}
if ($LASTEXITCODE -ne 0) { Die "Could not install PyInstaller." }

# ---- ffmpeg: prefer copying from PATH (same build the developer tests with) ----
$ffDest = Join-Path $Root "tools\ffmpeg\bin"
$ffExe = Join-Path $ffDest "ffmpeg.exe"
if (-not (Test-Path $ffExe)) {
    Info "Preparing bundled ffmpeg ..."
    New-Item -ItemType Directory -Force $ffDest | Out-Null
    $ff = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if ($ff) {
        Copy-Item $ff.Source $ffDest -Force
        $fp = Get-Command ffprobe -ErrorAction SilentlyContinue
        if ($fp) { Copy-Item $fp.Source $ffDest -Force }
    }
    if (-not (Test-Path $ffExe)) {
        Warn "ffmpeg not on PATH; downloading gyan.dev essentials ..."
        $zip = Join-Path $Root "tools\ffmpeg.zip"
        try {
            Invoke-WebRequest -Uri "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" `
                -OutFile $zip -UseBasicParsing
            $tmp = Join-Path $Root "tools\ffmpeg-tmp"
            Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
            Expand-Archive $zip -DestinationPath $tmp -Force
            $exe = Get-ChildItem $tmp -Recurse -Filter ffmpeg.exe | Select-Object -First 1
            if ($exe) { Copy-Item (Join-Path $exe.DirectoryName "*") $ffDest -Force }
            $probe = Get-ChildItem $tmp -Recurse -Filter ffprobe.exe | Select-Object -First 1
            if ($probe) { Copy-Item $probe.FullName $ffDest -Force }
            Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
            Remove-Item $zip -Force -ErrorAction SilentlyContinue
        } catch {
            Die ("ffmpeg download failed: " + $_.Exception.Message)
        }
    }
}
if (-not (Test-Path $ffExe)) { Die "ffmpeg.exe is still missing under tools\ffmpeg\bin." }

# ---- CUDA DLLs: ctranslate2 wheel does not ship cublas ----
$cublas = Join-Path $Root ".venv\Lib\site-packages\nvidia\cublas\bin"
$ct2 = Join-Path $Root ".venv\Lib\site-packages\ctranslate2"
if (-not $NoCuda) {
    if (Test-Path $cublas) {
        foreach ($dll in Get-ChildItem $cublas -Filter "*.dll" -ErrorAction SilentlyContinue) {
            $destDll = Join-Path $ct2 $dll.Name
            if ((Test-Path $destDll) -and ((Get-Item $destDll).Length -eq $dll.Length)) { continue }
            try {
                Copy-Item $dll.FullName $destDll -Force -ErrorAction Stop
            } catch {
                Warn ("Could not copy " + $dll.Name + " (is bili-note running?): " + $_.Exception.Message)
            }
        }
        Info "cublas DLLs staged into ctranslate2."
    } elseif (-not (Test-Path (Join-Path $ct2 "cublas64_12.dll"))) {
        Warn "No cublas runtime found. The GPU build will fall back to CPU. Run install.bat on an NVIDIA machine first."
    }
}

# ---- clean build ----
Info "Cleaning dist/build ..."
Remove-Item (Join-Path $Root "dist") -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $Root "build") -Recurse -Force -ErrorAction SilentlyContinue

Info "Running PyInstaller (this can take several minutes) ..."
& $VenvPy -m PyInstaller --noconfirm --clean bili-note.spec
if ($LASTEXITCODE -ne 0) { Die "PyInstaller failed." }

$Dist = Join-Path $Root "dist\bili-note"
$Exe = Join-Path $Dist "bili-note.exe"
if (-not (Test-Path $Exe)) { Die "bili-note.exe was not produced." }

if ($NoCuda) {
    Get-ChildItem (Join-Path $Dist "_internal\ctranslate2") -Filter "cublas*.dll" -ErrorAction SilentlyContinue |
        Remove-Item -Force -ErrorAction SilentlyContinue
    # nvblas depends on cublas; leaving it behind breaks `import ctranslate2` at startup.
    Remove-Item (Join-Path $Dist "_internal\ctranslate2\nvblas64_12.dll") -Force -ErrorAction SilentlyContinue
    Info "CPU build: removed cublas + nvblas DLLs."
}

# ---- smoke tests: probe + real server on a temp data dir ----
Info "Probing CUDA from the built exe ..."
$probeOut = & $Exe --probe-cuda 2>&1 | Select-Object -Last 1
Info ("probe: " + $probeOut)

Info "Smoke-testing the built server ..."
# 不用 %TEMP%：受限沙箱/杀软偶尔会让新目录写不进去，烟雾测试会变成假失败。
# 用仓库内的 .verify 目录（可写、且跑完删掉）。
$TestData = Join-Path $Root ".verify\portable-build-test"
Remove-Item $TestData -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $TestData | Out-Null
$OutLog = Join-Path $TestData "stdout.log"
$ErrLog = Join-Path $TestData "stderr.log"
$env:BILI_NOTE_DATA_DIR = $TestData
$env:BILI_NOTE_NO_BROWSER = "1"
$env:BILI_NOTE_PORT = "8021"
$proc = Start-Process -FilePath $Exe -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput $OutLog -RedirectStandardError $ErrLog

function Test-TcpPort([int]$port) {
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $iar = $client.BeginConnect("127.0.0.1", $port, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne(400)) { $client.Close(); return $false }
        $client.EndConnect($iar)
        $client.Close()
        return $true
    } catch { return $false }
}

$version = $null
$Port = 8021
try {
    for ($i = 0; $i -lt 160; $i++) {
        $inst = Join-Path $TestData "instance.json"
        if (Test-Path $inst) {
            try {
                $info = Get-Content -Raw $inst | ConvertFrom-Json
                if ($info.port) { $Port = [int]$info.port }
            } catch { }
        }
        if (Test-TcpPort $Port) {
            try {
                $version = Invoke-RestMethod ("http://127.0.0.1:" + $Port + "/api/version") -TimeoutSec 5
                if ($version) { break }
            } catch { }
        }
        Start-Sleep -Milliseconds 500
    }
    if (-not $version) {
        if (Test-Path $OutLog) { Get-Content $OutLog -Tail 30 }
        if (Test-Path $ErrLog) { Get-Content $ErrLog -Tail 30 }
        Die "Built exe did not answer /api/version."
    }
    Info ("server version: " + $version.version + " / " + $version.name)
    $status = Invoke-RestMethod ("http://127.0.0.1:" + $Port + "/api/asr/status?refresh=1") -TimeoutSec 90
    if (-not $status.models) { Die "Built exe /api/asr/status returned no models." }
    Info ("status: " + $status.machine.label + " / models_dir=" + $status.models_dir.path)
} finally {
    if ($proc -and -not $proc.HasExited) { Stop-Process -Id $proc.Id -Force }
    Start-Sleep -Milliseconds 800
    Remove-Item Env:BILI_NOTE_DATA_DIR -ErrorAction SilentlyContinue
    Remove-Item Env:BILI_NOTE_NO_BROWSER -ErrorAction SilentlyContinue
    Remove-Item Env:BILI_NOTE_PORT -ErrorAction SilentlyContinue
    Remove-Item $TestData -Recurse -Force -ErrorAction SilentlyContinue
}

# ---- ship notes + clean runtime data out of dist ----
# ASCII filename on purpose: Windows bsdtar (used for zipping) stores non-ASCII
# names in the OEM code page, which shows up as mojibake in Explorer.
Copy-Item (Join-Path $Root "PORTABLE-README.txt") (Join-Path $Dist "README-PORTABLE.txt") -Force
foreach ($name in @("config.json", "output", "models", "instance.json", "server.log", "install.log")) {
    Remove-Item (Join-Path $Dist $name) -Recurse -Force -ErrorAction SilentlyContinue
}

# ---- zip ----
$Release = Join-Path $Root "release"
New-Item -ItemType Directory -Force $Release | Out-Null
$zipName = if ($NoCuda) { "bili-note-v3-portable-cpu-win64.zip" } else { "bili-note-v3-portable-win64.zip" }
$zip = Join-Path $Release $zipName
Remove-Item $zip -Force -ErrorAction SilentlyContinue
Info "Zipping to $zip ..."
# tar -C needs the PARENT of the dist folder, otherwise it looks for dist\bili-note\bili-note.
& tar.exe -a -c -f $zip -C (Split-Path $Dist -Parent) "bili-note"
if ($LASTEXITCODE -ne 0) {
    Warn "tar failed; falling back to Compress-Archive (slower) ..."
    Compress-Archive -Path $Dist -DestinationPath $zip -Force
}
if (-not (Test-Path $zip)) { Die "zip was not produced." }
$sizeMB = [math]::Round((Get-Item $zip).Length / 1MB, 1)
Info ("artifact: " + $zip + " (" + $sizeMB + " MB)")
Info "Done. Unzip to a writable folder and run bili-note.exe."
