"""Coordinate spaces, monitors and DPI-aware transforms.

Computer control fails in a specific, common way: a point that looks right in a
screenshot is not the point the OS actually clicks. That happens when logical
(DPI-scaled) coordinates are confused with physical pixels, or when a
multi-monitor layout with negative virtual-screen coordinates is assumed to be a
single 1920x1080 display.

This module makes the coordinate model explicit and pure, so every transform is
a deterministic function that can be tested without a display:

* a :class:`MonitorInfo` describes one display in its **native** coordinate
  space (``x``/``y`` may be negative on a multi-monitor virtual screen), with a
  ``scale`` factor between logical and physical pixels;
* :class:`Point`/:class:`Rect` are tagged by :class:`CoordinateSpace` so a caller
  cannot silently mix them;
* :func:`MonitorInfo.to_physical` / :func:`MonitorInfo.to_logical` convert for a
  given monitor;
* :func:`monitor_for_point`, :func:`clamp_point` and :func:`virtual_bounds`
  resolve a point against a monitor layout.

Nothing here touches the OS. The backend adapters produce the ``MonitorInfo``
list and perform the actual input.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class CoordinateSpace(StrEnum):
    """Which pixel space a coordinate is expressed in.

    ``PHYSICAL`` is what the compositor/OS delivers input in (device pixels).
    ``LOGICAL`` is the DPI-scaled space a screenshot or accessibility tree is
    usually expressed in. A transform between them is required whenever
    ``scale != 1.0`` (e.g. a Windows display at 150%).
    """

    PHYSICAL = "physical"
    LOGICAL = "logical"


class MonitorInfo(BaseModel):
    """One display in a (possibly multi-monitor) virtual screen.

    ``x``/``y`` are the monitor's top-left corner in the virtual screen, in
    logical pixels; they may be negative (a monitor to the left of the primary
    one). ``width``/``height`` are logical pixels. ``scale`` maps logical to
    physical pixels (``physical = logical * scale``); it is 1.0 on an unscaled
    display.
    """

    model_config = ConfigDict(frozen=True)

    id: int
    name: str = ""
    x: int = 0
    y: int = 0
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    scale: float = Field(default=1.0, gt=0.0)
    primary: bool = False

    @property
    def left(self) -> int:
        return self.x

    @property
    def top(self) -> int:
        return self.y

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    @property
    def physical_width(self) -> int:
        return round(self.width * self.scale)

    @property
    def physical_height(self) -> int:
        return round(self.height * self.scale)

    def contains(self, point: Point) -> bool:
        return self.left <= point.x < self.right and self.top <= point.y < self.bottom

    def to_physical(self, point: Point) -> Point:
        return Point(
            x=round(self.x * self.scale) + round((point.x - self.x) * self.scale),
            y=round(self.y * self.scale) + round((point.y - self.y) * self.scale),
        )

    def to_logical(self, point: Point) -> Point:
        return Point(
            x=self.x + round((point.x - round(self.x * self.scale)) / self.scale),
            y=self.y + round((point.y - round(self.y * self.scale)) / self.scale),
        )


class Point(BaseModel):
    """An (x, y) location in a named coordinate space."""

    model_config = ConfigDict(frozen=True)

    x: int
    y: int
    space: CoordinateSpace = CoordinateSpace.LOGICAL

    def as_tuple(self) -> tuple[int, int]:
        return self.x, self.y

    def to(self, space: CoordinateSpace, monitor: MonitorInfo) -> Point:
        """Return this point expressed in ``space`` for ``monitor``."""
        if self.space is space:
            return self
        converted = (
            monitor.to_physical(self)
            if space is CoordinateSpace.PHYSICAL
            else monitor.to_logical(self)
        )
        return converted.model_copy(update={"space": space})


class Rect(BaseModel):
    """An axis-aligned rectangle (top-left origin) in a coordinate space."""

    model_config = ConfigDict(frozen=True)

    x: int
    y: int
    width: int = Field(ge=0)
    height: int = Field(ge=0)
    space: CoordinateSpace = CoordinateSpace.LOGICAL

    @property
    def left(self) -> int:
        return self.x

    @property
    def top(self) -> int:
        return self.y

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    @property
    def center(self) -> Point:
        return Point(x=self.x + self.width // 2, y=self.y + self.height // 2, space=self.space)

    def contains(self, point: Point) -> bool:
        return self.x <= point.x < self.right and self.y <= point.y < self.bottom

    def to(self, space: CoordinateSpace, monitor: MonitorInfo) -> Rect:
        if self.space is space:
            return self
        top_left = Point(x=self.x, y=self.y, space=self.space).to(space, monitor)
        if space is CoordinateSpace.PHYSICAL:
            width = round(self.width * monitor.scale)
            height = round(self.height * monitor.scale)
        else:
            width = round(self.width / monitor.scale)
            height = round(self.height / monitor.scale)
        return Rect(x=top_left.x, y=top_left.y, width=width, height=height, space=space)


def virtual_bounds(monitors: list[MonitorInfo]) -> Rect:
    """The bounding rectangle of the whole virtual screen."""
    if not monitors:
        raise ValueError("no monitors")
    left = min(m.left for m in monitors)
    top = min(m.top for m in monitors)
    right = max(m.right for m in monitors)
    bottom = max(m.bottom for m in monitors)
    return Rect(x=left, y=top, width=right - left, height=bottom - top)


def monitor_for_point(monitors: list[MonitorInfo], point: Point) -> MonitorInfo | None:
    """Return the monitor containing ``point``, or ``None`` if it is off-screen."""
    for monitor in monitors:
        if monitor.contains(point):
            return monitor
    return None


def clamp_point(monitors: list[MonitorInfo], point: Point) -> Point:
    """Clamp ``point`` to the nearest on-screen pixel of the virtual screen.

    A point outside every monitor is pulled to the closest edge of the closest
    monitor, so an out-of-bounds request becomes a safe, in-bounds action rather
    than an error the model must guess its way out of.
    """
    if not monitors:
        raise ValueError("no monitors")
    bounds = virtual_bounds(monitors)
    x = min(max(point.x, bounds.left), bounds.right - 1)
    y = min(max(point.y, bounds.top), bounds.bottom - 1)
    return point.model_copy(update={"x": x, "y": y})


def is_on_screen(monitors: list[MonitorInfo], point: Point) -> bool:
    return monitor_for_point(monitors, point) is not None


__all__ = [
    "CoordinateSpace",
    "MonitorInfo",
    "Point",
    "Rect",
    "clamp_point",
    "is_on_screen",
    "monitor_for_point",
    "virtual_bounds",
]
