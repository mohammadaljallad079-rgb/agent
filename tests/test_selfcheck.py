"""Tests for the product's own install self-check.

The self-check is real verification, not a test double: it writes documents,
migrates a real SQLite store and answers a real HTTP request. These tests assert
its *honesty* — that it reports a real pass, that a missing optional extra is a
skip rather than a pass or a failure, and that a genuine breakage is a failure.
"""

from __future__ import annotations

import json
from pathlib import Path

from arabic_agent.desktop.selfcheck import (
    FAILED,
    OK,
    SKIPPED,
    SelfCheckReport,
    run_self_check,
)


def test_self_check_passes_on_a_real_install(tmp_path: Path) -> None:
    report = run_self_check(workdir=tmp_path)
    statuses = {c.name: c.status for c in report.checks}
    # The bundled assets and the database/API checks never depend on an extra.
    assert statuses["bundled_resources"] == OK
    assert statuses["local_database"] == OK
    assert statuses["local_api"] == OK
    # Document generation is either a real pass or an honest skip, never a fail.
    assert statuses["document_generation"] in (OK, SKIPPED)
    # The voice surface is present and importable; a host without an audio
    # device (CI, Linux) is an honest skip, never a fabricated pass.
    assert statuses["voice_surface"] in (OK, SKIPPED)
    assert report.ok is True


def test_self_check_generates_real_documents_when_extras_present(tmp_path: Path) -> None:
    import importlib.util

    if importlib.util.find_spec("reportlab") is None:
        return  # nothing to assert beyond the skip covered above
    report = run_self_check(workdir=tmp_path)
    doc = next(c for c in report.checks if c.name == "document_generation")
    assert doc.status == OK
    assert "pdf" in doc.detail
    # The PDF bytes really exist on disk.
    assert (tmp_path / "report.pdf").stat().st_size > 0


def test_self_check_reports_a_failure_honestly(monkeypatch, tmp_path: Path) -> None:
    """A real breakage must surface as ``failed``, not be swallowed."""
    import arabic_agent.desktop.selfcheck as selfcheck

    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated breakage")

    monkeypatch.setattr(selfcheck, "check_local_api", boom)
    report = selfcheck.run_self_check(workdir=tmp_path)
    assert report.ok is False


def test_self_check_fails_on_a_stale_or_unbranded_ui(monkeypatch, tmp_path: Path) -> None:
    """A shipped page from an older build must fail the resource check.

    The installer ships the page; if a build ever packaged a UI without the
    product identity or the assistant surface, the self-check must say so
    rather than pass on the directory merely existing.
    """
    import arabic_agent.core.resources as resources
    import arabic_agent.desktop.selfcheck as selfcheck

    ui = tmp_path / "desktop" / "ui"
    ui.mkdir(parents=True)
    (ui / "index.html").write_text("<html>stale build</html>", encoding="utf-8")
    monkeypatch.setattr(resources, "PACKAGE_DIR", tmp_path)
    # Isolate the UI assertion: give the migration resolvers something to find so
    # the check reaches the page rather than stopping at the earlier condition.
    monkeypatch.setattr(resources, "packaged_migrations_dir", lambda name: tmp_path / name)
    monkeypatch.setattr(resources, "source_migrations_dir", lambda name: None)

    report = SelfCheckReport()
    selfcheck.check_bundled_resources(report)
    assert report.checks[0].status == FAILED
    assert "UI is missing" in report.checks[0].detail


def test_voice_surface_check_fails_when_the_catalogue_is_empty(monkeypatch) -> None:
    """The voice check is real: an empty model catalogue is a failure, not a skip."""
    import arabic_agent.desktop.selfcheck as selfcheck
    import arabic_agent.speech.models as speech_models

    monkeypatch.setattr(speech_models, "speech_models", lambda: ())
    report = SelfCheckReport()
    selfcheck.check_voice_surface(report)
    assert report.ok is False
    assert report.checks[0].name == "voice_surface"
    assert report.checks[0].status == FAILED


def test_report_serialises_and_formats() -> None:
    report = SelfCheckReport()
    report.add("a", OK, "fine")
    report.add("b", SKIPPED, "no extra")
    payload = report.as_dict()
    assert payload["ok"] is True
    assert json.loads(json.dumps(payload))["checks"][0]["name"] == "a"
    assert "PASS" in report.format_text()
    assert "SKIP" in report.format_text()
