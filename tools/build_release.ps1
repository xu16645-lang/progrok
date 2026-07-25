param(
    [string]$Version = ""
)

$ErrorActionPreference = "Stop"
$ToolsRoot = [IO.Path]::GetFullPath((Split-Path -Parent $MyInvocation.MyCommand.Path))
$Root = [IO.Path]::GetFullPath((Split-Path -Parent $ToolsRoot))
$ReleaseRoot = [IO.Path]::GetFullPath((Join-Path $Root "artifacts\release"))
$Stage = [IO.Path]::GetFullPath((Join-Path $ReleaseRoot "progrok-windows"))
$ReleaseName = if ($Version.Trim()) { $Version.Trim().TrimStart('v') } else { Get-Date -Format "yyyyMMdd" }
$Zip = Join-Path $ReleaseRoot ("progrok-windows-{0}.zip" -f $ReleaseName)

function Assert-ChildPath([string]$Path, [string]$Parent) {
    $prefix = $Parent.TrimEnd('\') + '\'
    if (-not $Path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe release path: $Path"
    }
}

function Copy-SelectedFiles([string]$SourceRoot, [string]$DestinationRoot, [string[]]$Names) {
    New-Item -ItemType Directory -Force -Path $DestinationRoot | Out-Null
    foreach ($name in $Names) {
        $source = Join-Path $SourceRoot $name
        if (-not (Test-Path -LiteralPath $source)) { throw "Missing release file: $source" }
        Copy-Item -LiteralPath $source -Destination (Join-Path $DestinationRoot $name)
    }
}

function Copy-SourceTree([string]$SourceRoot, [string]$DestinationRoot, [string[]]$Extensions) {
    if (-not (Test-Path -LiteralPath $SourceRoot)) { throw "Missing source directory: $SourceRoot" }
    $files = Get-ChildItem -LiteralPath $SourceRoot -Recurse -File | Where-Object {
        $_.FullName -notmatch '(?i)\\(__pycache__|\.pytest_cache|\.ruff_cache|node_modules|\.venv|runtime|output|logs)\\' -and
        ($Extensions.Count -eq 0 -or $Extensions -contains $_.Extension.ToLowerInvariant())
    }
    foreach ($file in $files) {
        $relative = [IO.Path]::GetRelativePath($SourceRoot, $file.FullName)
        $destination = Join-Path $DestinationRoot $relative
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
        Copy-Item -LiteralPath $file.FullName -Destination $destination
    }
}

Assert-ChildPath $Stage $ReleaseRoot
New-Item -ItemType Directory -Force -Path $ReleaseRoot | Out-Null
if (Test-Path -LiteralPath $Stage) {
    Remove-Item -LiteralPath $Stage -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $Stage | Out-Null

Copy-SelectedFiles $Root $Stage @(
    ".gitignore", "README.md", "install_and_start.cmd", "install_and_start.ps1",
    "start.cmd", "start.ps1", "stop.cmd", "stop.ps1"
)

Copy-SourceTree (Join-Path $Root "backend") (Join-Path $Stage "backend") @(".py", ".txt")
Copy-SourceTree (Join-Path $Root "web") (Join-Path $Stage "web") @(".js", ".html", ".css", ".svg")
Copy-SelectedFiles (Join-Path $Root "config") (Join-Path $Stage "config") @(
    ".env.example"
)
Copy-SourceTree (Join-Path $Root "tools") (Join-Path $Stage "tools") @(".py", ".ps1")
Copy-SourceTree (Join-Path $Root "tests") (Join-Path $Stage "tests") @(".py")

$VendorFiles = [ordered]@{
    "turnstile-solver" = @("api_solver.py", "browser_configs.py", "db_results.py", "requirements.txt")
    "grok-build-auth" = @("LICENSE", "NOTICE", "requirements.txt", "run.py")
    "grok-build-auth\alias_mail" = @("alias_mail.py")
    "grok-build-auth\xconsole_client" = @(
        "__init__.py", "client.py", "config.py", "fingerprint.py", "grpcweb.py",
        "mailbox.py", "models.py", "oauth_protocol.py", "solver.py", "sso.py",
        "tempmail_transport.py", "xai_oauth.py"
    )
}
foreach ($entry in $VendorFiles.GetEnumerator()) {
    $source = Join-Path (Join-Path $Root "vendor") $entry.Key
    $destination = Join-Path (Join-Path $Stage "vendor") $entry.Key
    Copy-SelectedFiles $source $destination $entry.Value
}

$ForbiddenFiles = @(Get-ChildItem -LiteralPath $Stage -File -Recurse | Where-Object {
    $relativePath = $_.FullName.Substring($Stage.Length)
    $_.Name -match '(?i)^(config\.json|\.env)$' -or
    $_.Name -match '(?i)(credential|secret)' -or
    $relativePath -match '(?i)\\(runtime|artifacts|logs|output|\.venv|__pycache__)\\'
})
if ($ForbiddenFiles.Count -gt 0) {
    throw "Release contains forbidden local files."
}

$TextFiles = @(Get-ChildItem -LiteralPath $Stage -File -Recurse | Where-Object {
    $_.Extension -in @('.py', '.js', '.html', '.css', '.md', '.ps1', '.cmd', '.txt', '.json', '.example')
})
$LocalConfig = Join-Path $Root "config\config.json"
if (Test-Path -LiteralPath $LocalConfig) {
    $SensitiveKeys = @(
        "mail_api_key", "mail_base_url", "mail_domain", "yescaptcha_key",
        "proxy", "proxy_username", "proxy_password", "cpa_base_url",
        "cpa_management_key", "sub2api_base_url", "sub2api_admin_email",
        "sub2api_admin_password", "sub2api_api_key"
    )
    $LocalValues = Get-Content -LiteralPath $LocalConfig -Raw | ConvertFrom-Json
    foreach ($key in $SensitiveKeys) {
        $value = [string]$LocalValues.$key
        if (-not $value -or $value.Length -lt 5) { continue }
        if ($key -eq "mail_base_url" -and $value -eq "https://maliapi.215.im") { continue }
        if ($key -eq "mail_domain" -and -not $value.Trim()) { continue }
        if ($key -eq "proxy" -and $value -match '(?i)(127\.0\.0\.1|localhost)') { continue }
        if (Select-String -LiteralPath $TextFiles.FullName -SimpleMatch -Pattern $value -Quiet) {
            throw "Release security scan found a value from local private configuration key: $key"
        }
    }
}

if (Test-Path -LiteralPath $Zip) { Remove-Item -LiteralPath $Zip -Force }
Compress-Archive -LiteralPath $Stage -DestinationPath $Zip -CompressionLevel Optimal
Remove-Item -LiteralPath $Stage -Recurse -Force
$Hash = (Get-FileHash -LiteralPath $Zip -Algorithm SHA256).Hash
Write-Host "Release created: $Zip"
Write-Host "SHA256: $Hash"
