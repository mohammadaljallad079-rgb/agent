"""Product entry-point dispatch tests.

The dispatcher is what a packaged ``ArabicAIAgent.exe`` runs. These tests prove
the mapping from argument to job without launching a window or a runtime: the
``shell`` and ``runtime`` jobs are patched, and ``version`` is exercised for
real. An unknown subcommand must be a hard error, never a silent fallback.
"""

from __future__ import annotations

import arabic_agent.__main__ as entry


def test_default_command_is_shell(monkeypatch):
    called: list[bool] = []
    monkeypatch.setattr(entry, "_run_shell", lambda: called.append(True) or 0)
    assert entry.main([]) == 0
    assert called == [True]


def test_shell_aliases(monkeypatch):
    for alias in ("shell", "desktop", "gui"):
        called: list[bool] = []
        monkeypatch.setattr(entry, "_run_shell", lambda c=called: c.append(True) or 0)
        assert entry.main([alias]) == 0
        assert called == [True]


def test_runtime_dispatches(monkeypatch):
    called: list[bool] = []
    monkeypatch.setattr(entry, "_run_runtime", lambda: called.append(True))
    assert entry.main(["runtime"]) == 0
    assert called == [True]


def test_install_and_uninstall_dispatch(monkeypatch):
    class _Report:
        ok = True

        def as_dict(self):
            return {"ok": True}

    monkeypatch.setattr(entry, "_run_install", lambda: 0)
    monkeypatch.setattr(entry, "_run_uninstall", lambda: 0)
    assert entry.main(["install"]) == 0
    assert entry.main(["uninstall"]) == 0


def test_version_prints_the_product_version(capsys):
    assert entry.main(["version"]) == 0
    out = capsys.readouterr().out.strip()
    assert out  # a real version string


def test_unknown_command_is_an_error(capsys):
    assert entry.main(["frobnicate"]) == 2
    assert "usage:" in capsys.readouterr().err


def test_short_version_flags():
    assert entry.main(["--version"]) == 0
    assert entry.main(["-V"]) == 0
