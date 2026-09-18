@echo off
chcp 65001 >nul
setlocal

cd /d "%~dp0"
title Stop Video Translator

echo.
echo ========================================
echo        Stopping Video Translator
echo ========================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\stop.ps1" %*

if errorlevel 1 goto :failed
ping -n 3 127.0.0.1 >nul
exit /b 0

:failed
echo.
echo Stop failed. Review the error above.
pause
exit /b 1
