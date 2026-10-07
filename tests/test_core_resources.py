"""Bundled-resource resolution and frozen-entry regression tests.

These cover the two shapes a packaging/launch problem actually takes:

* resources (UI, migration chains) must be found from *source*, a *wheel* and a
  *freeze* without any cwd assumption;
* the frozen entry module must not use relative imports, which fail under
  PyInstaller's top-level ``__main__``.
"""

from __future__ import annotations

import ast
from pathlib import Path

from arabic_agent.core import resources
from arabic_agent.local.persistence import database


# --- Resource resolution ----------------------------------------------------
def test_package_dir_is_the_importable_package():
    assert resources.PACKAGE_DIR.name == "arabic_agent"
    assert (resources.PACKAGE_DIR / "__init__.py").is_file()


def test_migration_names_cover_local_and_cloud():
    assert resources.PACKAGED_MIGRATION_NAMES["migrations_local"] == "_migrations_local"
    assert resources.PACKAGED_MIGRATION_NAMES["migrations"] == "_migrations_cloud"


def test_source_migrations_resolve_in_a_checkout():
    local = resources.source_migrations_dir("migrations_local")
    cloud = resources.source_migrations_dir("migrations")
    assert local is not None and (local / "env.py").is_file()
    assert cloud is not None and (cloud / "env.py").is_file()


def test_packaged_migrations_none_when_not_bundled():
    # The package source tree does not contain the force-included copy.
    assert resources.packaged_migrations_dir("migrations_local") is None


def test_packaged_copy_wins_when_present(monkeypatch, tmp_path):
    """When the bundled copy exists, the resolver prefers it over the source tree.

    This is the frozen/wheel layout: ``_migrations_local`` sits next to the
    package, and the runtime must resolve *that*, not a repo-relative path.
    """
    fake_pkg = tmp_path / "arabic_agent"
    bundled = fake_pkg / "_migrations_local"
    (bundled / "versions").mkdir(parents=True)
    (bundled / "env.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(resources, "PACKAGE_DIR", fake_pkg)

    resolved = resources.packaged_migrations_dir("migrations_local")
    assert resolved == bundled
    assert database._default_migrations_dir() == bundled


def test_local_default_dir_is_a_real_chain_in_source():
    resolved = database._default_migrations_dir()
    assert resolved.is_dir()
    assert (resolved / "env.py").is_file()
    assert (resolved / "versions").is_dir()
    assert any((resolved / "versions").glob("*.py"))


def test_bundle_dir_prefers_meipass(monkeypatch, tmp_path):
    monkeypatch.delattr(resources.sys, "_MEIPASS", raising=False)
    assert resources.bundle_dir() == resources.PACKAGE_DIR
    monkeypatch.setattr(resources.sys, "_MEIPASS", str(tmp_path), raising=False)
    assert resources.bundle_dir() == tmp_path


def test_is_frozen_reflects_sys_frozen(monkeypatch):
    monkeypatch.delattr(resources.sys, "frozen", raising=False)
    assert resources.is_frozen() is False
    monkeypatch.setattr(resources.sys, "frozen", True, raising=False)
    assert resources.is_frozen() is True


# --- Frozen-entry safety ----------------------------------------------------
def _relative_imports(module: Path) -> list[ast.ImportFrom]:
    tree = ast.parse(module.read_text(encoding="utf-8"))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level and node.level > 0
    ]


def test_entry_module_has_no_relative_imports():
    """A frozen ``__main__`` runs as a top-level script; relative imports fail.

    Regression: ``from .desktop.shell import launch_shell`` raised
    "attempted relative import with no known parent package" in the frozen exe.
    """
    entry = resources.PACKAGE_DIR / "__main__.py"
    assert _relative_imports(entry) == []


def test_entry_module_still_dispatches_when_run_as_main(monkeypatch):
    import arabic_agent.__main__ as entry

    called: list[bool] = []
    monkeypatch.setattr(entry, "_run_runtime", lambda: called.append(True))
    assert entry.main(["runtime"]) == 0
    assert called == [True]
