"""Structured logging audit sink.

Emits one JSON log line per event. Payloads are already redacted by the event
constructor, so this sink never sees secrets.
"""

from __future__ import annotations

import json
import logging

from .base import AuditEvent, AuditSink

logger = logging.getLogger("arabic_agent.audit")


class LoggingAuditSink(AuditSink):
    """Writes audit events to the standard logging pipeline as JSON."""

    def __init__(self, *, level: int = logging.INFO) -> None:
        self._level = level

    async def emit(self, event: AuditEvent) -> None:
        logger.log(
            self._level,
            json.dumps(
                {
                    "event": event.type.value,
                    "id": event.id,
                    "run_id": event.run_id,
                    "tenant_id": event.tenant_id,
                    "user_id": event.user_id,
                    "sequence": event.sequence,
                    "timestamp": event.timestamp.isoformat(),
                    "message": event.message,
                    "data": event.data,
                },
                ensure_ascii=False,
                default=str,
            ),
        )
