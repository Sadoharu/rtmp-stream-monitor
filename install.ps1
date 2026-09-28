param(
    [string]$ConfigPath = ".\config\agent.yaml"
)
$ErrorActionPreference = "Stop"
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw "Run PowerShell as Administrator and retry .\install.ps1" }
$resolvedConfig = Resolve-Path -LiteralPath $ConfigPath -ErrorAction SilentlyContinue
if (-not $resolvedConfig) { throw "Agent config not found at $ConfigPath. Copy the dashboard generated YAML there first." }
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) { throw "FFmpeg is required. Install it with winget (winget install Gyan.FFmpeg) or add ffmpeg.exe to PATH, then retry." }
if (-not (Get-Command ffprobe -ErrorAction SilentlyContinue)) { throw "ffprobe is required and normally ships with FFmpeg. Add it to PATH, then retry." }
$py = Get-Command py -ErrorAction SilentlyContinue
if (-not $py) { throw "Python 3.12 is required. Install Python 3.12 and the Python Launcher, then retry." }
$pythonVersion = & py -3.12 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>&1
if ($LASTEXITCODE -ne 0 -or [version]$pythonVersion -lt [version]"3.12") { throw "Python 3.12 was not found. Install Python 3.12 for all users and retry." }
$pythonExe = (& py -3.12 -c "import sys; print(sys.executable)").Trim()
$userProfilePrefix = [System.IO.Path]::GetFullPath($env:USERPROFILE).TrimEnd('\') + '\'
if ([System.IO.Path]::GetFullPath($pythonExe).StartsWith($userProfilePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Python 3.12 is installed only for the current user at $pythonExe. Install Python 3.12 for all users; the Windows service runs as LocalSystem and cannot reliably use a user-profile Python installation."
}
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$programDir = Join-Path $env:ProgramFiles "RTMPMonitor"
$dataDir = Join-Path $env:ProgramData "RTMPMonitor"
$configDir = Join-Path $dataDir "config"
$logDir = Join-Path $dataDir "logs"
New-Item -ItemType Directory -Force -Path $programDir,$configDir,$logDir,(Join-Path $dataDir "data") | Out-Null
$venv = Join-Path $programDir ".venv"
$venvPython = Join-Path $venv "Scripts\python.exe"
$installedConfig = Join-Path $dataDir "agent.yaml"
$env:RTMP_MONITOR_CONFIG = $installedConfig
if (Get-Service -Name RtmpMonitorAgent -ErrorAction SilentlyContinue) {
    Stop-Service -Name RtmpMonitorAgent -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $venvPython) {
        & $venvPython -m rtmp_monitor.windows_service remove
        if ($LASTEXITCODE -ne 0) { throw "Could not remove the previous RtmpMonitorAgent service." }
    }
}
Copy-Item -Recurse -Force (Join-Path $repo "src") (Join-Path $programDir "src")
Copy-Item -Force (Join-Path $repo "pyproject.toml") $programDir
& py -3.12 -m venv --clear $venv
if ($LASTEXITCODE -ne 0) { throw "Failed to create the Python 3.12 virtual environment." }
& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Failed to upgrade pip in the virtual environment." }
& $venvPython -m pip install $programDir
if ($LASTEXITCODE -ne 0) { throw "Failed to install RTMP Monitor in the virtual environment." }
Copy-Item -Force $resolvedConfig.Path $installedConfig
$configAcl = New-Object System.Security.AccessControl.FileSecurity
$configAcl.SetAccessRuleProtection($true, $false)
$none = [System.Security.AccessControl.InheritanceFlags]::None
$noProp = [System.Security.AccessControl.PropagationFlags]::None
$allow = [System.Security.AccessControl.AccessControlType]::Allow
$configAcl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule("SYSTEM","FullControl",$none,$noProp,$allow)))
$configAcl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule("Administrators","FullControl",$none,$noProp,$allow)))
Set-Acl -LiteralPath $installedConfig -AclObject $configAcl
& $venvPython -m pip install "pywin32>=306"
if ($LASTEXITCODE -ne 0) { throw "Failed to install pywin32." }
& $venvPython -m pywin32_postinstall -install
if ($LASTEXITCODE -ne 0) { throw "pywin32 post-install setup failed." }
& $venvPython -m rtmp_monitor.windows_service --startup auto install
if ($LASTEXITCODE -ne 0) { throw "Failed to install the RtmpMonitorAgent Windows service." }
Start-Service -Name RtmpMonitorAgent
Write-Host "RTMP Monitor Agent service installed and started. Logs: $logDir"
