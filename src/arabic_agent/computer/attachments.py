"""Bridge a screen observation into a model context attachment.

When (and only when) the active model can see, the most recent screen
observation is offered to the runner as a :class:`ContextAttachment`. The
screen-data boundary is consulted first: under ``LOCAL_ONLY`` (the default) a
remote model never receives a screenshot, and the provider returns nothing
rather than leaking one. The image is encoded in memory and handed over; nothing
is written to disk.
"""

from __future__ import annotations

from ..context.attachments import ContextAttachment
from ..core.enums import DataSensitivity
from ..models.base import ImageContent
from .runtime import ComputerRuntime
from .screen_boundary import ScreenDataBoundary


class ObservationAttachmentProvider:
    """Supplies the latest screen observation as a vision attachment."""

    def __init__(
        self,
        *,
        runtime: ComputerRuntime,
        boundary: ScreenDataBoundary,
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
        if observation is None or observation.image is None:
            return []
        from .capture import encode_png

        image = observation.image
        data = encode_png(image)
        sensitivity = (
            DataSensitivity.INTERNAL
            if self._boundary.policy.value == "cloud_allowed"
            else DataSensitivity.CONFIDENTIAL
        )
        return [
            ContextAttachment(
                image=ImageContent.from_bytes(
                    data,
                    mime_type="image/png",
                    width=observation.image_width,
                    height=observation.image_height,
                ),
                description=f"screen capture {observation.image_width}x{observation.image_height}",
                sensitivity=sensitivity,
                source="computer_screen",
            )
        ]


__all__ = ["ObservationAttachmentProvider"]
