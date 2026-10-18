@echo off
chcp 65001 >nul
cd /d "%~dp0"
title bili-note - install

REM Real logic lives in scripts\install.ps1 (batch version checks are too error-prone).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1"
if errorlevel 1 (
    echo.
    echo Install did not finish. See the error above / install.log.
    pause
)
