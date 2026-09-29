param(
    [switch]$KeepLocalData,
    [switch]$Force
)
$ErrorActionPreference = "Stop"

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run PowerShell as Administrator and retry .\uninstall.ps1."
}

$programDir = Join-Path $env:ProgramFiles "RTMPMonitor"
$dataDir = Join-Path $env:ProgramData "RTMPMonitor"
if (-not $Force) {
    $scope = if ($KeepLocalData) { "service and installed program files; local config, queue and logs will be kept" } else { "service, program files, local config, queued telemetry and logs" }
    $answer = Read-Host "Remove the RTMP Monitor Agent $scope? The central server history is not deleted. [y/N]"
    if ($answer -notmatch '^(y|yes)$') {
        Write-Host "Uninstall cancelled."
        exit 0
    }
}

$service = Get-Service -Name "RtmpMonitorAgent" -ErrorAction SilentlyContinue
if ($service) {
    try {
        if ($service.Status -ne [System.ServiceProcess.ServiceControllerStatus]::Stopped) {
            Stop-Service -Name "RtmpMonitorAgent" -Force
            $service.Refresh()
            $service.WaitForStatus([System.ServiceProcess.ServiceControllerStatus]::Stopped, [TimeSpan]::FromSeconds(30))
        }
    } finally {
        $service.Dispose()
    }
    & sc.exe delete RtmpMonitorAgent | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not delete the RtmpMonitorAgent service (sc.exe exit code $LASTEXITCODE)." }
}

function Remove-RtmpMonitorDirectory {
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $expected = [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
    $resolved = [System.IO.Path]::GetFullPath((Resolve-Path -LiteralPath $Path).Path).TrimEnd('\')
    if ($resolved -ine $expected) { throw "Refusing to remove unexpected directory $resolved." }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}

Remove-RtmpMonitorDirectory -Path $programDir
if (-not $KeepLocalData) { Remove-RtmpMonitorDirectory -Path $dataDir }
Write-Host "RTMP Monitor Agent service and program files removed."
if ($KeepLocalData) {
    Write-Host "Local config, outbox and logs remain in $dataDir."
} else {
    Write-Host "Local config, outbox and logs removed. The central probe entry and history remain; remove or disable that probe in the dashboard if needed."
}
