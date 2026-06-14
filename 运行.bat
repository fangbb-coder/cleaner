@echo off
chcp 65001 >nul
cd /d "%~dp0"
title C Drive Cleaner - Launcher
echo.
echo === C Drive Cleaner ===
echo.

REM ---------- 1. find Python ----------
where py >nul 2>&1
if not errorlevel 1 ( set "PY=py.exe" & goto pyok )
where python >nul 2>&1
if not errorlevel 1 ( set "PY=python.exe" & goto pyok )
echo [ERROR] Python not found. Install Python 3.8+ from https://www.python.org/
pause
exit /b 1

:pyok
echo Python: %PY%
echo Requesting admin via UAC ...
echo.

REM ---------- 2. launch with admin (use full path) ----------
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $p = Start-Process -FilePath '%PY%' -ArgumentList '\"%~dp0cleaner.py\"' -WorkingDirectory '%~dp0' -Verb RunAs -WindowStyle Normal -PassThru -ErrorAction Stop; if ($p) { $p.WaitForExit() } else { Write-Host '[FAIL]' -ForegroundColor Red } } catch { Write-Host ('[FAIL] ' + $_.Exception.Message) -ForegroundColor Red; Write-Host 'TIP: right-click this .bat and Run as administrator' -ForegroundColor Yellow }"

echo.
timeout /t 3 /nobreak >nul
exit
