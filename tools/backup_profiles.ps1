<#
.SYNOPSIS
  Archive all Google Voice Chrome profiles into one portable .zip.

.DESCRIPTION
  Copies every .chrome_profile_* folder into a staging area, drops volatile
  cache/lock files that are NOT needed for the logged-in GV session, and
  compresses the result into profile_backups\profiles_<timestamp>.zip.

  Run this ONCE after you have logged every profile into Google Voice
  (via: python login_helper.py). Move the resulting .zip to a new server
  and run restore_profiles.ps1 there — no re-login required as long as the
  Google session stays valid.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tools\backup_profiles.ps1
#>
[CmdletBinding()]
param(
    [string] $OutDir = ""
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot          # project root (D:\update)
if ([string]::IsNullOrWhiteSpace($OutDir)) {
    $OutDir = Join-Path $root "profile_backups"
}
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$profiles = Get-ChildItem -Path $root -Directory -Filter ".chrome_profile_*"
if (-not $profiles -or $profiles.Count -eq 0) {
    Write-Host "No .chrome_profile_* folders found in $root — nothing to back up." -ForegroundColor Yellow
    exit 1
}

$stamp   = Get-Date -Format "yyyyMMdd_HHmmss"
$staging = Join-Path $env:TEMP "gv_profile_backup_$stamp"
New-Item -ItemType Directory -Force -Path $staging | Out-Null

# Volatile data that bloats the archive and is not needed for auth.
$excludeDirs  = @("Cache", "Code Cache", "GPUCache", "ShaderCache", "GrShaderCache", "Crashpad", "component_crx_cache")
$excludeFiles = @("SingletonLock", "SingletonCookie", "SingletonSocket", "DevToolsActivePort")

try {
    foreach ($p in $profiles) {
        Write-Host "Staging $($p.Name) ..." -ForegroundColor Cyan
        $dest = Join-Path $staging $p.Name
        robocopy $p.FullName $dest /MIR /XD $excludeDirs /XF $excludeFiles /NFL /NDL /NJH /NJS /NP /R:1 /W:1 | Out-Null
    }

    $zipPath = Join-Path $OutDir "profiles_$stamp.zip"
    if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
    Write-Host "Compressing -> $zipPath" -ForegroundColor Cyan
    Compress-Archive -Path (Join-Path $staging "*") -DestinationPath $zipPath -CompressionLevel Optimal

    $sizeMB = [math]::Round((Get-Item $zipPath).Length / 1MB, 1)
    Write-Host ""
    Write-Host "Backup complete: $zipPath ($sizeMB MB, $($profiles.Count) profiles)" -ForegroundColor Green
}
finally {
    if (Test-Path $staging) { Remove-Item $staging -Recurse -Force -ErrorAction SilentlyContinue }
}
