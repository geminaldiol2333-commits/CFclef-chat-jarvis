@echo off
REM Restart QQ NT with Chromium accessibility enabled, so its chat content shows up
REM in the Windows accessibility (UIA) tree. Without this flag QQ exposes only a
REM shell: no message list, no message text, nothing the collector can read.
REM
REM ASCII only: Chinese Windows cmd is GBK, and this file must not break on it.
setlocal
cd /d "%~dp0.."

set "QQ_EXE=%ProgramFiles%\Tencent\QQNT\QQ.exe"
if not exist "%QQ_EXE%" set "QQ_EXE=%ProgramFiles(x86)%\Tencent\QQNT\QQ.exe"
if not exist "%QQ_EXE%" (
    echo QQ.exe not found. Edit QQ_EXE in this file to point at your install.
    pause
    exit /b 1
)

echo This will CLOSE QQ and start it again with --force-renderer-accessibility.
echo Unsent drafts in QQ will be lost. Save anything you are typing first.
echo.
pause

echo Closing QQ ...
taskkill /IM QQ.exe /F >nul 2>nul
timeout /t 3 /nobreak >nul

echo Starting QQ with accessibility enabled ...
start "" "%QQ_EXE%" --force-renderer-accessibility
echo.
echo Done. QQ will log in again by itself. Then run:
echo     python probe\probe_qq_uia.py
pause
