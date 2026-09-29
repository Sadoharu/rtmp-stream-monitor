param(
    [ValidateRange(10, 180)]
    [int]$Seconds = 45,
    [ValidateRange(5, 120)]
    [int]$DipStartSeconds = 20,
    [ValidateRange(2, 30)]
    [int]$DipDurationSeconds = 6
)

$ErrorActionPreference = "Stop"
$dipEndSeconds = $DipStartSeconds + $DipDurationSeconds
if ($dipEndSeconds -ge $Seconds) {
    throw "The blackout must end before the publish duration."
}

$ffmpeg = (Get-Command ffmpeg -ErrorAction Stop).Source
$rtmpUrl = "rtmp://127.0.0.1:19350/live/m0-fault-fixture"
$filter = "drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill:enable='between(t,$DipStartSeconds,$dipEndSeconds)'"
$arguments = @(
    "-hide_banner", "-loglevel", "warning",
    "-re", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=50",
    "-re", "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000",
    "-filter:v", $filter,
    "-map", "0:v:0", "-map", "1:a:0",
    "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-pix_fmt", "yuv420p",
    "-b:v", "4000k", "-maxrate", "4000k", "-bufsize", "2000k",
    "-g", "50", "-keyint_min", "50", "-sc_threshold", "0",
    "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
    "-t", "$Seconds", "-f", "flv", $rtmpUrl
)

Write-Output "Publishing a local 720p50 RTMP fixture to $rtmpUrl for $Seconds seconds."
Write-Output "Video blackout: $DipStartSeconds-$dipEndSeconds seconds; the audio tone continues."
& $ffmpeg @arguments
exit $LASTEXITCODE
