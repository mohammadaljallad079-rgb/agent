<#
.SYNOPSIS
    Build the Arabic AI Agent Platform Windows installer from a clean checkout.

.DESCRIPTION
    Reproduces the documented flow end to end, exactly as the Windows CI job does:

        clean checkout
        -> install build dependencies
        -> generate packaging config   (python -m arabic_agent.desktop.packaging dist)
        -> PyInstaller freeze          (dist\ArabicAIAgent.spec)
        -> Inno Setup                  (dist\ArabicAIAgent.iss)
        -> SHA-256 checksum

    There is no manual copy step: the packaging generator reads the tracked
    migrations straight from the repository, so PyInstaller always finds them.

.PARAMETER OutputDir
    Directory for the generated spec, payload and installer. Default: dist

.PARAMETER SkipFreeze
    Generate the packaging inputs but do not run PyInstaller (input sanity check).

.PARAMETER SkipInstaller
    Freeze the payload but do not compile the Inno Setup installer.
#>
[CmdletBinding()]
param(
    [string]$OutputDir = "dist",
    [switch]$SkipFreeze,
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"

function Invoke-Step {
    param([string]$Name, [scriptblock]$Action)
    Write-Host "==> $Name" -ForegroundColor Cyan
    & $Action
    if ($LASTEXITCODE -ne 0) {
        throw "step failed: $Name (exit $LASTEXITCODE)"
    }
}

$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
try {
    Invoke-Step "Install build dependencies (project + packaging + desktop + documents + docgen)" {
        # The documents/docgen extras are what make document parsing and Arabic
        # PDF generation real in the frozen install; without them the packaged
        # product would honestly report those formats unsupported. local_model
        # is deliberately excluded: llama-cpp-python has no prebuilt Windows
        # wheel, so the frozen installer ships a real cloud path and downloads
        # the local runtime from the app rather than bundling it.
        uv sync --extra dev --extra desktop --extra packaging --extra documents --extra docgen
    }

    Invoke-Step "Generate packaging inputs" {
        uv run python -m arabic_agent.desktop.packaging $OutputDir
    }

    if (-not $SkipFreeze) {
        Invoke-Step "Freeze the product with PyInstaller" {
            uv run pyinstaller --clean --noconfirm (Join-Path $OutputDir "ArabicAIAgent.spec")
        }

        Invoke-Step "Smoke-test the frozen executable" {
            & (Join-Path $OutputDir "ArabicAIAgent\ArabicAIAgent.exe") version
        }
    }

    if (-not $SkipInstaller) {
        $iscc = Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"
        if (-not (Test-Path $iscc)) {
            throw "Inno Setup 6 not found at $iscc; install it (choco install innosetup) and re-run"
        }
        Invoke-Step "Compile the installer" {
            & $iscc (Join-Path $OutputDir "ArabicAIAgent.iss")
        }

        Invoke-Step "Hash the installer" {
            Get-FileHash (Join-Path $OutputDir "ArabicAIAgent-Setup-*.exe") -Algorithm SHA256 |
                Format-List
        }
    }

    Write-Host "Build complete." -ForegroundColor Green
}
finally {
    Pop-Location
}
