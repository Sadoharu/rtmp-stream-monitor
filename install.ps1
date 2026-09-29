param(
    [string]$ConfigPath = ".\config\agent.yaml",
    [string]$ServerUrl = "",
    [string]$PythonPath = "",
    [switch]$ReplaceConfig,
    [switch]$NoDependencyInstall
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
. (Join-Path $PSScriptRoot 'scripts\windows-install-guidance.ps1')
$bundledPythonExe = Join-Path $PSScriptRoot 'runtime\python.exe'
$bundledRuntime = Test-Path -LiteralPath $bundledPythonExe
if ($bundledRuntime -and $PythonPath) { throw "This package includes its own Python runtime; -PythonPath is not used." }
$configPathWasExplicit = $PSBoundParameters.ContainsKey("ConfigPath")
$resolvedConfig = Resolve-Path -LiteralPath $ConfigPath -ErrorAction SilentlyContinue
if (($configPathWasExplicit -or $ReplaceConfig) -and -not $resolvedConfig) { throw "Agent config not found at $ConfigPath." }
$userProfilePrefix = [System.IO.Path]::GetFullPath($env:USERPROFILE).TrimEnd('\') + '\'

function Install-RtmpMonitorWinGetPackage {
    param([Parameter(Mandatory)][string]$PackageId)
    $winget = Get-Command winget.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $winget) {
        throw (Get-RtmpMonitorWinGetUnavailableMessage -PackageId $PackageId)
    }
    Write-Host "Installing $PackageId for all users with winget..."
    Invoke-RtmpMonitorNativeCommand {
        & $winget.Source install --id $PackageId --exact --scope machine
    }
    if ($script:rtmpMonitorNativeExitCode -ne 0) { throw "winget could not install $PackageId (exit code $script:rtmpMonitorNativeExitCode)." }
}

function Test-RtmpMonitorMachineExecutable {
    param([string]$Path)
    if (-not $Path -or [System.IO.Path]::GetExtension($Path) -ine ".exe" -or -not (Test-Path -LiteralPath $Path)) { return $false }
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    return -not $fullPath.StartsWith($userProfilePrefix, [System.StringComparison]::OrdinalIgnoreCase)
}

function Get-RtmpMonitorMachineExecutable {
    param([Parameter(Mandatory)][string]$Name)
    $command = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command -and (Test-RtmpMonitorMachineExecutable $command.Source)) { return [System.IO.Path]::GetFullPath($command.Source) }
    $packageRoot = Join-Path $env:ProgramFiles 'WinGet\Packages\Gyan.FFmpeg_*'
    $candidate = Get-ChildItem -Path $packageRoot -Filter "$Name.exe" -File -Recurse -ErrorAction SilentlyContinue |
        Where-Object { Test-RtmpMonitorMachineExecutable $_.FullName } |
        Sort-Object FullName -Descending |
        Select-Object -First 1
    if ($candidate) { return [System.IO.Path]::GetFullPath($candidate.FullName) }
    return $null
}

if ($bundledRuntime) {
    $pythonExe = [System.IO.Path]::GetFullPath((Resolve-Path -LiteralPath $bundledPythonExe).Path)
    $pythonVersion = (& $pythonExe -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')").Trim()
    if ($LASTEXITCODE -ne 0 -or $pythonVersion -notmatch '^3\.(12|13|14)\.\d+$') {
        throw "The packaged Python runtime is missing or unsupported at $pythonExe."
    }
    Write-Host "Using bundled Python $pythonVersion at $pythonExe"
} else {
    try {
        $python = Resolve-RtmpMonitorPython -PythonPath $PythonPath
    } catch {
        if ($PythonPath -or $NoDependencyInstall) { throw }
        Install-RtmpMonitorWinGetPackage -PackageId 'Python.Python.3.12'
        $python = Resolve-RtmpMonitorPython
    }
    $pythonExe = $python.Path
    $pythonVersion = $python.Version
    Write-Host "Using Python $pythonVersion at $pythonExe"
}
$ffmpegPath = Get-RtmpMonitorMachineExecutable -Name 'ffmpeg'
$ffprobePath = Get-RtmpMonitorMachineExecutable -Name 'ffprobe'
if (-not $ffmpegPath -or -not $ffprobePath) {
    if ($NoDependencyInstall) { throw "Machine-wide FFmpeg and ffprobe are required. Install Gyan.FFmpeg with winget or rerun without -NoDependencyInstall." }
    Install-RtmpMonitorWinGetPackage -PackageId 'Gyan.FFmpeg'
    $ffmpegPath = Get-RtmpMonitorMachineExecutable -Name 'ffmpeg'
    $ffprobePath = Get-RtmpMonitorMachineExecutable -Name 'ffprobe'
}
if (-not $ffmpegPath -or -not $ffprobePath) { throw "winget reported FFmpeg installed, but machine-wide ffmpeg.exe/ffprobe.exe were not found. Restart PowerShell and retry, or set machine PATH to the Gyan.FFmpeg package bin directory." }
foreach ($toolPath in @($ffmpegPath,$ffprobePath)) {
    if (-not (Test-RtmpMonitorMachineExecutable $toolPath)) {
        throw "FFmpeg tools must be accessible outside the current user profile because the Windows service runs as LocalSystem. Found $toolPath."
    }
}
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$programDir = Join-Path $env:ProgramFiles "RTMPMonitor"
$dataDir = Join-Path $env:ProgramData "RTMPMonitor"
$configDir = Join-Path $dataDir "config"
$logDir = Join-Path $dataDir "logs"
New-Item -ItemType Directory -Force -Path $programDir,$configDir,$logDir,(Join-Path $dataDir "data") | Out-Null
$installedConfig = Join-Path $dataDir "agent.yaml"
$installedConfigExists = Test-Path -LiteralPath $installedConfig
if (-not $installedConfigExists -and -not $resolvedConfig -and -not $ServerUrl) {
    throw "For a new probe, run .\install.ps1 -ServerUrl https://monitor.example.net and enter the one-time enrollment code from the dashboard; or provide -ConfigPath."
}
if (-not $installedConfigExists -and -not $resolvedConfig -and $ServerUrl) {
    $serverUri = $null
    if (-not [Uri]::TryCreate($ServerUrl, [UriKind]::Absolute, [ref]$serverUri) -or
        ($serverUri.Scheme -ne "https" -and $serverUri.Host -notin @("localhost", "127.0.0.1", "::1")) -or
        $serverUri.UserInfo -or $serverUri.Query -or $serverUri.Fragment -or $serverUri.AbsolutePath -ne "/") {
        throw "-ServerUrl must be an HTTPS origin without credentials or a path (HTTP is allowed only for localhost testing)."
    }
    $secureCode = Read-Host "One-time probe enrollment code (valid for 15 minutes)" -AsSecureString
    $securePointer = [IntPtr]::Zero
    $plainCode = $null
    try {
        $securePointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureCode)
        $plainCode = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($securePointer)
        $redemption = Invoke-RestMethod -Method Post -Uri ($ServerUrl.TrimEnd("/") + "/api/v2/probe-enrollments/redeem") `
            -ContentType "application/json" -Body (@{ code = $plainCode } | ConvertTo-Json -Compress)
        $configJson = ConvertTo-Json -InputObject $redemption.config -Depth 20
        [System.IO.File]::WriteAllText($installedConfig, $configJson, (New-Object System.Text.UTF8Encoding($false)))
        $configAcl = New-Object System.Security.AccessControl.FileSecurity
        $configAcl.SetAccessRuleProtection($true, $false)
        $none = [System.Security.AccessControl.InheritanceFlags]::None
        $noProp = [System.Security.AccessControl.PropagationFlags]::None
        $allow = [System.Security.AccessControl.AccessControlType]::Allow
        $configAcl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule("SYSTEM","FullControl",$none,$noProp,$allow)))
        $configAcl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule("Administrators","FullControl",$none,$noProp,$allow)))
        Set-Acl -LiteralPath $installedConfig -AclObject $configAcl
        $resolvedConfig = Get-Item -LiteralPath $installedConfig
    } finally {
        if ($securePointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($securePointer) }
        $plainCode = $null
    }
}
if (-not $installedConfigExists -and -not $resolvedConfig) { throw "Agent config not found at $ConfigPath. Provide the Dashboard-generated YAML with -ConfigPath." }
if ($ReplaceConfig -and -not $resolvedConfig) { throw "-ReplaceConfig requires a valid -ConfigPath." }
$env:RTMP_MONITOR_CONFIG = $installedConfig
$existingService = Get-Service -Name RtmpMonitorAgent -ErrorAction SilentlyContinue
if ($existingService -and $existingService.Status -ne [System.ServiceProcess.ServiceControllerStatus]::Stopped) {
    Stop-Service -Name RtmpMonitorAgent -Force
    $existingService.Refresh()
    $existingService.WaitForStatus([System.ServiceProcess.ServiceControllerStatus]::Stopped, [TimeSpan]::FromSeconds(30))
}
$serviceRemoved = $false
if ($bundledRuntime) {
    $runtimeDir = Join-Path $programDir 'runtime'
    $stagedRuntimeDir = Join-Path $programDir '.runtime-staging'
    if (Test-Path -LiteralPath $stagedRuntimeDir) { Remove-Item -LiteralPath $stagedRuntimeDir -Recurse -Force }
    Copy-Item -Recurse -Force (Join-Path $PSScriptRoot 'runtime') $stagedRuntimeDir
} else {
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
}
if (-not $installedConfigExists -or $ReplaceConfig) {
    if ($resolvedConfig.Path -ne $installedConfig) { Copy-Item -Force $resolvedConfig.Path $installedConfig }
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
if ($bundledRuntime) {
    if ($existingService) {
        $existingService.Dispose()
        Invoke-RtmpMonitorNativeCommand { & $pythonExe -m rtmp_monitor.windows_service_cli remove }
        if ($script:rtmpMonitorNativeExitCode -ne 0) { throw "Could not remove the previous RtmpMonitorAgent service." }
        $serviceRemoved = $true
    }
    if (Test-Path -LiteralPath $runtimeDir) { Remove-Item -LiteralPath $runtimeDir -Recurse -Force }
    Move-Item -LiteralPath $stagedRuntimeDir -Destination $runtimeDir
    $pythonExe = Join-Path $runtimeDir 'python.exe'
    $runtimeVersion = (& $pythonExe -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')").Trim()
    if ($LASTEXITCODE -ne 0) { throw "Could not start the installed private Python runtime at $pythonExe." }
    Write-Host "Installed private Python runtime $runtimeVersion in $runtimeDir"
} else {
    Invoke-RtmpMonitorNativeCommand { & $pythonExe -m pip install --upgrade "pywin32>=306" }
    if ($script:rtmpMonitorNativeExitCode -ne 0) { throw "Failed to install pywin32 into the machine-wide Python environment." }
    Invoke-RtmpMonitorNativeCommand { & $pythonExe -m win32.scripts.pywin32_postinstall -install -quiet }
    if ($script:rtmpMonitorNativeExitCode -ne 0) { throw "pywin32 machine-wide post-install setup failed." }
}
if ($existingService -and -not $serviceRemoved) {
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
