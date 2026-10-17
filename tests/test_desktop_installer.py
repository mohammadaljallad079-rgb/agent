"""Windows desktop installer tests.

The installer is a real one. On this (non-Windows) host the Windows-only steps
(registry entry, Startup shortcut) must be reported as *skipped with a reason*,
never as success. The launcher, manifest and verification steps run for real and
are checked on disk. Nothing here touches the network or the registry.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from arabic_agent.branding import PRODUCT_ID
from arabic_agent.desktop.installer import (
    LAUNCHER_NAME,
    InstallReport,
    default_install_dir,
    install,
    is_windows,
    uninstall,
    write_manifest,
)
from arabic_agent.desktop.version import PRODUCT_VERSION, product_version


def _statuses(report: InstallReport) -> dict[str, str]:
    return {step["step"]: step["status"] for step in report.steps}


def test_default_install_dir_uses_localappdata_on_windows(monkeypatch):
    import arabic_agent.desktop.installer as installer

    monkeypatch.setattr(installer, "is_windows", lambda: True)
    path = default_install_dir(env={"LOCALAPPDATA": r"C:\Users\me\AppData\Local"})
    assert path.name == "ArabicAIAgent"
    assert "AppData" in str(path)


def test_default_install_dir_falls_back_without_env():
    path = default_install_dir(env={})
    assert path.name == "ArabicAIAgent"


def test_version_is_a_single_source():
    assert product_version() == PRODUCT_VERSION


def test_verify_runtime_invokes_the_entry_point(tmp_path: Path):
    """The verify step really invokes the runtime, and reports failure honestly."""
    from arabic_agent.desktop.installer import _verify_runtime

    ok, detail = _verify_runtime(python=sys.executable)
    assert ok is True
    assert PRODUCT_VERSION in detail
    assert "invoked" in detail

    # A broken interpreter must fail the check rather than pass on a constant.
    failed, why = _verify_runtime(python=str(tmp_path / "does-not-exist"))
    assert failed is False
    assert "could not invoke" in why


def test_install_reports_failed_verify_when_runtime_is_broken(tmp_path: Path, monkeypatch):
    """A broken install reports verify: failed and ok=False (no false green)."""
    import arabic_agent.desktop.installer as installer

    monkeypatch.setattr(
        installer, "_verify_runtime", lambda *, python=None: (False, "broken interpreter")
    )
    report = installer.install(install_dir=tmp_path / "install", create_shortcut=False)
    assert report.ok is False
    assert _statuses(report)["verify"] == "failed"


def test_install_writes_launcher_manifest_and_verifies(tmp_path: Path):
    target = tmp_path / "install"
    report = install(install_dir=target)
    assert report.ok is True
    statuses = _statuses(report)
    assert statuses["create_dir"] == "ok"
    assert statuses["launcher"] == "ok"
    assert statuses["manifest"] == "ok"
    assert statuses["verify"] == "ok"

    manifest = json.loads((target / "install-manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == PRODUCT_VERSION
    assert manifest["product"] == PRODUCT_ID
    assert manifest["python"]

    if is_windows():
        assert (target / LAUNCHER_NAME).is_file()
    else:
        # The POSIX launcher twin is produced for local verification, and the
        # Windows-only steps are honestly skipped rather than faked.
        launcher = target / "arabic-ai-agent.sh"
        assert launcher.is_file()
        assert "arabic_agent.desktop.shell" in launcher.read_text(encoding="utf-8")
        assert statuses["uninstall_entry"] == "skipped"
        assert statuses["startup_shortcut"] == "skipped"


def test_install_launcher_has_no_secret(tmp_path: Path):
    target = tmp_path / "install"
    install(install_dir=target, create_shortcut=False)
    for launcher in target.glob("arabic-ai-agent.*"):
        content = launcher.read_text(encoding="utf-8")
        assert "token" not in content.lower()
        assert "secret" not in content.lower()


def test_install_reports_stepwise_result(tmp_path: Path):
    report = install(install_dir=tmp_path / "install", create_shortcut=False)
    payload = report.as_dict()
    assert payload["ok"] is True
    assert isinstance(payload["steps"], list)
    assert all({"step", "status", "detail"} <= set(s) for s in payload["steps"])


def test_write_manifest_extra_fields(tmp_path: Path):
    path = write_manifest(tmp_path / "m", extra={"install_dir": "/x"})
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["install_dir"] == "/x"


def test_uninstall_removes_launcher_but_keeps_data(tmp_path: Path):
    target = tmp_path / "install"
    install(install_dir=target, create_shortcut=False)
    (target / "data").mkdir()
    (target / "data" / "local.db").write_bytes(b"db")

    report = uninstall(install_dir=target)
    assert report.ok is True
    statuses = _statuses(report)
    assert statuses["keep_data"] == "ok"
    assert not (target / "install-manifest.json").exists()
    assert (target / "data" / "local.db").exists()


def test_uninstall_can_remove_data(tmp_path: Path):
    target = tmp_path / "install"
    install(install_dir=target, create_shortcut=False)
    (target / "data").mkdir()
    (target / "data" / "local.db").write_bytes(b"db")

    report = uninstall(install_dir=target, keep_data=False)
    assert _statuses(report)["remove_data"] == "ok"
    assert not (target / "data").exists()


def test_installer_main_reports_json(tmp_path: Path, monkeypatch, capsys):
    import arabic_agent.desktop.installer as installer

    monkeypatch.setattr(installer, "default_install_dir", lambda: tmp_path / "install")
    monkeypatch.setattr("sys.argv", ["installer", "install"])
    try:
        installer.main()
    except SystemExit as exc:
        assert exc.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True


def test_installer_main_version(capsys):
    import arabic_agent.desktop.installer as installer

    original = __import__("sys").argv
    __import__("sys").argv = ["installer", "version"]
    try:
        installer.main()
    except SystemExit as exc:
        assert exc.code == 0
    finally:
        __import__("sys").argv = original
    assert capsys.readouterr().out.strip() == PRODUCT_VERSION
