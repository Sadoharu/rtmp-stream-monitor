$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\scripts\windows-install-guidance.ps1')

$pythonMessage = Get-RtmpMonitorWinGetUnavailableMessage -PackageId 'Python.Python.3.12'
if ($pythonMessage -notmatch 'Python 3\.12 machine-wide' -or $pythonMessage -match 'FFmpeg') {
    throw "Python dependency guidance is incorrect: $pythonMessage"
}

$ffmpegMessage = Get-RtmpMonitorWinGetUnavailableMessage -PackageId 'Gyan.FFmpeg'
if ($ffmpegMessage -notmatch 'Gyan\.FFmpeg machine-wide' -or
    $ffmpegMessage -notmatch 'ffmpeg\.exe' -or
    $ffmpegMessage -notmatch 'ffprobe\.exe' -or
    $ffmpegMessage -match 'Python 3\.12') {
    throw "FFmpeg dependency guidance is incorrect: $ffmpegMessage"
}

Write-Host 'WinGet-missing dependency guidance: passed.'
