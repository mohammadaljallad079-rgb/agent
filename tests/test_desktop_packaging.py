"""Product directory layout and packaging-input tests."""

from __future__ import annotations

from pathlib import Path

from arabic_agent.desktop import packaging, product_paths
from arabic_agent.desktop.packaging import build_installer_script, build_pyinstaller_spec
from arabic_agent.desktop.product_paths import (
    APP_DIR_NAME,
    ROOT_ENV,
    default_product_root,
    resolve_product_paths,
)


# --- Product paths ----------------------------------------------------------
def test_root_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv(ROOT_ENV, str(tmp_path / "custom"))
    assert default_product_root() == tmp_path / "custom"


def test_windows_root_uses_localappdata():
    env = {"LOCALAPPDATA": "C:\\Users\\Ahmed\\AppData\\Local"}
    root = default_product_root(env=env, windows=True)
    assert root.name == APP_DIR_NAME
    assert "AppData" in str(root)


def test_layout_is_separated_and_named(tmp_path):
    paths = resolve_product_paths(env={ROOT_ENV: str(tmp_path)})
    assert paths.root == tmp_path
    assert paths.data_dir == tmp_path / "data"
    assert paths.models_dir == tmp_path / "models"
    assert paths.logs_dir == tmp_path / "logs"
    assert paths.browser_dir == tmp_path / "browser"
    assert paths.cache_dir == tmp_path / "cache"
    assert paths.config_dir == tmp_path / "config"


def test_ensure_creates_every_directory(tmp_path):
    paths = resolve_product_paths(env={ROOT_ENV: str(tmp_path / "prod")}).ensure()
    assert all(p.is_dir() for p in paths.all())


def test_env_overrides_respect_existing_values(monkeypatch, tmp_path):
    monkeypatch.setenv("AAP_LOCAL_STORE_DIR", "/already/set")
    paths = resolve_product_paths(env={ROOT_ENV: str(tmp_path)})
    overrides = paths.env_overrides()
    # An already-set variable is left alone (not echoed back), so the operator's
    # explicit configuration always wins.
    assert "AAP_LOCAL_STORE_DIR" not in overrides
    assert overrides["AAP_PRODUCT_DATA_DIR"] == str(tmp_path)
    assert overrides["AAP_LOG_DIR"] == str(tmp_path / "logs")


# --- Packaging inputs -------------------------------------------------------
def test_pyinstaller_entry_is_a_real_script_path(tmp_path):
    """Regression: ``Analysis`` must receive a script *path*, not a module name.

    PyInstaller resolves the first ``Analysis`` argument as a file path; a module
    name (``arabic_agent.__main__``) fails with "script ... not found".
    """
    from arabic_agent.desktop.packaging import entry_script

    script = entry_script()
    assert script.is_file()
    assert script.name == "__main__.py"
    assert script.parent.name == "arabic_agent"

    text = build_pyinstaller_spec(output=tmp_path / "ArabicAIAgent.spec").read_text("utf-8")
    assert f"Analysis(\n    [{str(script)!r}]" in text
    # The old, broken module-name form must not come back.
    assert "Analysis(['arabic_agent.__main__']" not in text
    assert "'arabic_agent.__main__'" not in text


def test_pyinstaller_spec_collects_data_and_entry(tmp_path):
    from arabic_agent.desktop.packaging import entry_script

    spec = build_pyinstaller_spec(output=tmp_path / "ArabicAIAgent.spec")
    text = spec.read_text(encoding="utf-8")
    assert str(entry_script()) in text  # real entry script on disk
    assert "arabic_agent/desktop/ui" in text
    assert "arabic_agent/_migrations_local" in text
    assert "console=False" in text


def test_every_data_source_exists(tmp_path):
    """Every ``datas`` source must exist, or PyInstaller fails hard."""
    from arabic_agent.desktop.packaging import bundle_datas, missing_datas

    assert missing_datas() == []
    for source, dest in bundle_datas():
        assert source.exists(), source
        assert dest.startswith("arabic_agent/")


def test_both_migration_chains_are_packaged(tmp_path):
    """Local *and* cloud chains are packaged to their bundled destinations."""
    from arabic_agent.desktop.packaging import bundle_datas

    by_dest = {dest: source for source, dest in bundle_datas()}
    local_src = by_dest["arabic_agent/_migrations_local"]
    cloud_src = by_dest["arabic_agent/_migrations_cloud"]
    # Sources are the *tracked* directories, packaged directly (no source-tree copy).
    assert local_src.name == "migrations_local"
    assert cloud_src.name == "migrations"
    for src in (local_src, cloud_src):
        assert (src / "env.py").is_file()
        assert (src / "versions").is_dir()
        assert any((src / "versions").glob("*.py"))

    # The generated spec lists both, and the local destination matches what the
    # runtime resolves (arbitrary_agent/_migrations_local).
    text = build_pyinstaller_spec(output=tmp_path / "ArabicAIAgent.spec").read_text("utf-8")
    assert "arabic_agent/_migrations_local" in text
    assert "arabic_agent/_migrations_cloud" in text


def test_frozen_manifest_excludes_secrets_models_and_user_data(tmp_path):
    """The frozen manifest must never bundle secrets, models or runtime data."""
    text = build_pyinstaller_spec(output=tmp_path / "ArabicAIAgent.spec").read_text("utf-8")
    forbidden = (".env", ".gguf", "models", "cache", "cookies", "node_modules", ".venv", "token")
    lowered = text.lower()
    for needle in forbidden:
        assert needle not in lowered, needle


def test_pyinstaller_spec_is_one_folder_for_the_installer(tmp_path):
    """The spec must produce the <dist>/ArabicAIAgent/ folder Inno installs."""
    text = build_pyinstaller_spec(output=tmp_path / "ArabicAIAgent.spec").read_text("utf-8")
    assert "exclude_binaries=True" in text
    assert "coll = COLLECT(" in text


def test_pyinstaller_spec_declares_dynamic_hidden_imports(tmp_path):
    """Dynamically loaded modules must be named or the frozen app crashes.

    Regression: a frozen run raised ``No module named 'aiosqlite'`` then
    ``No module named 'logging.config'`` because SQLAlchemy and Alembic load
    them by name and PyInstaller's static analysis cannot see them.
    """
    text = build_pyinstaller_spec(output=tmp_path / "ArabicAIAgent.spec").read_text("utf-8")
    assert "'aiosqlite'" in text
    assert "'sqlalchemy.dialects.sqlite.aiosqlite'" in text
    assert "'logging.config'" in text
    assert 'collect_submodules("alembic", on_error="ignore")' in text
    assert 'not m.startswith("alembic.testing")' in text
    # The lazily-imported product subcommands are present too.
    assert "'arabic_agent.local.main'" in text
    assert "'arabic_agent.desktop.shell'" in text
    assert "'arabic_agent.desktop.installer'" in text


def test_installer_source_dir_matches_pyinstaller_output(tmp_path):
    """Regression: Inno's SourceDir must equal PyInstaller's one-folder output."""
    out = tmp_path / "ArabicAIAgent.spec"
    from arabic_agent.desktop.packaging import _default_source_dir

    iss = build_installer_script(output=out).read_text(encoding="utf-8")
    expected = str(_default_source_dir(out))
    assert f'#define SourceDir "{expected}"' in iss
    # PyInstaller's one-folder mode writes <dist>/ArabicAIAgent/.
    assert expected.endswith(str(Path("ArabicAIAgent")))
    # The Inno [Files] entry reads the payload from SourceDir and the exe name
    # from AppExe, so both tools agree on the exact file and folder.
    assert '#define AppExe "ArabicAIAgent.exe"' in iss
    assert 'Source: "{#SourceDir}\\{#AppExe}"' in iss
    assert 'Source: "{#SourceDir}\\_internal\\*"' in iss


def test_installer_output_dir_is_the_uploaded_directory(tmp_path):
    """The setup exe lands in dist/ (where CI hashes and uploads it)."""
    from arabic_agent.desktop.packaging import _default_installer_output_dir

    out = tmp_path / "ArabicAIAgent.spec"
    iss = build_installer_script(output=out).read_text(encoding="utf-8")
    expected = str(_default_installer_output_dir(out))
    assert f'OutputDir="{expected}"' in iss
    assert "OutputBaseFilename=ArabicAIAgent-Setup-{#AppVersion}" in iss


def test_installer_script_is_per_user_and_uninstallable(tmp_path):
    iss = build_installer_script(
        output=tmp_path / "ArabicAIAgent.iss",
        app_version="1.2.3",
        source_dir=tmp_path / "payload",
    )
    text = iss.read_text(encoding="utf-8")
    assert "PrivilegesRequired=lowest" in text  # no UAC
    assert "{localappdata}" in text
    assert '#define AppVersion "1.2.3"' in text
    # The output name is a preprocessor define expanded by ISCC at compile time.
    assert "ArabicAIAgent-Setup-{#AppVersion}" in text
    assert "ArabicAIAgent.exe" in text
    assert "[Icons]" in text and "userstartup" in text


def test_packaging_cli_writes_both_inputs(tmp_path):
    import subprocess
    import sys

    result = subprocess.run(  # noqa: S603 - fixed module invocation in a test
        [sys.executable, "-m", "arabic_agent.desktop.packaging", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "ArabicAIAgent.spec").is_file()
    assert (tmp_path / "ArabicAIAgent.iss").is_file()


def test_packaging_module_exports():
    assert packaging.PRODUCT_EXE_NAME == "ArabicAIAgent.exe"
    assert Path(packaging.__file__).name == "packaging.py"
    assert product_paths.APP_DIR_NAME == "ArabicAIAgent"


# --- Build script + Windows CI must use the corrected flow ------------------
def test_build_script_uses_production_packaging_generator():
    script = Path(__file__).resolve().parents[1] / "scripts" / "build_windows.ps1"
    text = script.read_text(encoding="utf-8")
    assert "arabic_agent.desktop.packaging" in text  # regenerates inputs
    assert "ArabicAIAgent.spec" in text
    assert "pyinstaller" in text.lower()
    assert "ArabicAIAgent.iss" in text
    assert "ISCC.exe" in text
    # It must regenerate the spec rather than trust a stale generated file.
    assert "SkipFreeze" in text


def test_windows_ci_uses_corrected_build_flow():
    workflow = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "windows.yml"
    ).read_text(encoding="utf-8")
    assert "build_windows.ps1" in workflow
    # It must not hand-roll the old, broken sequence.
    assert "python -m arabic_agent.desktop.packaging dist" not in workflow
    assert "pyinstaller --clean --noconfirm dist\\ArabicAIAgent.spec" not in workflow
    assert "ArabicAIAgent-Setup-*.exe" in workflow
