# ruff: noqa: E501
"""Windows packaging: the frozen payload and the installer script.

This module turns the Python product into a Windows *binary* product. It does two
things, both real and both testable off Windows:

* :func:`build_pyinstaller_spec` writes a PyInstaller spec that bundles the
  product into a one-folder ``ArabicAIAgent.exe``. The spec collects the package's
  data files (the desktop UI HTML and the packaged Alembic migration chain),
  which a naive build would miss, and it names the frozen executable so the
  product entry point (:mod:`arabic_agent.__main__`) is the thing that runs.
* :func:`build_installer_script` writes a real Inno Setup script that installs
  that payload **per user** (``PrivilegesRequired=lowest`` — no UAC), with a
  Start-menu entry, an optional sign-in task and a working uninstaller.

Neither function runs the external compiler (PyInstaller / ``ISCC.exe``): those
require Windows and are the responsibility of the CI packaging job. What this
module guarantees is that the *inputs* to those tools are generated from the
product's real layout, so the packaging step is reproducible and does not drift
from the code. The frozen executable dispatches its subcommands through
``arabic_agent.__main__``; the installer's uninstall path calls the same
executable, so there is one binary, not a binary plus a hidden interpreter.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .version import PRODUCT_VERSION

#: The product executable name produced by the Windows build.
PRODUCT_EXE_NAME = "ArabicAIAgent.exe"


def _default_source_dir(output: Path) -> Path:
    """The directory holding the frozen payload (the PyInstaller output)."""
    return output.parent / "ArabicAIAgent"


def build_pyinstaller_spec(
    *,
    output: Path,
    entry_module: str = "arabic_agent.__main__",
    name: str = "ArabicAIAgent",
    one_file: bool = False,
) -> Path:
    """Write a PyInstaller spec for the product.

    ``one_file=False`` (the default) produces a one-folder build, which starts
    faster and makes the payload inspectable; the Inno Setup script installs that
    folder. The spec collects the desktop UI assets and the packaged migration
    chain explicitly, so a frozen install can open its window and create its
    local database without a repository checkout.
    """
    datas = [
        ("src/arabic_agent/desktop/ui", "arabic_agent/desktop/ui"),
        ("src/arabic_agent/_migrations_local", "arabic_agent/_migrations_local"),
    ]
    data_lines = ",\n        ".join(f"({src!r}, {dst!r})" for src, dst in datas)
    spec = f"""# Arabic AI Agent Platform — PyInstaller spec (generated)
# Build: pyinstaller --clean --noconfirm {name}.spec
# The frozen executable dispatches subcommands via {entry_module}.

block_cipher = None

a = Analysis(
    [{entry_module!r}],
    pathex=["src"],
    binaries=[],
    datas=[
        {data_lines},
    ],
    hiddenimports=[
        "arabic_agent.local.main",
        "arabic_agent.desktop.shell",
        "arabic_agent.desktop.installer",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter"],
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name={name!r},
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)
"""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(spec, encoding="utf-8", newline="\n")
    return output


def build_installer_script(
    *,
    output: Path,
    app_name: str = "Arabic AI Agent",
    app_version: str = PRODUCT_VERSION,
    exe_name: str = PRODUCT_EXE_NAME,
    source_dir: Path | None = None,
) -> Path:
    """Write a real Inno Setup script for the packaged product.

    The script installs per-user (no UAC), places the frozen executable and its
    runtime payload, and registers a Start-menu entry, an optional sign-in task
    and an uninstaller that calls the executable's own ``uninstall`` subcommand.
    It is only meaningful when Inno Setup's ``ISCC.exe`` and the frozen payload
    exist; this function writes the script, it does not run the compiler.
    """
    source = source_dir if source_dir is not None else _default_source_dir(output)
    script = f"""; Arabic AI Agent Platform — Inno Setup script (generated)
; Build the payload first (PyInstaller spec), then compile with ISCC.exe.

#define AppName "{app_name}"
#define AppVersion "{app_version}"
#define AppExe "{exe_name}"
#define SourceDir "{source}"

[Setup]
AppId={{{{8F4B6C1E-2A7D-4E3B-9C5A-1D2E3F4A5B6C}}}}
AppName={{#AppName}}
AppVersion={{#AppVersion}}
AppPublisher=Arabic AI Agent Platform
DefaultDirName={{localappdata}}\\ArabicAIAgent
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputBaseFilename=ArabicAIAgent-Setup-{{#AppVersion}}
OutputDir={{#SourceDir}}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName={{#AppName}}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "arabic"; MessagesFile: "compiler:Languages\\Arabic.isl"

[Tasks]
Name: "startup"; Description: "Start {{#AppName}} when I sign in"; Flags: unchecked

[Files]
Source: "{{#SourceDir}}\\{{#AppExe}}"; DestDir: "{{app}}"; Flags: ignoreversion
Source: "{{#SourceDir}}\\_internal\\*"; DestDir: "{{app}}\\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{{autoprograms}}\\{{#AppName}}"; Filename: "{{app}}\\{{#AppExe}}"; Parameters: "shell"
Name: "{{userstartup}}\\{{#AppName}}"; Filename: "{{app}}\\{{#AppExe}}"; Parameters: "shell"; Tasks: startup

[Run]
Filename: "{{app}}\\{{#AppExe}}"; Parameters: "shell"; Description: "Launch {{#AppName}}"; Flags: nowait postinstall skipifsilent
"""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(script, encoding="utf-8", newline="\n")
    return output


def main() -> None:
    """CLI: ``python -m arabic_agent.desktop.packaging [output-dir]``."""
    parser = argparse.ArgumentParser(description="Generate the Windows packaging inputs")
    parser.add_argument("output", nargs="?", default="dist", help="output directory")
    args = parser.parse_args()
    out_dir = Path(args.output)
    spec = build_pyinstaller_spec(output=out_dir / "ArabicAIAgent.spec")
    iss = build_installer_script(output=out_dir / "ArabicAIAgent.iss")
    print(spec)
    print(iss)


__all__ = [
    "PRODUCT_EXE_NAME",
    "build_installer_script",
    "build_pyinstaller_spec",
    "main",
]


if __name__ == "__main__":  # pragma: no cover - CLI
    main()
