param([string]$BundlePath = '', [switch]$UseInstalledFfmpeg)

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

. (Join-Path $repoRoot 'scripts\windows-ffmpeg.ps1')

$testBin = Join-Path $dataDir 'installer-ci-bin'
$configPath = Join-Path $tempRoot 'agent.yaml'
$logDir = Join-Path $dataDir 'logs\installer-ci'
$stateDir = Join-Path $dataDir 'data\installer-ci'
$logPath = Join-Path $logDir 'rtmp-monitor.jsonl'
$originalPath = $env:PATH
$expectedFfmpegPath = Join-Path $testBin 'ffmpeg.exe'
$expectedFfprobePath = Join-Path $testBin 'ffprobe.exe'
$rtmpListener = $null
$rtmpAcceptTask = $null
$rtmpClient = $null
$rtmpStream = $null
$rtmpPort = 1935

function Read-ExactTcpBytes {
    param(
        [Parameter(Mandatory = $true)][System.IO.Stream]$Stream,
        [Parameter(Mandatory = $true)][int]$Count
    )

    $buffer = [byte[]]::new($Count)
    $offset = 0
    while ($offset -lt $Count) {
        $read = $Stream.Read($buffer, $offset, $Count - $offset)
        if ($read -le 0) { throw "RTMP client closed its TCP connection after $offset of $Count handshake bytes." }
        $offset += $read
    }
    return ,$buffer
}

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

    if ($UseInstalledFfmpeg) {
        foreach ($toolName in @('ffmpeg', 'ffprobe')) {
            $toolPath = Get-RtmpMonitorMachineExecutable -Name $toolName -WinGetOnly
            if (-not $toolPath) {
                $winget = Get-Command winget.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
                if ($winget) { & $winget.Source --info 2>&1 | Write-Host }
                $programFilesX86 = [Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFilesX86)
                foreach ($root in @(
                    (Join-Path $env:ProgramFiles 'WinGet'),
                    (Join-Path $programFilesX86 'WinGet'),
                    (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet')
                )) {
                    if (Test-Path -LiteralPath $root) {
                        Get-ChildItem -LiteralPath $root -Force | Select-Object FullName,Attributes | Format-Table -AutoSize | Out-String | Write-Host
                    }
                }
                throw "WinGet did not expose machine-accessible $toolName.exe through PATH, WinGet Links, or the portable package directory."
            }
            $toolOutput = & $toolPath -version 2>&1
            $toolExitCode = $LASTEXITCODE
            if ($toolExitCode -ne 0) { throw "Installed $toolName.exe did not run successfully at $toolPath (exit code $toolExitCode)." }
            $toolOutput | Select-Object -First 1 | Write-Host
            if ($toolName -eq 'ffmpeg') { $expectedFfmpegPath = $toolPath }
            if ($toolName -eq 'ffprobe') { $expectedFfprobePath = $toolPath }
        }
    } else {
        Copy-Item -LiteralPath (Join-Path $env:WINDIR 'System32\where.exe') -Destination (Join-Path $testBin 'ffmpeg.exe')
        Copy-Item -LiteralPath (Join-Path $env:WINDIR 'System32\where.exe') -Destination (Join-Path $testBin 'ffprobe.exe')
    }
    $env:PATH = "$testBin;$env:PATH"

    if ($UseInstalledFfmpeg) {
        $rtmpListener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
        $rtmpListener.Start()
        $rtmpPort = [int]$rtmpListener.LocalEndpoint.Port
        $rtmpAcceptTask = $rtmpListener.AcceptTcpClientAsync()
    }

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
    url: rtmp://127.0.0.1:$rtmpPort/live/windows-installer-ci
monitoring:
  dead_threshold: 120
network:
  enabled: $($UseInstalledFfmpeg.ToString().ToLowerInvariant())
  server_host: 127.0.0.1
  server_port: $rtmpPort
  ping_interval: 2
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
        $runtimeImportOutput = (& $cleanupPythonExe -c 'import rtmp_monitor, win32event, win32serviceutil' 2>&1 | Out-String)
        if ($LASTEXITCODE -ne 0) { throw "The installed private runtime cannot import the service dependencies: $runtimeImportOutput" }
        Write-Host 'Bundled agent imports OK.'
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
    foreach ($expectedToolPath in @($expectedFfmpegPath, $expectedFfprobePath)) {
        if ($installedConfigText -notmatch [regex]::Escape($expectedToolPath)) {
            throw "The installer did not persist the expected machine-accessible FFmpeg path: $expectedToolPath."
        }
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

    if ($UseInstalledFfmpeg) {
        if (-not $rtmpAcceptTask.Wait(10000)) {
            throw 'The LocalSystem Windows service did not connect ffprobe to the loopback RTMP TCP sink.'
        }
        $rtmpClient = $rtmpAcceptTask.GetAwaiter().GetResult()
        if (-not $rtmpClient.Connected) {
            throw 'The LocalSystem ffprobe TCP connection was not established.'
        }

        # Complete the RTMP simple handshake so ffprobe leaves its TCP flow open
        # while the service collects more than one receiver EStats interval.
        $rtmpStream = $rtmpClient.GetStream()
        $rtmpStream.ReadTimeout = 10000
        $clientHandshake = Read-ExactTcpBytes -Stream $rtmpStream -Count 1537
        if ($clientHandshake[0] -ne 3) {
            throw "Unsupported RTMP client version $($clientHandshake[0]); expected version 3."
        }
        $serverHandshake = [byte[]]::new(3073)
        $serverHandshake[0] = 3
        $serverTimestamp = [System.Net.IPAddress]::HostToNetworkOrder([int][DateTimeOffset]::UtcNow.ToUnixTimeSeconds())
        [Array]::Copy([BitConverter]::GetBytes($serverTimestamp), 0, $serverHandshake, 1, 4)
        $serverRandom = [byte[]]::new(1528)
        $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
        try { $rng.GetBytes($serverRandom) } finally { $rng.Dispose() }
        [Array]::Copy($serverRandom, 0, $serverHandshake, 9, $serverRandom.Length)
        [Array]::Copy($clientHandshake, 1, $serverHandshake, 1537, 1536)
        $rtmpStream.Write($serverHandshake, 0, $serverHandshake.Length)
        $rtmpStream.Flush()
        $null = Read-ExactTcpBytes -Stream $rtmpStream -Count 1536

        $queuePath = Join-Path $stateDir 'agent_queue.db'
        $queryOutbox = @'
import json
import sqlite3
import sys

with sqlite3.connect(sys.argv[1], timeout=3) as database:
    rows = database.execute("SELECT payload FROM outbox ORDER BY id DESC LIMIT 100").fetchall()
samples = []
for (raw,) in rows:
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        continue
    network = payload.get("metrics", {}).get("network", {})
    samples.append({
        "status": network.get("tcp_receiver_stats_status"),
        "flow_count": network.get("tcp_receiver_stats_flow_count", 0),
        "duplicate_ack_episodes": network.get("tcp_duplicate_ack_episodes"),
    })
measured = next((sample for sample in samples if sample["status"] == "AVAILABLE" and sample["flow_count"] > 0 and type(sample["duplicate_ack_episodes"]) is int), None)
print(json.dumps(measured or {"status": "NOT_AVAILABLE", "samples": samples[:10]}))
'@
        $queryScriptPath = Join-Path $tempRoot 'read-agent-network.py'
        Set-Content -LiteralPath $queryScriptPath -Value $queryOutbox -Encoding utf8
        $statsAvailable = $false
        $lastStats = ''
        $deadline = (Get-Date).AddSeconds(35)
        while ((Get-Date) -lt $deadline) {
            if (Test-Path -LiteralPath $queuePath) {
                $lastStats = (& $cleanupPythonExe $queryScriptPath $queuePath | Out-String).Trim()
                if ($LASTEXITCODE -ne 0) { throw 'Could not read the Windows agent telemetry outbox.' }
                try { $stats = $lastStats | ConvertFrom-Json } catch { throw "Invalid network telemetry from the service outbox: $lastStats" }
                if ($stats.status -eq 'AVAILABLE' -and [int]$stats.flow_count -gt 0) {
                    $statsAvailable = $true
                    break
                }
            }
            Start-Sleep -Seconds 1
        }
        if (-not $statsAvailable) {
            throw "Installed LocalSystem service did not publish a numeric receiver EStats interval for its live ffprobe flow. Last outbox query: $lastStats"
        }
        Write-Host "Installed LocalSystem service published duplicate_ack_episodes=$($stats.duplicate_ack_episodes) for $($stats.flow_count) live probe-owned TCP flow(s)."
    }

    if ($BundlePath) {
        $probeStartMessage = 'Started LIGHT probe subprocess for stream windows-installer-ci'
        $probeStartCountBeforeUpgrade = [regex]::Matches($logText, [regex]::Escape($probeStartMessage)).Count
        $configBeforeUpgrade = Get-Content -LiteralPath $installedConfig -Raw
        $preservationMarker = Join-Path $dataDir 'upgrade-preservation-marker.txt'
        Set-Content -LiteralPath $preservationMarker -Value 'preserve-local-data' -Encoding ascii

        $upgradeOutput = (& (Join-Path $installerRoot 'install.ps1') -ConfigPath $configPath *>&1 | Out-String)
        if ($LASTEXITCODE -ne 0) { throw 'Running the bundled installer over an existing installation failed.' }
        Write-Host $upgradeOutput
        if ($upgradeOutput -notmatch 'Preserving existing agent config') {
            throw 'The bundled installer did not preserve the existing agent configuration during upgrade.'
        }
        if (-not (Test-Path -LiteralPath $preservationMarker)) {
            throw 'The bundled installer removed local data during upgrade.'
        }
        $configAfterUpgrade = Get-Content -LiteralPath $installedConfig -Raw
        if ($configAfterUpgrade -ne $configBeforeUpgrade) {
            throw 'The bundled installer changed the existing agent configuration during upgrade.'
        }

        $deadline = (Get-Date).AddSeconds(20)
        do {
            $service = Get-Service -Name $serviceName -ErrorAction Stop
            if ($service.Status -eq 'Running') { break }
            Start-Sleep -Seconds 1
        } while ((Get-Date) -lt $deadline)
        if ($service.Status -ne 'Running') {
            throw "Upgraded service did not reach Running state (current: $($service.Status))."
        }

        $deadline = (Get-Date).AddSeconds(15)
        do {
            if (Test-Path -LiteralPath $logPath) { $logText = Get-Content -LiteralPath $logPath -Raw }
            $probeStartCountAfterUpgrade = [regex]::Matches($logText, [regex]::Escape($probeStartMessage)).Count
            if ($probeStartCountAfterUpgrade -gt $probeStartCountBeforeUpgrade) { break }
            Start-Sleep -Seconds 1
        } while ((Get-Date) -lt $deadline)
        if ($probeStartCountAfterUpgrade -le $probeStartCountBeforeUpgrade) {
            throw 'The upgraded Windows service did not relaunch its configured ffprobe probe.'
        }
        Write-Host 'Bundled installer upgrade preserved config/data and restarted the probe service.'
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
    if ($UseInstalledFfmpeg) {
        Write-Host 'Production installer resolved machine-wide WinGet FFmpeg and launched ffprobe from that package.'
    }
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
    if ($rtmpStream) { $rtmpStream.Dispose() }
    if ($rtmpClient) { $rtmpClient.Dispose() }
    if ($rtmpListener) { $rtmpListener.Stop() }
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
