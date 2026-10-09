@echo off
REM Start both local llama-server processes in the foreground (Ctrl-C stops them).
REM The app normally starts them by itself; use this only for manual control or troubleshooting.
REM ASCII only: Chinese Windows cmd is GBK.
setlocal
cd /d "%~dp0.."

where python >nul 2>nul
if errorlevel 1 (
    echo Python not found. Run from source needs Python 3.11; the packaged exe has its own button.
    pause
    exit /b 1
)

echo Starting clef-flash (judge) and the drafting text model ...
echo Config is read from config.json. Ports default to 8790 (clef) and 8791 (draft).
echo.
python -m app.local_server start
pause
