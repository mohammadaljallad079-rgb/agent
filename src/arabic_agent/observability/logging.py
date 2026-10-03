"""Structured JSON logging.

Production debugging needs logs that are machine-parseable and correlated. This
formatter emits one JSON object per record, always including the current
correlation identifiers and never including secrets: both the message and the
formatted exception are passed through :func:`redact` before serialization.

The agent code logs normally (``logging.getLogger(__name__)``); the correlation
fields are picked up from the context automatically, so no call site has to
remember to pass them.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from ..core.redaction import redact
from .context import current_context

#: Attribute names that are part of the standard record and must not be copied
#: into the structured payload as ad-hoc fields.
_RESERVED = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
        "message",
        "asctime",
    }
)

#: Callers attach structured fields under this attribute so they are logged as
#: top-level JSON keys rather than embedded in the message string.
EXTRA_ATTR = "extra_fields"


class JsonLogFormatter(logging.Formatter):
    """Render a log record as a single-line JSON object."""

    def __init__(self, *, service: str = "arabic-agent", environment: str = "dev") -> None:
        super().__init__()
        self._service = service
        self._environment = environment

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
            "service": self._service,
            "environment": self._environment,
        }
        payload.update(current_context().as_dict())

        extra = getattr(record, EXTRA_ATTR, None)
        if isinstance(extra, dict):
            for key, value in extra.items():
                if key not in payload:
                    payload[key] = redact(value)

        # Preserve any ad-hoc `extra={...}` fields the caller supplied.
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key != EXTRA_ATTR and not key.startswith("_"):
                payload.setdefault(key, redact(value))

        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        if record.stack_info:
            payload["stack"] = redact(self.formatStack(record.stack_info))

        return json.dumps(payload, ensure_ascii=False, default=str)


_configured = False


def configure_logging(
    *,
    level: int = logging.INFO,
    service: str = "arabic-agent",
    environment: str = "dev",
    force: bool = False,
) -> logging.Handler:
    """Install the JSON formatter on the root logger.

    Idempotent: repeated calls replace the previously installed handler rather
    than stacking handlers (which would duplicate every log line). Returns the
    installed handler so callers and tests can inspect it.
    """
    global _configured
    root = logging.getLogger()
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter(service=service, environment=environment))

    if not _configured or force:
        for existing in list(root.handlers):
            if getattr(existing, "_arabic_agent_json", False):
                root.removeHandler(existing)
        handler._arabic_agent_json = True  # type: ignore[attr-defined]
        root.addHandler(handler)
        root.setLevel(level)
        _configured = True
    return handler
