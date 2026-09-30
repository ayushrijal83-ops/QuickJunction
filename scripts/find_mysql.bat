@echo off
rem Internal helper for setup_quick_junction.bat and scripts\setup_database.bat.
rem Looks for MySQL without assuming one install folder. On return:
rem   MYSQL_EXE   full path to mysql.exe (empty if not found)
rem   MYSQL_SVC   name of the MySQL Windows service (empty if none)
rem   MYSQL_STATE Running / Stopped / ... (empty if no service)
rem Exit code 0 = MySQL looks usable, 1 = the user chose to stop.

set "MYSQL_EXE="
set "MYSQL_SVC="
set "MYSQL_STATE="

rem 1) mysql.exe on PATH
for /f "delims=" %%M in ('where mysql.exe 2^>nul') do if not defined MYSQL_EXE set "MYSQL_EXE=%%M"

rem 2) the standard installer location (any 8.x / 9.x folder)
if not defined MYSQL_EXE for /d %%D in ("%ProgramFiles%\MySQL\MySQL Server *") do if exist "%%D\bin\mysql.exe" set "MYSQL_EXE=%%D\bin\mysql.exe"

rem 3) the Windows service (MySQL80, MySQL84, MySQL, ...)
for /f "tokens=1,2" %%A in ('powershell -NoProfile -Command "Get-Service -Name 'MySQL*' -ErrorAction SilentlyContinue | Select-Object -First 1 | ForEach-Object { $_.Name + ' ' + $_.Status }"') do (
    set "MYSQL_SVC=%%A"
    set "MYSQL_STATE=%%B"
)

if defined MYSQL_EXE echo     [OK] MySQL client found: %MYSQL_EXE%
if defined MYSQL_SVC echo     [OK] MySQL service found: %MYSQL_SVC% - %MYSQL_STATE%

if defined MYSQL_EXE goto :check_service
if defined MYSQL_SVC goto :check_service

echo.
echo     MySQL was not found on this computer.
echo     MySQL Server must be installed before Quick Junction can create its database.
echo     Follow docs\DATABASE_SETUP_GUIDE.md, section 4 "Install MySQL Server".
echo.
echo     If MySQL IS installed in another folder, type the full path to mysql.exe,
echo     for example  D:\Tools\MySQL\bin\mysql.exe
set "MYSQL_EXE_IN="
set /p "MYSQL_EXE_IN=    Path to mysql.exe, or press Enter to stop: "
if not defined MYSQL_EXE_IN exit /b 1
if not exist "%MYSQL_EXE_IN%" (
    echo     That file does not exist.
    exit /b 1
)
set "MYSQL_EXE=%MYSQL_EXE_IN%"
echo     [OK] Using %MYSQL_EXE%

:check_service
if not defined MYSQL_SVC (
    echo     NOTE: no MySQL Windows service was found. That is fine if MySQL runs elsewhere
    echo           or was started by hand; the next step tests the connection itself.
    exit /b 0
)
if /i "%MYSQL_STATE%"=="Running" exit /b 0
echo.
echo     The MySQL service "%MYSQL_SVC%" is not running.
echo     Start it: press Win+R, type services.msc, find %MYSQL_SVC%, click Start.
echo     Or in an Administrator terminal:  net start %MYSQL_SVC%
exit /b 1
