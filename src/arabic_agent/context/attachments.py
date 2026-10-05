"""Multimodal context attachments.

The context manager is text-first, but a computer-use agent must be able to
attach a *screenshot* to a turn when (and only when) the active model can see.
This module defines the small contract for that:

* :class:`ContextAttachment` — one image with provenance and its own sensitivity;
* :class:`AttachmentProvider` — an object the runner asks for the attachments
  relevant to the current turn (the computer subsystem implements it with the
  latest observation);
* :class:`NullAttachmentProvider` — the default, which returns nothing.

The context manager never fetches attachments itself; the runner supplies them.
That keeps the context layer free of any knowledge of screens or tools, and lets
a test inject a deterministic provider.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict

from ..core.enums import DataSensitivity
from ..models.base import ImageContent


class ContextAttachment(BaseModel):
    """An image the runner may attach to the next model call.

    ``sensitivity`` travels with the image so the runner can apply the data
    boundary (a confidential screenshot is never sent to a remote model) before
    the context manager ever sees it.
    """

    model_config = ConfigDict(frozen=True)

    kind: str = "image"
    image: ImageContent
    #: Short, non-secret description for the prompt (e.g. "screenshot 1280x900").
    description: str = ""
    sensitivity: DataSensitivity = DataSensitivity.CONFIDENTIAL
    source: str = ""

    @property
    def estimated_tokens(self) -> int:
        """A rough, provider-independent vision-token estimate.

        Real vision tokenisers vary, but a bounded estimate lets the budget
        reserve something honest rather than treating an image as free. The
        estimate grows with area, capped so one screenshot cannot consume the
        whole window.
        """
        if self.image.width and self.image.height:
            # ~ (w*h)/750 is a common OpenAI low-detail approximation; clamp.
            return max(85, min(1200, (self.image.width * self.image.height) // 750))
        return 512


class AttachmentProvider(Protocol):
    """Supplies the attachments relevant to the current turn."""

    async def attachments_for(self, *, run_id: str) -> list[ContextAttachment]: ...


class NullAttachmentProvider:
    """The default provider: no attachments."""

    async def attachments_for(self, *, run_id: str) -> list[ContextAttachment]:
        return []


class StaticAttachmentProvider:
    """A provider that always returns a fixed set (used by tests)."""

    def __init__(self, attachments: list[ContextAttachment] | None = None) -> None:
        self._attachments = list(attachments or [])

    async def attachments_for(self, *, run_id: str) -> list[ContextAttachment]:
        return list(self._attachments)


__all__ = [
    "AttachmentProvider",
    "ContextAttachment",
    "NullAttachmentProvider",
    "StaticAttachmentProvider",
]
