@echo off
REM Quick environment check: llama-server, model files, ports.
REM ASCII only: Chinese Windows cmd is GBK.
setlocal
cd /d "%~dp0.."

where python >nul 2>nul
if errorlevel 1 (
    echo Python not found. Use scripts\doctor.ps1 instead, or the packaged exe's Settings page.
    pause
    exit /b 1
)

python -m app.local_server check
echo.
echo Full end-to-end check (starts llama-server and runs one real decision + draft):
echo   python tools\verify_local.py
pause
