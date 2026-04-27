# Build script for FaujiSetup.exe
# Run from the FaujiPackage folder:  powershell -ExecutionPolicy Bypass -File installer\build.ps1
#
# What it does:
#  1. Downloads embeddable Python 3.12 to installer\python\
#  2. Downloads vanilla MetaTrader5 installer to installer\mt5setup.exe
#  3. Downloads pip wheels for all requirements into installer\wheels\
#  4. Copies bot/ and supervisor/ next to those bundles
#  5. Compiles installer\fauji.iss with Inno Setup → installer\Output\FaujiSetup.exe

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$inst = Join-Path $root "installer"
$pyDir = Join-Path $inst "python"
$wheelsDir = Join-Path $inst "wheels"
$mt5 = Join-Path $inst "mt5setup.exe"

Write-Host "[build] root: $root"

New-Item -ItemType Directory -Force -Path $pyDir, $wheelsDir | Out-Null

# 1) Embeddable Python 3.12 (pinned)
$pyVer = "3.12.7"
$pyZip = Join-Path $inst "python-$pyVer-embed-amd64.zip"
if (-not (Test-Path (Join-Path $pyDir "python.exe"))) {
  $url = "https://www.python.org/ftp/python/$pyVer/python-$pyVer-embed-amd64.zip"
  Write-Host "[build] Downloading $url"
  Invoke-WebRequest -Uri $url -OutFile $pyZip
  Expand-Archive -Path $pyZip -DestinationPath $pyDir -Force
  # Enable site-packages in the embeddable distro
  $pth = Get-ChildItem -Path $pyDir -Filter "python*._pth" | Select-Object -First 1
  if ($pth) {
    (Get-Content $pth.FullName) -replace '^#import site', 'import site' | Set-Content $pth.FullName
  }
  # Bootstrap pip
  $getpip = Join-Path $inst "get-pip.py"
  Invoke-WebRequest -Uri "https://bootstrap.pypa.io/get-pip.py" -OutFile $getpip
  & "$pyDir\python.exe" $getpip --no-warn-script-location
}

# 2) MT5 installer (vanilla MetaQuotes build)
if (-not (Test-Path $mt5)) {
  Write-Host "[build] Downloading MT5 installer"
  Invoke-WebRequest -Uri "https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe" -OutFile $mt5
}

# 3) Wheels for offline install on the RDP
Write-Host "[build] Downloading wheels..."
& "$pyDir\python.exe" -m pip download `
  -r (Join-Path $root "requirements.txt") `
  -d $wheelsDir `
  --platform win_amd64 --python-version 312 --only-binary=:all: `
  --no-deps
& "$pyDir\python.exe" -m pip download `
  -r (Join-Path $root "requirements.txt") `
  -d $wheelsDir `
  --platform win_amd64 --python-version 312 --only-binary=:all:

# 4) Auto-install Inno Setup 6 if missing
$iscc = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $iscc)) { $iscc = "C:\Program Files\Inno Setup 6\ISCC.exe" }
if (-not (Test-Path $iscc)) {
  Write-Host "[build] Inno Setup 6 not found. Downloading and installing silently..."
  $isExe = Join-Path $inst "innosetup-installer.exe"
  $innoUrl = "https://files.jrsoftware.org/is/6/innosetup-6.4.3.exe"
  try {
    Invoke-WebRequest -Uri $innoUrl -OutFile $isExe -UseBasicParsing
  } catch {
    Write-Host "[build] Primary URL failed, trying GitHub mirror..."
    Invoke-WebRequest -Uri "https://github.com/jrsoftware/issrc/releases/download/is-6_4_3/innosetup-6.4.3.exe" -OutFile $isExe -UseBasicParsing
  }
  Write-Host "[build] Installing Inno Setup (silent)..."
  $p = Start-Process -FilePath $isExe -ArgumentList "/VERYSILENT","/SUPPRESSMSGBOXES","/NORESTART","/SP-" -Wait -PassThru
  if ($p.ExitCode -ne 0) { throw "Inno Setup installer exited with code $($p.ExitCode)" }
  Remove-Item $isExe -ErrorAction SilentlyContinue
  $iscc = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
  if (-not (Test-Path $iscc)) { $iscc = "C:\Program Files\Inno Setup 6\ISCC.exe" }
  if (-not (Test-Path $iscc)) { throw "Inno Setup install completed but ISCC.exe not found." }
  Write-Host "[build] Inno Setup installed at $iscc"
}

# 5) Compile installer
Write-Host "[build] Compiling installer..."
& $iscc (Join-Path $inst "fauji.iss")
if ($LASTEXITCODE -ne 0) { throw "ISCC compilation failed (exit $LASTEXITCODE)" }
Write-Host ""
Write-Host "==========================================" -ForegroundColor Green
Write-Host " BUILD COMPLETE" -ForegroundColor Green
Write-Host "==========================================" -ForegroundColor Green
Write-Host " Output: $inst\Output\FaujiSetup.exe"
Write-Host " Copy that file to your AWS RDP and run as administrator."
Write-Host ""
