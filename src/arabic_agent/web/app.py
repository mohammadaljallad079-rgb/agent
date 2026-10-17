"""Browser-served Command Center.

The cloud HTTP API is the product's programmatic surface. This module adds the
*human* surface for a multi-tenant deployment: a single-page, Arabic-first RTL
application served from the same origin as the API, backed entirely by the real
API routes — every list, approval, artifact and model status shown in the UI is
a live API response, never a placeholder.

It deliberately does **not** invent a second API or a session mechanism:

* the UI is one static document plus one script, served with a strict CSP,
* the operator supplies an API key, which the page stores in ``localStorage``
  and sends as ``X-API-Key`` on every request — the same authentication the API
  already enforces, so tenant isolation is unchanged,
* the page is served on the API origin, so ``connect-src 'self'`` is enough and
  no cross-origin relaxation is needed.

Serving the UI is opt-in (the ``agent-web`` entry point or
:func:`build_default_web_app`); the bare API keeps its existing routes and
gains only the read-only ``GET /`` and ``GET /ui`` document routes.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from ..api.app import create_app
from ..container import Platform
from ..core.config import Settings

#: Directory holding the browser UI assets.
UI_DIR = Path(__file__).resolve().parent / "ui"

#: Content-Security-Policy for the served page. Same-origin only: the document
#: may call the API it was served from and nothing else, and may not be framed.
CSP = (
    "default-src 'none'; "
    "style-src 'unsafe-inline'; "
    "script-src 'unsafe-inline'; "
    "img-src data:; "
    "connect-src 'self'; "
    "frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'"
)


def _read_index() -> str:
    index = UI_DIR / "index.html"
    if not index.is_file():
        raise RuntimeError(f"web UI assets are missing: {index}")
    return index.read_text(encoding="utf-8")


def create_web_app(
    platform: Platform,
    *,
    settings: Settings | None = None,
) -> FastAPI:
    """Build the API app and add the browser Command Center document routes."""
    settings = settings or platform.settings
    app = create_app(platform, settings=settings)
    index = _read_index()

    def _document() -> HTMLResponse:
        return HTMLResponse(
            content=index,
            headers={
                "Content-Security-Policy": CSP,
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
            },
        )

    # The document is public (it holds no data); every byte of tenant data comes
    # from an authenticated API call the page makes itself.
    app.add_api_route(
        "/", _document, methods=["GET"], response_class=HTMLResponse, include_in_schema=False
    )
    app.add_api_route(
        "/ui", _document, methods=["GET"], response_class=HTMLResponse, include_in_schema=False
    )
    return app


def build_default_web_app() -> FastAPI:
    """Build the web app from environment configuration (used by ``uvicorn``)."""
    settings = Settings()
    return create_web_app(Platform.build(settings), settings=settings)


def main() -> None:
    """Console entry point: serve the Command Center and API."""
    import sys

    import uvicorn

    from ..core.errors import ConfigurationError
    from ..observability import configure_observability

    settings = Settings()
    configure_observability(settings)
    try:
        app = create_web_app(Platform.build(settings), settings=settings)
    except ConfigurationError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level="debug" if settings.debug else "info",
    )


__all__ = ["CSP", "UI_DIR", "build_default_web_app", "create_web_app", "main"]
