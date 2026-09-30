$script:rtmpMonitorUserProfilePrefix = [System.IO.Path]::GetFullPath($env:USERPROFILE).TrimEnd('\') + '\'

function Test-RtmpMonitorMachineExecutable {
    param([string]$Path)
    if (-not $Path -or [System.IO.Path]::GetExtension($Path) -ine ".exe" -or -not (Test-Path -LiteralPath $Path)) { return $false }
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    return -not $fullPath.StartsWith($script:rtmpMonitorUserProfilePrefix, [System.StringComparison]::OrdinalIgnoreCase)
}

function Get-RtmpMonitorMachineExecutable {
    param(
        [Parameter(Mandatory)][string]$Name,
        [switch]$WinGetOnly
    )

    $programFilesX86 = [Environment]::GetFolderPath([Environment+SpecialFolder]::ProgramFilesX86)
    $candidatePaths = @(
        (Join-Path $env:ProgramFiles "WinGet\Links\$Name.exe"),
        (Join-Path $programFilesX86 "WinGet\Links\$Name.exe")
    )
    foreach ($candidatePath in $candidatePaths) {
        if (Test-RtmpMonitorMachineExecutable $candidatePath) {
            return [System.IO.Path]::GetFullPath($candidatePath)
        }
    }

    foreach ($packageRoot in @(
        (Join-Path $env:ProgramFiles 'WinGet\Packages\Gyan.FFmpeg_*'),
        (Join-Path $programFilesX86 'WinGet\Packages\Gyan.FFmpeg_*')
    )) {
        $candidate = Get-ChildItem -Path $packageRoot -Filter "$Name.exe" -File -Recurse -ErrorAction SilentlyContinue |
            Where-Object { Test-RtmpMonitorMachineExecutable $_.FullName } |
            Sort-Object FullName -Descending |
            Select-Object -First 1
        if ($candidate) { return [System.IO.Path]::GetFullPath($candidate.FullName) }
    }

    if (-not $WinGetOnly) {
        $command = Get-Command "$Name.exe" -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($command -and (Test-RtmpMonitorMachineExecutable $command.Source)) {
            return [System.IO.Path]::GetFullPath($command.Source)
        }
    }

    return $null
}
