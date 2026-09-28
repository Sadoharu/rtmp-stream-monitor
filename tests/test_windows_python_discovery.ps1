$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\scripts\windows-python.ps1')

$pythonExe = (Get-Command python -ErrorAction Stop).Source
$testRoot = Join-Path $env:PUBLIC ('rtmp-monitor-python-discovery-' + [guid]::NewGuid().ToString())
$programFilesRoot = Join-Path $testRoot 'ProgramFiles'
$pythonInstallDir = Join-Path $programFilesRoot 'PythonCI'
$registryRoot = "HKCU:\Software\RTMPMonitorPythonDiscovery-$([guid]::NewGuid().ToString())\PythonCore"
$testRootFull = [System.IO.Path]::GetFullPath($testRoot)
$publicPrefix = [System.IO.Path]::GetFullPath($env:PUBLIC).TrimEnd('\') + '\'
if (-not $testRootFull.StartsWith($publicPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to use an unexpected test directory: $testRootFull"
}

try {
    New-Item -ItemType Directory -Path $programFilesRoot -Force | Out-Null
    New-Item -ItemType Junction -Path $pythonInstallDir -Target (Split-Path -Parent $pythonExe) | Out-Null

    $programFilesPython = Resolve-RtmpMonitorPython `
        -ProgramFilesRoot $programFilesRoot `
        -RegistryRoots @() `
        -UserProfilePath $env:USERPROFILE
    $expectedPath = [System.IO.Path]::GetFullPath((Join-Path $pythonInstallDir 'python.exe'))
    if ($programFilesPython.Path -ine $expectedPath) {
        throw "Program Files discovery selected $($programFilesPython.Path), expected $expectedPath."
    }
    Write-Host "Program Files discovery: Python $($programFilesPython.Version) at $($programFilesPython.Path)"

    $installPathKey = Join-Path (Join-Path $registryRoot $programFilesPython.Version.ToString(2)) 'InstallPath'
    New-Item -ItemType Directory -Path $installPathKey -Force | Out-Null
    $registrySubPath = $installPathKey.Substring('HKCU:\'.Length)
    $writableRegistryKey = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey($registrySubPath)
    try {
        $writableRegistryKey.SetValue('', $pythonInstallDir, [Microsoft.Win32.RegistryValueKind]::String)
    } finally {
        $writableRegistryKey.Dispose()
    }
    $registryPython = Resolve-RtmpMonitorPython `
        -ProgramFilesRoot (Join-Path $testRoot 'NoProgramFiles') `
        -RegistryRoots @($registryRoot) `
        -UserProfilePath $env:USERPROFILE
    if ($registryPython.Path -ine $expectedPath) {
        throw "Registry discovery selected $($registryPython.Path), expected $expectedPath."
    }
    Write-Host "Registry discovery: Python $($registryPython.Version) at $($registryPython.Path)"

    $perUserPath = [System.IO.Path]::GetFullPath($pythonExe)
    $perUserRoot = Split-Path -Parent $perUserPath
    try {
        $null = Resolve-RtmpMonitorPython -PythonPath $perUserPath -UserProfilePath $perUserRoot
        throw 'A Python executable inside the user profile was accepted for the Windows service.'
    } catch {
        if ($_.Exception.Message -notmatch 'installed only for the current user') { throw }
    }
    Write-Host 'Per-user Python rejection: passed'

    function py {
        $global:LASTEXITCODE = 0
        Write-Output $perUserPath
    }
    try {
        $null = Resolve-RtmpMonitorPython `
            -ProgramFilesRoot (Join-Path $testRoot 'NoProgramFiles') `
            -RegistryRoots @() `
            -UserProfilePath $perUserRoot
        throw 'The Python launcher per-user fallback was accepted for the Windows service.'
    } catch {
        if ($_.Exception.Message -notmatch 'installed only for the current user') { throw }
    } finally {
        Remove-Item Function:\py -ErrorAction SilentlyContinue
    }
    Write-Host 'Per-user Python launcher fallback rejection: passed'
} finally {
    Remove-Item -LiteralPath $registryRoot -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $pythonInstallDir) {
        # Windows PowerShell 5.1 Remove-Item can throw NullReferenceException
        # when deleting a junction. Directory.Delete removes the reparse point
        # itself, without touching the Python installation it targets.
        [System.IO.Directory]::Delete($pythonInstallDir, $false)
    }
    if (Test-Path -LiteralPath $testRoot) {
        Remove-Item -LiteralPath $testRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
