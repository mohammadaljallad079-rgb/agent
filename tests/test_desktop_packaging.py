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
def test_pyinstaller_spec_collects_data_and_entry(tmp_path):
    spec = build_pyinstaller_spec(output=tmp_path / "ArabicAIAgent.spec")
    text = spec.read_text(encoding="utf-8")
    assert "arabic_agent.__main__" in text
    assert "arabic_agent/desktop/ui" in text
    assert "arabic_agent/_migrations_local" in text
    assert "console=False" in text


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
