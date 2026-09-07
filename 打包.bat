@echo off
chcp 65001 >nul
cd /d "%~dp0"
title C Drive Cleaner - Build
echo.
echo === C Drive Cleaner - Build ===
echo.

REM ---------- 1. find Python ----------
where py >nul 2>&1
if not errorlevel 1 ( set "PY=py.exe" & goto pyok )
where python >nul 2>&1
if not errorlevel 1 ( set "PY=python.exe" & goto pyok )
echo [ERROR] Python not found. Install Python 3.8+ first.
pause
exit /b 1

:pyok
echo Python: %PY%

REM ---------- 2. PyInstaller ----------
%PY% -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo Installing PyInstaller ...
    %PY% -m pip install --upgrade pyinstaller
    if errorlevel 1 ( echo [FAIL] pip install & pause & exit /b 1 )
) else ( echo PyInstaller OK )

REM ---------- 3. prep temp build dir (avoid Chinese path) ----------
set "TMPBUILD=%TEMP%\cc_build_%RANDOM%"
mkdir "%TMPBUILD%" 2>nul
if errorlevel 1 ( echo [FAIL] mkdir tmp & pause & exit /b 1 )
echo Build workspace: %TMPBUILD%
copy /y "%~dp0cleaner.py" "%TMPBUILD%\" >nul

REM ---------- 4. build in pure-ASCII path ----------
cd /d "%TMPBUILD%"
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist C_Cleaner.spec del /q C_Cleaner.spec 2>nul

echo Building C_Cleaner.exe (1-3 min) ...
%PY% -m PyInstaller --onefile --noconsole --uac-admin --name C_Cleaner --icon "%~dp0app.ico" --clean --noconfirm cleaner.py >"%TMPBUILD%\build.log" 2>&1
if errorlevel 1 goto fail
if not exist "%TMPBUILD%\dist\C_Cleaner.exe" goto fail

REM ---------- 5. copy exe back ----------
if not exist "%~dp0dist" mkdir "%~dp0dist"
copy /y "%TMPBUILD%\dist\C_Cleaner.exe" "%~dp0dist\C_Cleaner.exe" >nul
echo.
echo === BUILD OK ===
echo Output: %~dp0dist\C_Cleaner.exe
dir "%~dp0dist\C_Cleaner.exe" | findstr C_Cleaner.exe

rmdir /s /q "%TMPBUILD%" 2>nul
explorer "%~dp0dist"
pause
exit /b 0

:fail
echo.
echo === BUILD FAILED ===
echo Last 30 lines of build.log:
powershell -NoProfile -Command "Get-Content '%TMPBUILD%\build.log' -Tail 30"
echo Full log: %TMPBUILD%\build.log
pause
exit /b 1
