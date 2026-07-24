$ErrorActionPreference = "Stop"
$ToolsRoot = [IO.Path]::GetFullPath((Split-Path -Parent $MyInvocation.MyCommand.Path))
$Root = [IO.Path]::GetFullPath((Split-Path -Parent $ToolsRoot))
$ReleaseRoot = [IO.Path]::GetFullPath((Join-Path $Root "artifacts\release"))
$Stage = [IO.Path]::GetFullPath((Join-Path $ReleaseRoot "progrok-windows"))
$Zip = Join-Path $ReleaseRoot ("progrok-windows-{0}.zip" -f (Get-Date -Format "yyyyMMdd"))

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

Copy-SelectedFiles (Join-Path $Root "backend") (Join-Path $Stage "backend") @(
    "account_pipeline.py", "accounts.py", "app.py", "config.py", "export_formats.py",
    "chatgpt_browser.py", "chatgpt_build_adapter.py", "chatgpt_session_to_auth.py",
    "grok_build_adapter.py", "hotmail_local.py", "model_health.py", "moemail.py",
    "performance_tuning.py", "proxy_pool.py", "requirements.txt", "sso_to_auth_json.py"
)
Copy-SelectedFiles (Join-Path $Root "web\static") (Join-Path $Stage "web\static") @(
    "app.js", "index.html", "style.css"
)
Copy-SelectedFiles (Join-Path $Root "config") (Join-Path $Stage "config") @(
    ".env.example"
)
Copy-SelectedFiles (Join-Path $Root "tools") (Join-Path $Stage "tools") @(
    "build_release.ps1", "hotmail_helper.py"
)
Copy-SelectedFiles (Join-Path $Root "tests") (Join-Path $Stage "tests") @(
    "test_account_pipeline.py", "test_chatgpt_browser.py", "test_hotmail_local.py",
    "test_performance_tuning.py"
)

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
        if (Select-String -LiteralPath $TextFiles.FullName -SimpleMatch -Pattern $value -Quiet) {
            throw "Release security scan found a value from the local private configuration."
        }
    }
}

if (Test-Path -LiteralPath $Zip) { Remove-Item -LiteralPath $Zip -Force }
Compress-Archive -LiteralPath $Stage -DestinationPath $Zip -CompressionLevel Optimal
Remove-Item -LiteralPath $Stage -Recurse -Force
$Hash = (Get-FileHash -LiteralPath $Zip -Algorithm SHA256).Hash
Write-Host "Release created: $Zip"
Write-Host "SHA256: $Hash"
