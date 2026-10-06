<#
Build Game Mechanic Lab for Windows.

This script is intentionally self-contained.  It creates a local .venv,
installs the optional chart dependency and PyInstaller, and emits:

  dist\GameMechanicLab.exe

Run from the project root in PowerShell:
  .\build_windows.ps1

For a standard-library runtime build (no matplotlib/PyYAML), use:
  .\build_windows.ps1 -SkipOptionalPackages

The script also runs a one-fight packaged-app smoke test and writes a
sidecar build manifest to dist\GameMechanicLab-build-info.json.  Use
`-SkipValidation` only when the host policy prevents launching a freshly
built executable.
#>
[CmdletBinding()]
param(
    [switch]$SkipOptionalPackages,
    [switch]$Clean,
    [switch]$SkipValidation
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot
$VenvPath = Join-Path $ProjectRoot ".venv-build"
$Python = Join-Path $VenvPath "Scripts\python.exe"
$PyInstaller = Join-Path $VenvPath "Scripts\pyinstaller.exe"

if ($Clean) {
    if (Test-Path (Join-Path $ProjectRoot "build")) {
        Remove-Item -Recurse -Force (Join-Path $ProjectRoot "build")
    }
    if (Test-Path (Join-Path $ProjectRoot "dist")) {
        Remove-Item -Recurse -Force (Join-Path $ProjectRoot "dist")
    }
}

if (-not (Test-Path $Python)) {
    py -3 -m venv $VenvPath
    if ($LASTEXITCODE -ne 0) { throw "Could not create a Python 3 virtual environment." }
}

& $Python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip bootstrap failed." }
if (-not $SkipOptionalPackages) {
    & $Python -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "Optional chart dependencies failed to install." }
}
& $Python -m pip install pyinstaller
if ($LASTEXITCODE -ne 0) { throw "PyInstaller installation failed." }

$DataArgs = @()
if (Test-Path (Join-Path $ProjectRoot "configs")) {
    $DataArgs += @("--add-data", "configs;configs")
}
if (Test-Path (Join-Path $ProjectRoot "docs")) {
    $DataArgs += @("--add-data", "docs;docs")
}
if (Test-Path (Join-Path $ProjectRoot "demo")) {
    $DataArgs += @("--add-data", "demo;demo")
}

& $PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name GameMechanicLab `
    @DataArgs `
    app.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

$ExePath = Join-Path $ProjectRoot "dist\GameMechanicLab.exe"
if (-not (Test-Path $ExePath -PathType Leaf)) {
    throw "PyInstaller reported success, but the expected executable was not found: $ExePath"
}

# Record the source tree and toolchain used for this build.  The timestamp is
# intentionally informational; source_tree_sha256 is the stable identifier
# that lets a later build be compared without pretending PyInstaller output
# bytes are bit-for-bit reproducible across hosts.
$SourceRoots = @(
    "app.py", "run_demo.py", "pyproject.toml", "requirements.txt",
    "GameMechanicLab.spec", "configs", "demo", "docs", "gamemechaniclab"
)
$SourceFiles = foreach ($Root in $SourceRoots) {
    $Candidate = Join-Path $ProjectRoot $Root
    if (Test-Path $Candidate -PathType Leaf) {
        Get-Item $Candidate
    } elseif (Test-Path $Candidate -PathType Container) {
        Get-ChildItem $Candidate -Recurse -File
    }
}
$SourceFiles = @($SourceFiles |
    Where-Object {
        $_.FullName -notmatch "[\\/](__pycache__|\.pytest_cache|\.mypy_cache)[\\/]" -and
        $_.Extension -notin @(".pyc", ".pyo")
    } |
    Sort-Object FullName -Unique)
$FileRecords = @($SourceFiles | ForEach-Object {
    # Use slash-separated paths in the manifest so the tree identifier is
    # independent of the host's native separator.
    # Avoid Path.GetRelativePath here: the script also supports the inbox
    # Windows PowerShell 5.1/.NET Framework where that API is unavailable.
    $Relative = $_.FullName.Substring($ProjectRoot.Length).TrimStart([char[]]@([char]92, [char]47)).Replace([char]92, [char]47)
    [PSCustomObject]@{
        path = $Relative
        sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash.ToLowerInvariant()
    }
})
$ManifestLines = @($FileRecords | ForEach-Object { "$($_.path)`n$($_.sha256)" })
$ManifestText = $ManifestLines -join "`n"
$Sha = [System.Security.Cryptography.SHA256]::Create()
try {
    $ManifestBytes = [System.Text.Encoding]::UTF8.GetBytes($ManifestText)
    $SourceTreeHash = -join ($Sha.ComputeHash($ManifestBytes) | ForEach-Object { $_.ToString("x2") })
} finally {
    $Sha.Dispose()
}
$PythonVersion = (& $Python --version 2>&1 | Out-String).Trim()
$PyInstallerVersion = (& $PyInstaller --version 2>&1 | Out-String).Trim()
$BuildInfo = [ordered]@{
    schema = "game-mechanic-lab.build.v1"
    product = "Game Mechanic Lab"
    version = "0.1.0"
    target = "windows"
    artifact = "dist/GameMechanicLab.exe"
    built_at_utc = (Get-Date).ToUniversalTime().ToString("o")
    python = $PythonVersion
    pyinstaller = $PyInstallerVersion
    optional_packages_installed = (-not $SkipOptionalPackages)
    source_tree_sha256 = $SourceTreeHash
    source_files = $FileRecords
}
$BuildInfoPath = Join-Path $ProjectRoot "dist\GameMechanicLab-build-info.json"
$BuildInfo | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $BuildInfoPath -Encoding UTF8

if (-not $SkipValidation) {
    $SmokeRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("GameMechanicLab-smoke-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $SmokeRoot -Force | Out-Null
    try {
        # --output is used instead of stdout because this is a --windowed
        # executable and Windows may not attach a console to it.
        # A windowed executable can detach from direct PowerShell invocation.
        # Wait for its actual process and preserve quoted output paths with spaces.
        $SmokeProcess = Start-Process -FilePath $ExePath -ArgumentList @(
            "--headless", "--runs", "1", "--output", ('"' + $SmokeRoot + '"')
        ) -Wait -PassThru
        if ($SmokeProcess.ExitCode -ne 0) {
            throw "Packaged executable smoke test exited with code $($SmokeProcess.ExitCode)."
        }
        $SmokeResult = Join-Path $SmokeRoot "simulation_result.json"
        if (-not (Test-Path $SmokeResult -PathType Leaf)) {
            throw "Packaged executable smoke test did not create simulation_result.json."
        }
        $SmokePayload = Get-Content -LiteralPath $SmokeResult -Raw | ConvertFrom-Json
        if ([int]$SmokePayload.runs -ne 1 -or $null -eq $SmokePayload.summary) {
            throw "Packaged executable smoke test returned an invalid result payload."
        }
    } finally {
        if (Test-Path $SmokeRoot) {
            Remove-Item -Recurse -Force -LiteralPath $SmokeRoot
        }
    }
    Write-Host "Smoke test passed: one real headless fight." -ForegroundColor Green
}

Write-Host "Built: $ExePath" -ForegroundColor Green
Write-Host "Build metadata: $BuildInfoPath" -ForegroundColor Green
