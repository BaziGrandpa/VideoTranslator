@echo off
chcp 65001 >nul
setlocal

cd /d "%~dp0"
title Video Translator
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

echo.
echo ========================================
echo        Starting Video Translator
echo ========================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [First run] Installing the runtime. This may take a few minutes...
    powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\setup.ps1"
    if errorlevel 1 goto :failed
)

if not exist ".env" (
    copy /Y ".env.example" ".env" >nul
)

echo Checking for an existing Video Translator process...
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\stop.ps1"
if errorlevel 1 goto :failed

for /L %%I in (1,1,20) do (
    powershell -NoProfile -Command "if (-not (Get-NetTCPConnection -LocalPort 6866 -State Listen -ErrorAction SilentlyContinue)) { exit 0 } else { exit 1 }"
    if not errorlevel 1 goto :port_ready
    powershell -NoProfile -Command "Start-Sleep -Milliseconds 250"
)

echo Port 6866 is still occupied. The process was not started by this project.
goto :failed

:port_ready
echo Workbench URL: http://127.0.0.1:6866
echo Your browser will open when the service is ready.
echo Keep this window open. Close it to stop the service.
echo.

start "" /b powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ".\scripts\open-workbench.ps1"
".venv\Scripts\python.exe" -m app.launcher

if errorlevel 1 goto :failed
exit /b 0

:failed
echo.
echo Startup failed. Review the error above.
pause
exit /b 1
