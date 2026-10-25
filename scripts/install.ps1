# OhMyLLM installer for Windows (PowerShell)
# Usage: powershell -ExecutionPolicy Bypass -File scripts\install.ps1 [-InstallDir DIR] [-NoDoctor]
param(
    [string]$InstallDir = "$env:LOCALAPPDATA\ohmyllm-app",
    [switch]$NoDoctor
)
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path | Split-Path -Parent
Write-Host "OhMyLLM installer" -ForegroundColor Cyan

function Find-Python {
    foreach ($c in @("py", "python", "python3")) {
        $p = Get-Command $c -ErrorAction SilentlyContinue
        if ($p) {
            $ver = & $c -c "import sys;print('1' if sys.version_info >= (3,11) else '0')" 2>$null
            if ($LASTEXITCODE -eq 0 -and $ver -match "1") { return $c }
        }
    }
    return $null
}

$py = Find-Python
if (-not $py) {
    Write-Host "Python >= 3.11 not found." -ForegroundColor Red
    Write-Host "Install from https://python.org/downloads (check 'Add to PATH'), then re-run."
    exit 1
}
Write-Host "python via: $py"

$venv = Join-Path $InstallDir "venv"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
& $py -m venv $venv
$pyExe = Join-Path $venv "Scripts\python.exe"
& $pyExe -m pip install -q --upgrade pip
& $pyExe -m pip install -q $repo
if ($LASTEXITCODE -ne 0) { Write-Host "pip install failed" -ForegroundColor Red; exit 1 }

$bin = Join-Path $venv "Scripts"
if (-not (Test-Path (Join-Path $bin "ohmyllm.exe"))) {
    Write-Host "ohmyllm.exe missing after install" -ForegroundColor Red
    exit 1
}
foreach ($alias in @("omllm", "OMLLM", "oml", "OML")) {
    $dest = Join-Path $bin "$alias.exe"
    if (-not (Test-Path $dest)) {
        Copy-Item (Join-Path $bin "ohmyllm.exe") $dest
    }
}

# PATH for current user
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$bin*") {
    [Environment]::SetEnvironmentVariable("Path", "$bin;$userPath", "User")
    Write-Host "added $bin to user PATH (new terminals will pick it up)" -ForegroundColor Green
}
$env:Path = "$bin;$env:Path"

& (Join-Path $bin "ohmyllm.exe") --version

if (-not $NoDoctor) {
    Write-Host "running doctor..."
    & (Join-Path $bin "ohmyllm.exe") doctor
}

Write-Host ""
Write-Host "Installed. Commands: ohmyllm | omllm | oml | OMLLM | OML (all work; case-insensitive FS friendly)"
Write-Host "Start with: ohmyllm   (first run opens the wizard)"
