param([string]$BundlePath = '')

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$originalLocation = (Get-Location).Path
$installerRoot = $repoRoot
$bundleExtractRoot = ''
$cleanupPythonExe = ''
if ($BundlePath) {
    $bundlePathResolved = (Resolve-Path -LiteralPath $BundlePath -ErrorAction Stop).Path
    $bundleExtractRoot = Join-Path $env:TEMP ('rtmp-monitor-bundle-' + [guid]::NewGuid().ToString())
    New-Item -ItemType Directory -Path $bundleExtractRoot -Force | Out-Null
    Expand-Archive -LiteralPath $bundlePathResolved -DestinationPath $bundleExtractRoot
    $installerRoot = $bundleExtractRoot
    $cleanupPythonExe = Join-Path $installerRoot 'runtime\python.exe'
    if (-not (Test-Path -LiteralPath (Join-Path $installerRoot 'install.ps1')) -or -not (Test-Path -LiteralPath $cleanupPythonExe)) {
        throw 'The Windows bundle does not contain its installer and private Python runtime.'
    }
} else {
    $pythonExe = (Get-Command python -ErrorAction Stop).Source
    $pythonVersion = (& $pythonExe -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')").Trim()
    if ($LASTEXITCODE -ne 0 -or [version]$pythonVersion -lt [version]'3.12') {
        throw "The installer smoke test requires Python 3.12+, got $pythonVersion at $pythonExe."
    }
    $cleanupPythonExe = $pythonExe
}
Set-Location -LiteralPath $installerRoot
$serviceName = 'RtmpMonitorAgent'
$programDir = Join-Path $env:ProgramFiles 'RTMPMonitor'
$dataDir = Join-Path $env:ProgramData 'RTMPMonitor'
$pythonAlias = Join-Path $env:ProgramFiles ('PythonRTMPMonitorCI-' + [guid]::NewGuid().ToString())
$pythonAliasCreated = $false
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
    New-Item -ItemType Directory -Path $tempRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $testBin -Force | Out-Null
    if (-not $BundlePath) {
        New-Item -ItemType Junction -Path $pythonAlias -Target (Split-Path -Parent $pythonExe) | Out-Null
        $pythonAliasCreated = $true
        . (Join-Path $repoRoot 'scripts\windows-python.ps1')
        function py {
            throw 'The production installer must not fall back to the per-user Python launcher when a machine Python is present.'
        }
        $resolvedPython = Resolve-RtmpMonitorPython
        $pythonExe = $resolvedPython.Path
        $pythonVersion = $resolvedPython.Version.ToString(2)
    }

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

    $installerOutput = (& (Join-Path $installerRoot 'install.ps1') -ConfigPath $configPath *>&1 | Out-String)
    if ($LASTEXITCODE -ne 0) { throw 'The production install.ps1 returned a failure exit code.' }
    Write-Host $installerOutput
    if ($BundlePath) {
        if ($installerOutput -notmatch 'Using bundled Python 3\.(12|13|14)\.\d+ at ') {
            throw 'Production install.ps1 did not use its bundled Python runtime.'
        }
        $cleanupPythonExe = Join-Path $programDir 'runtime\python.exe'
        if (-not (Test-Path -LiteralPath (Join-Path $programDir 'runtime\pythonservice.exe'))) {
            throw 'The installer did not place pywin32 service host beside the private Python runtime.'
        }
        $runtimeImport = & $cleanupPythonExe -c 'import rtmp_monitor, win32event, win32serviceutil; print("Bundled agent imports OK")'
        if ($LASTEXITCODE -ne 0) { throw "The installed private runtime cannot import the service dependencies: $runtimeImport" }
    } elseif ($installerOutput -notmatch [regex]::Escape("Using Python $pythonVersion at $pythonExe")) {
        throw "Production install.ps1 did not use the auto-discovered machine-wide Python $pythonExe."
    }

    & sc.exe qfailure $serviceName
    if ($LASTEXITCODE -ne 0) { throw 'Windows service recovery actions were not configured.' }
    $failureFlagOutput = (& sc.exe qfailureflag $serviceName 2>&1) -join "`n"
    if ($LASTEXITCODE -ne 0 -or $failureFlagOutput -notmatch ':\s*(1|TRUE)\b') { throw "Recovery on non-crash service errors was not enabled: $failureFlagOutput" }

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

    if ($service) { $service.Dispose(); $service = $null }
    $uninstallOutput = (& (Join-Path $installerRoot 'uninstall.ps1') -Force *>&1 | Out-String)
    Write-Host $uninstallOutput
    if (Test-Path -LiteralPath $programDir) { throw 'Windows uninstall left the RTMP Monitor program directory.' }
    if (Test-Path -LiteralPath $dataDir) { throw 'Windows uninstall left local config, queue, or log files.' }
    $deleteDeadline = (Get-Date).AddSeconds(15)
    do {
        & sc.exe query $serviceName 2>$null | Out-Null
        $serviceQueryExitCode = $LASTEXITCODE
        if ($serviceQueryExitCode -eq 1060) { break }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deleteDeadline)
    if ($serviceQueryExitCode -ne 1060) {
        throw "Windows uninstall did not fully remove the service (sc.exe query exit code $serviceQueryExitCode)."
    }
    $global:LASTEXITCODE = 0
    if ($BundlePath) {
        Write-Host 'Bundled Windows installer and uninstall passed without requiring a system Python installation.'
    } else {
        Write-Host "Production Windows installer and uninstall passed with Python $pythonVersion at $pythonExe."
    }
} catch {
    Write-Host 'Production installer smoke-test diagnostics:'
    Write-Host $_.Exception.ToString()
    if (Test-Path -LiteralPath $logPath) { Get-Content -LiteralPath $logPath -Tail 100 }
    & sc.exe queryex $serviceName
    & sc.exe qc $serviceName
    throw
} finally {
    $env:PATH = $originalPath
    Set-Location -LiteralPath $originalLocation
    Remove-Item Function:\py -ErrorAction SilentlyContinue
    if (Get-Service -Name $serviceName -ErrorAction SilentlyContinue) {
        Stop-Service -Name $serviceName -Force -ErrorAction SilentlyContinue
        $service = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
        if ($service -and $service.Status -ne 'Stopped') {
            $service.WaitForStatus(
                [System.ServiceProcess.ServiceControllerStatus]::Stopped,
                [TimeSpan]::FromSeconds(20)
            )
        }
        if ($cleanupPythonExe -and (Test-Path -LiteralPath $cleanupPythonExe)) {
            & $cleanupPythonExe -m rtmp_monitor.windows_service_cli remove
            if ($LASTEXITCODE -ne 0) { & sc.exe delete $serviceName | Out-Null }
        } else {
            & sc.exe delete $serviceName | Out-Null
        }
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
    if ($pythonAliasCreated -and (Test-Path -LiteralPath $pythonAlias)) {
        if ([System.IO.Path]::GetFullPath($pythonAlias).StartsWith([System.IO.Path]::GetFullPath($env:ProgramFiles).TrimEnd('\') + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
            [System.IO.Directory]::Delete($pythonAlias, $false)
        }
    }
    if (Test-Path -LiteralPath $tempRoot) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force
    }
    if ($bundleExtractRoot -and (Test-Path -LiteralPath $bundleExtractRoot)) {
        Remove-Item -LiteralPath $bundleExtractRoot -Recurse -Force
    }
}
