param(
    [string]$ConfigPath = ".\config\agent.yaml",
    [string]$PythonPath = "",
    [switch]$ReplaceConfig
)
$ErrorActionPreference = "Stop"
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw "Run PowerShell as Administrator and retry .\install.ps1" }
$configPathWasExplicit = $PSBoundParameters.ContainsKey("ConfigPath")
$resolvedConfig = Resolve-Path -LiteralPath $ConfigPath -ErrorAction SilentlyContinue
if (($configPathWasExplicit -or $ReplaceConfig) -and -not $resolvedConfig) { throw "Agent config not found at $ConfigPath." }
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) { throw "FFmpeg is required. Install it with winget (winget install Gyan.FFmpeg) or add ffmpeg.exe to PATH, then retry." }
if (-not (Get-Command ffprobe -ErrorAction SilentlyContinue)) { throw "ffprobe is required and normally ships with FFmpeg. Add it to PATH, then retry." }
$pythonExe = ""
if ($PythonPath) {
    $resolvedPython = Resolve-Path -LiteralPath $PythonPath -ErrorAction SilentlyContinue
    if (-not $resolvedPython) { throw "Python executable not found at $PythonPath." }
    $pythonExe = $resolvedPython.Path
} else {
    $machinePythonCandidates = @()
    $registryRoots = @('HKLM:\SOFTWARE\Python\PythonCore')
    foreach ($registryRoot in $registryRoots) {
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
    if ($env:ProgramFiles) {
        Get-ChildItem -Path (Join-Path $env:ProgramFiles 'Python*') -Directory -ErrorAction SilentlyContinue | ForEach-Object {
            $machinePythonCandidates += (Join-Path $_.FullName 'python.exe')
        }
    }
    $machinePythonCandidates = @($machinePythonCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -Unique)
    $validMachinePythons = foreach ($candidate in $machinePythonCandidates) {
        $candidateVersion = (& $candidate -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null).Trim()
        if ($LASTEXITCODE -eq 0 -and $candidateVersion -match '^\d+\.\d+$' -and [version]$candidateVersion -ge [version]'3.12') {
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
$pythonVersion = (& $pythonExe -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>&1).Trim()
if ($LASTEXITCODE -ne 0 -or [version]$pythonVersion -lt [version]"3.12") { throw "Python 3.12+ was not found at $pythonExe." }
$userProfilePrefix = [System.IO.Path]::GetFullPath($env:USERPROFILE).TrimEnd('\') + '\'
if ([System.IO.Path]::GetFullPath($pythonExe).StartsWith($userProfilePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Python is installed only for the current user at $pythonExe. Install Python 3.12+ for all users, or pass its machine-wide executable path with -PythonPath. The Windows service runs as LocalSystem and cannot reliably use a user-profile Python installation."
}
Write-Host "Using Python $pythonVersion at $pythonExe"
$repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$programDir = Join-Path $env:ProgramFiles "RTMPMonitor"
$dataDir = Join-Path $env:ProgramData "RTMPMonitor"
$configDir = Join-Path $dataDir "config"
$logDir = Join-Path $dataDir "logs"
New-Item -ItemType Directory -Force -Path $programDir,$configDir,$logDir,(Join-Path $dataDir "data") | Out-Null
$venv = Join-Path $programDir ".venv"
$venvPython = Join-Path $venv "Scripts\python.exe"
$installedConfig = Join-Path $dataDir "agent.yaml"
$installedConfigExists = Test-Path -LiteralPath $installedConfig
if (-not $installedConfigExists -and -not $resolvedConfig) { throw "Agent config not found at $ConfigPath. Provide the Dashboard-generated YAML with -ConfigPath for the first installation." }
if ($ReplaceConfig -and -not $resolvedConfig) { throw "-ReplaceConfig requires a valid -ConfigPath." }
$env:RTMP_MONITOR_CONFIG = $installedConfig
if (Get-Service -Name RtmpMonitorAgent -ErrorAction SilentlyContinue) {
    Stop-Service -Name RtmpMonitorAgent -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $venvPython) {
        # Use the legacy-compatible module here because the existing venv may
        # predate windows_service_cli. The class is registered with a stable
        # package path in both entry points.
        & $venvPython -m rtmp_monitor.windows_service remove
        if ($LASTEXITCODE -ne 0) { throw "Could not remove the previous RtmpMonitorAgent service." }
    }
}
$sourceDir = Join-Path $repo "src"
$installedSourceDir = Join-Path $programDir "src"
$stagedSourceDir = Join-Path $programDir ".src-staging"
if (Test-Path -LiteralPath $stagedSourceDir) { Remove-Item -LiteralPath $stagedSourceDir -Recurse -Force }
Copy-Item -Recurse -Force $sourceDir $stagedSourceDir
if (Test-Path -LiteralPath $installedSourceDir) { Remove-Item -LiteralPath $installedSourceDir -Recurse -Force }
Move-Item -LiteralPath $stagedSourceDir -Destination $installedSourceDir
Copy-Item -Force (Join-Path $repo "pyproject.toml") $programDir
& $pythonExe -m venv --clear $venv
if ($LASTEXITCODE -ne 0) { throw "Failed to create the Python virtual environment." }
& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Failed to upgrade pip in the virtual environment." }
& $venvPython -m pip install $programDir
if ($LASTEXITCODE -ne 0) { throw "Failed to install RTMP Monitor in the virtual environment." }
if (-not $installedConfigExists -or $ReplaceConfig) {
    Copy-Item -Force $resolvedConfig.Path $installedConfig
} elseif ($resolvedConfig) {
    Write-Host "Preserving existing agent config. Use -ReplaceConfig to install the YAML from $ConfigPath."
}
$configAcl = New-Object System.Security.AccessControl.FileSecurity
$configAcl.SetAccessRuleProtection($true, $false)
$none = [System.Security.AccessControl.InheritanceFlags]::None
$noProp = [System.Security.AccessControl.PropagationFlags]::None
$allow = [System.Security.AccessControl.AccessControlType]::Allow
$configAcl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule("SYSTEM","FullControl",$none,$noProp,$allow)))
$configAcl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule("Administrators","FullControl",$none,$noProp,$allow)))
Set-Acl -LiteralPath $installedConfig -AclObject $configAcl
& $pythonExe -m pip install --upgrade "pywin32>=306"
if ($LASTEXITCODE -ne 0) { throw "Failed to install pywin32 into the machine-wide Python environment." }
& $pythonExe -m win32.scripts.pywin32_postinstall -install -quiet
if ($LASTEXITCODE -ne 0) { throw "pywin32 machine-wide post-install setup failed." }
& $venvPython -m pip install "pywin32>=306"
if ($LASTEXITCODE -ne 0) { throw "Failed to install pywin32 in the agent virtual environment." }
& $venvPython -m rtmp_monitor.windows_service_cli --startup auto install
if ($LASTEXITCODE -ne 0) { throw "Failed to install the RtmpMonitorAgent Windows service." }
Start-Service -Name RtmpMonitorAgent
Write-Host "RTMP Monitor Agent service installed and started. Logs: $logDir"
