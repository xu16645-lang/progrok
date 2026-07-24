$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

trap {
    Write-Host ""
    Write-Host "Installation or startup failed: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

function Test-Python([string]$Path) {
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return $false }
    try {
        $version = & $Path -c "import sys; print(sys.version_info.major * 100 + sys.version_info.minor)" 2>$null
        return $LASTEXITCODE -eq 0 -and [int]$version -ge 310
    } catch {
        return $false
    }
}

function Find-Python {
    if (Test-Python $env:PROGROK_PYTHON) { return $env:PROGROK_PYTHON }
    foreach ($name in @("python", "python3")) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command -and (Test-Python $command.Source)) { return $command.Source }
    }
    $launcher = Get-Command "py" -ErrorAction SilentlyContinue
    if ($launcher) {
        foreach ($minor in @("3.14", "3.13", "3.12", "3.11", "3.10")) {
            try {
                $path = & $launcher.Source "-$minor" -c "import sys; print(sys.executable)" 2>$null
                if ($LASTEXITCODE -eq 0 -and (Test-Python $path.Trim())) { return $path.Trim() }
            } catch {}
        }
    }
    foreach ($path in @(
        "$env:LocalAppData\Programs\Python\Python312\python.exe",
        "$env:ProgramFiles\Python312\python.exe"
    )) {
        if (Test-Python $path) { return $path }
    }
    return $null
}

$Python = Find-Python
if (-not $Python) {
    Write-Host "Python 3.10+ was not found. Installing Python 3.12..."
    $winget = Get-Command "winget" -ErrorAction SilentlyContinue
    if ($winget) {
        & $winget.Source install --id Python.Python.3.12 --exact --scope user --silent --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "winget installation failed. Trying the official Python installer."
        }
    }
    $Python = Find-Python
    if (-not $Python) {
        $Installer = Join-Path $env:TEMP "python-3.12.10-amd64.exe"
        $Url = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
        Write-Host "Downloading the installer from python.org..."
        Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $Installer
        $process = Start-Process -FilePath $Installer -ArgumentList @(
            "/quiet", "InstallAllUsers=0", "PrependPath=1", "Include_pip=1",
            "Include_launcher=1", "Include_test=0", "Shortcuts=0"
        ) -Wait -PassThru
        if ($process.ExitCode -ne 0) {
            throw "Python installation failed with exit code $($process.ExitCode)."
        }
        $Python = Find-Python
    }
}

if (-not $Python) {
    throw "Python was installed but could not be located. Reopen the terminal and run this script again."
}

$env:PROGROK_PYTHON = $Python
Write-Host "Using Python: $Python"
& (Join-Path $Root "start.ps1")
if (-not $?) { throw "start.ps1 failed." }
Write-Host ""
Write-Host "Startup completed. Open http://127.0.0.1:3080"
