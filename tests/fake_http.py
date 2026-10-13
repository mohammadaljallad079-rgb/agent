"""A real local HTTP server for connector tests.

The connector tests exercise the *real* transport (httpx → a socket → a real
server), not a stubbed client, so the URL policy, redirect handling, size caps
and credential containment are proven against actual HTTP behaviour. This module
wraps :mod:`http.server` in a thread with a request log and scriptable routes.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass
class ReceivedRequest:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8")) if self.body else None


@dataclass
class Route:
    status: int = 200
    body: bytes = b"{}"
    content_type: str = "application/json"
    headers: dict[str, str] = field(default_factory=dict)
    #: When set, the response streams this file from disk instead of ``body``.
    #: Used to serve a real, large artifact (e.g. a model) without holding it
    #: in memory, so the installer's streaming/resume path is exercised for real.
    file: str | None = None


class _Handler(BaseHTTPRequestHandler):
    server_version = "FakeHTTP/1.0"

    def log_message(self, *args: object) -> None:
        return

    def _send_file(self, route: Route) -> None:
        import os

        assert route.file is not None
        size = os.path.getsize(route.file)
        start = 0
        range_header = self.headers.get("range")
        if range_header and range_header.startswith("bytes="):
            start = int(range_header[len("bytes=") :].split("-")[0] or 0)
        self.send_response(206 if start else route.status)
        self.send_header("Content-Type", route.content_type)
        for key, value in route.headers.items():
            self.send_header(key, value)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(size - start))
        if start:
            self.send_header("Content-Range", f"bytes {start}-{size - 1}/{size}")
        self.end_headers()
        with open(route.file, "rb") as handle:
            handle.seek(start)
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _handle(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        record = ReceivedRequest(
            method=self.command,
            path=self.path,
            headers={k.lower(): v for k, v in self.headers.items()},
            body=body,
        )
        self.server.requests.append(record)  # type: ignore[attr-defined]
        route = self.server.routes.get(self.path)  # type: ignore[attr-defined]
        if route is None:
            route = self.server.default  # type: ignore[attr-defined]
        if route.file is not None:
            self._send_file(route)
            return
        self.send_response(route.status)
        self.send_header("Content-Type", route.content_type)
        for key, value in route.headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(route.body)))
        self.end_headers()
        self.wfile.write(route.body)

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_PATCH = _handle
    do_DELETE = _handle


class FakeHTTPServer:
    """A threaded HTTP server with a request log and scriptable routes."""

    def __init__(self) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.requests = []  # type: ignore[attr-defined]
        self._server.routes = {}  # type: ignore[attr-defined]
        self._server.default = Route()  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> FakeHTTPServer:
        self._thread.start()
        return self

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def route(
        self, path: str, *, status: int = 200, body: bytes | str = b"{}", **kwargs: Any
    ) -> None:
        payload = body.encode("utf-8") if isinstance(body, str) else body
        self._server.routes[path] = Route(status=status, body=payload, **kwargs)  # type: ignore[attr-defined]

    def route_file(
        self, path: str, file_path: str, *, content_type: str = "application/octet-stream"
    ) -> None:
        """Serve a real file from disk (streamed, with Range support)."""
        self._server.routes[path] = Route(  # type: ignore[attr-defined]
            content_type=content_type, file=file_path
        )

    def set_default(self, *, status: int = 200, body: bytes | str = b"{}", **kwargs: Any) -> None:
        payload = body.encode("utf-8") if isinstance(body, str) else body
        self._server.default = Route(status=status, body=payload, **kwargs)  # type: ignore[attr-defined]

    @property
    def requests(self) -> list[ReceivedRequest]:
        return list(self._server.requests)  # type: ignore[attr-defined]

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@contextmanager
def http_server() -> Iterator[FakeHTTPServer]:
    server = FakeHTTPServer().start()
    try:
        yield server
    finally:
        server.stop()


__all__ = ["FakeHTTPServer", "ReceivedRequest", "Route", "http_server"]
