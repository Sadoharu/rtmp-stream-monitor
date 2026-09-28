$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repoRoot

$pythonExe = (Get-Command python -ErrorAction Stop).Source
$pythonVersion = (& $pythonExe -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')").Trim()
if ($LASTEXITCODE -ne 0 -or [version]$pythonVersion -lt [version]'3.12') {
    throw "The installer smoke test requires Python 3.12+, got $pythonVersion at $pythonExe."
}
$serviceName = 'RtmpMonitorAgent'
$programDir = Join-Path $env:ProgramFiles 'RTMPMonitor'
$dataDir = Join-Path $env:ProgramData 'RTMPMonitor'
$tempRoot = Join-Path $env:TEMP ('rtmp-monitor-installer-' + [guid]::NewGuid().ToString())
$tempPrefix = [System.IO.Path]::GetFullPath($env:TEMP).TrimEnd('\') + '\'
$programDirWasAbsent = -not (Test-Path -LiteralPath $programDir)
$dataDirWasAbsent = -not (Test-Path -LiteralPath $dataDir)
if (-not [System.IO.Path]::GetFullPath($tempRoot).StartsWith($tempPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to use an unexpected temporary directory: $tempRoot"
}
if (Get-Service -Name $serviceName -ErrorAction SilentlyContinue) {
    throw "Unexpected pre-existing service $serviceName on the hosted runner."
}
if (-not $programDirWasAbsent -or -not $dataDirWasAbsent) {
    throw 'Unexpected pre-existing RTMP Monitor installation directories on the hosted runner.'
}

$testBin = Join-Path $dataDir 'installer-ci-bin'
$configPath = Join-Path $tempRoot 'agent.yaml'
$logDir = Join-Path $dataDir 'logs\installer-ci'
$stateDir = Join-Path $dataDir 'data\installer-ci'
$logPath = Join-Path $logDir 'rtmp-monitor.jsonl'
$originalPath = $env:PATH

try {
    New-Item -ItemType Directory -Path $testBin -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $env:WINDIR 'System32\where.exe') -Destination (Join-Path $testBin 'ffmpeg.exe')
    Copy-Item -LiteralPath (Join-Path $env:WINDIR 'System32\where.exe') -Destination (Join-Path $testBin 'ffprobe.exe')
    $env:PATH = "$testBin;$env:PATH"

    $logDirYaml = $logDir.Replace('\', '/')
    $stateDirYaml = $stateDir.Replace('\', '/')
    $testConfig = @"
server:
  url: http://127.0.0.1:9
agent:
  name: windows-installer-ci
  location: github-actions
  role: CLIENT
  token: windows-installer-ci-token
  profile: LIGHT
streams:
  - id: windows-installer-ci
    url: rtmp://127.0.0.1:1935/live/windows-installer-ci
network:
  enabled: false
state_dir: '$stateDirYaml'
log_dir: '$logDirYaml'
"@
    Set-Content -LiteralPath $configPath -Value $testConfig -Encoding utf8

    & .\install.ps1 -ConfigPath $configPath -PythonPath $pythonExe
    if ($LASTEXITCODE -ne 0) { throw 'The production install.ps1 returned a failure exit code.' }

    & sc.exe qfailure $serviceName
    if ($LASTEXITCODE -ne 0) { throw 'Windows service recovery actions were not configured.' }
    $failureFlagOutput = (& sc.exe qfailureflag $serviceName 2>&1) -join "`n"
    if ($LASTEXITCODE -ne 0 -or $failureFlagOutput -notmatch ':\s*1') { throw "Recovery on non-crash service errors was not enabled: $failureFlagOutput" }

    $deadline = (Get-Date).AddSeconds(20)
    do {
        $service = Get-Service -Name $serviceName -ErrorAction Stop
        if ($service.Status -eq 'Running') { break }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)
    if ($service.Status -ne 'Running') {
        throw "Service did not reach Running state (current: $($service.Status))."
    }

    $installedConfig = Join-Path $dataDir 'agent.yaml'
    $installedConfigText = Get-Content -LiteralPath $installedConfig -Raw
    if ($installedConfigText -notmatch [regex]::Escape((Join-Path $testBin 'ffprobe.exe'))) {
        throw 'The installer did not persist the machine-accessible ffprobe path in the protected config.'
    }

    $deadline = (Get-Date).AddSeconds(15)
    $logText = ''
    while ($logText -notmatch 'Started LIGHT probe subprocess for stream windows-installer-ci' -and (Get-Date) -lt $deadline) {
        if (Test-Path -LiteralPath $logPath) { $logText = Get-Content -LiteralPath $logPath -Raw }
        Start-Sleep -Seconds 1
    }
    if ($logText -notmatch 'Started LIGHT probe subprocess for stream windows-installer-ci') {
        throw "Service did not launch the configured ffprobe executable. Log: $logPath"
    }
    Write-Host "Production Windows installer passed with Python $pythonVersion at $pythonExe."
} catch {
    Write-Host 'Production installer smoke-test diagnostics:'
    Write-Host $_.Exception.ToString()
    if (Test-Path -LiteralPath $logPath) { Get-Content -LiteralPath $logPath -Tail 100 }
    & sc.exe queryex $serviceName
    & sc.exe qc $serviceName
    throw
} finally {
    $env:PATH = $originalPath
    if (Get-Service -Name $serviceName -ErrorAction SilentlyContinue) {
        Stop-Service -Name $serviceName -Force -ErrorAction SilentlyContinue
        $service = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
        if ($service -and $service.Status -ne 'Stopped') {
            $service.WaitForStatus(
                [System.ServiceProcess.ServiceControllerStatus]::Stopped,
                [TimeSpan]::FromSeconds(20)
            )
        }
        & $pythonExe -m rtmp_monitor.windows_service_cli remove
        if ($LASTEXITCODE -ne 0) { & sc.exe delete $serviceName | Out-Null }
    }
    if ($programDirWasAbsent -and (Test-Path -LiteralPath $programDir)) {
        if ([System.IO.Path]::GetFullPath($programDir) -eq [System.IO.Path]::GetFullPath((Join-Path $env:ProgramFiles 'RTMPMonitor'))) {
            Remove-Item -LiteralPath $programDir -Recurse -Force
        }
    }
    if ($dataDirWasAbsent -and (Test-Path -LiteralPath $dataDir)) {
        if ([System.IO.Path]::GetFullPath($dataDir) -eq [System.IO.Path]::GetFullPath((Join-Path $env:ProgramData 'RTMPMonitor'))) {
            Remove-Item -LiteralPath $dataDir -Recurse -Force
        }
    }
    if (Test-Path -LiteralPath $tempRoot) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force
    }
}
