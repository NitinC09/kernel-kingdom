# kernel-kingdom installer for Windows (PowerShell 5.1+).
#   irm https://raw.githubusercontent.com/rsmedrano-cloud/kernel-kingdom/main/install.ps1 | iex
# Uses the newest Python from python.org (offers to install it with winget) and pip: psutil,
# pycairo and pygame-ce all ship ready-made Windows wheels, so nothing is compiled.
# Run it again any time to update, or to move to a newer Python: your world is kept.
# Installs into %LOCALAPPDATA%\kernel-kingdom\venv, puts a `kernel-kingdom` command on your PATH,
# a "Kernel Kingdom" entry in the Start menu, and starts the world and the wallpaper (behind your
# desktop icons) with Windows (Startup folder).
# Options (env vars): KERNEL_KINGDOM_REPO=<github url>  KERNEL_KINGDOM_NO_SERVICE=1
#                     KERNEL_KINGDOM_YES=1 (install the newest Python without asking)
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

# -- Python: the newest one ---------------------------------------------------------------
# Newest Pythons first. Patch updates (3.15.1 -> 3.15.2, via winget upgrade or python.org)
# apply in place and the venv keeps working; for a new minor version just run this again.
function Get-Pythons {
  # every installed python.org Python >= 3.11 (64-bit), newest first, from the `py` launcher
  $found = @{}
  if (Get-Command py -ErrorAction SilentlyContinue) {
    # "-V:3.15 *   C:\...\python.exe" (launcher 3.11+) or "-3.12-64   C:\..." (older); skips -32 and 3.13t
    foreach ($line in (& py -0p 2>$null)) {
      if ($line -match '(?:-V:|-)3\.(\d+)(?:-64)?\s+\*?\s*([A-Za-z]:\\.+?python\.exe)\s*$' -and [int]$Matches[1] -ge 11) {
        $found[[int]$Matches[1]] = $Matches[2]
      }
    }
  }
  # the usual install folders too (per-user winget installs, all-users python.org installs)
  $dirs = @((Join-Path $env:LOCALAPPDATA 'Programs\Python'), $env:ProgramFiles) | Where-Object { $_ -and (Test-Path $_) }
  foreach ($d in (Get-ChildItem -Directory -Path $dirs -Filter 'Python3*' -ErrorAction SilentlyContinue)) {
    if ($d.Name -match '^Python3(\d+)$' -and [int]$Matches[1] -ge 11 -and -not $found.ContainsKey([int]$Matches[1])) {
      $exe = Join-Path $d.FullName 'python.exe'
      if (Test-Path $exe) { $found[[int]$Matches[1]] = $exe }
    }
  }
  $cmd = Get-Command python -ErrorAction SilentlyContinue      # a python on PATH that isn't the Store stub
  if ($cmd -and $cmd.Source -notmatch 'WindowsApps') {
    $minor = & $cmd.Source -c "import sys; print(sys.version_info[1] if sys.version_info >= (3, 11) else '')" 2>$null
    if ($LASTEXITCODE -eq 0 -and "$minor".Trim() -and -not $found.ContainsKey([int]$minor)) { $found[[int]$minor] = $cmd.Source }
  }
  return $found.GetEnumerator() | Sort-Object Name -Descending | ForEach-Object { [pscustomobject]@{ Minor = $_.Name; Exe = $_.Value } }
}

function Get-LatestWingetPython([int]$from) {
  # probe upward (3.15, 3.16...): keeps working as new Pythons are released
  $latest = $null
  for ($n = $from; $n -lt $from + 6; $n++) {
    winget show -e --id "Python.Python.3.$n" --accept-source-agreements *> $null
    if ($LASTEXITCODE -eq 0) { $latest = $n } elseif ($latest) { break }
  }
  return $latest
}

$Pythons = @(Get-Pythons)
$Have = if ($Pythons) { [int]$Pythons[0].Minor } else { 0 }
if (Get-Command winget -ErrorAction SilentlyContinue) {
  $Latest = Get-LatestWingetPython ([Math]::Max($Have, 14))
  if ($Latest -and $Latest -gt $Have) {
    $q = if ($Have) { "Python 3.$Latest is out (you have 3.$Have). Install it with winget?" } else { "Kernel Kingdom needs Python. Install Python 3.$Latest with winget?" }
    if (Ask $q) {
      winget install -e --id "Python.Python.3.$Latest" --scope user --accept-source-agreements --accept-package-agreements
      $env:PATH = [Environment]::GetEnvironmentVariable('Path', 'User') + ';' + [Environment]::GetEnvironmentVariable('Path', 'Machine')
      $Pythons = @(Get-Pythons)
    }
  }
}
if (-not $Pythons) { Die "Install Python 3.11+ from https://www.python.org/downloads/ (tick 'Add to PATH'), then run this installer again." }

# -- venv + package ------------------------------------------------------------------------
# A running Kernel Kingdom keeps the venv's python.exe locked: stop it before rebuilding.
$Shim = Join-Path $Prefix 'bin\kernel-kingdom.cmd'
if (Test-Path $Shim) { Say "Stopping the running Kernel Kingdom"; & $Shim stop *> $null }

$Src = "$Repo/archive/refs/heads/main.tar.gz"
if ((Test-Path 'pyproject.toml') -and (Select-String -Quiet -Path 'pyproject.toml' -Pattern 'name = "kernel-kingdom"')) { $Src = '.' }
$Venv = Join-Path $Prefix 'venv'
$VPy = Join-Path $Venv 'Scripts\python.exe'
$ok = $false
foreach ($p in $Pythons) {
  # the newest Python whose packages (pycairo, pygame-ce) already have Windows wheels
  Say "Using Python 3.$($p.Minor): $($p.Exe)"
  if (Test-Path $Venv) { Remove-Item -Recurse -Force $Venv }
  & $p.Exe -m venv $Venv
  if ($LASTEXITCODE -or -not (Test-Path $VPy)) { Say "Couldn't create a virtualenv with it, trying the next one."; continue }
  Say "Installing kernel-kingdom from $Src (with psutil, pycairo and pygame-ce)"
  & $VPy -m pip install --quiet --disable-pip-version-check --upgrade pip
  & $VPy -m pip install --quiet --disable-pip-version-check --only-binary pycairo,pygame-ce,psutil --upgrade $Src
  if ($LASTEXITCODE -eq 0) { $ok = $true; break }
  Say "Python 3.$($p.Minor) has no ready-made packages yet, trying an older one."
}
if (-not $ok) { Die "pip install failed with every Python (are you online?)." }

# -- `kernel-kingdom` command --------------------------------------------------------------
# A .cmd shim (not pip's .exe): `kernel-kingdom update` can then replace everything while running.
$Bin = Join-Path $Prefix 'bin'
New-Item -ItemType Directory -Force -Path $Bin | Out-Null
Set-Content -Encoding ASCII -Path (Join-Path $Bin 'kernel-kingdom.cmd') -Value "@`"$VPy`" -m kingdom %*"
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
