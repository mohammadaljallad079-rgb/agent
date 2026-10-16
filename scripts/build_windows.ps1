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
            # The frozen build is windowed (console=False); PowerShell's call
            # operator does not wait for a GUI executable, so use Start-Process
            # -Wait and check the real exit code.
            $exe = Join-Path $OutputDir "ArabicAIAgent\ArabicAIAgent.exe"
            $proc = Start-Process -FilePath $exe -ArgumentList "version" -Wait -PassThru
            if ($proc.ExitCode -ne 0) {
                throw "frozen executable returned exit $($proc.ExitCode)"
            }
        }

        Invoke-Step "Self-check the frozen executable (assets, documents, database, API)" {
            # A windowed build has no stdout: the report goes to a file and the
            # exit code is the verdict. This exercises the *packaged* product —
            # bundled migrations and UI, real document writes, a real SQLite
            # migration and a real HTTP request — so a broken payload fails the
            # build instead of shipping.
            $exe = Join-Path $OutputDir "ArabicAIAgent\ArabicAIAgent.exe"
            $report = Join-Path $OutputDir "selfcheck.json"
            $proc = Start-Process -FilePath $exe -ArgumentList "selfcheck", "--report", $report -Wait -PassThru
            if ($proc.ExitCode -ne 0) {
                throw "self-check failed (exit $($proc.ExitCode))"
            }
            if (-not (Test-Path $report)) {
                throw "self-check exited 0 but wrote no report to $report"
            }
            Get-Content $report -Raw
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

        Invoke-Step "Install, verify and uninstall the real installer" {
            # The strongest gate short of a human clicking through: run the
            # produced setup silently, self-check the *installed* executable
            # (proving the installer's file layout, not just the PyInstaller
            # payload, is correct), then remove it so the runner is left clean.
            $setup = (Get-Item (Join-Path $OutputDir "ArabicAIAgent-Setup-*.exe")).FullName
            $installDir = Join-Path $env:LOCALAPPDATA "ArabicAIAgent"
            $installed = Join-Path $installDir "ArabicAIAgent.exe"
            $proc = Start-Process -FilePath $setup -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART" -Wait -PassThru
            if ($proc.ExitCode -ne 0) {
                throw "installer returned exit $($proc.ExitCode)"
            }
            if (-not (Test-Path $installed)) {
                throw "installer did not place $installed"
            }
            $report = Join-Path $OutputDir "installed-selfcheck.json"
            $proc = Start-Process -FilePath $installed -ArgumentList "selfcheck", "--report", $report -Wait -PassThru
            if ($proc.ExitCode -ne 0) {
                throw "installed self-check failed (exit $($proc.ExitCode))"
            }
            Get-Content $report -Raw
            $uninstaller = Join-Path $installDir "unins000.exe"
            if (Test-Path $uninstaller) {
                Start-Process -FilePath $uninstaller -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART" -Wait
            }
        }
    }

    Write-Host "Build complete." -ForegroundColor Green
}
finally {
    Pop-Location
}
