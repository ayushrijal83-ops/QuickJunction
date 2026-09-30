@echo off
rem ==========================================================================
rem  Quick Junction - database setup (NON-DESTRUCTIVE, safe to run again)
rem
rem  Creates the MySQL database "quick_junction" and the application account
rem  "qj_user" if they do not exist, writes DATABASE_URL to .env, runs every
rem  migration and verifies the result. It never drops or empties anything.
rem
rem  Needs: MySQL Server installed and running, and the .venv created by
rem  setup_quick_junction.bat. Guide: docs\DATABASE_SETUP_GUIDE.md
rem ==========================================================================
setlocal EnableExtensions
title Quick Junction - database setup

if not "%OS%"=="Windows_NT" (
    echo This script must be run on Windows.
    exit /b 1
)

pushd "%~dp0.." || (
    echo Could not open the Quick Junction folder.
    exit /b 1
)

echo.
echo ==========================================================
echo   Quick Junction - database setup  (nothing is deleted)
echo ==========================================================

echo.
echo ==^> Checking Python environment
set "PY=%CD%\.venv\Scripts\python.exe"
if not exist "%PY%" goto :no_venv
"%PY%" -c "import pymysql, flask_migrate, alembic" >nul 2>&1
if errorlevel 1 goto :no_deps
for /f "delims=" %%V in ('"%PY%" --version') do echo     [OK] %%V in .venv

echo.
echo ==^> Checking MySQL
call "%~dp0find_mysql.bat"
if errorlevel 1 goto :fail

"%PY%" "%~dp0db_setup.py" setup
if errorlevel 1 goto :fail

echo.
echo ==========================================================
echo   Database setup complete.
echo   Start Quick Junction with:   .venv\Scripts\python run.py
echo   Then open:                   http://127.0.0.1:5000
echo ==========================================================
set "RC=0"
goto :end

:no_venv
echo     [FAILED] The Python virtual environment (.venv) was not found.
echo     Run setup_quick_junction.bat first - it creates .venv and installs
echo     the requirements, then calls this script for you.
goto :fail

:no_deps
echo     [FAILED] .venv exists but the required packages are missing.
echo     Run:  .venv\Scripts\python -m pip install -r requirements.txt
goto :fail

:fail
echo.
echo   Database setup did NOT complete. Read the message above.
echo   Help: docs\DATABASE_SETUP_GUIDE.md, section 15 "Troubleshooting".
set "RC=1"

:end
popd
if not defined QJ_NO_PAUSE pause
exit /b %RC%
