@echo off
rem ==========================================================================
rem  Quick Junction - RESET DATABASE        *** DESTRUCTIVE ***
rem
rem  Permanently deletes the Quick Junction database named in .env (default
rem  "quick_junction"), recreates it empty, re-grants the application account
rem  and rebuilds every table from the migrations. No other database on the
rem  MySQL server is touched. This is NOT part of normal setup.
rem
rem  It only proceeds after you type exactly:  RESET QUICK JUNCTION
rem ==========================================================================
setlocal EnableExtensions
title Quick Junction - RESET DATABASE (destructive)

if not "%OS%"=="Windows_NT" (
    echo This script must be run on Windows.
    exit /b 1
)
pushd "%~dp0.." || exit /b 1

set "PY=%CD%\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [FAILED] .venv not found. Run setup_quick_junction.bat first.
    set "RC=1"
    goto :end
)

echo.
echo ==============================================================
echo   WARNING: RESET DATABASE - ALL QUICK JUNCTION DATA WILL BE
echo   PERMANENTLY DELETED. Make a backup first if you need it:
echo   docs\DATABASE_SETUP_GUIDE.md, section 19 "Backup / restore".
echo ==============================================================

"%PY%" "%~dp0db_setup.py" reset
if errorlevel 1 (
    set "RC=1"
) else (
    set "RC=0"
)

:end
popd
if not defined QJ_NO_PAUSE pause
exit /b %RC%
