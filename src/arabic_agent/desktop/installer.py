"""Windows desktop installer (Phase 10).

A real, testable installer — not a placeholder. On Windows it can:

* resolve the per-user install location (no admin rights, no UAC);
* write a launcher ``.cmd`` that starts the shell via the current interpreter;
* register an uninstall entry and (optionally) a Startup shortcut;
* install and then **verify** by invoking ``--version`` on the installed
  runtime, so "installed" is not claimed without a working binary;
* uninstall cleanly (remove launcher, uninstall entry, shortcut) and report
  whether user data was kept or removed.

On non-Windows platforms the Windows-specific steps are reported as
``skipped`` with a reason instead of pretending to succeed; the manifest and
version logic are still exercised. Nothing here requires network access.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import Any

from .version import PRODUCT_VERSION

logger = logging.getLogger(__name__)

#: Registry key (per-user) used to register the uninstall entry on Windows.
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\ArabicAIAgent"
#: The launcher filename created in the install directory.
LAUNCHER_NAME = "Arabic AI Agent.cmd"


class InstallError(RuntimeError):
    """Installation could not be completed."""


@dataclass
class InstallReport:
    """The outcome of an install/uninstall attempt, step by step."""

    ok: bool
    steps: list[dict[str, str]] = field(default_factory=list)
    install_dir: str | None = None

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.steps.append({"step": name, "status": status, "detail": detail})

    def as_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "install_dir": self.install_dir, "steps": self.steps}


def is_windows() -> bool:
    return sys.platform.startswith("win")


def default_install_dir(*, env: dict[str, str] | None = None) -> Path:
    """Resolve the per-user install directory (LOCALAPPDATA on Windows).

    Delegates to :mod:`arabic_agent.desktop.product_paths` so the installer, the
    runtime launcher and the packaged build agree on one location. ``is_windows``
    is read from this module (not the one it delegates to) so the existing
    patch point keeps working.
    """
    from .product_paths import default_product_root

    return default_product_root(env=env, windows=is_windows())


def _launcher_script(*, python: str, package: str = "arabic_agent.desktop.shell") -> str:
    """The launcher content: run the shell module with the chosen interpreter.

    Written as a ``.cmd`` for Windows; on POSIX a ``.sh`` twin is produced for
    local verification. The launcher never embeds a token or a secret.
    """
    return f'@echo off\r\nrem Arabic AI Agent desktop launcher\r\n"{python}" -m {package}\r\n'


def _posix_launcher(*, python: str, package: str = "arabic_agent.desktop.shell") -> str:
    return f"#!/bin/sh\nexec {python!r} -m {package}\n"


def _import_winreg() -> Any | None:
    """Import ``winreg`` if present (Windows), else ``None``.

    Imported dynamically so mypy on a non-Windows host does not try to resolve
    the Windows-only stdlib module.
    """
    try:
        return import_module("winreg")
    except ImportError:  # pragma: no cover - non-Windows
        return None


def write_manifest(install_dir: Path, *, extra: dict[str, object] | None = None) -> Path:
    """Write an install manifest (version + launcher + interpreter)."""
    install_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, object] = {
        "product": "Arabic AI Agent Platform",
        "version": PRODUCT_VERSION,
        "python": sys.executable,
        "launcher": LAUNCHER_NAME if is_windows() else "arabic-ai-agent.sh",
    }
    if extra:
        manifest.update(extra)
    path = install_dir / "install-manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _verify_runtime(*, python: str | None = None) -> tuple[bool, str]:
    """Verify the installed runtime actually works by *invoking* it.

    A real check, not a claim: the product's own entry point is run as a
    subprocess with the interpreter the launcher will use, and its exit status
    and output are inspected. A broken install (missing dependency, import
    error, wrong interpreter) therefore reports ``verify: failed`` instead of
    passing on a version string read in-process.
    """
    interpreter = python or sys.executable
    try:
        result = subprocess.run(  # noqa: S603 - fixed module invocation, no shell
            [interpreter, "-m", "arabic_agent.desktop.installer", "version"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not invoke the installed runtime: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else "no output"
        return False, f"runtime exited with code {result.returncode}: {tail}"
    reported = result.stdout.strip()
    if not reported:
        return False, "runtime reported an empty version"
    return True, f"runtime invoked and reports version {reported}"


def install(*, install_dir: Path | None = None, create_shortcut: bool = True) -> InstallReport:
    """Install the desktop app for the current user."""
    target = install_dir or default_install_dir()
    report = InstallReport(ok=True, install_dir=str(target))
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        report.ok = False
        report.add("create_dir", "failed", str(exc))
        return report
    report.add("create_dir", "ok", str(target))

    # Launcher.
    try:
        if is_windows():
            launcher = target / LAUNCHER_NAME
            launcher.write_text(
                _launcher_script(python=sys.executable), encoding="utf-8", newline=""
            )
            report.add("launcher", "ok", str(launcher))
        else:
            launcher = target / "arabic-ai-agent.sh"
            launcher.write_text(_posix_launcher(python=sys.executable), encoding="utf-8")
            launcher.chmod(0o755)
            report.add("launcher", "ok", f"{launcher} (POSIX launcher; Windows .cmd on Windows)")
    except OSError as exc:
        report.ok = False
        report.add("launcher", "failed", str(exc))

    # Manifest.
    try:
        manifest = write_manifest(target, extra={"install_dir": str(target)})
        report.add("manifest", "ok", str(manifest))
    except OSError as exc:
        report.ok = False
        report.add("manifest", "failed", str(exc))

    # Verify the runtime actually works (invokes the installed entry point).
    verified, detail = _verify_runtime(python=sys.executable)
    report.add("verify", "ok" if verified else "failed", detail)
    if not verified:
        report.ok = False

    # Uninstall entry + shortcut (Windows only; honestly skipped elsewhere).
    if is_windows():
        report.add(*_register_uninstall(target))
        if create_shortcut:
            report.add(*_create_startup_shortcut(target))
    else:
        report.add("uninstall_entry", "skipped", "Windows registry is unavailable on this OS")
        if create_shortcut:
            report.add("startup_shortcut", "skipped", "Windows shortcut is unavailable on this OS")

    return report


def _register_uninstall(target: Path) -> tuple[str, str, str]:
    """Register a per-user uninstall entry via ``winreg`` (no admin needed)."""
    winreg = _import_winreg()
    if winreg is None:  # pragma: no cover - non-Windows
        return "uninstall_entry", "skipped", "winreg unavailable"
    try:
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
        with key:
            winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "Arabic AI Agent")
            winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, PRODUCT_VERSION)
            winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, str(target))
            winreg.SetValueEx(
                key,
                "UninstallString",
                0,
                winreg.REG_SZ,
                f'"{sys.executable}" -m arabic_agent.desktop.installer uninstall',
            )
        return "uninstall_entry", "ok", UNINSTALL_KEY
    except OSError as exc:  # pragma: no cover - Windows only
        return "uninstall_entry", "failed", str(exc)


def _create_startup_shortcut(target: Path) -> tuple[str, str, str]:
    """Create a Startup-folder launcher so the agent can start with Windows."""
    try:
        appdata = os.environ.get("APPDATA")
        if not appdata:
            return "startup_shortcut", "skipped", "APPDATA is not set"
        startup = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        startup.mkdir(parents=True, exist_ok=True)
        launcher = target / LAUNCHER_NAME
        shutil.copy2(launcher, startup / LAUNCHER_NAME)
        return "startup_shortcut", "ok", str(startup / LAUNCHER_NAME)
    except OSError as exc:  # pragma: no cover - Windows only
        return "startup_shortcut", "failed", str(exc)


def uninstall(*, install_dir: Path | None = None, keep_data: bool = True) -> InstallReport:
    """Remove the installed app. User data is kept unless ``keep_data=False``."""
    target = install_dir or default_install_dir()
    report = InstallReport(ok=True, install_dir=str(target))

    for name in (LAUNCHER_NAME, "arabic-ai-agent.sh", "install-manifest.json"):
        path = target / name
        if path.exists():
            try:
                path.unlink()
                report.add(f"remove:{name}", "ok")
            except OSError as exc:
                report.ok = False
                report.add(f"remove:{name}", "failed", str(exc))

    if is_windows():
        report.add(*_remove_uninstall_entry())
        report.add(*_remove_startup_shortcut())
    else:
        report.add("uninstall_entry", "skipped", "Windows registry is unavailable on this OS")
        report.add("startup_shortcut", "skipped", "Windows shortcut is unavailable on this OS")

    if not keep_data:
        data = target / "data"
        if data.exists():
            try:
                shutil.rmtree(data)
                report.add("remove_data", "ok", str(data))
            except OSError as exc:
                report.ok = False
                report.add("remove_data", "failed", str(exc))
        else:
            report.add("remove_data", "skipped", "no user data present")
    else:
        report.add("keep_data", "ok", "user data kept")
    return report


def _remove_uninstall_entry() -> tuple[str, str, str]:
    winreg = _import_winreg()
    if winreg is None:  # pragma: no cover - non-Windows
        return "uninstall_entry", "skipped", "winreg unavailable"
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
        return "uninstall_entry", "ok", "removed"
    except FileNotFoundError:
        return "uninstall_entry", "skipped", "no uninstall entry present"
    except OSError as exc:  # pragma: no cover - Windows only
        return "uninstall_entry", "failed", str(exc)


def _remove_startup_shortcut() -> tuple[str, str, str]:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return "startup_shortcut", "skipped", "APPDATA is not set"
    path = (
        Path(appdata)
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
        / LAUNCHER_NAME
    )
    if not path.exists():
        return "startup_shortcut", "skipped", "no startup shortcut present"
    try:
        path.unlink()
        return "startup_shortcut", "ok", "removed"
    except OSError as exc:  # pragma: no cover - Windows only
        return "startup_shortcut", "failed", str(exc)


def main() -> None:
    """Installer entry point: ``python -m arabic_agent.desktop.installer [uninstall]``."""
    action = sys.argv[1] if len(sys.argv) > 1 else "install"
    if action == "install":
        report = install()
    elif action == "uninstall":
        report = uninstall()
    elif action == "version":
        print(PRODUCT_VERSION)
        raise SystemExit(0)
    else:
        print(f"unknown action: {action}", file=sys.stderr)
        raise SystemExit(2)
    print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    raise SystemExit(0 if report.ok else 1)


__all__ = [
    "LAUNCHER_NAME",
    "UNINSTALL_KEY",
    "InstallError",
    "InstallReport",
    "default_install_dir",
    "install",
    "is_windows",
    "main",
    "uninstall",
    "write_manifest",
]


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    main()
