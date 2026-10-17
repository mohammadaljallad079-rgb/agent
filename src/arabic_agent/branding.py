"""User-facing product branding: one source of truth.

The product's *public* identity is **Super AI Agent**, under the **SUPER AI**
brand. This module is the single place those strings live, so the window title,
the desktop UI, the installer, the About panel, notification titles and the
diagnostics bundle cannot drift apart.

It deliberately does **not** own the *on-disk* identity. The data directory, the
uninstall registry key, the single-instance mutex and the Python distribution
name stay as they were, so a user upgrading from an earlier build keeps their
runs, settings, memory and generated documents. A public rename is presentation;
changing storage identity would be a data migration, and none is needed here.

Arabic is authored first: it is the product's primary market.
"""

from __future__ import annotations

#: The umbrella brand, as a wordmark.
BRAND = "SUPER AI"

#: The Arabic wordmark.
BRAND_AR = "سوبر إي آي"

#: The product's public name (English / Latin), used in titles and packaging.
PRODUCT = "Super AI Agent"

#: The Arabic product name.
PRODUCT_AR = "الوكيل الذكي الخارق"

#: One-line positioning, English and Arabic.
TAGLINE = "The agent that plans, executes and verifies."
TAGLINE_AR = "وكيلك الذكي الذي يخطّط وينفّذ ويتحقّق."

#: The desktop window / page title.
WINDOW_TITLE = f"{PRODUCT} — {BRAND}"

#: The value reported as the product in diagnostics and install manifests.
PRODUCT_ID = PRODUCT


def product_name(locale: str = "ar") -> str:
    """The product name for a locale (Arabic is the default)."""
    return PRODUCT_AR if str(locale).lower().startswith("ar") else PRODUCT


def brand_name(locale: str = "ar") -> str:
    """The brand wordmark for a locale."""
    return BRAND_AR if str(locale).lower().startswith("ar") else BRAND


__all__ = [
    "BRAND",
    "BRAND_AR",
    "PRODUCT",
    "PRODUCT_AR",
    "PRODUCT_ID",
    "TAGLINE",
    "TAGLINE_AR",
    "WINDOW_TITLE",
    "brand_name",
    "product_name",
]
