"""Printing: real spooler integration behind a platform-neutral seam.

This package lets the agent send a document to a printer. It mirrors the
computer-use and browser packages: a :class:`PrintBackend` is the only place
that touches an OS spooler; a :class:`PrintRuntime` enforces policy, a per-run
budget, the data boundary and audit above it; and the tools translate model
arguments into runtime calls. Nothing here fabricates a job — an unavailable
platform reports why, and a rejected job is a real error.
"""

from __future__ import annotations

from .backends import build_print_backend
from .base import (
    PrintBackend,
    PrintColorMode,
    PrintDocument,
    PrintDuplex,
    PrinterInfo,
    PrintJob,
    PrintJobStatus,
    PrintOptions,
)
from .policy import DEFAULT_PRINTABLE_TYPES, JobBudget, PrintPolicy
from .runtime import PrintRuntime

__all__ = [
    "DEFAULT_PRINTABLE_TYPES",
    "JobBudget",
    "PrintBackend",
    "PrintColorMode",
    "PrintDocument",
    "PrintDuplex",
    "PrintJob",
    "PrintJobStatus",
    "PrintOptions",
    "PrintPolicy",
    "PrintRuntime",
    "PrinterInfo",
    "build_print_backend",
]
