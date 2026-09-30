param(
    [string]$OutputDirectory = (Join-Path $PSScriptRoot '..\dist'),
    [string]$BuildPython = '',
    [string]$RuntimeVersion = '3.13.15',
    [string]$RuntimeSha256 = 'd1f04d990aee1253d8569e8e5104e30fa9f5fa830899f14843448872d936a2cf'
)
$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not $BuildPython) {
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) { $BuildPython = (& $launcher.Source -3.12 -c 'import sys; print(sys.executable)').Trim() }
    if (-not $BuildPython -or $LASTEXITCODE -ne 0) { throw 'The bundle builder needs a local Python 3.12+ build interpreter.' }
}
if (-not (Test-Path -LiteralPath $BuildPython)) { throw "Build Python not found: $BuildPython" }

$runtimeParts = $RuntimeVersion.Split('.')
if ($runtimeParts.Count -ne 3 -or $runtimeParts[0] -ne '3' -or [int]$runtimeParts[1] -lt 12) {
    throw 'The private Windows runtime must be a pinned Python 3.12+ release in major.minor.patch form.'
}
if ($RuntimeSha256 -notmatch '^[0-9a-fA-F]{64}$') { throw 'RuntimeSha256 must be the official Python archive SHA-256.' }
$pythonMinor = "$($runtimeParts[0])$($runtimeParts[1])"
$pythonAbi = "cp$pythonMinor"
$pythonVersion = "$($runtimeParts[0]).$($runtimeParts[1])"
$pythonArchiveName = "python-$RuntimeVersion-embed-amd64.zip"
$pythonArchiveUri = "https://www.python.org/ftp/python/$RuntimeVersion/$pythonArchiveName"

New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$OutputDirectory = (Resolve-Path -LiteralPath $OutputDirectory).Path
$workRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("rtmp-monitor-windows-bundle-" + [guid]::NewGuid().ToString('N'))
$bundleRoot = Join-Path $workRoot 'rtmp-monitor-agent-windows'
$wheelSource = Join-Path $workRoot 'wheel-source'
$runtimeDirectory = Join-Path $bundleRoot 'runtime'
$wheelhouse = Join-Path $workRoot 'wheelhouse'
$buildSucceeded = $false
New-Item -ItemType Directory -Force -Path $runtimeDirectory,$wheelhouse,$wheelSource,(Join-Path $bundleRoot 'scripts') | Out-Null
try {
    $pythonArchive = Join-Path $workRoot $pythonArchiveName
    Invoke-WebRequest -Uri $pythonArchiveUri -OutFile $pythonArchive
    $actualPythonHash = (Get-FileHash -LiteralPath $pythonArchive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualPythonHash -ne $RuntimeSha256.ToLowerInvariant()) {
        throw "Official Python archive SHA-256 mismatch. Expected $RuntimeSha256, received $actualPythonHash."
    }
    Expand-Archive -LiteralPath $pythonArchive -DestinationPath $runtimeDirectory

    $pythonExe = (Resolve-Path -LiteralPath $BuildPython).Path
    $hostVersionOutput = (& $pythonExe --version 2>&1 | Out-String).Trim()
    $hostVersionMatch = [regex]::Match($hostVersionOutput, '^Python\s+(\d+\.\d+(?:\.\d+)?)$')
    if ($LASTEXITCODE -ne 0 -or -not $hostVersionMatch.Success) {
        throw "Could not read the build Python version from '$pythonExe': $hostVersionOutput"
    }
    $hostVersion = $hostVersionMatch.Groups[1].Value
    if ([version]$hostVersion -lt [version]'3.12') {
        throw "Build Python 3.12+ is required; found $hostVersion at $pythonExe."
    }

    Copy-Item -LiteralPath (Join-Path $repoRoot 'pyproject.toml'),(Join-Path $repoRoot 'README.md'),(Join-Path $repoRoot 'LICENSE') -Destination $wheelSource
    Copy-Item -Recurse -Force -LiteralPath (Join-Path $repoRoot 'src') -Destination $wheelSource
    & $pythonExe -m pip wheel --no-deps --wheel-dir $wheelhouse $wheelSource
    if ($LASTEXITCODE -ne 0) { throw 'Could not build the application wheel.' }
    $requirements = @(
        'fastapi>=0.115,<1',
        'uvicorn[standard]>=0.30,<1',
        'SQLAlchemy>=2.0,<3',
        'PyYAML>=6.0,<7',
        'psutil>=6.0,<8',
        'pywin32>=306'
    )
    $downloadArgs = @(
        '-m','pip','download','--only-binary=:all:','--dest',$wheelhouse,
        '--platform','win_amd64','--python-version',$pythonVersion,
        '--implementation','cp','--abi',$pythonAbi
    ) + $requirements
    & $pythonExe @downloadArgs
    if ($LASTEXITCODE -ne 0) { throw "Could not download Windows wheels for embedded Python $RuntimeVersion." }

    $applicationWheels = @(Get-ChildItem -LiteralPath $wheelhouse -Filter 'rtmp_stream_monitor-*.whl' -File)
    if ($applicationWheels.Count -ne 1) { throw "Expected one RTMP Stream Monitor wheel; found $($applicationWheels.Count)." }
    $sitePackages = Join-Path $runtimeDirectory 'Lib\site-packages'
    New-Item -ItemType Directory -Force -Path $sitePackages | Out-Null
    $installArgs = @(
        '-m','pip','install','--target',$sitePackages,'--no-index','--find-links',$wheelhouse,
        '--only-binary=:all:','--no-compile','--platform','win_amd64',
        '--python-version',$pythonVersion,'--implementation','cp','--abi',$pythonAbi,
        $applicationWheels[0].FullName,'pywin32>=306'
    )
    & $pythonExe @installArgs
    if ($LASTEXITCODE -ne 0) { throw 'Could not install the application and dependency wheels into the embedded runtime.' }

    $serviceHost = Join-Path $sitePackages 'win32\pythonservice.exe'
    if (-not (Test-Path -LiteralPath $serviceHost)) { throw 'pywin32 did not provide its Windows service host executable.' }
    Copy-Item -LiteralPath $serviceHost -Destination $runtimeDirectory
    $pywin32Dlls = @(Get-ChildItem -LiteralPath (Join-Path $sitePackages 'pywin32_system32') -Filter 'pywintypes*.dll' -File)
    if ($pywin32Dlls.Count -ne 1) { throw "Expected one pywintypes runtime DLL; found $($pywin32Dlls.Count)." }
    Copy-Item -LiteralPath $pywin32Dlls[0].FullName -Destination $runtimeDirectory

    $pthFile = Join-Path $runtimeDirectory "python$pythonMinor._pth"
    if (-not (Test-Path -LiteralPath $pthFile)) { throw "Embedded Python did not contain $([IO.Path]::GetFileName($pthFile))." }
    @(
        "python$pythonMinor.zip",
        '.',
        'Lib\site-packages',
        'Lib\site-packages\win32',
        'Lib\site-packages\win32\lib',
        'Lib\site-packages\pythonwin',
        'import site'
    ) | Set-Content -LiteralPath $pthFile -Encoding ascii

    Copy-Item -LiteralPath (Join-Path $repoRoot 'install.ps1') -Destination $bundleRoot
    Copy-Item -LiteralPath (Join-Path $repoRoot 'uninstall.ps1') -Destination $bundleRoot
    Copy-Item -LiteralPath (Join-Path $repoRoot 'README.md') -Destination $bundleRoot
    Copy-Item -LiteralPath (Join-Path $repoRoot 'LICENSE') -Destination $bundleRoot
    Copy-Item -LiteralPath (Join-Path $repoRoot 'scripts\windows-python.ps1') -Destination (Join-Path $bundleRoot 'scripts')
    Copy-Item -LiteralPath (Join-Path $repoRoot 'scripts\windows-install-guidance.ps1') -Destination (Join-Path $bundleRoot 'scripts')
    Copy-Item -LiteralPath (Join-Path $repoRoot 'scripts\windows-ffmpeg.ps1') -Destination (Join-Path $bundleRoot 'scripts')

    $embeddedPython = Join-Path $runtimeDirectory 'python.exe'
    & $embeddedPython -c 'import fastapi, psutil, rtmp_monitor, win32event, win32serviceutil, yaml'
    if ($LASTEXITCODE -ne 0) { throw 'The embedded runtime failed its import smoke check.' }
    Write-Host 'Embedded agent runtime imports OK.'

    $manifest = [ordered]@{
        app_version = [regex]::Match((Get-Content -Raw (Join-Path $repoRoot 'pyproject.toml')), '(?m)^version\s*=\s*"([^"]+)"').Groups[1].Value
        python_version = $RuntimeVersion
        python_archive_sha256 = $actualPythonHash
        architecture = 'x64'
    }
    $manifest | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $bundleRoot 'runtime-manifest.json') -Encoding utf8

    $archiveName = 'rtmp-monitor-agent-windows.zip'
    $archivePath = Join-Path $OutputDirectory $archiveName
    if (Test-Path -LiteralPath $archivePath) { Remove-Item -LiteralPath $archivePath -Force }
    Compress-Archive -Path (Join-Path $bundleRoot '*') -DestinationPath $archivePath -CompressionLevel Optimal
    $archiveHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant()
    "$archiveHash  $archiveName" | Set-Content -LiteralPath (Join-Path $OutputDirectory "$archiveName.sha256") -Encoding ascii
    Write-Host "Built $archivePath with embedded Python $RuntimeVersion and SHA-256 checksum."
    $buildSucceeded = $true
} finally {
    if (Test-Path -LiteralPath $workRoot) {
        if ($buildSucceeded) { Remove-Item -LiteralPath $workRoot -Recurse -Force }
        else { Write-Host "Windows bundle build files preserved for diagnosis at $workRoot" }
    }
}
