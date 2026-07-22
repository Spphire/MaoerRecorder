[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Version,
    [string]$Repo = "",
    [string]$NotesFile = "",
    [switch]$Push,
    [switch]$Publish,
    [switch]$Draft,
    [switch]$Prerelease,
    [switch]$NoBuild,
    [switch]$SkipTests,
    [switch]$AllowDirty,
    [switch]$KeepStaging
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string]$File,
        [string[]]$Arguments = @()
    )
    & $File @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE"
    }
}

function Get-CommandOutput {
    param(
        [Parameter(Mandatory = $true)][string]$File,
        [string[]]$Arguments = @()
    )
    $output = & $File @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed: $File $($Arguments -join ' ')"
    }
    return (($output | Out-String).Trim())
}

function Restore-EnvironmentValue {
    param([string]$Name, [AllowNull()][string]$Value, [bool]$WasDefined)
    if ($WasDefined) {
        [Environment]::SetEnvironmentVariable($Name, $Value, "Process")
    } else {
        [Environment]::SetEnvironmentVariable($Name, $null, "Process")
    }
}

if ($Version -notmatch '^v?[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$') {
    throw "Version must look like 1.2.3 or v1.2.3"
}
$Tag = if ($Version.StartsWith("v")) { $Version } else { "v$Version" }
$Root = Get-CommandOutput "git" @("rev-parse", "--show-toplevel")
Set-Location $Root
$Branch = Get-CommandOutput "git" @("branch", "--show-current")
if ([string]::IsNullOrWhiteSpace($Branch)) {
    throw "The release must be made from a named branch."
}

$status = Get-CommandOutput "git" @("status", "--porcelain")
if (-not [string]::IsNullOrWhiteSpace($status)) {
    if ($Publish -and -not $AllowDirty) {
        throw "Worktree is dirty. Commit changes first, or explicitly pass -AllowDirty."
    }
    Write-Warning "Building from a dirty worktree."
}

Invoke-Native "git fetch" "git" @("fetch", "--prune", "origin")
$tracking = Get-CommandOutput "git" @("rev-list", "--left-right", "--count", "HEAD...origin/$Branch")
$trackingParts = $tracking -split '\s+'
$ahead = [int]$trackingParts[0]
$behind = [int]$trackingParts[1]
if ($behind -gt 0) {
    throw "Local branch is $behind commit(s) behind origin/$Branch."
}
if ($ahead -gt 0 -and $Publish -and -not $Push) {
    throw "Local branch is ahead of origin/$Branch. Pass -Push before publishing."
}

if (-not $SkipTests) {
    Invoke-Native "pytest" "py" @("-m", "pytest", "-q")
    Invoke-Native "compileall" "py" @(
        "-m", "compileall", "-q", "dashboard.py", "main.py", "maoer", "tests"
    )
    $node = Get-Command "node.exe" -ErrorAction SilentlyContinue
    if ($null -ne $node) {
        Invoke-Native "JavaScript syntax check" $node.Source @(
            "--check", "maoer\static\dashboard.js"
        )
    }
    Invoke-Native "git diff check" "git" @("diff", "--check")
    Invoke-Native "cached git diff check" "git" @("diff", "--cached", "--check")
}

$StageRoot = Join-Path ([IO.Path]::GetTempPath()) "maoer-release-$Tag-$PID"
$StageDist = Join-Path $StageRoot "dist"
$StageWork = Join-Path $StageRoot "build"
$SelfTestData = Join-Path $StageRoot "self-test-data"
$ArtifactDir = Join-Path $Root "release-artifacts"
$ArchiveName = "MaoerRecorder-$Tag-windows-x64.zip"
$ArchivePath = Join-Path $ArtifactDir $ArchiveName
$ChecksumPath = "$ArchivePath.sha256"
$DistRoot = Join-Path $StageDist "MaoerRecorder"
New-Item -ItemType Directory -Force -Path $StageRoot, $ArtifactDir, $SelfTestData |
    Out-Null

try {
    if ($NoBuild) {
        $DistRoot = Join-Path $Root "dist\MaoerRecorder"
        if (-not (Test-Path -LiteralPath $DistRoot -PathType Container)) {
            throw "-NoBuild requested but dist\MaoerRecorder does not exist."
        }
    } else {
        $BuildVenv = Join-Path $Root ".build-venv"
        $BuildPython = Join-Path $BuildVenv "Scripts\python.exe"
        if (-not (Test-Path -LiteralPath $BuildPython)) {
            Invoke-Native "create build environment" "py" @(
                "-3.13", "-m", "venv", $BuildVenv
            )
        }
        Invoke-Native "install build dependencies" $BuildPython @(
            "-m", "pip", "install", "--disable-pip-version-check", "-r",
            (Join-Path $Root "requirements-build.txt")
        )

        $OldBrowser = [Environment]::GetEnvironmentVariable(
            "PLAYWRIGHT_BROWSERS_PATH", "Process"
        )
        $OldBrowserDefined = $null -ne $OldBrowser
        $env:PLAYWRIGHT_BROWSERS_PATH = "0"
        try {
            Invoke-Native "install bundled Chromium" $BuildPython @(
                "-m", "playwright", "install", "--only-shell", "chromium"
            )
        } finally {
            Restore-EnvironmentValue "PLAYWRIGHT_BROWSERS_PATH" `
                $OldBrowser $OldBrowserDefined
        }

        $FfmpegCommand = Get-Command "ffmpeg.exe" -ErrorAction SilentlyContinue
        $FfmpegPath = if ($null -ne $FfmpegCommand) {
            $FfmpegCommand.Source
        } else {
            $KnownRoot = Join-Path $HOME (
                "AppData\Local\Microsoft\WinGet\Packages\" +
                "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
            )
            Get-ChildItem -LiteralPath $KnownRoot -Filter "ffmpeg.exe" -Recurse `
                -ErrorAction SilentlyContinue |
                Select-Object -First 1 -ExpandProperty FullName
        }
        if (
            [string]::IsNullOrWhiteSpace($FfmpegPath) -or
            -not (Test-Path -LiteralPath $FfmpegPath)
        ) {
            throw "ffmpeg.exe was not found."
        }
        $FfprobePath = Join-Path (Split-Path -Parent $FfmpegPath) "ffprobe.exe"
        if (-not (Test-Path -LiteralPath $FfprobePath)) {
            throw "ffprobe.exe was not found next to $FfmpegPath"
        }

        $OldFfmpeg = [Environment]::GetEnvironmentVariable(
            "MAOER_BUILD_FFMPEG", "Process"
        )
        $OldFfmpegDefined = $null -ne $OldFfmpeg
        $OldFfprobe = [Environment]::GetEnvironmentVariable(
            "MAOER_BUILD_FFPROBE", "Process"
        )
        $OldFfprobeDefined = $null -ne $OldFfprobe
        $env:MAOER_BUILD_FFMPEG = $FfmpegPath
        $env:MAOER_BUILD_FFPROBE = $FfprobePath
        try {
            Invoke-Native "PyInstaller build" $BuildPython @(
                "-m", "PyInstaller", "--noconfirm", "--clean",
                "--distpath", $StageDist, "--workpath", $StageWork,
                (Join-Path $Root "MaoerRecorder.spec")
            )
        } finally {
            Restore-EnvironmentValue "MAOER_BUILD_FFMPEG" `
                $OldFfmpeg $OldFfmpegDefined
            Restore-EnvironmentValue "MAOER_BUILD_FFPROBE" `
                $OldFfprobe $OldFfprobeDefined
        }
    }

    $ExePath = Join-Path $DistRoot "MaoerRecorder.exe"
    if (-not (Test-Path -LiteralPath $ExePath -PathType Leaf)) {
        throw "MaoerRecorder.exe was not produced."
    }
    $Required = @(
        "MaoerRecorder.exe",
        "_internal\vendor\ffmpeg\ffmpeg.exe",
        "_internal\vendor\ffmpeg\ffprobe.exe",
        "_internal\assets\missevan-tray-light.png",
        "_internal\assets\missevan-tray-dark.png",
        "_internal\maoer\templates\dashboard.html"
    )
    foreach ($Relative in $Required) {
        if (-not (Test-Path -LiteralPath (Join-Path $DistRoot $Relative))) {
            throw "Required bundle file is missing: $Relative"
        }
    }
    $Forbidden = @(
        Get-ChildItem -LiteralPath $DistRoot -Recurse -File |
            Where-Object {
                $_.FullName -match (
                    '(recordings|\.dashboard|\.env|cookie|profile|' +
                    'tasks\.json|self-test|dashboard\.log)'
                )
            }
    )
    if ($Forbidden.Count -gt 0) {
        throw "Forbidden runtime data found: $($Forbidden[0].FullName)"
    }

    $OldBase = [Environment]::GetEnvironmentVariable("MAOER_BASE_DIR", "Process")
    $OldBaseDefined = $null -ne $OldBase
    $OldState = [Environment]::GetEnvironmentVariable(
        "MAOER_DASHBOARD_STATE_DIR", "Process"
    )
    $OldStateDefined = $null -ne $OldState
    $env:MAOER_BASE_DIR = Join-Path $SelfTestData "recordings"
    $env:MAOER_DASHBOARD_STATE_DIR = Join-Path $SelfTestData "state"
    try {
        $SelfTest = Start-Process -FilePath $ExePath -ArgumentList "--self-test" `
            -WorkingDirectory (Split-Path -Parent $ExePath) -WindowStyle Hidden `
            -Wait -PassThru
        if ($SelfTest.ExitCode -ne 0) {
            throw "Frozen self-test failed with exit code $($SelfTest.ExitCode)"
        }
    } finally {
        Restore-EnvironmentValue "MAOER_BASE_DIR" $OldBase $OldBaseDefined
        Restore-EnvironmentValue "MAOER_DASHBOARD_STATE_DIR" `
            $OldState $OldStateDefined
    }

    Remove-Item -LiteralPath $ArchivePath, $ChecksumPath -Force `
        -ErrorAction SilentlyContinue
    $Tar = Get-Command "tar.exe" -ErrorAction SilentlyContinue
    if ($null -eq $Tar) {
        throw "tar.exe is required to create the release ZIP."
    }
    Push-Location (Split-Path -Parent $DistRoot)
    try {
        Invoke-Native "create release ZIP" $Tar.Source @(
            "-a", "-c", "-f", $ArchivePath, "MaoerRecorder"
        )
    } finally {
        Pop-Location
    }
    $ArchiveEntries = @(& $Tar.Source -t -f $ArchivePath)
    if ($LASTEXITCODE -ne 0) {
        throw "The release ZIP could not be read back."
    }
    $RequiredArchiveEntries = @(
        "MaoerRecorder/MaoerRecorder.exe",
        "MaoerRecorder/_internal/vendor/ffmpeg/ffmpeg.exe",
        "MaoerRecorder/_internal/vendor/ffmpeg/ffprobe.exe"
    )
    foreach ($Entry in $RequiredArchiveEntries) {
        if ($ArchiveEntries -notcontains $Entry) {
            throw "Required ZIP entry is missing: $Entry"
        }
    }
    $ForbiddenArchiveEntries = @(
        $ArchiveEntries | Where-Object {
            $_ -match (
                '(^|/)(recordings|\.dashboard|\.env|\.profile)(/|$)|' +
                '(^|/)(tasks\.json|dashboard\.log)$'
            )
        }
    )
    if ($ForbiddenArchiveEntries.Count -gt 0) {
        throw "Forbidden ZIP entry found: $($ForbiddenArchiveEntries[0])"
    }
    $Hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $ArchivePath).Hash
    $Hash = $Hash.ToLowerInvariant()
    "$Hash  $ArchiveName" | Set-Content -LiteralPath $ChecksumPath -Encoding ascii

    if ($Push) {
        Invoke-Native "git push" "git" @("push", "origin", $Branch)
    }

    $ReleaseUrl = $null
    if ($Publish) {
        if ([string]::IsNullOrWhiteSpace($Repo)) {
            $Repo = Get-CommandOutput "gh" @(
                "repo", "view", "--json", "nameWithOwner", "--jq",
                ".nameWithOwner"
            )
        }
        Invoke-Native "GitHub authentication" "gh" @("auth", "status")

        $RemoteTagLines = @(& git ls-remote --tags origin `
            "refs/tags/$Tag" "refs/tags/$Tag^{}")
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to inspect remote tag $Tag."
        }
        if ($RemoteTagLines.Count -gt 0) {
            $Peeled = @($RemoteTagLines | Where-Object { $_ -match '\^\{\}$' })
            $RemoteTagLine = if ($Peeled.Count -gt 0) {
                $Peeled[0]
            } else {
                $RemoteTagLines[0]
            }
            $RemoteTagCommit = ($RemoteTagLine -split '\s+')[0]
            $HeadCommit = Get-CommandOutput "git" @("rev-parse", "HEAD")
            if ($RemoteTagCommit -ne $HeadCommit) {
                throw "Remote tag $Tag does not point to current HEAD."
            }
        }

        $ResolvedNotes = $NotesFile
        if ([string]::IsNullOrWhiteSpace($ResolvedNotes)) {
            $ResolvedNotes = Join-Path $StageRoot "release-notes.md"
            @(
                "## MaoerRecorder $Tag",
                "",
                "- Windows control panel and taskbar tray integration.",
                "- New recordings use AAC-LC 48 kHz stereo in final.m4a.",
                "- Existing MP3/TS recordings remain compatible and unchanged.",
                "- Portable package includes Chromium, ffmpeg, and ffprobe.",
                "",
                "Validated by the project tests and frozen-bundle self-test."
            ) | Set-Content -LiteralPath $ResolvedNotes -Encoding utf8
        }

        & gh release view $Tag --repo $Repo --json tagName *> $null
        $ReleaseExists = $LASTEXITCODE -eq 0
        if ($ReleaseExists) {
            Invoke-Native "upload release assets" "gh" @(
                "release", "upload", $Tag, $ArchivePath, $ChecksumPath,
                "--repo", $Repo, "--clobber"
            )
            $EditArguments = @(
                "release", "edit", $Tag, "--repo", $Repo,
                "--title", "MaoerRecorder $Tag", "--notes-file", $ResolvedNotes
            )
            if ($Draft) {
                $EditArguments += "--draft"
            } else {
                $EditArguments += "--draft=false"
            }
            if ($Prerelease) {
                $EditArguments += "--prerelease"
            } else {
                $EditArguments += "--prerelease=false"
            }
            Invoke-Native "update release" "gh" $EditArguments
        } else {
            $CreateArguments = @(
                "release", "create", $Tag, $ArchivePath, $ChecksumPath,
                "--repo", $Repo, "--target", $Branch,
                "--title", "MaoerRecorder $Tag", "--notes-file", $ResolvedNotes
            )
            if ($Draft) { $CreateArguments += "--draft" }
            if ($Prerelease) { $CreateArguments += "--prerelease" }
            Invoke-Native "create release" "gh" $CreateArguments
        }

        $ReleaseJson = Get-CommandOutput "gh" @(
            "release", "view", $Tag, "--repo", $Repo,
            "--json", "url,isDraft,assets"
        )
        $Release = $ReleaseJson | ConvertFrom-Json
        $ReleaseUrl = $Release.url
        $AssetNames = @($Release.assets | ForEach-Object { $_.name })
        if (
            $AssetNames -notcontains $ArchiveName -or
            $AssetNames -notcontains "$ArchiveName.sha256"
        ) {
            throw "GitHub release is missing an expected asset."
        }
        if (-not $Draft -and $Release.isDraft) {
            throw "GitHub release remains a draft unexpectedly."
        }
    }

    [PSCustomObject]@{
        tag = $Tag
        commit = Get-CommandOutput "git" @("rev-parse", "HEAD")
        branch = $Branch
        package = $ArchivePath
        sha256 = $Hash
        release_url = $ReleaseUrl
    } | ConvertTo-Json -Compress
} finally {
    if (-not $KeepStaging -and (Test-Path -LiteralPath $StageRoot)) {
        Remove-Item -LiteralPath $StageRoot -Recurse -Force `
            -ErrorAction SilentlyContinue
    }
}
