<#
.SYNOPSIS
  Restore Google Voice Chrome profiles from a backup .zip.

.DESCRIPTION
  Extracts a profiles_*.zip (produced by backup_profiles.ps1) into the
  project root, recreating every .chrome_profile_* folder. Use this on a
  fresh RDP/server to bring profiles over without re-logging into Google.

  If -ZipPath is omitted, the newest archive in profile_backups\ is used.
  Existing profile folders are overwritten (a confirmation is required
  unless -Force is given).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tools\restore_profiles.ps1
  powershell -ExecutionPolicy Bypass -File tools\restore_profiles.ps1 -ZipPath C:\drop\profiles_20260518_120000.zip -Force
#>
[CmdletBinding()]
param(
    [string] $ZipPath = "",
    [switch] $Force
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot          # project root (D:\update)

if ([string]::IsNullOrWhiteSpace($ZipPath)) {
    $backupDir = Join-Path $root "profile_backups"
    if (-not (Test-Path $backupDir)) {
        Write-Host "No profile_backups\ folder and no -ZipPath given." -ForegroundColor Red
        exit 1
    }
    $latest = Get-ChildItem -Path $backupDir -Filter "profiles_*.zip" |
              Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $latest) {
        Write-Host "No profiles_*.zip found in $backupDir." -ForegroundColor Red
        exit 1
    }
    $ZipPath = $latest.FullName
}

if (-not (Test-Path $ZipPath)) {
    Write-Host "Archive not found: $ZipPath" -ForegroundColor Red
    exit 1
}

Write-Host "Restoring from: $ZipPath" -ForegroundColor Cyan

$existing = Get-ChildItem -Path $root -Directory -Filter ".chrome_profile_*" -ErrorAction SilentlyContinue
if ($existing -and $existing.Count -gt 0 -and -not $Force) {
    Write-Host "$($existing.Count) existing .chrome_profile_* folder(s) will be OVERWRITTEN." -ForegroundColor Yellow
    $answer = Read-Host "Type YES to continue"
    if ($answer -ne "YES") {
        Write-Host "Aborted." -ForegroundColor Yellow
        exit 1
    }
}

$stamp   = Get-Date -Format "yyyyMMdd_HHmmss"
$staging = Join-Path $env:TEMP "gv_profile_restore_$stamp"
New-Item -ItemType Directory -Force -Path $staging | Out-Null

try {
    Expand-Archive -Path $ZipPath -DestinationPath $staging -Force

    $restored = 0
    Get-ChildItem -Path $staging -Directory -Filter ".chrome_profile_*" | ForEach-Object {
        $target = Join-Path $root $_.Name
        if (Test-Path $target) { Remove-Item $target -Recurse -Force }
        Move-Item $_.FullName $target
        Write-Host "Restored $($_.Name)" -ForegroundColor Cyan
        $restored++
    }

    if ($restored -eq 0) {
        Write-Host "Archive contained no .chrome_profile_* folders." -ForegroundColor Red
        exit 1
    }

    Write-Host ""
    Write-Host "Restore complete: $restored profile(s) into $root" -ForegroundColor Green
    Write-Host "Start the bot:  python main.py" -ForegroundColor Green
}
finally {
    if (Test-Path $staging) { Remove-Item $staging -Recurse -Force -ErrorAction SilentlyContinue }
}
