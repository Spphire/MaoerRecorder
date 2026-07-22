---
name: maoerrecorder-release
description: Build, validate, package, and optionally publish the MaoerRecorder Windows onedir release. Use when Codex is asked to build or distribute the EXE, create or update a GitHub Release, upload the portable Windows package, regenerate its checksum, or repeat the project's release workflow.
---

# MaoerRecorder Release

Use `scripts/package_release.ps1` from the repository root. It runs checks, builds the Windows onedir bundle in a temporary staging directory, runs the frozen self-test, audits package contents, creates a ZIP and SHA-256 file, and only mutates GitHub when `-Publish` is explicitly supplied.

Run the examples with `pwsh` when PowerShell 7 is available. On Windows PowerShell 5, replace the command prefix with `powershell.exe -NoProfile -ExecutionPolicy Bypass -File`; the script supports both hosts.

## Safety Rules

- Never include `recordings`, `.dashboard`, `.env`, cookies, logs, profiles, or build caches in a release asset.
- Upload the complete `dist\MaoerRecorder` onedir folder as a ZIP. Do not upload only `MaoerRecorder.exe`; the bundle contains Chromium, ffmpeg, ffprobe, templates, and static assets.
- Keep a running dashboard and its workers untouched during staging builds. Replacing the installed `dist\MaoerRecorder` is a separate controlled deployment action.
- Require a clean worktree for a published release unless the user explicitly authorizes `-AllowDirty`.
- Treat `-Push` and `-Publish` as write operations. Do not add either switch just to validate or make a local package.

## Workflow

1. Inspect the branch, tracking branch, worktree, and existing release tag. Resolve unexpected dirty or behind state before publishing.
2. Run the default test and syntax checks. Do not use `-SkipTests` unless the user explicitly accepts the risk.
3. Build the onedir package in a temporary staging directory. The script provisions `.build-venv` and bundled Playwright Chromium when needed, and locates ffmpeg/ffprobe for PyInstaller.
4. Run the frozen EXE's `--self-test` with isolated temporary state. ffmpeg, ffprobe, AAC-to-M4A muxing, and Chromium checks must pass.
5. Audit required bundle files and reject forbidden runtime data.
6. Create `MaoerRecorder-v<version>-windows-x64.zip` and its `.sha256` companion. Verify the hash and archive contents.
7. Without `-Publish`, stop after producing local artifacts. With `-Push -Publish`, push the current branch and create or update the matching GitHub Release, then verify both assets and the release URL.

## Commands

Local validation and packaging:

```powershell
pwsh -File .codex\skills\maoerrecorder-release\scripts\package_release.ps1 `
  -Version 0.2.0
```

Publish after the source commit is ready and GitHub CLI is authenticated:

```powershell
pwsh -File .codex\skills\maoerrecorder-release\scripts\package_release.ps1 `
  -Version 0.2.0 -Push -Publish
```

Use `-NoBuild` only when an independently verified `dist\MaoerRecorder` already exists. Use `-Draft` or `-Prerelease` when the GitHub asset should not be presented as stable. Use `-NotesFile <path>` for release-specific notes. Use `-AllowDirty` only when the user explicitly wants an artifact from uncommitted files.

## Failure Handling

- A failed test, self-test, package audit, checksum, or required-file check stops before any GitHub mutation.
- If the requested tag already exists, the script uploads verified assets with `--clobber`; it does not silently change source history.
- If a live service is using the installed EXE, leave it running and report that deployment must be performed separately. Staging is safe alongside it.
- Report the local ZIP path, SHA-256, commit, branch, and release URL after completion.

The deterministic implementation is [`scripts/package_release.ps1`](scripts/package_release.ps1). Keep the instructions and script synchronized when the PyInstaller layout, test commands, or release asset names change.
