@echo off
chcp 65001 >nul
cd /d "%~dp0"
title bili-note V3

if not exist ".venv\Scripts\python.exe" (
    echo ============================================
    echo   bili-note is not installed yet.
    echo   Double-click install.bat first, then start.bat.
    echo ============================================
    pause
    exit /b 1
)

set "PYTHONIOENCODING=utf-8"
".venv\Scripts\python.exe" -u run.py
echo.
echo bili-note has exited.
pause
