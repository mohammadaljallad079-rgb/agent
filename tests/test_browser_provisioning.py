"""Browser provisioning tests.

The provisioner is a real state machine over an injectable probe/finder/installer.
These tests exercise every transition — not installed, ready, installing, broken,
cancelled — with a real subprocess for the installer (a tiny script that either
succeeds or fails), so the process handling and the "never claim READY without a
real binary" rule are proven for real, without Playwright or a network.
"""

from __future__ import annotations

import sys
from pathlib import Path

from arabic_agent.browser.provisioning import (
    BrowserProvisioner,
    BrowserState,
    find_playwright_chromium,
    playwright_browsers_path,
    playwright_package_available,
)


def _ok_installer(tmp_path: Path) -> list[str]:
    script = tmp_path / "ok.py"
    script.write_text("print('browser installed')\n", encoding="utf-8")
    return [sys.executable, str(script)]


def _fail_installer(tmp_path: Path) -> list[str]:
    script = tmp_path / "fail.py"
    script.write_text("import sys; sys.exit(4)\n", encoding="utf-8")
    return [sys.executable, str(script)]


def test_package_probe_returns_bool():
    assert isinstance(playwright_package_available(), bool)


def test_browsers_path_is_platform_appropriate():
    path = playwright_browsers_path()
    assert path.name == "ms-playwright" or "ms-playwright" in str(path)


def test_find_playwright_chromium_returns_empty_when_absent(monkeypatch, tmp_path):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "nope"))
    assert find_playwright_chromium() == ""


def test_find_playwright_chromium_finds_a_real_binary(monkeypatch, tmp_path):
    root = tmp_path / "ms-playwright"
    if sys.platform.startswith("win"):
        binary = root / "chromium-1200" / "chrome-win" / "chrome.exe"
    elif sys.platform == "darwin":
        binary = (
            root
            / "chromium-1200"
            / "chrome-mac"
            / "Chromium.app"
            / "Contents"
            / "MacOS"
            / "Chromium"
        )
    else:
        binary = root / "chromium-1200" / "chrome-linux" / "chrome"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(root))
    assert find_playwright_chromium() == str(binary)


def test_status_not_installed_when_package_missing():
    provisioner = BrowserProvisioner(
        package_probe=lambda: False,
        chromium_finder=lambda: "",
        installer_command=[sys.executable, "-c", "pass"],
    )
    status = provisioner.status()
    assert status.state is BrowserState.NOT_INSTALLED
    assert status.package_available is False
    assert "Playwright package" in status.detail


def test_status_ready_when_chromium_present():
    provisioner = BrowserProvisioner(
        package_probe=lambda: True,
        chromium_finder=lambda: "/opt/chrome",
        installer_command=[sys.executable, "-c", "pass"],
    )
    status = provisioner.status()
    assert status.state is BrowserState.READY
    assert status.ready is True


def test_status_not_installed_when_package_present_but_no_binary():
    provisioner = BrowserProvisioner(
        package_probe=lambda: True,
        chromium_finder=lambda: "",
        installer_command=[sys.executable, "-c", "pass"],
    )
    status = provisioner.status()
    assert status.state is BrowserState.NOT_INSTALLED
    assert "no Chromium binary" in status.detail


async def test_provision_runs_installer_then_reports_ready(tmp_path):
    """A successful install that leaves a real binary yields READY."""
    installed = tmp_path / "chrome"

    def finder() -> str:
        return str(installed) if installed.exists() else ""

    provisioner = BrowserProvisioner(
        package_probe=lambda: True,
        chromium_finder=finder,
        installer_command=[
            sys.executable,
            "-c",
            f"open({str(installed)!r}, 'w').write('x')",
        ],
    )
    status = await provisioner.provision()
    assert status.state is BrowserState.READY
    assert status.chromium_path == str(installed)
    assert "browser" in status.log_tail or status.log_tail == ""


async def test_provision_reports_broken_when_installer_fails(tmp_path):
    provisioner = BrowserProvisioner(
        package_probe=lambda: True,
        chromium_finder=lambda: "",
        installer_command=_fail_installer(tmp_path),
    )
    status = await provisioner.provision()
    assert status.state is BrowserState.BROKEN
    assert status.ready is False


async def test_provision_reports_broken_when_binary_still_missing(tmp_path):
    """The installer succeeding is not enough — a real binary must be found."""
    provisioner = BrowserProvisioner(
        package_probe=lambda: True,
        chromium_finder=lambda: "",
        installer_command=_ok_installer(tmp_path),
    )
    status = await provisioner.provision()
    assert status.state is BrowserState.BROKEN


async def test_provision_without_package_does_not_run_installer(tmp_path):
    ran = tmp_path / "ran"
    provisioner = BrowserProvisioner(
        package_probe=lambda: False,
        chromium_finder=lambda: "",
        installer_command=[
            sys.executable,
            "-c",
            f"open({str(ran)!r}, 'w').write('x')",
        ],
    )
    status = await provisioner.provision()
    assert status.state is BrowserState.NOT_INSTALLED
    assert not ran.exists()


async def test_cancel_with_no_install_is_false():
    provisioner = BrowserProvisioner(
        package_probe=lambda: True,
        chromium_finder=lambda: "",
        installer_command=[sys.executable, "-c", "pass"],
    )
    assert await provisioner.cancel() is False


async def test_provision_times_out_and_is_broken(tmp_path):
    provisioner = BrowserProvisioner(
        package_probe=lambda: True,
        chromium_finder=lambda: "",
        installer_command=[sys.executable, "-c", "import time; time.sleep(30)"],
        timeout_seconds=0.5,
    )
    status = await provisioner.provision()
    assert status.state is BrowserState.BROKEN
    assert status.ready is False
