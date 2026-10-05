"""Image encoding, resizing and change detection for screen captures.

Screenshots are large and sensitive. This module provides the small, pure image
operations the perception layer needs — deterministic PNG encoding, bounded
resizing, cropping and a cheap perceptual hash — so a capture can be shrunk for
the model context, compared for change, or cropped to a region without the
perception layer depending on any particular imaging library at import time.

Pillow is a hard dependency of the local capture path (it is present in the
runtime image). It is imported lazily so that importing the computer package on
a machine without a display, or in a unit test that never captures, does not
require it.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

from .coords import Rect

if TYPE_CHECKING:  # pragma: no cover - typing only
    from PIL.Image import Image

#: Longest side, in pixels, a captured image is allowed to have before it is
#: downscaled. Bounds both memory and the vision-model token cost.
DEFAULT_MAX_DIMENSION = 1600


def _require_pillow() -> object:
    try:
        import PIL.Image
    except ImportError as exc:  # pragma: no cover - environment dependent
        from ..core.errors import ConfigurationError

        raise ConfigurationError(
            "screen capture requires Pillow; install the 'computer' extra"
        ) from exc
    return PIL.Image


def encode_png(image: Image) -> bytes:
    """Encode a PIL image as PNG, deterministically (no timestamps)."""
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=False)
    return buffer.getvalue()


def decode_png(data: bytes) -> Image:
    _require_pillow()
    from PIL import Image as PILImage

    return PILImage.open(io.BytesIO(data)).convert("RGB")


def bounded(image: Image, *, max_dimension: int = DEFAULT_MAX_DIMENSION) -> Image:
    """Downscale ``image`` so its longest side is at most ``max_dimension``.

    Aspect ratio is preserved. An image already within bounds is returned
    unchanged, so a small capture is never needlessly re-encoded.
    """
    longest = max(image.width, image.height)
    if longest <= max_dimension:
        return image
    ratio = max_dimension / longest
    new_size = (max(1, round(image.width * ratio)), max(1, round(image.height * ratio)))
    resample = getattr(image, "Resampling", None)
    method = resample.LANCZOS if resample is not None else 1
    return image.resize(new_size, method)


def crop(image: Image, region: Rect) -> Image:
    """Crop ``image`` to ``region`` (clamped to the image bounds)."""
    left = max(0, region.x)
    top = max(0, region.y)
    right = min(image.width, region.right)
    bottom = min(image.height, region.bottom)
    return image.crop((left, top, max(left, right), max(top, bottom)))


def perceptual_hash(image: Image, *, size: int = 8) -> str:
    """Return a small perceptual hash used only for change detection.

    This is a difference hash (dHash): resize to ``size+1`` x ``size`` grey,
    compare adjacent pixels. It is deliberately cheap and non-cryptographic —
    it answers "did the screen change enough to re-analyse?" and nothing else.
    """
    grey = image.convert("L").resize((size + 1, size))
    pixels = grey.tobytes()
    bits: list[str] = []
    for row in range(size):
        for col in range(size):
            left = pixels[row * (size + 1) + col]
            right = pixels[row * (size + 1) + col + 1]
            bits.append("1" if left > right else "0")
    value = int("".join(bits), 2)
    return f"{value:0{size * size // 4}x}"


def hamming_distance(a: str, b: str) -> int:
    """Bit distance between two perceptual hashes of equal length."""
    if len(a) != len(b):
        return max(len(a), len(b)) * 4
    return bin(int(a, 16) ^ int(b, 16)).count("1")


__all__ = [
    "DEFAULT_MAX_DIMENSION",
    "bounded",
    "crop",
    "decode_png",
    "encode_png",
    "hamming_distance",
    "perceptual_hash",
]
