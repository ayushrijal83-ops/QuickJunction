@echo off
rem ==========================================================================
rem  Quick Junction - Windows setup
rem
rem  BEFORE running this, install Python 3.11 (or 3.10) and MySQL Server 8.0
rem  as described in docs\DATABASE_SETUP_GUIDE.md, sections 3 and 4.
rem
rem  This script then: checks Python and MySQL, creates .venv, installs
rem  requirements.txt, creates the quick_junction database and the qj_user
rem  account, writes .env, runs every migration, verifies the migration head
rem  and runs a start-up smoke test. It does NOT start the application and it
rem  never deletes data. Safe to run again.
rem ==========================================================================
setlocal EnableExtensions
title Quick Junction - setup
pushd "%~dp0" || exit /b 1

echo.
echo ==========================================================
echo   Welcome to Quick Junction setup
echo   Guide: docs\DATABASE_SETUP_GUIDE.md
echo ==========================================================

if not "%OS%"=="Windows_NT" (
    echo This script must be run on Windows.
    goto :fail
)

if not exist "requirements.txt" goto :not_project
if not exist "run.py" goto :not_project
if not exist "migrations\versions" goto :not_project

rem ---------------------------------------------------------------- Python
echo.
echo ==^> Checking Python 3.10 or 3.11
rem numpy 1.24.3, scipy 1.15.3 and scikit-learn 1.7.2 in requirements.txt
rem need Python 3.10 or 3.11 (no numpy 1.24 wheels exist for 3.12+).
set "SYSPY="
where py >nul 2>&1
if not errorlevel 1 (
    py -3.11 -c "import sys" >nul 2>&1 && set "SYSPY=py -3.11"
    if not defined SYSPY py -3.10 -c "import sys" >nul 2>&1 && set "SYSPY=py -3.10"
)
if not defined SYSPY (
    python -c "import sys; sys.exit(0 if sys.version_info[:2] in ((3, 10), (3, 11)) else 1)" >nul 2>&1 && set "SYSPY=python"
)
if not defined SYSPY goto :no_python
for /f "delims=" %%V in ('%SYSPY% --version') do echo     [OK] %%V  (%SYSPY%)

rem ---------------------------------------------------------------- MySQL
echo.
echo ==^> Checking MySQL
call "scripts\find_mysql.bat"
if errorlevel 1 goto :fail

rem ---------------------------------------------------------------- venv
echo.
echo ==^> Preparing the virtual environment (.venv)
set "PY=%CD%\.venv\Scripts\python.exe"
if exist "%PY%" (
    "%PY%" -c "import sys; sys.exit(0 if sys.version_info[:2] in ((3, 10), (3, 11)) else 1)" >nul 2>&1
    if errorlevel 1 goto :bad_venv
    echo     [OK] .venv already exists
) else (
    %SYSPY% -m venv .venv
    if errorlevel 1 goto :fail
    echo     [OK] .venv created
)

echo.
echo ==^> Installing requirements.txt  (first run downloads about 100 MB)
"%PY%" -m pip install --disable-pip-version-check --quiet --upgrade pip
"%PY%" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
    echo     [FAILED] pip could not install requirements.txt. Check the internet connection.
    goto :fail
)
echo     [OK] Requirements installed
echo     NOTE: the optional AI explanation packages (requirements-ai.txt) and the
echo           models\ folder are not installed by this script. Quick Junction runs
echo           without them; the AI explanation then shows as unavailable.

rem ---------------------------------------------------------------- database
set "QJ_NO_PAUSE=1"
"%PY%" "scripts\db_setup.py" setup
if errorlevel 1 goto :fail

echo.
echo ==========================================================
echo   Quick Junction is ready.
echo.
echo   Start it with:   .venv\Scripts\python run.py
echo   Then open:       http://127.0.0.1:5000
echo   Stop it with:    Ctrl+C in that window
echo ==========================================================
set "RC=0"
goto :end

:not_project
echo     [FAILED] Run this file from the Quick Junction folder (where run.py is).
goto :fail

:no_python
echo     [FAILED] Python 3.11 or 3.10 was not found.
echo     Install Python 3.11 from https://www.python.org/downloads/windows/
echo     and tick "Add python.exe to PATH". Then open a NEW terminal and run
echo     this script again. See docs\DATABASE_SETUP_GUIDE.md, section 3.
goto :fail

:bad_venv
echo     [FAILED] .venv was made with a Python version other than 3.10/3.11,
echo     or was copied from another computer. Delete the .venv folder and run
echo     this script again - it will be recreated.
goto :fail

:fail
echo.
echo   Setup did NOT complete. Read the message above.
echo   Help: docs\DATABASE_SETUP_GUIDE.md, section 15 "Troubleshooting".
set "RC=1"

:end
popd
pause
exit /b %RC%
