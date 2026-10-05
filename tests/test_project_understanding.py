"""Phase 6: project / repository understanding."""

from __future__ import annotations

from pathlib import Path

import pytest

from arabic_agent.project.summary import read_manifest_summary, summarize_project

pytestmark = pytest.mark.asyncio


def _make_project(root: Path) -> None:
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "pkg" / "app.py").write_text("def main():\n    return 0\n", encoding="utf-8")
    (root / "src" / "pkg" / "util.ts").write_text("export const x = 1;\n", encoding="utf-8")
    (root / "tests" / "test_app.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8"
    )
    (root / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "1.2.3"\n', encoding="utf-8"
    )
    (root / "package.json").write_text('{"name": "frontend", "version": "0.1.0"}', encoding="utf-8")
    (root / "README.md").write_text("# Demo\n", encoding="utf-8")
    (root / "ARCHITECTURE.md").write_text("# Arch\n", encoding="utf-8")
    (root / "main.py").write_text("print('hi')\n", encoding="utf-8")
    # Noise that must be skipped.
    (root / "node_modules").mkdir()
    (root / "node_modules" / "dep.js").write_text("x", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("x", encoding="utf-8")


async def test_summarize_detects_languages_manifests_tests_docs(tmp_path: Path) -> None:
    _make_project(tmp_path)
    summary = summarize_project(tmp_path)
    languages = dict(summary.languages)
    assert languages.get("Python", 0) >= 2
    assert languages.get("TypeScript", 0) >= 1
    assert summary.primary_language in {"Python", "TypeScript"}
    manifest_paths = {m.path for m in summary.manifests}
    assert "pyproject.toml" in manifest_paths
    assert "package.json" in manifest_paths
    assert any(t.path.endswith("test_app.py") for t in summary.tests)
    assert any(d.path == "README.md" for d in summary.docs)
    assert any(d.path == "ARCHITECTURE.md" for d in summary.docs)
    assert any(e.path == "main.py" for e in summary.entrypoints)
    # Noise excluded.
    assert not any("node_modules" in m.path for m in summary.manifests)
    assert not any(".git" in t.path for t in summary.tests)


async def test_summarize_is_bounded(tmp_path: Path) -> None:
    for i in range(50):
        (tmp_path / f"f{i}.py").write_text("x = 1\n", encoding="utf-8")
    summary = summarize_project(tmp_path, max_files=10)
    assert summary.truncated is True
    assert summary.total_files <= 11


async def test_read_manifest_summary_flags_secrets(tmp_path: Path) -> None:
    manifest = tmp_path / "package.json"
    manifest.write_text(
        '{"name": "x", "token": "ghp_abcdefghijklmnopqrstuvwxyz0123456789"}', encoding="utf-8"
    )
    summary = read_manifest_summary(manifest)
    assert summary["name"] == "x"
    assert "github_token" in summary["sensitive_kinds"]


async def test_read_manifest_summary_never_returns_secret_value(tmp_path: Path) -> None:
    manifest = tmp_path / "package.json"
    secret = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    manifest.write_text('{"name": "x", "token": "' + secret + '"}', encoding="utf-8")
    summary = read_manifest_summary(manifest)
    assert secret not in str(summary)
