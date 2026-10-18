@echo off
chcp 65001 >nul
cd /d "%~dp0"
title bili-note - update

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\update.ps1"
pause
