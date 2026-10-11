@echo off
cd /d "%~dp0"

set "FOUND="
for /f "tokens=5" %%p in ('netstat -ano ^| findstr "127.0.0.1:8001" ^| findstr "LISTENING"') do (
    set "FOUND=1"
    echo Stopping bili-note PID: %%p ...
    taskkill /PID %%p /F >nul 2>&1
)

if not defined FOUND (
    echo bili-note is not running.
    pause
    exit /b 0
)

echo Waiting for port to release...
set "TRIES=0"
:waitloop
ping -n 1 127.0.0.1 >nul
set /a TRIES+=1
netstat -ano | findstr "127.0.0.1:8001" | findstr "LISTENING" >nul 2>&1
if errorlevel 1 goto released
if %TRIES% geq 8 (
    echo Stop FAILED, kill it manually in Task Manager.
    pause
    exit /b 0
)
goto waitloop

:released
echo Stopped, port 8001 released.
pause
