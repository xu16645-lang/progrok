$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$BackendRoot = Join-Path $Root "backend"
$RuntimeRoot = Join-Path $Root "runtime"
$VendorRoot = Join-Path $Root "vendor"
$PidDir = Join-Path $RuntimeRoot "pids"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

function Resolve-BasePython {
    if ($env:PROGROK_PYTHON -and (Test-Path -LiteralPath $env:PROGROK_PYTHON)) {
        return $env:PROGROK_PYTHON
    }
    foreach ($candidate in @("python", "python3")) {
        $command = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($command) {
            try {
                $version = & $command.Source -c "import sys; print(sys.version_info.major * 100 + sys.version_info.minor)"
                if ([int]$version -ge 310) { return $command.Source }
            } catch {}
        }
    }
    $launcher = Get-Command "py" -ErrorAction SilentlyContinue
    if ($launcher) {
        foreach ($minor in @("3.13", "3.12", "3.11", "3.10")) {
            try {
                $resolved = & $launcher.Source "-$minor" -c "import sys; print(sys.executable)" 2>$null
                if ($LASTEXITCODE -eq 0 -and $resolved -and (Test-Path -LiteralPath $resolved.Trim())) {
                    return $resolved.Trim()
                }
            } catch {}
        }
    }
    throw "Python 3.10 or newer was not found. Run install_and_start.cmd first."
}

function Ensure-Venv([string]$Path, [string]$Requirements, [string]$BasePython) {
    $Python = Join-Path $Path "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $Python)) {
        & $BasePython -m venv $Path | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "Failed to create venv: $Path" }
    }
    & $Python -m pip install -q --disable-pip-version-check -r $Requirements | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Failed to install: $Requirements" }
    return $Python
}

function Resolve-SolverThreads {
    $configured = 0
    if ([int]::TryParse($env:PROGROK_SOLVER_THREADS, [ref]$configured) -and $configured -ge 1) {
        return [Math]::Min(10, $configured)
    }
    try {
        $physicalCores = (Get-CimInstance Win32_Processor | Measure-Object NumberOfCores -Sum).Sum
        $freeMemoryGB = (Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory / 1MB
        $cpuCap = [Math]::Max(1, [Math]::Floor([double]$physicalCores * 0.75))
        $memoryCap = [Math]::Max(1, [Math]::Floor([double]$freeMemoryGB / 0.6))
        return [int](($cpuCap, $memoryCap, 8 | Measure-Object -Minimum).Minimum)
    } catch {
        return 3
    }
}

$BasePython = Resolve-BasePython
$SolverThreads = Resolve-SolverThreads
$env:GROK2API_LOCAL_CAPTCHA_CONCURRENCY = "$SolverThreads"
if (-not $env:GROK2API_REG_GLOBAL_INFLIGHT) {
    $env:GROK2API_REG_GLOBAL_INFLIGHT = "$SolverThreads"
}
Write-Host "Recommended registration capacity: $SolverThreads"
$MainPython = Ensure-Venv (Join-Path $RuntimeRoot ".venv") (Join-Path $BackendRoot "requirements.txt") $BasePython
& $MainPython -m patchright install chromium | Out-Host
if ($LASTEXITCODE -ne 0) { throw "Failed to install the ChatGPT browser runtime." }
& $MainPython -m camoufox fetch | Out-Host
if ($LASTEXITCODE -ne 0) { throw "Failed to install the Camoufox browser runtime." }
$SolverRoot = Join-Path $VendorRoot "turnstile-solver"
$SolverPython = Ensure-Venv (Join-Path $SolverRoot ".venv") (Join-Path $SolverRoot "requirements.txt") $BasePython
New-Item -ItemType Directory -Force -Path $PidDir | Out-Null
$LogDir = Join-Path $RuntimeRoot "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$HotmailHelperUp = $false
try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:17373/health" -TimeoutSec 2
    $HotmailHelperUp = $health.service -eq "hotmail-helper"
} catch {}

if (-not $HotmailHelperUp) {
    $HotmailHelperScript = Join-Path $Root "tools\hotmail_helper.py"
    if (-not (Test-Path -LiteralPath $HotmailHelperScript)) {
        throw "Hotmail helper script was not found: $HotmailHelperScript"
    }
    $hotmailHelper = Start-Process -FilePath $MainPython -ArgumentList @(
        $HotmailHelperScript, "--host", "127.0.0.1", "--port", "17373"
    ) -WorkingDirectory $Root -WindowStyle Hidden -RedirectStandardOutput (Join-Path $LogDir "hotmail-helper.out.log") -RedirectStandardError (Join-Path $LogDir "hotmail-helper.err.log") -PassThru
    $hotmailHelper.Id | Set-Content -LiteralPath (Join-Path $PidDir "hotmail-helper.pid") -Encoding ascii
    $HotmailHelperReady = $false
    foreach ($attempt in 1..15) {
        Start-Sleep -Milliseconds 300
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:17373/health" -TimeoutSec 1
            if ($health.service -eq "hotmail-helper") {
                $HotmailHelperReady = $true
                break
            }
        } catch {}
        if ($hotmailHelper.HasExited) { break }
    }
    if (-not $HotmailHelperReady) {
        throw "Hotmail Helper failed to become ready. Check runtime\logs\hotmail-helper.err.log"
    }
    Write-Host "Hotmail Helper started: PID=$($hotmailHelper.Id), http://127.0.0.1:17373"
} else {
    Write-Host "Hotmail Helper already available: http://127.0.0.1:17373"
}

$SolverUp = $false
try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:5072/health" -TimeoutSec 2
    $SolverUp = $null -ne $health
} catch {}

if (-not $SolverUp) {
    # Eager Camoufox prewarm + page reuse (override with env if needed).
    if (-not $env:TURNSTILE_LAZY) { $env:TURNSTILE_LAZY = "0" }
    if (-not $env:TURNSTILE_REUSE_PAGE) { $env:TURNSTILE_REUSE_PAGE = "1" }
    if (-not $env:TURNSTILE_IDLE_SEC) { $env:TURNSTILE_IDLE_SEC = "600" }
    & $SolverPython -m camoufox fetch
    $solver = Start-Process -FilePath $SolverPython -ArgumentList @(
        "api_solver.py", "--browser_type", "camoufox", "--thread", "$SolverThreads",
        "--proxy", "--host", "127.0.0.1", "--port", "5072"
    ) -WorkingDirectory $SolverRoot -WindowStyle Hidden -PassThru
    $solver.Id | Set-Content -LiteralPath (Join-Path $PidDir "solver.pid") -Encoding ascii
    Write-Host "Turnstile Solver started: PID=$($solver.Id), port=5072 (prewarm, reuse_page, idle=$($env:TURNSTILE_IDLE_SEC)s)"
} else {
    Write-Host "Turnstile Solver already available on port 5072"
}

$AppUp = $false
try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:3080/api/health" -TimeoutSec 2
    $AppUp = $health.service -eq "progrok-registration"
} catch {}

if (-not $AppUp) {
    $app = Start-Process -FilePath $MainPython -ArgumentList @(
        "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "3080", "--workers", "1"
    ) -WorkingDirectory $BackendRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $LogDir "app.out.log") -RedirectStandardError (Join-Path $LogDir "app.err.log") -PassThru
    $app.Id | Set-Content -LiteralPath (Join-Path $PidDir "app.pid") -Encoding ascii
    Write-Host "ProGrok started: PID=$($app.Id), http://127.0.0.1:3080"
} else {
    Write-Host "ProGrok already available: http://127.0.0.1:3080"
}
