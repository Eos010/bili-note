@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM 把模型下载到项目 models 目录（D 盘），不占 C 盘；走梯子代理
set "HF_HOME=%~dp0models"
set "HTTP_PROXY=http://127.0.0.1:7890"
set "HTTPS_PROXY=http://127.0.0.1:7890"

echo ============================================
echo   bili-note 转写模型预下载
echo   目标目录: %HF_HOME%
echo   代理: %HTTP_PROXY%（需先开启 Clash 梯子）
echo ============================================
echo.

.venv\Scripts\python.exe download_model.py

echo.
pause
