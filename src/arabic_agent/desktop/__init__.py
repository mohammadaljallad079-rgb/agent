"""Desktop product surface (Phase 10).

This package turns the existing agent runtime into an installable desktop
product. It adds **no** agent logic: it is a client-facing facade over the
platform's existing run service, model manager, policy engine, approvals, audit
and local runtime.

* :mod:`~arabic_agent.desktop.service` — product-level operations.
* :mod:`~arabic_agent.desktop.http` — the authenticated loopback HTTP/SSE surface.
* :mod:`~arabic_agent.desktop.events` — real-time run events (audit mirror).
* :mod:`~arabic_agent.desktop.model_manager` — the Local AI / Model Manager.
* :mod:`~arabic_agent.desktop.model_catalogue` — the supported-model catalogue.
* :mod:`~arabic_agent.desktop.shell` — the native-webview shell (pywebview).
* :mod:`~arabic_agent.desktop.runtime_manager` — desktop lifecycle (start the
  runtime, detect readiness, restart).
* :mod:`~arabic_agent.desktop.version` — a single version source of truth.
"""

from __future__ import annotations

from .version import PRODUCT_VERSION, product_version

__all__ = ["PRODUCT_VERSION", "product_version"]
