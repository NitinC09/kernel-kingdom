# kernel-kingdom installer for Windows (PowerShell 5.1+).
#   irm https://raw.githubusercontent.com/rsmedrano-cloud/kernel-kingdom/main/install.ps1 | iex
# GTK 4 + PyGObject on Windows come from MSYS2 (UCRT64), the way gtk.org recommends.
# Installs into %LOCALAPPDATA%\kernel-kingdom\venv, puts a `kernel-kingdom` command on your PATH,
# a "Kernel Kingdom" entry in the Start menu, and starts the world and the wallpaper (behind your
# desktop icons) with Windows (Startup folder).
# Options (env vars): KERNEL_KINGDOM_REPO=<github url>  KERNEL_KINGDOM_NO_SERVICE=1
#                     KERNEL_KINGDOM_YES=1 (install missing packages without asking)
#                     MSYS2_ROOT=<path> (default C:\msys64)
# 'Continue': PowerShell 5.1 turns any stderr line of a native command into an error under 'Stop'.
# Failures are checked with $LASTEXITCODE instead.
$ErrorActionPreference = 'Continue'

$Repo   = if ($env:KERNEL_KINGDOM_REPO) { $env:KERNEL_KINGDOM_REPO } else { 'https://github.com/rsmedrano-cloud/kernel-kingdom' }
$Prefix = Join-Path $env:LOCALAPPDATA 'kernel-kingdom'
$Msys   = if ($env:MSYS2_ROOT) { $env:MSYS2_ROOT } else { 'C:\msys64' }
$Ucrt   = Join-Path $Msys 'ucrt64\bin'
$Py     = Join-Path $Ucrt 'python.exe'

function Say($m) { Write-Host "==> $m" -ForegroundColor Yellow }
function Die($m) { Write-Host "xx $m" -ForegroundColor Red; throw "kernel-kingdom install: $m" }  # not `exit`: under `irm | iex` it would close the terminal
function Ask($q) {
  if ($env:KERNEL_KINGDOM_YES) { return $true }
  $r = Read-Host "==> $q [Y/n]"
  return -not ($r -match '^[nN]')
}

# -- MSYS2 -------------------------------------------------------------------------------
if (-not (Test-Path (Join-Path $Msys 'usr\bin\bash.exe'))) {
  Say "Kernel Kingdom needs MSYS2 (it provides GTK 4 and PyGObject for Windows)."
  if ((Get-Command winget -ErrorAction SilentlyContinue) -and (Ask "Install MSYS2 with winget now?")) {
    winget install -e --id MSYS2.MSYS2 --accept-source-agreements --accept-package-agreements
    if (-not (Test-Path (Join-Path $Msys 'usr\bin\bash.exe'))) { Die "MSYS2 isn't at $Msys. Set MSYS2_ROOT and run this again." }
  } else {
    Die "Install MSYS2 from https://www.msys2.org (default folder C:\msys64), then run this installer again."
  }
}

$Pkgs = 'mingw-w64-ucrt-x86_64-python mingw-w64-ucrt-x86_64-python-gobject mingw-w64-ucrt-x86_64-python-cairo ' +
        'mingw-w64-ucrt-x86_64-gtk4 mingw-w64-ucrt-x86_64-python-psutil mingw-w64-ucrt-x86_64-python-pip ' +
        'mingw-w64-ucrt-x86_64-python-setuptools mingw-w64-ucrt-x86_64-python-wheel'

function Test-Core {
  if (-not (Test-Path $Py)) { return $false }
  $env:PATH = "$Ucrt;$env:PATH"
  & $Py -c "import sys; assert sys.version_info >= (3, 11); import venv, psutil, cairo, gi; gi.require_version('Gtk', '4.0'); from gi.repository import Gtk" 2>$null
  return $LASTEXITCODE -eq 0
}

if (-not (Test-Core)) {
  Say "Missing Python / GTK 4 / PyGObject in MSYS2 UCRT64. Command:"
  Write-Host "    pacman -S --needed --noconfirm $Pkgs"
  if (-not (Ask "Install them now?")) { Die "Run the command above in the MSYS2 UCRT64 terminal, then this installer again." }
  & (Join-Path $Msys 'usr\bin\bash.exe') -lc "pacman -S --needed --noconfirm $Pkgs"
  if (-not (Test-Core)) { Die "Still missing a dependency after installing. Please open an issue with your Windows version." }
}

# -- venv + package ----------------------------------------------------------------------
Say "Creating virtualenv in $Prefix\venv"
& $Py -m venv --system-site-packages (Join-Path $Prefix 'venv')
if ($LASTEXITCODE) { Die "Couldn't create the virtualenv." }
$VPy = @('bin\python.exe', 'Scripts\python.exe') | ForEach-Object { Join-Path $Prefix "venv\$_" } |
       Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $VPy) { Die "The virtualenv has no python.exe." }

$Src = "$Repo/archive/refs/heads/main.tar.gz"
if ((Test-Path 'pyproject.toml') -and (Select-String -Quiet -Path 'pyproject.toml' -Pattern 'name = "kernel-kingdom"')) { $Src = '.' }
Say "Installing kernel-kingdom from $Src"
# psutil/setuptools come from MSYS2: nothing is fetched from PyPI
& $VPy -m pip install --quiet --disable-pip-version-check --upgrade --no-build-isolation --no-deps $Src
if ($LASTEXITCODE) { Die "pip install failed." }

# -- `kernel-kingdom` command ------------------------------------------------------------
# A .cmd shim (not pip's .exe): `kernel-kingdom update` can then replace everything while running.
$Bin = Join-Path $Prefix 'bin'
New-Item -ItemType Directory -Force -Path $Bin | Out-Null
Set-Content -Encoding ASCII -Path (Join-Path $Bin 'kernel-kingdom.cmd') -Value @"
@echo off
set "PATH=$Ucrt;%PATH%"
"$VPy" -m reino %*
"@
$UserPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if (-not (($UserPath -split ';') -contains $Bin)) {
  [Environment]::SetEnvironmentVariable('Path', (@($UserPath, $Bin) | Where-Object { $_ }) -join ';', 'User')
  Say "Added $Bin to your PATH (open a new terminal to use it)."
}
$env:PATH = "$Bin;$env:PATH"

& (Join-Path $Bin 'kernel-kingdom.cmd') doctor
if (-not $env:KERNEL_KINGDOM_NO_SERVICE) {
  Say "Adding the Start menu launcher, the wallpaper, and starting them with Windows"
  & (Join-Path $Bin 'kernel-kingdom.cmd') install
}
Say "Done. Commands: kernel-kingdom (wallpaper) | kernel-kingdom window | kernel-kingdom wallpaper off | kernel-kingdom update | kernel-kingdom uninstall"
