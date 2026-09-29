param(
    [Parameter(Mandatory = $true)]
    [string]$StreamUrl,
    [ValidateRange(3, 60)]
    [int]$Seconds = 8
)

$ErrorActionPreference = "Stop"
$ffprobe = (Get-Command ffprobe -ErrorAction Stop).Source
$ffmpeg = (Get-Command ffmpeg -ErrorAction Stop).Source

function Invoke-Measurement([string]$Name, [string]$Executable, [string[]]$Arguments, [string]$Mode) {
    $stdout = [IO.Path]::GetTempFileName()
    $stderr = [IO.Path]::GetTempFileName()
    $watch = [Diagnostics.Stopwatch]::StartNew()
    $process = Start-Process -FilePath $Executable -ArgumentList $Arguments -PassThru -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    $peakWorkingSet = [long]0
    while (-not $process.HasExited) {
        $process.Refresh()
        $peakWorkingSet = [Math]::Max($peakWorkingSet, $process.WorkingSet64)
        Start-Sleep -Milliseconds 100
    }
    $process.WaitForExit()
    $watch.Stop()
    $process.Refresh()

    $packetSizes = [Collections.Generic.List[long]]::new()
    foreach ($line in [IO.File]::ReadLines($stdout)) {
        if ($Mode -eq "light" -and $line -match '(?:^|\|)size=(\d+)(?:\||$)') {
            $packetSizes.Add([long]$Matches[1])
        }
        elseif ($Mode -eq "deep-packets" -and $line -match '^\s*\d+,\s*-?\d+,\s*-?\d+,\s*-?\d+,\s*(\d+),') {
            $packetSizes.Add([long]$Matches[1])
        }
    }

    $totalBytes = ($packetSizes | Measure-Object -Sum).Sum
    if ($null -eq $totalBytes) { $totalBytes = 0 }
    $seconds = [Math]::Max($watch.Elapsed.TotalSeconds, 0.01)
    $singleCoreCpuPct = 100 * $process.TotalProcessorTime.TotalSeconds / $seconds
    $progressPath = if ($Name -eq "DEEP / current") { $stdout } else { $stderr }
    $progress = [IO.File]::ReadLines($progressPath) | Where-Object { $_ -match '^bitrate=' } | Select-Object -Last 1
    $result = [pscustomobject]@{
        mode = $Name
        wall_seconds = [Math]::Round($seconds, 2)
        packets = $packetSizes.Count
        packet_bytes = [long]$totalBytes
        mean_media_mbps = $(if ($packetSizes.Count -gt 0) { [Math]::Round(($totalBytes * 8 / $seconds) / 1000000, 2) } else { $null })
        cpu_single_core_percent = [Math]::Round($singleCoreCpuPct, 1)
        cpu_all_cores_percent = [Math]::Round($singleCoreCpuPct / [Environment]::ProcessorCount, 2)
        peak_rss_mb = [Math]::Round($peakWorkingSet / 1MB, 1)
        exit_code = $process.ExitCode
        progress_bitrate = $progress
    }
    Remove-Item -LiteralPath $stdout, $stderr -Force -ErrorAction SilentlyContinue
    return $result
}

$light = @(
    "-hide_banner", "-v", "error", "-rw_timeout", "15000000",
    "-read_intervals", "%+$Seconds", "-show_packets",
    "-show_entries", "packet=stream_index,size", "-of", "compact=p=0:nk=0", $StreamUrl
)
$deepCurrent = @(
    "-hide_banner", "-nostats", "-loglevel", "warning", "-progress", "pipe:1",
    "-stats_period", "1", "-rw_timeout", "15000000", "-i", $StreamUrl,
    "-t", "$Seconds", "-map", "0:v?", "-map", "0:a?", "-vf", "showinfo",
    "-af", "ashowinfo", "-f", "null", "-"
)
$deepPacketTap = @(
    "-hide_banner", "-nostats", "-loglevel", "warning", "-progress", "pipe:2",
    "-stats_period", "1", "-rw_timeout", "15000000", "-i", $StreamUrl,
    "-t", "$Seconds", "-map", "0:v?", "-map", "0:a?", "-vf", "showinfo",
    "-af", "ashowinfo", "-f", "null", "-", "-t", "$Seconds", "-map", "0:v?",
    "-map", "0:a?", "-c", "copy", "-f", "framecrc", "-hash", "crc32", "pipe:1"
)

Invoke-Measurement "LIGHT / ffprobe" $ffprobe $light "light"
Invoke-Measurement "DEEP / current" $ffmpeg $deepCurrent "no-packets"
Invoke-Measurement "DEEP / shared demux + framecrc" $ffmpeg $deepPacketTap "deep-packets"
