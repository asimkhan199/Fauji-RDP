# ============================================================
#  FaujiBot RDP Bootstrap — runs on a fresh AWS Windows RDP.
#
#  One-line invocation (paste in PowerShell as Administrator):
#    iwr https://raw.githubusercontent.com/asimkhan199/Fauji-RDP/main/bootstrap_rdp.ps1 -UseBasicParsing | iex
#
#  What it does:
#   1. Downloads this repo as a zip from GitHub (no git required)
#   2. Installs Python 3.12 (if missing)
#   3. Installs MetaTrader 5 silently
#   4. Installs Python packages
#   5. Generates self-signed cert + first-run config
#   6. Creates Scheduled Task (auto-start on logon)
#   7. Opens Windows firewall port 8443
#   8. Launches the supervisor and opens the dashboard
# ============================================================

param(
  [string]$RepoOwner = "asimkhan199",
  [string]$RepoName  = "Fauji-RDP",
  [string]$Branch    = "main",
  [string]$InstallDir = "C:\FaujiBot"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"  # huge speedup on Invoke-WebRequest

function Section($t) { Write-Host ""; Write-Host "==> $t" -ForegroundColor Cyan }
function Ok($t)      { Write-Host "    OK: $t" -ForegroundColor Green }
function Warn2($t)   { Write-Host "    WARN: $t" -ForegroundColor Yellow }

# Must be admin
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
  Write-Host "ERROR: Run this in PowerShell *as Administrator*." -ForegroundColor Red
  exit 1
}

# 1) Download repo zip from GitHub
Section "Downloading FaujiBot repo from GitHub"
$zipUrl = "https://codeload.github.com/$RepoOwner/$RepoName/zip/refs/heads/$Branch"
$zipPath = "$env:TEMP\fauji-repo.zip"
$extractRoot = "$env:TEMP\fauji-extract"
if (Test-Path $extractRoot) { Remove-Item -Recurse -Force $extractRoot }
New-Item -ItemType Directory -Path $extractRoot | Out-Null

Invoke-WebRequest -Uri $zipUrl -OutFile $zipPath -UseBasicParsing
Expand-Archive -Path $zipPath -DestinationPath $extractRoot -Force
$srcDir = Get-ChildItem $extractRoot -Directory | Select-Object -First 1
Ok "Source extracted to $($srcDir.FullName)"

# Copy to install dir (preserve user's data\ if it already exists)
Section "Copying files to $InstallDir"
if (-not (Test-Path $InstallDir)) { New-Item -ItemType Directory -Path $InstallDir | Out-Null }
$preserveData = Test-Path "$InstallDir\data"
robocopy $srcDir.FullName $InstallDir /E /NFL /NDL /NJH /NJS /XD ".git" "installer\Output" "installer\python" "installer\wheels" ".venv" /XF "*.zip" | Out-Null
if (-not $preserveData) { New-Item -ItemType Directory -Path "$InstallDir\data" -Force | Out-Null }
Ok "Files in place"

# 2) Python 3.12
Section "Ensuring Python 3.12"
$py = $null
foreach ($p in @("$InstallDir\python\python.exe", "C:\Python312\python.exe", "C:\Program Files\Python312\python.exe")) {
  if (Test-Path $p) { $py = $p; break }
}
if (-not $py) {
  try {
    $cmd = Get-Command py -ErrorAction Stop
    & py -3.12 --version 2>$null
    if ($LASTEXITCODE -eq 0) { $py = "py" }
  } catch {}
}
if (-not $py) {
  Write-Host "    Installing Python 3.12..." -ForegroundColor Gray
  $pyExe = "$env:TEMP\python-3.12.7-amd64.exe"
  Invoke-WebRequest -Uri "https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe" -OutFile $pyExe -UseBasicParsing
  Start-Process -FilePath $pyExe -ArgumentList "/quiet","InstallAllUsers=1","PrependPath=1","Include_test=0","Include_doc=0","Include_launcher=1" -Wait
  Remove-Item $pyExe -ErrorAction SilentlyContinue
  $py = "C:\Program Files\Python312\python.exe"
  if (-not (Test-Path $py)) { $py = "C:\Python312\python.exe" }
}
Ok "Python: $py"

# 3) MetaTrader 5
Section "Ensuring MetaTrader 5"
$mt5Exe = "C:\Program Files\MetaTrader 5\terminal64.exe"
if (-not (Test-Path $mt5Exe)) {
  Write-Host "    Installing MT5 silently..." -ForegroundColor Gray
  $mt5Setup = "$env:TEMP\mt5setup.exe"
  Invoke-WebRequest -Uri "https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe" -OutFile $mt5Setup -UseBasicParsing
  Start-Process -FilePath $mt5Setup -ArgumentList "/auto" -Wait
  Remove-Item $mt5Setup -ErrorAction SilentlyContinue
}
Ok "MT5: $mt5Exe"

# 4) Python packages
Section "Installing Python packages"
& $py -m pip install --upgrade pip --quiet
& $py -m pip install -r "$InstallDir\requirements.txt" --quiet
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
Ok "Packages installed"

# Persist python path for the launcher
"$py" | Out-File -FilePath "$InstallDir\python_path.txt" -Encoding ascii -NoNewline

# 5) Launcher script
Section "Writing launcher"
$launcher = @"
@echo off
setlocal
set APP=%~dp0
cd /d "%APP%"
set PYTHONPATH=%APP%
set /p PY=<"%APP%python_path.txt"
"%PY%" -m supervisor.main
"@
Set-Content -Path "$InstallDir\FaujiBot.cmd" -Value $launcher -Encoding ascii
Ok "Launcher: $InstallDir\FaujiBot.cmd"

# 6) Scheduled Task — auto-start on logon
Section "Registering Scheduled Task (auto-start on logon)"
schtasks /Delete /F /TN "FaujiBot" 2>$null | Out-Null
schtasks /Create /F /SC ONLOGON /RL HIGHEST /TN "FaujiBot" /TR "`"$InstallDir\FaujiBot.cmd`"" | Out-Null
Ok "Task 'FaujiBot' registered"

# 7) Firewall rule
Section "Opening Windows firewall port 8443"
netsh advfirewall firewall delete rule name="FaujiBot Dashboard" 2>$null | Out-Null
netsh advfirewall firewall add rule name="FaujiBot Dashboard" dir=in action=allow protocol=TCP localport=8443 | Out-Null
Ok "Local firewall: TCP 8443 inbound allowed"

# 8) Detect public IP for the AWS Security Group reminder
Section "Detecting AWS public IP"
$publicIp = $null
try {
  $tok = Invoke-RestMethod -Method PUT -Uri "http://169.254.169.254/latest/api/token" -Headers @{ "X-aws-ec2-metadata-token-ttl-seconds" = "60" } -TimeoutSec 2
  $publicIp = Invoke-RestMethod -Uri "http://169.254.169.254/latest/meta-data/public-ipv4" -Headers @{ "X-aws-ec2-metadata-token" = $tok } -TimeoutSec 2
} catch {
  try { $publicIp = (Invoke-RestMethod -Uri "https://api.ipify.org" -TimeoutSec 4) } catch {}
}
if ($publicIp) { Ok "Public IP: $publicIp" } else { Warn2 "Could not detect public IP automatically" }

# 9) Launch supervisor
Section "Starting FaujiBot supervisor"
Start-Process -FilePath "$InstallDir\FaujiBot.cmd" -WindowStyle Minimized

Start-Sleep -Seconds 4
Start-Process "https://localhost:8443"

Write-Host ""
Write-Host "=========================================================" -ForegroundColor Green
Write-Host " FAUJIBOT IS RUNNING" -ForegroundColor Green
Write-Host "=========================================================" -ForegroundColor Green
Write-Host ""
Write-Host " Local URL :  https://localhost:8443"
if ($publicIp) {
  Write-Host " Phone URL :  https://$publicIp" + ":8443" -ForegroundColor Cyan
  Write-Host ""
  Write-Host " >> AWS Console: open inbound TCP 8443 from your phone IP <<"
} else {
  Write-Host " Phone URL :  https://<your-aws-public-ip>:8443"
}
Write-Host ""
Write-Host " Next steps:"
Write-Host "  1. Accept the red HTTPS warning (self-signed cert)"
Write-Host "  2. 3-step wizard: password -> magic_number -> Start bot"
Write-Host "  3. Open MT5 from Start menu and log into your broker"
Write-Host ""
Write-Host " To update later: re-run this same one-liner."
Write-Host ""
