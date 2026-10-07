"""Bundled-resource resolution that works in source, wheel and frozen builds.

The product ships non-Python assets (the desktop UI and the Alembic migration
chains). They must be found in three different layouts:

* **source checkout** — ``<repo>/src/arabic_agent/...`` and ``<repo>/migrations*``;
* **installed wheel** — inside the installed ``arabic_agent`` package, where the
  local chain is force-included as ``arabic_agent/_migrations_local``;
* **PyInstaller freeze** — extracted under ``sys._MEIPASS`` at run time, with the
  package placed there too.

The one rule that holds everywhere is that a resource is addressed *relative to
the importable ``arabic_agent`` package directory* (``Path(arabic_agent.__file__)
.parent``). That directory is correct in a source tree, in an installed wheel,
and in a frozen build (PyInstaller sets ``sys._MEIPASS`` and puts the package
there). Nothing here reads the current working directory, so the product behaves
identically no matter where it is launched from.
"""

from __future__ import annotations

import sys
from pathlib import Path

import arabic_agent

#: Directory of the importable ``arabic_agent`` package (the freeze bundle root
#: under PyInstaller, the package dir otherwise).
PACKAGE_DIR = Path(arabic_agent.__file__).resolve().parent

#: Repository-root migration directory name → the name it is bundled under inside
#: the package. Both chains the product can ship are listed so one resolver covers
#: the local SQLite chain and the cloud Postgres chain.
PACKAGED_MIGRATION_NAMES: dict[str, str] = {
    "migrations_local": "_migrations_local",
    "migrations": "_migrations_cloud",
}


def is_frozen() -> bool:
    """Whether the process is a PyInstaller-frozen build."""
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path:
    """Root under which bundled data files live.

    PyInstaller extracts data to ``sys._MEIPASS``; every other layout puts the
    package's data next to the package itself.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return PACKAGE_DIR


def packaged_migrations_dir(name: str) -> Path | None:
    """The migration chain shipped *inside* the package, if present.

    ``name`` is the repository directory name (``migrations_local`` or
    ``migrations``); it maps to the bundled name (``_migrations_local`` /
    ``_migrations_cloud``). Returns ``None`` when that directory is absent — a
    source checkout that has not been packaged — never a guessed path.
    """
    bundled = PACKAGED_MIGRATION_NAMES.get(name, f"_{name}")
    candidate = PACKAGE_DIR / bundled
    return candidate if candidate.is_dir() else None


def source_migrations_dir(name: str) -> Path | None:
    """A migration chain next to the repository root, if this is a source tree.

    ``PACKAGE_DIR`` is ``<repo>/src/arabic_agent`` in a checkout, so the repo
    root is two levels up. Returns ``None`` when that layout is not present
    (an installed wheel or a frozen build), never a guessed path.
    """
    candidate = PACKAGE_DIR.parents[1] / name
    return candidate if candidate.is_dir() else None


def resolve_migrations_dir(name: str) -> Path | None:
    """Locate a migration chain, preferring the packaged copy over the source one."""
    return packaged_migrations_dir(name) or source_migrations_dir(name)


__all__ = [
    "PACKAGED_MIGRATION_NAMES",
    "PACKAGE_DIR",
    "bundle_dir",
    "is_frozen",
    "packaged_migrations_dir",
    "resolve_migrations_dir",
    "source_migrations_dir",
]
