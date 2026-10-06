"""Desktop runtime entry point.

Runs the local agent runtime and serves its loopback control API:

    agent-desktop

This is the process a desktop client (or a future Electron/Tauri shell) talks
to. It is deliberately thin: it builds the same :class:`Platform` the cloud
deployment builds, in ``local`` mode, and mounts the existing control app plus
the Phase 10 desktop product surface. Neither app executes tools itself — tool
execution goes through the shared executor with the same permissions, approvals
and audit as the cloud runtime.

Binding is loopback-only by default and refuses to start without a control
token unless auth has been explicitly disabled in a non-production environment,
so the surface cannot be exposed by accident.
"""

from __future__ import annotations

import sys
from typing import Any

from fastapi import FastAPI, Header

from ..container import Platform
from ..core.config import Settings
from ..core.errors import ConfigurationError
from ..desktop.events import DesktopEventBroker
from ..desktop.http import create_desktop_app
from ..desktop.service import DesktopService, build_model_manager
from ..models.registry import ProviderRegistry
from ..observability import configure_observability
from .control import LocalControlAPI
from .http import create_local_app


def build_local_control_app(
    settings: Settings,
    *,
    provider_registry: ProviderRegistry | None = None,
    default_model: str | None = None,
    with_desktop: bool = True,
) -> FastAPI:
    """Build the platform and the authenticated control app for ``settings``.

    Exposed separately from :func:`main` so tests can construct the real app
    without binding a port. ``provider_registry``/``default_model`` may be
    injected exactly as for :meth:`Platform.build` (a desktop deployment
    supplies its local model this way).

    When ``with_desktop`` is set (the default), the Phase 10 product surface is
    mounted alongside the control API, and a :class:`DesktopEventBroker` is
    attached to the audit log so the UI can stream real runtime events.
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
    app = create_local_app(control, settings=settings)

    if with_desktop:
        # Mirror every audit event to the UI (redacted, bounded, non-blocking).
        broker = DesktopEventBroker()
        platform.audit.add_sink(broker)
        model_manager = build_model_manager(platform)
        service = DesktopService(
            platform=platform,
            model_manager=model_manager,
            events=broker,
            locale=settings.default_locale,
        )
        desktop_app = create_desktop_app(service, authorize=_control_authorize(control))
        app.mount("/", desktop_app)
        app.state.desktop_service = service
        app.state.desktop_events = broker

    return app


def _control_authorize(control: LocalControlAPI) -> Any:
    """Return a FastAPI dependency that authorises via the control token."""

    async def authorize(
        x_local_token: str | None = Header(default=None, alias="X-Local-Token"),
    ) -> None:
        control.authorize(x_local_token)

    return authorize


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
