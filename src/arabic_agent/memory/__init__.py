"""Memory layer: scoped, policy-governed persistence of durable facts.

Memory is deliberately separate from the agent core and from conversation
history. Not everything is remembered; a memory policy decides what may be
stored and who may read it.
"""

from .base import MemoryEntry, MemoryQuery, MemoryStore
from .memory_store import InMemoryMemoryStore
from .policy import MemoryPolicy, RetentionPolicy

__all__ = [
    "InMemoryMemoryStore",
    "MemoryEntry",
    "MemoryPolicy",
    "MemoryQuery",
    "MemoryStore",
    "RetentionPolicy",
]
