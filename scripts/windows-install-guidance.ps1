function Get-RtmpMonitorWinGetUnavailableMessage {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$PackageId)

    switch ($PackageId) {
        'Python.Python.3.12' {
            return 'winget.exe was not found. Install Python 3.12 machine-wide, then rerun; or install the Windows Package Manager and retry.'
        }
        'Gyan.FFmpeg' {
            return 'winget.exe was not found. Install Gyan.FFmpeg machine-wide so the service can access ffmpeg.exe and ffprobe.exe, then rerun; or install the Windows Package Manager and retry.'
        }
        default {
            return "winget.exe was not found. Install $PackageId machine-wide, then rerun; or install the Windows Package Manager and retry."
        }
    }
}
