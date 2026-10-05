"""Desktop runtime entry point.

Runs the local agent runtime and serves its loopback control API:

    agent-desktop

This is the process a desktop client (or a future Electron/Tauri shell) talks
to. It is deliberately thin: it builds the same :class:`Platform` the cloud
deployment builds, in ``local`` mode, and mounts the existing control app. The
control app never executes tools itself — tool execution goes through the
shared executor with the same permissions, approvals and audit as the cloud
runtime.

Binding is loopback-only by default and refuses to start without a control
token unless auth has been explicitly disabled in a non-production environment,
so the surface cannot be exposed by accident.
"""

from __future__ import annotations

import sys

from fastapi import FastAPI

from ..container import Platform
from ..core.config import Settings
from ..core.errors import ConfigurationError
from ..models.registry import ProviderRegistry
from ..observability import configure_observability
from .control import LocalControlAPI
from .http import create_local_app


def build_local_control_app(
    settings: Settings,
    *,
    provider_registry: ProviderRegistry | None = None,
    default_model: str | None = None,
) -> FastAPI:
    """Build the platform and the authenticated control app for ``settings``.

    Exposed separately from :func:`main` so tests can construct the real app
    without binding a port. ``provider_registry``/``default_model`` may be
    injected exactly as for :meth:`Platform.build` (a desktop deployment
    supplies its local model this way).
    """
    platform = Platform.build(
        settings, provider_registry=provider_registry, default_model=default_model
    )
    runtime = platform.local_runtime
    if runtime is None:
        raise ConfigurationError(
            "the desktop runtime requires AAP_RUNTIME_KIND=local so a local runtime is assembled"
        )
    control = LocalControlAPI(runtime=runtime, platform=platform, settings=settings)
    if settings.require_auth and not control.auth_configured:
        raise ConfigurationError(
            "AAP_LOCAL_CONTROL_TOKEN is required to start the desktop control API"
        )
    return create_local_app(control, settings=settings)


def main() -> None:
    """Console entry point for the desktop runtime."""
    import uvicorn

    settings = Settings()
    configure_observability(settings)
    try:
        app = build_local_control_app(settings)
    except ConfigurationError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    uvicorn.run(
        app,
        host=settings.local_control_host,
        port=settings.local_control_port,
        log_level="debug" if settings.debug else "info",
    )


if __name__ == "__main__":
    main()
