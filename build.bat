@echo off
REM =============================================================================
REM D:\程序\C盘清理工具\build.bat
REM -----------------------------------------------------------------------------
REM 用途: 打包 C_Cleaner.exe 的统一入口
REM 流程: 探测 Python ^>^= 3.8 ^>^> 检查/安装 pyinstaller ^>^> 复制源码到 %TEMP%
REM       ^>^> pyinstaller cleaner.spec ^>^> 备份旧 exe ^>^> 复制新 exe ^>^> 打开目录
REM 替代: 原 打包.bat (PyInstaller 命令行参数已沉淀到 cleaner.spec)
REM =============================================================================

setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
title C Drive Cleaner - Build
echo.
echo === C Drive Cleaner - Build ===
echo.

REM -----------------------------------------------------------------------------
REM 1. 探测 Python 3.8+
REM -----------------------------------------------------------------------------
set "PY="
where py >nul 2>&1
if not errorlevel 1 ( set "PY=py.exe" & goto :py_detected )
where python >nul 2>&1
if not errorlevel 1 ( set "PY=python.exe" & goto :py_detected )

echo [ERROR] Python not found. Install Python 3.8+ first.
echo         https://www.python.org/downloads/
pause
exit /b 1

:py_detected
echo Python: %PY%
%PY% -c "import sys; v=sys.version_info; sys.exit(0 if (v.major,v.minor)>=(3,8) else 1)" 2>nul
if errorlevel 1 (
    echo [ERROR] Python 3.8+ required. Current: %PY%
    pause
    exit /b 1
)
%PY% --version

REM -----------------------------------------------------------------------------
REM 2. 检查 PyInstaller (缺失则自动安装)
REM -----------------------------------------------------------------------------
%PY% -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo PyInstaller not found, installing ...
    %PY% -m pip install --upgrade pip >nul 2>&1
    %PY% -m pip install --upgrade pyinstaller
    if errorlevel 1 (
        echo [FAIL] pip install pyinstaller failed.
        echo        Try manually: %PY% -m pip install pyinstaller
        pause
        exit /b 1
    )
) else (
    echo PyInstaller OK
)
%PY% -m PyInstaller --version

REM -----------------------------------------------------------------------------
REM 3. 准备临时构建目录 (规避中文路径导致的 pyinstaller 偶发问题)
REM -----------------------------------------------------------------------------
set "SCRIPT_DIR=%~dp0"
set "TMPBUILD=%TEMP%\cc_build_%RANDOM%"
mkdir "%TMPBUILD%" 2>nul
if errorlevel 1 (
    echo [FAIL] mkdir tmp: %TMPBUILD%
    pause
    exit /b 1
)
echo Build workspace: %TMPBUILD%

REM -----------------------------------------------------------------------------
REM 4. 复制必要文件到临时目录 (cleaner.py + cleaner.spec + app.ico)
REM -----------------------------------------------------------------------------
copy /y "%SCRIPT_DIR%cleaner.py"      "%TMPBUILD%\"      >nul
copy /y "%SCRIPT_DIR%cleaner.spec"    "%TMPBUILD%\"      >nul
copy /y "%SCRIPT_DIR%app.ico"         "%TMPBUILD%\"      >nul
if errorlevel 1 (
    echo [FAIL] copy source files to tmp.
    pause
    exit /b 1
)

REM -----------------------------------------------------------------------------
REM 5. 在纯 ASCII 路径下执行 pyinstaller
REM -----------------------------------------------------------------------------
cd /d "%TMPBUILD%"
if exist build rmdir /s /q build
if exist dist  rmdir /s /q dist
if exist __pycache__ rmdir /s /q __pycache__

echo.
echo Building C_Cleaner.exe (1-3 min) ...
%PY% -m PyInstaller --clean --noconfirm cleaner.spec >"%TMPBUILD%\build.log" 2>&1
if errorlevel 1 goto :fail
if not exist "%TMPBUILD%\dist\C_Cleaner.exe" goto :fail

REM -----------------------------------------------------------------------------
REM 6. 备份现有 exe ^>^> 复制新 exe ^>^> 删除过期的 .old
REM -----------------------------------------------------------------------------
if not exist "%SCRIPT_DIR%dist" mkdir "%SCRIPT_DIR%dist"

REM 6.1 当前存在 C_Cleaner.exe ^>^> 备份为 .prev
if exist "%SCRIPT_DIR%dist\C_Cleaner.exe" (
    if exist "%SCRIPT_DIR%dist\C_Cleaner.exe.prev" (
        REM 已存在 .prev (上上次),直接覆盖
        move /y "%SCRIPT_DIR%dist\C_Cleaner.exe.prev" "%SCRIPT_DIR%dist\C_Cleaner.exe.tmp.prev" >nul
        move /y "%SCRIPT_DIR%dist\C_Cleaner.exe"     "%SCRIPT_DIR%dist\C_Cleaner.exe.prev" >nul
        move /y "%SCRIPT_DIR%dist\C_Cleaner.exe.tmp.prev" "%SCRIPT_DIR%dist\C_Cleaner.exe.prev" >nul
    ) else (
        move /y "%SCRIPT_DIR%dist\C_Cleaner.exe"     "%SCRIPT_DIR%dist\C_Cleaner.exe.prev" >nul
    )
)

REM 6.2 删除过期的 .old (按任务约束: 备份而非删除,只保留 .prev 一份回退点)
if exist "%SCRIPT_DIR%dist\C_Cleaner.exe.old" (
    del /f /q "%SCRIPT_DIR%dist\C_Cleaner.exe.old"
    echo Removed stale C_Cleaner.exe.old
)

REM 6.3 复制新 exe
copy /y "%TMPBUILD%\dist\C_Cleaner.exe" "%SCRIPT_DIR%dist\C_Cleaner.exe" >nul
if errorlevel 1 (
    echo [FAIL] copy new exe to %SCRIPT_DIR%dist\
    pause
    exit /b 1
)

REM -----------------------------------------------------------------------------
REM 7. 验证 + 清理临时目录 + 打开 dist
REM -----------------------------------------------------------------------------
echo.
echo === BUILD OK ===
echo Output: %SCRIPT_DIR%dist\C_Cleaner.exe
dir "%SCRIPT_DIR%dist\C_Cleaner.exe" | findstr C_Cleaner.exe
echo.
echo Backup (回退用): %SCRIPT_DIR%dist\C_Cleaner.exe.prev

rmdir /s /q "%TMPBUILD%" 2>nul
explorer "%SCRIPT_DIR%dist"
pause
exit /b 0

REM -----------------------------------------------------------------------------
REM 失败处理: 输出 build.log 最后 30 行
REM -----------------------------------------------------------------------------
:fail
echo.
echo === BUILD FAILED ===
echo Last 30 lines of build.log:
powershell -NoProfile -Command "Get-Content '%TMPBUILD%\build.log' -Tail 30"
echo Full log: %TMPBUILD%\build.log
pause
exit /b 1
