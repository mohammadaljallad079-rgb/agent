"""Memory policy.

Decides what may be remembered and for how long. This is the mechanism that
keeps the platform from hoarding sensitive data: a write is rejected unless a
policy explicitly allows it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from ..core.enums import MemoryKind

#: Patterns that must never be persisted to memory, regardless of policy.
_FORBIDDEN_CONTENT_RE = re.compile(
    r"(?i)(password|passwd|secret|api[_-]?key|private[_-]?key|"
    r"bearer\s+[A-Za-z0-9._\-]+|sk-[A-Za-z0-9]{16,})"
)


@dataclass(frozen=True)
class RetentionPolicy:
    """How long a memory kind is retained, and whether it may be written."""

    kind: MemoryKind
    writable: bool
    #: None means retain indefinitely.
    ttl_seconds: int | None
    #: Whether entries of this kind are shared tenant-wide or per user.
    tenant_scoped: bool = False


class MemoryPolicy(BaseModel):
    """Per-tenant rules governing memory writes and reads."""

    model_config = ConfigDict(frozen=True)

    tenant_id: str
    retention: dict[MemoryKind, RetentionPolicy] = Field(default_factory=dict)
    #: If True, content matching a secret-like pattern is rejected outright.
    block_secret_like_content: bool = True
    #: Maximum importance permitted without explicit override.
    max_importance: float = Field(default=1.0, ge=0.0, le=1.0)

    @classmethod
    def default(cls, tenant_id: str) -> MemoryPolicy:
        """Sensible defaults: durable facts yes, raw conversation no.

        Conversation memory is intentionally not writable here — transcripts are
        large, often sensitive, and belong in the conversation store, not in
        long-term memory.
        """
        return cls(
            tenant_id=tenant_id,
            retention={
                MemoryKind.PREFERENCE: RetentionPolicy(MemoryKind.PREFERENCE, True, None),
                MemoryKind.COMPANY: RetentionPolicy(
                    MemoryKind.COMPANY, True, None, tenant_scoped=True
                ),
                MemoryKind.KNOWLEDGE: RetentionPolicy(MemoryKind.KNOWLEDGE, True, None),
                MemoryKind.TASK: RetentionPolicy(MemoryKind.TASK, True, 60 * 60 * 24 * 30),
                MemoryKind.WORKFLOW: RetentionPolicy(
                    MemoryKind.WORKFLOW, True, None, tenant_scoped=True
                ),
                MemoryKind.CONVERSATION: RetentionPolicy(
                    MemoryKind.CONVERSATION, False, 60 * 60 * 24
                ),
            },
        )

    def retention_for(self, kind: MemoryKind) -> RetentionPolicy:
        policy = self.retention.get(kind)
        if policy is not None:
            return policy
        # Unknown kinds are not writable by default.
        return RetentionPolicy(kind=kind, writable=False, ttl_seconds=None)

    def check_write(self, kind: MemoryKind, content: str) -> tuple[bool, str]:
        """Return ``(allowed, reason)`` for a prospective memory write."""
        retention = self.retention_for(kind)
        if not retention.writable:
            return False, f"memory kind {kind.value!r} is not writable under policy"
        if self.block_secret_like_content and _FORBIDDEN_CONTENT_RE.search(content):
            return False, "content looks like a secret and must not be stored"
        return True, "allowed"
