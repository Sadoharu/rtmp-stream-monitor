param(
    [string]$ConfigPath = ".\config\agent.yaml",
    [string]$PythonPath = "",
    [switch]$ReplaceConfig
)
$ErrorActionPreference = "Stop"
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw "Run PowerShell as Administrator and retry .\install.ps1" }
. (Join-Path $PSScriptRoot 'scripts\windows-python.ps1')
$configPathWasExplicit = $PSBoundParameters.ContainsKey("ConfigPath")
$resolvedConfig = Resolve-Path -LiteralPath $ConfigPath -ErrorAction SilentlyContinue
if (($configPathWasExplicit -or $ReplaceConfig) -and -not $resolvedConfig) { throw "Agent config not found at $ConfigPath." }
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) { throw "FFmpeg is required. Install it with winget (winget install Gyan.FFmpeg) or add ffmpeg.exe to PATH, then retry." }
if (-not (Get-Command ffprobe -ErrorAction SilentlyContinue)) { throw "ffprobe is required and normally ships with FFmpeg. Add it to PATH, then retry." }
$python = Resolve-RtmpMonitorPython -PythonPath $PythonPath
$pythonExe = $python.Path
$pythonVersion = $python.Version
Write-Host "Using Python $pythonVersion at $pythonExe"
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$programDir = Join-Path $env:ProgramFiles "RTMPMonitor"
$dataDir = Join-Path $env:ProgramData "RTMPMonitor"
$configDir = Join-Path $dataDir "config"
$logDir = Join-Path $dataDir "logs"
New-Item -ItemType Directory -Force -Path $programDir,$configDir,$logDir,(Join-Path $dataDir "data") | Out-Null
$installedConfig = Join-Path $dataDir "agent.yaml"
$installedConfigExists = Test-Path -LiteralPath $installedConfig
if (-not $installedConfigExists -and -not $resolvedConfig) { throw "Agent config not found at $ConfigPath. Provide the Dashboard-generated YAML with -ConfigPath for the first installation." }
if ($ReplaceConfig -and -not $resolvedConfig) { throw "-ReplaceConfig requires a valid -ConfigPath." }
$env:RTMP_MONITOR_CONFIG = $installedConfig
$existingService = Get-Service -Name RtmpMonitorAgent -ErrorAction SilentlyContinue
if ($existingService) {
    Stop-Service -Name RtmpMonitorAgent -Force -ErrorAction SilentlyContinue
}
$sourceDir = Join-Path $repo "src"
$installedSourceDir = Join-Path $programDir "src"
$stagedSourceDir = Join-Path $programDir ".src-staging"
if (Test-Path -LiteralPath $stagedSourceDir) { Remove-Item -LiteralPath $stagedSourceDir -Recurse -Force }
Copy-Item -Recurse -Force $sourceDir $stagedSourceDir
if (Test-Path -LiteralPath $installedSourceDir) { Remove-Item -LiteralPath $installedSourceDir -Recurse -Force }
Move-Item -LiteralPath $stagedSourceDir -Destination $installedSourceDir
Copy-Item -Force (Join-Path $repo "pyproject.toml") $programDir
& $pythonExe -m pip install --upgrade $programDir
if ($LASTEXITCODE -ne 0) { throw "Failed to install RTMP Monitor into the machine-wide Python environment." }
if (-not $installedConfigExists -or $ReplaceConfig) {
    Copy-Item -Force $resolvedConfig.Path $installedConfig
} elseif ($resolvedConfig) {
    Write-Host "Preserving existing agent config. Use -ReplaceConfig to install the YAML from $ConfigPath."
}
$configAcl = New-Object System.Security.AccessControl.FileSecurity
$configAcl.SetAccessRuleProtection($true, $false)
$none = [System.Security.AccessControl.InheritanceFlags]::None
$noProp = [System.Security.AccessControl.PropagationFlags]::None
$allow = [System.Security.AccessControl.AccessControlType]::Allow
$configAcl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule("SYSTEM","FullControl",$none,$noProp,$allow)))
$configAcl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule("Administrators","FullControl",$none,$noProp,$allow)))
Set-Acl -LiteralPath $installedConfig -AclObject $configAcl
& $pythonExe -m pip install --upgrade "pywin32>=306"
if ($LASTEXITCODE -ne 0) { throw "Failed to install pywin32 into the machine-wide Python environment." }
& $pythonExe -m win32.scripts.pywin32_postinstall -install -quiet
if ($LASTEXITCODE -ne 0) { throw "pywin32 machine-wide post-install setup failed." }
if ($existingService) {
    & $pythonExe -m rtmp_monitor.windows_service_cli remove
    if ($LASTEXITCODE -ne 0) { throw "Could not remove the previous RtmpMonitorAgent service." }
}
& $pythonExe -m rtmp_monitor.windows_service_cli --startup auto install
if ($LASTEXITCODE -ne 0) { throw "Failed to install the RtmpMonitorAgent Windows service." }
Start-Service -Name RtmpMonitorAgent
Write-Host "RTMP Monitor Agent service installed and started. Logs: $logDir"
