@echo off
cd /d "%~dp0"

REM Model cache dir (D drive, not C); default transcription model
set "HF_HOME=%~dp0models"
set "BILI_NOTE_MODEL=large-v3"

REM If port 8001 is already listening, backend is running: just open browser
netstat -ano | findstr "127.0.0.1:8001" | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 (
    echo bili-note is already running, opening browser...
    start "" http://127.0.0.1:8001
    exit /b 0
)

echo Starting bili-note in background (log: server.log)...
start "" /b cmd /c ".venv\Scripts\python.exe -u run.py >> server.log 2>&1"

REM Poll until port is listening (import can take several seconds), up to ~15s
echo Waiting for service to come up...
set "TRIES=0"
:waitloop
ping -n 1 127.0.0.1 >nul
set /a TRIES+=1
netstat -ano | findstr "127.0.0.1:8001" | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 goto up
if %TRIES% geq 15 (
    echo Start failed, check server.log.
    exit /b 0
)
goto waitloop

:up
echo Started OK, browser will open automatically.
exit /b 0
