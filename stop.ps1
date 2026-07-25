$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$LegacyAppRoot = Join-Path $Root "app"
$PidDir = Join-Path (Join-Path $Root "runtime") "pids"
$LegacyPidDir = Join-Path (Join-Path $LegacyAppRoot "runtime") "pids"

function Stop-ProcessTree([int]$ProcessId) {
    $children = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.ParentProcessId -eq $ProcessId } |
        Select-Object -ExpandProperty ProcessId)
    foreach ($child in $children) {
        Stop-ProcessTree ([int]$child)
    }
    Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
}

foreach ($location in @(
    @{ Base = $PidDir; Names = @("app.pid", "main.pid", "solver.pid", "hotmail-helper.pid", "startup.pid") },
    @{ Base = $LegacyPidDir; Names = @("app.pid", "main.pid", "solver.pid", "hotmail-helper.pid", "startup.pid") },
    @{ Base = $LegacyAppRoot; Names = @(".app.pid", ".main.pid", ".solver.pid", ".hotmail-helper.pid", ".startup.pid") },
    @{ Base = $Root; Names = @(".app.pid", ".main.pid", ".solver.pid", ".hotmail-helper.pid", ".startup.pid") }
)) {
    foreach ($name in $location.Names) {
        $path = Join-Path $location.Base $name
        if (-not (Test-Path -LiteralPath $path)) { continue }
        $raw = (Get-Content -LiteralPath $path -Raw).Trim()
        if ($raw -match '^\d+$') {
            Stop-ProcessTree ([int]$raw)
        }
        Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    }
}

# Camoufox can detach from the Python launcher, so process-tree cleanup alone
# leaves registration and solver browsers running after the project stops.
$CamoufoxRoot = Join-Path $env:LOCALAPPDATA "camoufox"
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {
        $_.Name -eq "camoufox.exe" -and
        $_.ExecutablePath -and
        $_.ExecutablePath.StartsWith($CamoufoxRoot, [StringComparison]::OrdinalIgnoreCase)
    } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }

Write-Host "Stopped ProGrok background processes recorded by this project."
