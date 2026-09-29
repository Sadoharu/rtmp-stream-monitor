param(
    [string]$ConfigPath = ".\config\agent.yaml",
    [string]$PythonPath = "",
    [switch]$ReplaceConfig
)
$ErrorActionPreference = "Stop"
function Invoke-RtmpMonitorNativeCommand {
    param([Parameter(Mandatory)][scriptblock]$Command)

    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $Command 2>&1
        $script:rtmpMonitorNativeExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
}

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw "Run PowerShell as Administrator and retry .\install.ps1" }
. (Join-Path $PSScriptRoot 'scripts\windows-python.ps1')
$configPathWasExplicit = $PSBoundParameters.ContainsKey("ConfigPath")
$resolvedConfig = Resolve-Path -LiteralPath $ConfigPath -ErrorAction SilentlyContinue
if (($configPathWasExplicit -or $ReplaceConfig) -and -not $resolvedConfig) { throw "Agent config not found at $ConfigPath." }
$ffmpegCommand = Get-Command ffmpeg -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
$ffprobeCommand = Get-Command ffprobe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $ffmpegCommand) { throw "FFmpeg is required. Install it machine-wide (for example, winget install Gyan.FFmpeg) and retry." }
if (-not $ffprobeCommand) { throw "ffprobe is required and normally ships with FFmpeg. Install it machine-wide and retry." }
$ffmpegPath = [System.IO.Path]::GetFullPath($ffmpegCommand.Source)
$ffprobePath = [System.IO.Path]::GetFullPath($ffprobeCommand.Source)
$userProfilePrefix = [System.IO.Path]::GetFullPath($env:USERPROFILE).TrimEnd('\') + '\'
foreach ($toolPath in @($ffmpegPath,$ffprobePath)) {
    if ([System.IO.Path]::GetExtension($toolPath) -ine ".exe") {
        throw "FFmpeg tools must resolve to executable .exe files for Windows Services. Found $toolPath."
    }
    if ($toolPath.StartsWith($userProfilePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "FFmpeg tools must be installed outside the user profile because the service runs as LocalSystem. Found $toolPath. Install FFmpeg for all users, then retry."
    }
}
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
Invoke-RtmpMonitorNativeCommand { & $pythonExe -m pip install --upgrade $programDir }
if ($script:rtmpMonitorNativeExitCode -ne 0) { throw "Failed to install RTMP Monitor into the machine-wide Python environment." }
if (-not $installedConfigExists -or $ReplaceConfig) {
    Copy-Item -Force $resolvedConfig.Path $installedConfig
} elseif ($resolvedConfig) {
    Write-Host "Preserving existing agent config. Use -ReplaceConfig to install the YAML from $ConfigPath."
}
$configureTools = @'
import sys
from pathlib import Path
import yaml

path = Path(sys.argv[1])
config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
config["ffmpeg_path"] = sys.argv[2]
config["ffprobe_path"] = sys.argv[3]
path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")
'@
$configureToolsPath = Join-Path $env:TEMP ("rtmp-monitor-config-" + [guid]::NewGuid().ToString() + ".py")
[System.IO.File]::WriteAllText($configureToolsPath, $configureTools, [System.Text.Encoding]::ASCII)
$configureToolsExitCode = 1
try {
    Invoke-RtmpMonitorNativeCommand { & $pythonExe $configureToolsPath $installedConfig $ffmpegPath $ffprobePath }
    $configureToolsExitCode = $script:rtmpMonitorNativeExitCode
} finally {
    Remove-Item -LiteralPath $configureToolsPath -Force -ErrorAction SilentlyContinue
}
if ($configureToolsExitCode -ne 0) { throw "Failed to configure machine-wide FFmpeg paths for the Windows service." }
$configAcl = New-Object System.Security.AccessControl.FileSecurity
$configAcl.SetAccessRuleProtection($true, $false)
$none = [System.Security.AccessControl.InheritanceFlags]::None
$noProp = [System.Security.AccessControl.PropagationFlags]::None
$allow = [System.Security.AccessControl.AccessControlType]::Allow
$configAcl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule("SYSTEM","FullControl",$none,$noProp,$allow)))
$configAcl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule("Administrators","FullControl",$none,$noProp,$allow)))
Set-Acl -LiteralPath $installedConfig -AclObject $configAcl
Invoke-RtmpMonitorNativeCommand { & $pythonExe -m pip install --upgrade "pywin32>=306" }
if ($script:rtmpMonitorNativeExitCode -ne 0) { throw "Failed to install pywin32 into the machine-wide Python environment." }
Invoke-RtmpMonitorNativeCommand { & $pythonExe -m win32.scripts.pywin32_postinstall -install -quiet }
if ($script:rtmpMonitorNativeExitCode -ne 0) { throw "pywin32 machine-wide post-install setup failed." }
if ($existingService) {
    Invoke-RtmpMonitorNativeCommand { & $pythonExe -m rtmp_monitor.windows_service_cli remove }
    if ($script:rtmpMonitorNativeExitCode -ne 0) { throw "Could not remove the previous RtmpMonitorAgent service." }
}
Invoke-RtmpMonitorNativeCommand { & $pythonExe -m rtmp_monitor.windows_service_cli --startup auto install }
if ($script:rtmpMonitorNativeExitCode -ne 0) { throw "Failed to install the RtmpMonitorAgent Windows service." }
Invoke-RtmpMonitorNativeCommand { & sc.exe failure RtmpMonitorAgent reset= 86400 actions= restart/5000/restart/15000/restart/60000 | Out-Null }
if ($script:rtmpMonitorNativeExitCode -ne 0) { throw "Failed to configure automatic recovery for the RtmpMonitorAgent Windows service." }
Invoke-RtmpMonitorNativeCommand { & sc.exe failureflag RtmpMonitorAgent 1 | Out-Null }
if ($script:rtmpMonitorNativeExitCode -ne 0) { throw "Failed to enable recovery for non-crash service errors." }
Start-Service -Name RtmpMonitorAgent
Write-Host "RTMP Monitor Agent service installed and started. Logs: $logDir"
