function Resolve-RtmpMonitorPython {
    [CmdletBinding()]
    param(
        [string]$PythonPath = "",
        [string]$ProgramFilesRoot = $env:ProgramFiles,
        [string[]]$RegistryRoots = @('HKLM:\SOFTWARE\Python\PythonCore'),
        [string]$UserProfilePath = $env:USERPROFILE
    )

    $pythonExe = ""
    if ($PythonPath) {
        $resolvedPython = Resolve-Path -LiteralPath $PythonPath -ErrorAction SilentlyContinue
        if (-not $resolvedPython) { throw "Python executable not found at $PythonPath." }
        $pythonExe = $resolvedPython.Path
    } else {
        $machinePythonCandidates = @()
        foreach ($registryRoot in $RegistryRoots) {
            if (Test-Path -LiteralPath $registryRoot) {
                Get-ChildItem -LiteralPath $registryRoot -ErrorAction SilentlyContinue | ForEach-Object {
                    $installPathKey = Join-Path $_.PSPath 'InstallPath'
                    if (Test-Path -LiteralPath $installPathKey) {
                        $installDir = (Get-Item -LiteralPath $installPathKey).GetValue('')
                        if ($installDir) { $machinePythonCandidates += (Join-Path $installDir 'python.exe') }
                    }
                }
            }
        }
        if ($ProgramFilesRoot) {
            Get-ChildItem -Path (Join-Path $ProgramFilesRoot 'Python*') -Directory -ErrorAction SilentlyContinue | ForEach-Object {
                $machinePythonCandidates += (Join-Path $_.FullName 'python.exe')
            }
        }
        $machinePythonCandidates = @($machinePythonCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -Unique)
        $validMachinePythons = foreach ($candidate in $machinePythonCandidates) {
            $candidateOutput = & $candidate -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
            $candidateExitCode = $LASTEXITCODE
            $candidateVersion = ($candidateOutput | Out-String).Trim()
            if ($candidateExitCode -eq 0 -and $candidateVersion -match '^\d+\.\d+$' -and [version]$candidateVersion -ge [version]'3.12') {
                [pscustomobject]@{ Path = $candidate; Version = [version]$candidateVersion }
            }
        }
        $selectedMachinePython = $validMachinePythons | Sort-Object Version -Descending | Select-Object -First 1
        if ($selectedMachinePython) {
            $pythonExe = $selectedMachinePython.Path
        } else {
            $py = Get-Command py -ErrorAction SilentlyContinue
            if (-not $py) { throw "Python 3.12+ was not found. Install it for all users and retry, or pass -PythonPath with the full path to python.exe." }
            $pythonExe = (& py -3 -c "import sys; print(sys.executable)").Trim()
            if ($LASTEXITCODE -ne 0) { throw "Python 3.12+ was not found. Install it and retry." }
        }
    }

    $pythonVersion = (& $pythonExe -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $pythonVersion -notmatch '^\d+\.\d+$' -or [version]$pythonVersion -lt [version]'3.12') {
        throw "Python 3.12+ was not found at $pythonExe."
    }
    $pythonExe = [System.IO.Path]::GetFullPath($pythonExe)
    if ($UserProfilePath) {
        $userProfilePrefix = [System.IO.Path]::GetFullPath($UserProfilePath).TrimEnd('\') + '\'
        if ($pythonExe.StartsWith($userProfilePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Python is installed only for the current user at $pythonExe. Install Python 3.12+ for all users, or pass -PythonPath with a machine-accessible path outside the user profile. The Windows service runs as LocalSystem and cannot reliably use a user-profile Python installation."
        }
    }
    return [pscustomobject]@{ Path = $pythonExe; Version = [version]$pythonVersion }
}
