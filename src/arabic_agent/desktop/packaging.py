# ruff: noqa: E501
"""Windows packaging: the frozen payload and the installer script.

This module turns the Python product into a Windows *binary* product. It does two
things, both real and both testable off Windows:

* :func:`build_pyinstaller_spec` writes a PyInstaller spec that bundles the
  product into a one-folder ``ArabicAIAgent.exe``. The spec names the **real
  entry script** (``src/arabic_agent/__main__.py``) and lists **existing**
  data sources — the desktop UI, the local migration chain and the cloud
  migration chain — resolved from this file's location, never from the current
  working directory.
* :func:`build_installer_script` writes a real Inno Setup script that installs
  that payload **per user** (``PrivilegesRequired=lowest`` — no UAC), with a
  Start-menu entry, an optional sign-in task and a working uninstaller. Its
  ``SourceDir`` is the absolute directory PyInstaller produces, so the two tools
  cannot disagree about where the payload lives.

Neither function runs the external compiler (PyInstaller / ``ISCC.exe``): those
require Windows and are the responsibility of the CI packaging job. What this
module guarantees is that the *inputs* to those tools are generated from the
product's real layout and that every path they reference exists, so the packaging
step is reproducible and does not drift from the code.

Migration sources
-----------------
Both chains are packaged, from the **tracked** directories, straight to the
destinations the frozen runtime expects:

* ``migrations_local/`` → ``arabic_agent/_migrations_local`` (the local SQLite
  chain; exactly what the wheel's ``force-include`` ships, and what
  :func:`arabic_agent.local.persistence.database._default_migrations_dir`
  resolves).
* ``migrations/`` → ``arabic_agent/_migrations_cloud`` (the cloud Postgres
  chain, so a frozen install can also operate against a remote database).

Nothing is copied into the source tree first: PyInstaller reads the tracked
directories directly, so there is no artificial pre-freeze mutation and no second
migration chain.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ..branding import PRODUCT
from .version import PRODUCT_VERSION

#: The product executable name produced by the Windows build.
PRODUCT_EXE_NAME = "ArabicAIAgent.exe"

#: The importable package name.
PACKAGE_NAME = "arabic_agent"


def _repo_root() -> Path:
    """Repository root, derived from this file's location (never the cwd)."""
    return Path(__file__).resolve().parents[3]


def entry_script() -> Path:
    """The real entry script PyInstaller must freeze.

    PyInstaller's ``Analysis`` takes *script file paths*, not module names, so
    the spec points at ``src/arabic_agent/__main__.py`` on disk.
    """
    return _repo_root() / "src" / PACKAGE_NAME / "__main__.py"


def package_source_dir() -> Path:
    """The ``arabic_agent`` package directory in the source tree."""
    return _repo_root() / "src" / PACKAGE_NAME


def bundle_datas() -> list[tuple[Path, str]]:
    """The (source, destination) data pairs the frozen product needs.

    Sources are the tracked directories; destinations are where the frozen
    runtime resolves them (relative to the bundled ``arabic_agent`` package).
    """
    root = _repo_root()
    pkg = package_source_dir()
    return [
        (pkg / "desktop" / "ui", f"{PACKAGE_NAME}/desktop/ui"),
        (root / "migrations_local", f"{PACKAGE_NAME}/_migrations_local"),
        (root / "migrations", f"{PACKAGE_NAME}/_migrations_cloud"),
    ]


def missing_datas() -> list[Path]:
    """Data sources that do not exist on disk (empty means the manifest is valid).

    A missing source makes PyInstaller fail with *"Unable to find …"*; callers
    (and the spec generator) use this to fail early and clearly instead.
    """
    return [source for source, _dest in bundle_datas() if not source.exists()]


#: Packages a full Windows build installs but that a minimal install may omit.
#: They are never required to freeze the product: each is collected only when
#: present, so the generated spec is valid on a bare checkout *and* complete on
#: a build host. The documents/docgen extras are what make document parsing and
#: generation real in a frozen install; the local-model extra is what makes
#: offline inference possible.
_OPTIONAL_DATA_PACKAGES: tuple[str, ...] = (
    "pypdf",
    "docx",
    "openpyxl",
    "pptx",
    "bs4",
    "reportlab",
    "arabic_reshaper",
    "bidi",
    "llama_cpp",
)


def optional_data_packages() -> list[str]:
    """The optional data packages that are installed on the build host.

    Returned in a stable order so a regenerated spec does not churn between
    identical builds.
    """
    import importlib.util

    return [name for name in _OPTIONAL_DATA_PACKAGES if importlib.util.find_spec(name) is not None]


def _default_source_dir(output: Path) -> Path:
    """The absolute directory holding the frozen payload (PyInstaller output).

    Absolute on purpose: the Inno Setup script is compiled by ``ISCC.exe`` from
    an arbitrary working directory, so a relative path would be resolved against
    the wrong base and the installer would not find the payload.
    """
    return (output.parent / "ArabicAIAgent").resolve()


def _default_installer_output_dir(output: Path) -> Path:
    """The directory the compiled ``ArabicAIAgent-Setup-*.exe`` is written to.

    The parent of the payload folder (the same ``dist`` directory the CI hashes
    and uploads), so the installer never lands *inside* the payload it packages.
    """
    return output.parent.resolve()


def build_pyinstaller_spec(
    *,
    output: Path,
    name: str = "ArabicAIAgent",
    one_file: bool = False,
) -> Path:
    """Write a PyInstaller spec for the product.

    ``one_file=False`` (the default) produces a one-folder build, which starts
    faster and makes the payload inspectable; the Inno Setup script installs that
    folder. The spec freezes the real entry script and bundles the desktop UI and
    both migration chains, so a frozen install can open its window and create (or
    upgrade) its database without a repository checkout.
    """
    script = entry_script()
    if not script.is_file():
        raise FileNotFoundError(f"packaging entry script not found: {script}")
    missing = missing_datas()
    if missing:
        joined = ", ".join(str(p) for p in missing)
        raise FileNotFoundError(f"packaging data sources not found: {joined}")

    datas = bundle_datas()
    # Emit every path in its portable POSIX form: a spec generated on Windows
    # (or checked in) must build on any platform, and PyInstaller accepts
    # forward slashes everywhere.
    data_lines = ",\n        ".join(f"({src.as_posix()!r}, {dest!r})" for src, dest in datas)
    pathex = (_repo_root() / "src").as_posix()
    # PyInstaller resolves paths in a spec with forward slashes on every
    # platform, and a backslash-separated path is not portable between the
    # machine that generated the spec and the one that builds it.
    script_posix = script.as_posix()
    # Modules loaded dynamically at run time are invisible to PyInstaller's static
    # analysis and must be named explicitly:
    #  * the product subcommands are imported lazily inside __main__.main;
    #  * SQLAlchemy loads its SQLite async driver (aiosqlite) by name;
    #  * Alembic loads the migration ``env.py`` from disk, so the modules that
    #    env.py imports (logging.config, the alembic runtime) are also dynamic.
    # ``collect_submodules`` pulls the whole alembic runtime so an upgrade run
    # from the frozen product cannot hit a missing alembic module.
    hiddenimports = [
        f"{PACKAGE_NAME}.local.main",
        f"{PACKAGE_NAME}.desktop.shell",
        f"{PACKAGE_NAME}.desktop.installer",
        f"{PACKAGE_NAME}.desktop.selfcheck",
        f"{PACKAGE_NAME}.api.app",
        f"{PACKAGE_NAME}.worker.main",
        f"{PACKAGE_NAME}.local.persistence.schema",
        "aiosqlite",
        "sqlalchemy.dialects.sqlite.aiosqlite",
        "logging.config",
        # Used only by the frozen ``selfcheck`` subcommand (in-process HTTP
        # probe); named explicitly so the packaged build can always self-check.
        "fastapi.testclient",
        "httpx",
    ]
    hidden_lines = ",\n        ".join(repr(module) for module in hiddenimports)
    optional = optional_data_packages()
    if optional:
        optional_names = ",\n".join(f"    {name!r}" for name in optional)
        optional_block = f"""# Optional packages present on the build host: their modules and their own data
# files (fonts, templates, etc.) must ship with the payload, or a frozen
# document render or local-model load fails at run time even though the code is
# bundled. Each name is collected only if installed, so this spec is valid on a
# bare checkout too.
for _pkg in [
{optional_names},
]:
    hidden += collect_submodules(_pkg, on_error="ignore")
    datas += collect_data_files(_pkg, include_py_files=False)
"""
    else:
        optional_block = (
            "# No optional data packages are installed on this host; document\n"
            "# parsing/generation and local inference would be absent from the\n"
            "# frozen payload, so none are collected.\n"
        )

    if one_file:
        build_body = f"""exe = EXE(
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
    else:
        # One-folder build: the EXE holds only the bootloader/scripts and
        # COLLECT gathers the binaries and bundled data into <name>/ beside it.
        # This is the layout the Inno Setup script installs.
        build_body = f"""exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name={name!r},
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name={name!r},
)
"""

    spec = f"""# Super AI Agent — PyInstaller spec (generated)
# Build: pyinstaller --clean --noconfirm {name}.spec
# The frozen executable is {script.name} and dispatches subcommands via the
# {PACKAGE_NAME} package.

from PyInstaller.utils.hooks import collect_submodules, collect_data_files

block_cipher = None

# Data bundled by path (the desktop UI and both migration chains). The optional
# packages' own data files are appended below, when they are installed.
datas = [
    {data_lines},
]

# Dynamically imported modules PyInstaller cannot see statically.
hidden = [
    {hidden_lines},
]
{optional_block}# The Alembic runtime is loaded from disk by the migration ``env.py``; collect
# it wholesale so a frozen database upgrade cannot miss a submodule. Alembic's
# own test suite cannot be imported here (on_error="ignore") and is excluded.
hidden += [
    m
    for m in collect_submodules("alembic", on_error="ignore")
    if not m.startswith("alembic.testing")
]

a = Analysis(
    [{script_posix!r}],
    pathex=[{pathex!r}],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter"],
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

{build_body}"""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(spec, encoding="utf-8", newline="\n")
    return output


def build_installer_script(
    *,
    output: Path,
    app_name: str = PRODUCT,
    app_version: str = PRODUCT_VERSION,
    exe_name: str = PRODUCT_EXE_NAME,
    source_dir: Path | None = None,
    output_dir: Path | None = None,
) -> Path:
    """Write a real Inno Setup script for the packaged product.

    The script installs per-user (no UAC), places the frozen executable and its
    runtime payload, and registers a Start-menu entry, an optional sign-in task
    and an uninstaller that calls the executable's own ``uninstall`` subcommand.
    ``source_dir`` defaults to the absolute directory PyInstaller produces and
    ``output_dir`` to its parent, so the installer reads the payload the freezer
    wrote and writes the setup ``.exe`` beside it — the exact directory the CI
    hashes and uploads. It is only meaningful when Inno Setup's ``ISCC.exe`` and
    the frozen payload exist; this function writes the script, it does not run
    the compiler.
    """
    source = source_dir if source_dir is not None else _default_source_dir(output)
    installer_out = output_dir if output_dir is not None else _default_installer_output_dir(output)
    from .shell import SINGLE_INSTANCE_MUTEX

    script = f"""; Super AI Agent — Inno Setup script (generated)
; Build the payload first (PyInstaller spec), then compile with ISCC.exe.

#define AppName "{app_name}"
#define AppVersion "{app_version}"
#define AppExe "{exe_name}"
#define SourceDir "{source}"

[Setup]
AppId={{{{8F4B6C1E-2A7D-4E3B-9C5A-1D2E3F4A5B6C}}}}
AppName={{#AppName}}
AppVersion={{#AppVersion}}
AppPublisher=SUPER AI
DefaultDirName={{localappdata}}\\ArabicAIAgent
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
; Wait for a running copy to release the single-instance mutex (see
; arabic_agent.desktop.shell) instead of failing to overwrite the locked
; ArabicAIAgent.exe / _internal files with "Access is denied".
AppMutex={SINGLE_INSTANCE_MUTEX}
OutputBaseFilename=ArabicAIAgent-Setup-{{#AppVersion}}
OutputDir="{installer_out}"
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
    "bundle_datas",
    "entry_script",
    "main",
    "missing_datas",
    "package_source_dir",
]


if __name__ == "__main__":  # pragma: no cover - CLI
    main()
