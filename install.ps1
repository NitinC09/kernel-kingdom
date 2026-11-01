# kernel-kingdom installer for Windows (PowerShell 5.1+).
#   irm https://raw.githubusercontent.com/rsmedrano-cloud/kernel-kingdom/main/install.ps1 | iex
# Uses the regular Python from python.org (installed with winget if you don't have it) and pip:
# psutil, pycairo and pygame-ce all ship ready-made Windows wheels, so nothing is compiled.
# Installs into %LOCALAPPDATA%\kernel-kingdom\venv, puts a `kernel-kingdom` command on your PATH,
# a "Kernel Kingdom" entry in the Start menu, and starts the world and the wallpaper (behind your
# desktop icons) with Windows (Startup folder).
# Options (env vars): KERNEL_KINGDOM_REPO=<github url>  KERNEL_KINGDOM_NO_SERVICE=1
#                     KERNEL_KINGDOM_YES=1 (install Python without asking)
# 'Continue': PowerShell 5.1 turns any stderr line of a native command into an error under 'Stop'.
# Failures are checked with $LASTEXITCODE instead.
$ErrorActionPreference = 'Continue'

$Repo   = if ($env:KERNEL_KINGDOM_REPO) { $env:KERNEL_KINGDOM_REPO } else { 'https://github.com/rsmedrano-cloud/kernel-kingdom' }
$Prefix = Join-Path $env:LOCALAPPDATA 'kernel-kingdom'

function Say($m) { Write-Host "==> $m" -ForegroundColor Yellow }
function Die($m) { Write-Host "xx $m" -ForegroundColor Red; throw "kernel-kingdom install: $m" }  # not `exit`: under `irm | iex` it would close the terminal
function Ask($q) {
  if ($env:KERNEL_KINGDOM_YES) { return $true }
  $r = Read-Host "==> $q [Y/n]"
  return -not ($r -match '^[nN]')
}

# -- Python 3.11+ --------------------------------------------------------------------------
# The `py` launcher comes with python.org Python; plain `python` may be the Microsoft Store stub.
function Find-Python {
  foreach ($v in '3.13', '3.12', '3.11', '3.14') {
    if (Get-Command py -ErrorAction SilentlyContinue) {
      $exe = & py "-$v" -c "import sys; print(sys.executable)" 2>$null
      if ($LASTEXITCODE -eq 0 -and $exe) { return $exe.Trim() }
    }
  }
  $cmd = Get-Command python -ErrorAction SilentlyContinue
  if ($cmd -and $cmd.Source -notmatch 'WindowsApps') {
    & $cmd.Source -c "import sys; assert sys.version_info >= (3, 11)" 2>$null
    if ($LASTEXITCODE -eq 0) { return $cmd.Source }
  }
  return $null
}

$Py = Find-Python
if (-not $Py) {
  Say "Kernel Kingdom needs Python 3.11 or newer."
  if ((Get-Command winget -ErrorAction SilentlyContinue) -and (Ask "Install Python 3.13 with winget now?")) {
    winget install -e --id Python.Python.3.13 --scope user --accept-source-agreements --accept-package-agreements
    $env:PATH = [Environment]::GetEnvironmentVariable('Path', 'User') + ';' + [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $Py = Find-Python
  }
  if (-not $Py) { Die "Install Python 3.11+ from https://www.python.org/downloads/ (tick 'Add to PATH'), then run this installer again." }
}
Say "Using Python: $Py"

# -- venv + package ------------------------------------------------------------------------
Say "Creating virtualenv in $Prefix\venv"
& $Py -m venv (Join-Path $Prefix 'venv')
if ($LASTEXITCODE) { Die "Couldn't create the virtualenv." }
$VPy = Join-Path $Prefix 'venv\Scripts\python.exe'
if (-not (Test-Path $VPy)) { Die "The virtualenv has no python.exe." }

$Src = "$Repo/archive/refs/heads/main.tar.gz"
if ((Test-Path 'pyproject.toml') -and (Select-String -Quiet -Path 'pyproject.toml' -Pattern 'name = "kernel-kingdom"')) { $Src = '.' }
Say "Installing kernel-kingdom from $Src (with psutil, pycairo and pygame-ce)"
& $VPy -m pip install --quiet --disable-pip-version-check --upgrade pip
& $VPy -m pip install --quiet --disable-pip-version-check --upgrade $Src
if ($LASTEXITCODE) { Die "pip install failed (are you online?)." }

# -- `kernel-kingdom` command --------------------------------------------------------------
# A .cmd shim (not pip's .exe): `kernel-kingdom update` can then replace everything while running.
$Bin = Join-Path $Prefix 'bin'
New-Item -ItemType Directory -Force -Path $Bin | Out-Null
Set-Content -Encoding ASCII -Path (Join-Path $Bin 'kernel-kingdom.cmd') -Value "@`"$VPy`" -m reino %*"
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
