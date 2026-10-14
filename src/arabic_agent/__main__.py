"""Product entry point.

One executable, several jobs. A packaged Windows build is a single
``ArabicAIAgent.exe``; this module is the dispatcher that decides what that
executable does from its first argument:

    ArabicAIAgent.exe                 -> open the desktop shell (default)
    ArabicAIAgent.exe shell           -> open the desktop shell
    ArabicAIAgent.exe runtime         -> run the local control-plane runtime
    ArabicAIAgent.exe install         -> install for the current user
    ArabicAIAgent.exe uninstall       -> remove the installed app
    ArabicAIAgent.exe selfcheck       -> verify this install works end to end
    ArabicAIAgent.exe version         -> print the product version

The same dispatch works unfrozen (``python -m arabic_agent runtime``), so the
launcher, the frozen executable and the developer workflow all share one entry.
The desktop runtime manager starts a frozen build by re-invoking the executable
with the ``runtime`` subcommand (see ``runtime_manager.runtime_command``).

Dispatch is deliberately explicit — an unknown subcommand is an error, never a
silent fallback — so a mistyped installer action cannot quietly open a window.

Frozen-build note: a PyInstaller executable runs its entry script as the
top-level ``__main__``, where a *relative* import (``from .desktop …``) fails
with "attempted relative import with no known parent package". The lazy imports
below therefore use the absolute ``arabic_agent.`` package path, which works
both when this module is frozen directly and when it is run as
``python -m arabic_agent``.
"""

from __future__ import annotations

import sys

_USAGE = (
    "usage: ArabicAIAgent [shell|runtime|install|uninstall|selfcheck|version]\n"
    "  (no argument)  open the desktop shell\n"
    "  shell          open the desktop shell\n"
    "  runtime        run the local control-plane runtime\n"
    "  install        install for the current user\n"
    "  uninstall      remove the installed app\n"
    "  selfcheck      verify this install works end to end\n"
    "  version        print the product version\n"
)


def _run_shell() -> int:
    from arabic_agent.desktop.shell import launch_shell

    return launch_shell()


def _run_runtime() -> None:
    from arabic_agent.local.main import main as runtime_main

    runtime_main()


def _run_install() -> int:
    from arabic_agent.desktop.installer import install

    report = install()
    print(report.as_dict())
    return 0 if report.ok else 1


def _run_uninstall() -> int:
    from arabic_agent.desktop.installer import uninstall

    report = uninstall()
    print(report.as_dict())
    return 0 if report.ok else 1


def _print_version() -> int:
    from arabic_agent.desktop.version import PRODUCT_VERSION

    print(PRODUCT_VERSION)
    return 0


def _run_selfcheck() -> int:
    from arabic_agent.desktop.selfcheck import main as selfcheck_main

    return selfcheck_main([])


def main(argv: list[str] | None = None) -> int:
    """Dispatch the product entry point; return a process exit code."""
    args = list(sys.argv[1:] if argv is None else argv)
    command = args[0] if args else "shell"

    if command in {"shell", "desktop", "gui"}:
        return _run_shell()
    if command in {"runtime", "local"}:
        _run_runtime()
        return 0
    if command == "install":
        return _run_install()
    if command == "uninstall":
        return _run_uninstall()
    if command == "selfcheck":
        return _run_selfcheck()
    if command in {"version", "--version", "-V"}:
        return _print_version()

    print(_USAGE, file=sys.stderr)
    return 2


__all__ = ["main"]


if __name__ == "__main__":  # pragma: no cover - exercised via the entry point
    raise SystemExit(main())
