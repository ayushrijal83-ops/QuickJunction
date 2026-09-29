# Quick Junction -- one-shot setup and run (Windows).
#
#   Double-click setup.bat, or:  powershell -ExecutionPolicy Bypass -File setup.ps1
#
# Safe to run every time: each step checks first and only acts when something
# is missing. It installs Python 3.11 and MySQL 8 via winget if absent,
# creates .venv, installs requirements, writes .env, creates the MySQL
# database + qj_user, applies migrations, seeds demo data, and starts the app.
# Admin rights are requested only when an install or service start is needed.

$ErrorActionPreference = 'Continue'   # native stderr must not abort (PS 5.1)
$Root = $PSScriptRoot
Set-Location $Root

function Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Die($m)  { Write-Host "ERROR: $m" -ForegroundColor Red; Read-Host 'Press Enter to exit'; exit 1 }
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                [Environment]::GetEnvironmentVariable('Path', 'User')
}
function Require-Admin($why) {
    $id = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    if ($id.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { return }
    Write-Host "Administrator rights needed to $why -- relaunching elevated..." -ForegroundColor Yellow
    Start-Process powershell -Verb RunAs -ArgumentList "-NoExit -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    exit
}
function Winget-Install($id) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Die "winget is not available. Install '$id' manually, then re-run this script."
    }
    winget install --id $id -e --scope machine --accept-package-agreements --accept-source-agreements
    Refresh-Path
}

# ---------------------------------------------------------------- Python
Step 'Checking Python 3.10/3.11 (numpy 1.24.3 has no wheels for 3.12+)'
function Find-Python {
    foreach ($v in '3.11', '3.10') {
        if (Get-Command py -ErrorAction SilentlyContinue) {
            $p = & py "-$v" -c 'import sys;print(sys.executable)' 2>$null
            if ($LASTEXITCODE -eq 0 -and $p) { return $p.Trim() }
        }
    }
    $candidates = @((Get-Command python -All -ErrorAction SilentlyContinue).Source) +
        (Get-ChildItem "$env:ProgramFiles\Python31[01]\python.exe", "$env:LOCALAPPDATA\Programs\Python\Python31[01]\python.exe" -ErrorAction SilentlyContinue).FullName
    foreach ($p in $candidates) {
        if (-not $p -or $p -like '*WindowsApps*') { continue }   # skip the Store stub
        $ver = & $p -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>$null
        if ($ver -in '3.10', '3.11') { return $p }
    }
    return $null
}
$Py = Find-Python
if (-not $Py) {
    Require-Admin 'install Python 3.11'
    Winget-Install 'Python.Python.3.11'
    $Py = Find-Python
    if (-not $Py) { Die 'Python 3.11 install did not complete. Open a new terminal and re-run.' }
}
Write-Host "Using $Py"

# ---------------------------------------------------------------- venv + requirements
Step 'Preparing virtual environment (.venv)'
$VPy = Join-Path $Root '.venv\Scripts\python.exe'
if (Test-Path $VPy) {
    $ver = & $VPy -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>$null
    if ($LASTEXITCODE -ne 0 -or $ver -notin '3.10', '3.11') {
        Write-Host '.venv is broken or copied from another machine -- recreating.'
        Remove-Item -Recurse -Force (Join-Path $Root '.venv')
    }
}
if (-not (Test-Path $VPy)) {
    & $Py -m venv (Join-Path $Root '.venv')
    if ($LASTEXITCODE -ne 0) { Die 'Could not create .venv' }
}

Step 'Installing requirements'
& $VPy -m pip install --disable-pip-version-check -q --upgrade pip
& $VPy -m pip install --disable-pip-version-check -r requirements.txt
if ($LASTEXITCODE -ne 0) { Die 'pip install -r requirements.txt failed (check internet connection).' }
if (Test-Path (Join-Path $Root 'models\Qwen3-0.6B-Base')) {
    Write-Host 'models/ found -- installing AI stack (large download on first run).'
    & $VPy -m pip install --disable-pip-version-check -r requirements-ai.txt
    if ($LASTEXITCODE -ne 0) { Write-Host 'AI stack failed to install; app will run without AI explanations.' -ForegroundColor Yellow }
} else {
    Write-Host 'models/ not present -- skipping AI stack. App runs; AI explanation shows as unavailable.' -ForegroundColor Yellow
}

# ---------------------------------------------------------------- .env
Step 'Checking .env'
$EnvFile = Join-Path $Root '.env'
if (-not (Test-Path $EnvFile)) { Copy-Item (Join-Path $Root '.env.example') $EnvFile; Write-Host 'Created .env from .env.example' }
$envText = [IO.File]::ReadAllText($EnvFile)
if ($envText -match '(?m)^SECRET_KEY=(replace-with.*|)\s*$') {
    $key = (& $VPy -c 'import secrets;print(secrets.token_urlsafe(64))').Trim()
    $envText = $envText -replace '(?m)^SECRET_KEY=.*$', "SECRET_KEY=$key"
    Write-Host 'Generated SECRET_KEY'
}
if ($envText -match '(?m)^DATABASE_URL=.*CHANGE_ME') {
    $pw = (& $VPy -c 'import secrets;print(secrets.token_hex(16))').Trim()
    $envText = $envText -replace '(?m)^(DATABASE_URL=.*?:)CHANGE_ME@', "`${1}$pw@"
    Write-Host 'Generated database password for qj_user'
}
[IO.File]::WriteAllText($EnvFile, $envText, (New-Object Text.UTF8Encoding $false))

if ($envText -notmatch '(?m)^DATABASE_URL=mysql\+pymysql://([^:]+):([^@]*)@([^:/]+):(\d+)/(\w+)') {
    Die 'DATABASE_URL in .env is not in the form mysql+pymysql://user:pass@host:port/db'
}
$DbUser = $Matches[1]; $DbPass = [uri]::UnescapeDataString($Matches[2])
$DbHost = $Matches[3]; $DbPort = $Matches[4]; $DbName = $Matches[5]
$LocalDb = $DbHost -in '127.0.0.1', 'localhost'

# ---------------------------------------------------------------- MySQL
function Find-Mysql($exe) {
    $c = Get-Command $exe -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    (Get-ChildItem "$env:ProgramFiles\MySQL\MySQL Server *\bin\$exe.exe" -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | Select-Object -First 1).FullName
}
function Get-MysqlService { Get-Service -ErrorAction SilentlyContinue | Where-Object { $_.Name -match '^MySQL\d*$' } | Select-Object -First 1 }

if ($LocalDb) {
    Step 'Checking MySQL server'
    $Mysql = Find-Mysql 'mysql'
    if (-not $Mysql) {
        Require-Admin 'install MySQL Server'
        Winget-Install 'Oracle.MySQL'
        $Mysql = Find-Mysql 'mysql'
        if (-not $Mysql) { Die 'MySQL install did not complete. Install MySQL 8 manually (docs/MYSQL_SETUP_HANDOFF.md section 3) and re-run.' }
    }
    $svc = Get-MysqlService
    if (-not $svc) {
        # Bare MSI install: no data dir, no service. Initialise with an empty root password.
        Require-Admin 'initialise the MySQL service'
        $Mysqld = Find-Mysql 'mysqld'
        Write-Host 'Initialising MySQL data directory (root password empty)...'
        & $Mysqld --initialize-insecure --console
        & $Mysqld --install MySQL80
        $svc = Get-MysqlService
        if (-not $svc) { Die 'Could not register the MySQL service.' }
    }
    if ($svc.Status -ne 'Running') {
        Require-Admin 'start the MySQL service'
        Start-Service $svc.Name
    }
    Write-Host "MySQL service '$($svc.Name)' running; client: $Mysql"

    # Can the app account already log in? If so, nothing to do.
    $env:MYSQL_PWD = $DbPass
    & $Mysql -h $DbHost -P $DbPort -u $DbUser -e 'SELECT 1' $DbName *> $null
    if ($LASTEXITCODE -ne 0) {
        Step "Creating database '$DbName' and user '$DbUser' (needs MySQL root once)"
        $esc = $DbPass.Replace("\", "\\").Replace("'", "\'")
        $sql = "CREATE DATABASE IF NOT EXISTS ``$DbName`` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
        foreach ($h in '127.0.0.1', 'localhost') {
            $sql += "CREATE USER IF NOT EXISTS '$DbUser'@'$h' IDENTIFIED BY '$esc';" +
                    "ALTER USER '$DbUser'@'$h' IDENTIFIED BY '$esc';" +
                    "GRANT ALL PRIVILEGES ON ``$DbName``.* TO '$DbUser'@'$h';"
        }
        $sql += 'FLUSH PRIVILEGES;'
        $env:MYSQL_PWD = ''                           # fresh installs have an empty root password
        $ok = $false
        for ($i = 0; $i -le 3 -and -not $ok; $i++) {
            if ($i -gt 0) {
                $sec = Read-Host 'MySQL root password' -AsSecureString
                $env:MYSQL_PWD = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
                    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
            }
            & $Mysql -h $DbHost -P $DbPort -u root -e $sql *> $null
            $ok = $LASTEXITCODE -eq 0
        }
        if (-not $ok) { Die 'Could not log in as MySQL root. See docs/MYSQL_SETUP_HANDOFF.md sections 4-5.' }
        Write-Host 'Database and user ready.'
    } else {
        Write-Host "Database '$DbName' reachable as '$DbUser'."
    }
    Remove-Item Env:MYSQL_PWD -ErrorAction SilentlyContinue
} else {
    Write-Host "DATABASE_URL points at remote host '$DbHost' -- skipping local MySQL setup." -ForegroundColor Yellow
}

# ---------------------------------------------------------------- migrate + seed
Step 'Applying database migrations'
& $VPy -m flask --app run.py db upgrade
if ($LASTEXITCODE -ne 0) { Die 'Migration failed.' }

Step 'Seeding demo data (idempotent)'
& $VPy scripts\seed_demo.py
if ($LASTEXITCODE -ne 0) { Write-Host 'Seeding failed; continuing.' -ForegroundColor Yellow }

# ---------------------------------------------------------------- run
$Port = if ($envText -match '(?m)^FLASK_RUN_PORT=(\d+)') { $Matches[1] } else { '5000' }
$Url = "http://127.0.0.1:$Port"
Step "Starting Quick Junction at $Url  (Ctrl+C to stop)"
Write-Host 'Demo logins: customer / staff / admin  password: demo-password-1'
Start-Process cmd -ArgumentList "/c timeout /t 5 >nul & start $Url" -WindowStyle Hidden
& $VPy run.py
