"""Autonomous browser and web-research subsystem (Phase 8).

This package gives the *existing* agent the ability to open, read, search and
interact with the web through ordinary tools. It is deliberately not a separate
"browser agent": the tools are plain :class:`~arabic_agent.tools.base.Tool`
instances that run through the same executor, permission/policy gate, approval
flow and audit trail as every other capability.

Layering (top to bottom):

* ``tools/builtin/browser.py`` -- the model-facing tools.
* ``runtime`` -- :class:`BrowserRuntime`, the single entry point that enforces
  observation freshness, action budgets, the URL/domain policy and the
  web-content data boundary.
* ``policy`` / ``url_security`` -- explicit, pure, testable safety rules.
* ``observations`` / ``content`` -- bounded, normalised page observations and
  bounded text extraction (never a raw DOM dump).
* ``backends`` -- the automation seam (Playwright, plus an honest null backend).

Web content is treated as **untrusted data**, never as instructions.
"""

from __future__ import annotations

from .base import (
    BrowserBackend,
    BrowserPageState,
    DialogInfo,
    DownloadedFile,
    ElementInfo,
    FormFieldInfo,
    FormInfo,
    TabInfo,
)
from .observations import BrowserObservation, ObservationStore

__all__ = [
    "BrowserBackend",
    "BrowserObservation",
    "BrowserPageState",
    "DialogInfo",
    "DownloadedFile",
    "ElementInfo",
    "FormFieldInfo",
    "FormInfo",
    "ObservationStore",
    "TabInfo",
]
