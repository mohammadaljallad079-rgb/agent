"""Persistent speech-action metadata.

A recording or a spoken reply is a physical side effect the user may want to
review later — possibly after an application restart. The runtime records each
action here so its metadata survives the process, without ever storing the audio
bytes or the raw transcript: a record names the action kind, duration, language,
engine and outcome, nothing more. A transcript is only kept when the caller
explicitly opts into it (``keep_text``), and even then it is the *recognised*
text, never the audio.

Two implementations share one contract:

* :class:`InMemorySpeechStore` for tests and a purely in-memory build;
* :class:`LocalSpeechStore` for the local/desktop runtime, backed by the embedded
  SQLite store so an action survives a restart.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from ..core.enums import StrEnum
from ..core.errors import NotFoundError


class SpeechActionKind(StrEnum):
    """What kind of speech action a record describes."""

    RECORDING = "recording"
    TRANSCRIPTION = "transcription"
    SYNTHESIS = "synthesis"
    PLAYBACK = "playback"


class SpeechActionState(StrEnum):
    """Outcome of one speech action."""

    OK = "ok"
    FAILED = "failed"
    DENIED = "denied"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class SpeechRecord(BaseModel):
    """Durable, safe metadata about one speech action."""

    model_config = ConfigDict(frozen=True)

    id: str
    tenant_id: str
    user_id: str
    run_id: str = ""
    kind: SpeechActionKind
    state: SpeechActionState = SpeechActionState.OK
    #: Backend/engine that performed the action (for an honest audit trail).
    engine: str = ""
    language: str = ""
    duration_seconds: float = 0.0
    size_bytes: int = 0
    #: Recognised text, kept only when the caller opted in. Empty otherwise.
    text: str = ""
    error: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class SpeechStore(ABC):
    """Persistence contract for speech-action metadata."""

    @abstractmethod
    async def add(self, record: SpeechRecord) -> SpeechRecord:
        """Persist a new action record."""

    @abstractmethod
    async def get(self, record_id: str, *, tenant_id: str) -> SpeechRecord:
        """Return one record, isolated to the tenant."""

    @abstractmethod
    async def list_for_run(self, *, tenant_id: str, run_id: str) -> list[SpeechRecord]:
        """Return the run's records, oldest first."""

    @abstractmethod
    async def list_recent(self, *, tenant_id: str, limit: int = 50) -> list[SpeechRecord]:
        """Return the tenant's most recent records, newest first."""


class InMemorySpeechStore(SpeechStore):
    """An in-process store for tests and a build with no local database."""

    def __init__(self) -> None:
        self._records: dict[str, SpeechRecord] = {}

    async def add(self, record: SpeechRecord) -> SpeechRecord:
        self._records[record.id] = record
        return record

    async def get(self, record_id: str, *, tenant_id: str) -> SpeechRecord:
        record = self._records.get(record_id)
        if record is None or record.tenant_id != tenant_id:
            raise NotFoundError("speech record not found", details={"id": record_id})
        return record

    async def list_for_run(self, *, tenant_id: str, run_id: str) -> list[SpeechRecord]:
        return sorted(
            (r for r in self._records.values() if r.tenant_id == tenant_id and r.run_id == run_id),
            key=lambda r: r.created_at,
        )

    async def list_recent(self, *, tenant_id: str, limit: int = 50) -> list[SpeechRecord]:
        records = sorted(
            (r for r in self._records.values() if r.tenant_id == tenant_id),
            key=lambda r: r.created_at,
            reverse=True,
        )
        return records[:limit]


__all__ = [
    "InMemorySpeechStore",
    "SpeechActionKind",
    "SpeechActionState",
    "SpeechRecord",
    "SpeechStore",
]
