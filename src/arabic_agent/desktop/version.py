"""Product version: a single source of truth.

The desktop shell, the local control API and the installer all report the same
version so an installer, an application binary and the running runtime cannot
drift silently. The value is read from the installed distribution metadata when
available and falls back to the packaged constant.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

#: Fallback used when the package is not installed (running from a source tree).
_FALLBACK = "0.1.0"

#: The distribution name in ``pyproject.toml``.
_DISTRIBUTION = "arabic-ai-agent-platform"


def product_version() -> str:
    """Return the product version string."""
    try:
        return version(_DISTRIBUTION)
    except PackageNotFoundError:
        return _FALLBACK


#: Convenience constant for templates and diagnostics.
PRODUCT_VERSION = product_version()

__all__ = ["PRODUCT_VERSION", "product_version"]
