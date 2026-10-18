@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM Model dir comes from config.json (models.dir); no HF_HOME, no hard-coded proxy.
echo ============================================
echo   bili-note ASR model download
echo   Default dir: %~dp0models   (change it in the app)
echo   Default source: hf-mirror.com (change it in the app)
echo ============================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [error] Not installed yet. Run install.bat first.
    pause
    exit /b 1
)

.venv\Scripts\python.exe download_model.py %*
echo.
pause
