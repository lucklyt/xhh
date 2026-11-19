@echo off
setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "VENV_PY=%SCRIPT_DIR%\.venv\Scripts\python.exe"

if exist "%VENV_PY%" (
    "%VENV_PY%" -m xhh.cli %*
    exit /b %ERRORLEVEL%
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%\xhh.ps1" %*
exit /b %ERRORLEVEL%
