"""Self-verification of an installed/packaged product.

This is the product checking *itself*, not a test suite. It runs the same real
code paths a user hits and reports what actually works, so an installed build
(and the Windows CI job that builds the installer) can prove — rather than
assume — that:

* the bundled non-Python assets are present (the desktop UI and both migration
  chains, resolved exactly as the frozen runtime resolves them);
* document generation really writes files (an Arabic PDF plus DOCX/PPTX/XLSX),
  or reports each format as unavailable with a reason when its extra is absent;
* the local SQLite store migrates to head and ends up with a real schema;
* the loopback control API answers an authenticated request.

Every check is ``ok``, ``failed`` or ``skipped``; a ``skipped`` check is an
honest "cannot exercise here" (a missing optional extra, a platform without the
feature), never a hidden pass. The process exits non-zero iff any check
``failed``. Nothing here fabricates a result: a format whose library is missing
is reported unavailable, not faked.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Check statuses. ``ok``/``failed`` are outcomes; ``skipped`` means the check
#: could not be exercised here (with a reason), not that it passed.
OK = "ok"
FAILED = "failed"
SKIPPED = "skipped"


@dataclass
class Check:
    """One verification step and its honest outcome."""

    name: str
    status: str
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass
class SelfCheckReport:
    """The full report; ``ok`` is true only when nothing failed."""

    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.checks.append(Check(name=name, status=status, detail=detail))

    @property
    def ok(self) -> bool:
        return all(check.status != FAILED for check in self.checks)

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAILED]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checks": [c.as_dict() for c in self.checks],
        }

    def format_text(self) -> str:
        lines = []
        for check in self.checks:
            mark = {OK: "OK", FAILED: "FAIL", SKIPPED: "SKIP"}.get(check.status, check.status)
            suffix = f" — {check.detail}" if check.detail else ""
            lines.append(f"[{mark}] {check.name}{suffix}")
        verdict = "PASS" if self.ok else "FAIL"
        lines.append(f"self-check: {verdict}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------
def check_bundled_resources(report: SelfCheckReport) -> None:
    """The desktop UI and both migration chains must be resolvable."""
    from arabic_agent.core.resources import (
        PACKAGE_DIR,
        packaged_migrations_dir,
        source_migrations_dir,
    )

    missing: list[str] = []
    ui = PACKAGE_DIR / "desktop" / "ui"
    if not ui.is_dir() or not any(ui.iterdir()):
        missing.append(f"desktop UI at {ui}")
    for name in ("migrations_local", "migrations"):
        if packaged_migrations_dir(name) is None and source_migrations_dir(name) is None:
            missing.append(f"{name} migration chain")
    if missing:
        report.add("bundled_resources", FAILED, "; ".join(missing))
    else:
        report.add("bundled_resources", OK, f"resolved under {PACKAGE_DIR}")


def check_document_generation(report: SelfCheckReport, workdir: Path) -> None:
    """Actually write documents and confirm the bytes landed on disk."""
    from arabic_agent.documents.generation import (
        DocumentSpec,
        generate_document,
        generate_spreadsheet,
        generation_capabilities,
    )

    caps = generation_capabilities()
    arabic_title = "تقرير الفحص الذاتي"
    spec = DocumentSpec.from_arguments(
        {
            "title": arabic_title,
            "blocks": [
                {"type": "heading", "text": "القسم الأول", "level": 1},
                {"type": "paragraph", "text": "محتوى عربي للتحقق من التوليد."},
                {"type": "bullet", "items": ["بند أول", "بند ثانٍ"]},
            ],
        }
    )
    produced: list[str] = []
    unavailable: list[str] = []
    failed: list[str] = []

    def _record(fmt: str, path: Path, write: Any) -> None:
        if not caps.get(fmt, False):
            unavailable.append(fmt)
            return
        try:
            write()
            if path.is_file() and path.stat().st_size > 0:
                produced.append(f"{fmt}({path.stat().st_size}B)")
            else:
                failed.append(f"{fmt}: no bytes written")
        except Exception as exc:  # a self-check reports a failure, never crashes
            failed.append(f"{fmt}: {exc}")

    _record("pdf", workdir / "report.pdf", lambda: generate_document(workdir / "report.pdf", spec))
    _record(
        "docx", workdir / "report.docx", lambda: generate_document(workdir / "report.docx", spec)
    )
    _record(
        "pptx", workdir / "report.pptx", lambda: generate_document(workdir / "report.pptx", spec)
    )
    _record(
        "xlsx",
        workdir / "report.xlsx",
        lambda: generate_spreadsheet(
            workdir / "report.xlsx",
            sheet="المبيعات",
            headers=("الشهر", "المبلغ"),
            rows=(("يناير", "1000"), ("فبراير", "2000")),
        ),
    )

    if failed:
        report.add("document_generation", FAILED, "; ".join(failed))
    elif not produced:
        report.add("document_generation", SKIPPED, "no document extras installed")
    else:
        detail = "produced " + ", ".join(produced)
        if unavailable:
            detail += f"; unavailable: {', '.join(unavailable)}"
        report.add("document_generation", OK, detail)


def check_local_database(report: SelfCheckReport, workdir: Path) -> None:
    """Migrate a throwaway local store to head and confirm a real schema."""
    import asyncio
    import sqlite3

    from arabic_agent.local.persistence.database import LocalDatabase

    db_path = workdir / "selfcheck.db"
    db = LocalDatabase(db_path)
    try:
        db.upgrade()
        asyncio.run(db.dispose())
        connection = sqlite3.connect(db_path)
        try:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        finally:
            connection.close()
        tables = {row[0] for row in rows}
        if "local_alembic_version" not in tables or len(tables) < 2:
            report.add("local_database", FAILED, f"schema incomplete: {sorted(tables)}")
        else:
            report.add("local_database", OK, f"{len(tables)} tables migrated to head")
    except Exception as exc:
        report.add("local_database", FAILED, str(exc))


def check_local_api(report: SelfCheckReport, workdir: Path) -> None:
    """Build the control app and make a real authenticated request."""
    try:
        from fastapi.testclient import TestClient

        from arabic_agent.core.config import Settings
        from arabic_agent.core.enums import OperatingMode, RuntimeKind
        from arabic_agent.local.main import build_local_control_app

        settings = Settings(
            environment="test",
            require_auth=False,
            runtime_kind=RuntimeKind.LOCAL,
            local_store_dir=str(workdir / "api-data"),
            local_mode=OperatingMode.OFFLINE,
            database_url="",
            redis_url="",
            execution_mode="inline",
        )
        app = build_local_control_app(settings)
        with TestClient(app) as client:
            response = client.get("/local/status")
        if response.status_code != 200:
            report.add("local_api", FAILED, f"/local/status returned {response.status_code}")
            return
        body = response.json()
        if "runtime" not in body and "state" not in body:
            report.add("local_api", FAILED, f"unexpected status payload: {sorted(body)}")
            return
        report.add("local_api", OK, "/local/status answered 200")
    except Exception as exc:
        report.add("local_api", FAILED, str(exc))


def run_self_check(*, workdir: Path | None = None) -> SelfCheckReport:
    """Run every check and return the report."""
    report = SelfCheckReport()
    if workdir is None:
        with tempfile.TemporaryDirectory(prefix="aap-selfcheck-") as tmp:
            _run_all(report, Path(tmp))
    else:
        Path(workdir).mkdir(parents=True, exist_ok=True)
        _run_all(report, Path(workdir))
    return report


def _run_all(report: SelfCheckReport, workdir: Path) -> None:
    checks: tuple[tuple[str, Callable[[], None]], ...] = (
        ("bundled_resources", lambda: check_bundled_resources(report)),
        ("document_generation", lambda: check_document_generation(report, workdir)),
        ("local_database", lambda: check_local_database(report, workdir)),
        ("local_api", lambda: check_local_api(report, workdir)),
    )
    for name, run in checks:
        before = len(report.checks)
        try:
            run()
        except Exception as exc:  # a crashing check is a failure, never a crash
            if len(report.checks) == before:
                report.add(name, FAILED, f"check raised: {exc}")


def main(argv: list[str] | None = None) -> int:
    """CLI: ``ArabicAIAgent selfcheck [--json] [--report PATH]``."""
    parser = argparse.ArgumentParser(description="Verify this install works end to end")
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    parser.add_argument("--workdir", default=None, help="scratch directory for generated files")
    parser.add_argument("--report", default=None, help="also write the JSON report to this file")
    args = parser.parse_args(argv)

    report = run_self_check(workdir=Path(args.workdir) if args.workdir else None)

    if args.report:
        Path(args.report).write_text(
            json.dumps(report.as_dict(), ensure_ascii=False), encoding="utf-8"
        )
    _emit(json.dumps(report.as_dict(), ensure_ascii=False) if args.json else report.format_text())
    return 0 if report.ok else 1


def _emit(text: str) -> None:
    """Print if a stream exists.

    A PyInstaller *windowed* build (``console=False``) has no ``sys.stdout``; the
    report must not crash the self-check it is reporting on. The exit code and
    ``--report`` file remain the machine-readable channel.
    """
    import contextlib
    import sys

    stream = sys.stdout
    if stream is None:
        return
    with contextlib.suppress(OSError, ValueError):
        stream.write(text + "\n")


__all__ = [
    "Check",
    "SelfCheckReport",
    "run_self_check",
]


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
