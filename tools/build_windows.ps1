[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $Root '.build-venv'
$Python = Join-Path $Venv 'Scripts\python.exe'

Set-Location $Root

if (-not (Test-Path -LiteralPath $Python)) {
    Write-Host '[1/5] Creating isolated build environment...'
    & py -3.13 -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw 'Unable to create the Python build environment.' }
}

Write-Host '[2/5] Installing build dependencies...'
& $Python -m pip install --disable-pip-version-check -r (Join-Path $Root 'requirements-build.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }

Write-Host '[3/5] Installing bundled headless Chromium...'
$env:PLAYWRIGHT_BROWSERS_PATH = '0'
& $Python -m playwright install --only-shell chromium
if ($LASTEXITCODE -ne 0) { throw 'Playwright browser installation failed.' }

Write-Host '[4/5] Locating ffmpeg and ffprobe...'
$Ffmpeg = Get-Command ffmpeg.exe -ErrorAction SilentlyContinue
if (-not $Ffmpeg) {
    $KnownFfmpeg = Join-Path $HOME 'AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe'
    $FfmpegPath = Get-ChildItem -LiteralPath $KnownFfmpeg -Filter ffmpeg.exe -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName
} else {
    $FfmpegPath = $Ffmpeg.Source
}
if (-not $FfmpegPath -or -not (Test-Path -LiteralPath $FfmpegPath)) {
    throw 'ffmpeg.exe was not found. Install it with: winget install --id=Gyan.FFmpeg -e'
}
$FfprobePath = Join-Path (Split-Path -Parent $FfmpegPath) 'ffprobe.exe'
if (-not (Test-Path -LiteralPath $FfprobePath)) {
    throw "ffprobe.exe was not found next to $FfmpegPath"
}
$env:MAOER_BUILD_FFMPEG = $FfmpegPath
$env:MAOER_BUILD_FFPROBE = $FfprobePath

Write-Host '[5/5] Building MaoerRecorder.exe...'
& $Python -m PyInstaller --noconfirm --clean (Join-Path $Root 'MaoerRecorder.spec')
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }

$Exe = Join-Path $Root 'dist\MaoerRecorder\MaoerRecorder.exe'
if (-not (Test-Path -LiteralPath $Exe)) { throw 'Build finished without producing MaoerRecorder.exe.' }
Write-Host "Ready: $Exe"
