"""Per-user production directories.

A packaged desktop install must never write next to the executable (Program
Files is read-only) or into the current working directory. It writes everything
under a single per-user application root:

* Windows — ``%LOCALAPPDATA%\\ArabicAIAgent`` (no admin rights, no UAC);
* macOS — ``~/Library/Application Support/ArabicAIAgent``;
* other — ``$XDG_DATA_HOME/ArabicAIAgent`` or ``~/.local/share/ArabicAIAgent``.

Inside that root the product keeps clearly separated subtrees — durable data,
downloaded models, logs, cache, config and browser state — so a user can find,
back up, or remove one without touching the others, and so an uninstall can keep
or delete user data as a deliberate choice.

The layout is derived from a single function so the installer, the runtime
launcher and the packaged build all agree on where things live. Nothing here
creates a directory unless asked, and nothing here reads a secret.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

#: Environment variable that overrides the whole application root (tests, or a
#: portable install on a USB stick).
ROOT_ENV = "AAP_PRODUCT_DATA_DIR"

#: The application folder name, shared with the installer.
APP_DIR_NAME = "ArabicAIAgent"


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def default_product_root(*, env: dict[str, str] | None = None, windows: bool | None = None) -> Path:
    """Resolve the per-user application root for the current platform.

    ``windows`` overrides the platform probe, so the installer (which already
    exposes a patchable ``is_windows``) and tests can exercise the Windows
    layout on any host.
    """
    environment = env if env is not None else os.environ
    override = environment.get(ROOT_ENV)
    if override:
        return Path(override).expanduser()
    on_windows = _is_windows() if windows is None else windows
    if on_windows:
        base = environment.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = environment.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_DIR_NAME


@dataclass(frozen=True)
class ProductPaths:
    """The full set of per-user directories the product uses."""

    root: Path
    data_dir: Path
    models_dir: Path
    logs_dir: Path
    cache_dir: Path
    config_dir: Path
    browser_dir: Path

    def all(self) -> tuple[Path, ...]:
        return (
            self.root,
            self.data_dir,
            self.models_dir,
            self.logs_dir,
            self.cache_dir,
            self.config_dir,
            self.browser_dir,
        )

    def ensure(self) -> ProductPaths:
        """Create every directory (idempotent)."""
        for path in self.all():
            path.mkdir(parents=True, exist_ok=True)
        return self

    def env_overrides(self) -> dict[str, str]:
        """Environment variables that point the runtime at these directories.

        Only values the operator has not already set are produced, so an
        explicit configuration always wins over the product default.
        """
        overrides: dict[str, str] = {"AAP_PRODUCT_DATA_DIR": str(self.root)}
        if not os.environ.get("AAP_LOCAL_STORE_DIR"):
            overrides["AAP_LOCAL_STORE_DIR"] = str(self.data_dir)
        if not os.environ.get("AAP_LOCAL_MODEL_STORAGE_DIR"):
            overrides["AAP_LOCAL_MODEL_STORAGE_DIR"] = str(self.models_dir)
        if not os.environ.get("AAP_LOG_DIR"):
            overrides["AAP_LOG_DIR"] = str(self.logs_dir)
        if not os.environ.get("AAP_BROWSER_USER_DATA_DIR"):
            overrides["AAP_BROWSER_USER_DATA_DIR"] = str(self.browser_dir)
        return overrides


def resolve_product_paths(*, env: dict[str, str] | None = None) -> ProductPaths:
    """Build the directory layout for the current platform and environment."""
    root = default_product_root(env=env)
    return ProductPaths(
        root=root,
        data_dir=root / "data",
        models_dir=root / "models",
        logs_dir=root / "logs",
        cache_dir=root / "cache",
        config_dir=root / "config",
        browser_dir=root / "browser",
    )


__all__ = [
    "APP_DIR_NAME",
    "ROOT_ENV",
    "ProductPaths",
    "default_product_root",
    "resolve_product_paths",
]
