"""Bridge a browser observation into a model context attachment.

When (and only when) the active model can see, the most recent browser
observation's screenshot is offered to the runner as a
:class:`~arabic_agent.context.attachments.ContextAttachment`. The web-content
boundary is consulted first: under ``LOCAL_ONLY`` (the default) a remote model
never receives a page screenshot, and the provider returns nothing rather than
leaking one. The image is encoded in memory and handed over; nothing is written
to disk.

This mirrors :mod:`arabic_agent.computer.attachments` -- the browser subsystem
reuses the Phase 7 attachment contract rather than inventing a parallel one.
"""

from __future__ import annotations

from ..context.attachments import ContextAttachment
from ..core.enums import DataSensitivity
from ..models.base import ImageContent
from .boundary import WebDataBoundary
from .runtime import BrowserRuntime


class BrowserAttachmentProvider:
    """Supplies the latest browser observation screenshot as a vision attachment."""

    def __init__(
        self,
        *,
        runtime: BrowserRuntime,
        boundary: WebDataBoundary,
        destination_is_remote: bool,
    ) -> None:
        self._runtime = runtime
        self._boundary = boundary
        self._remote = destination_is_remote

    async def attachments_for(self, *, run_id: str) -> list[ContextAttachment]:
        decision = self._boundary.evaluate(remote=self._remote)
        if not decision.allowed:
            return []
        observation = self._runtime.latest_observation(run_id=run_id)
        if observation is None or observation.screenshot is None:
            return []
        image = observation.screenshot
        sensitivity = (
            DataSensitivity.INTERNAL
            if self._boundary.policy.value == "cloud_allowed"
            else DataSensitivity.CONFIDENTIAL
        )
        return [
            ContextAttachment(
                image=ImageContent.from_bytes(
                    image.data,
                    mime_type=image.mime_type,
                    width=image.width,
                    height=image.height,
                ),
                description=f"page screenshot {image.width}x{image.height}",
                sensitivity=sensitivity,
                source="browser_observe",
            )
        ]


__all__ = ["BrowserAttachmentProvider"]
