"""Internationalization: Arabic-first, RTL-aware.

Arabic is not a translation layer bolted on later — locale and direction are
first-class fields throughout the platform, and message catalogs are authored in
Arabic first, English second.
"""

from .catalog import MESSAGES, MessageKey, direction_for, translate

__all__ = ["MESSAGES", "MessageKey", "direction_for", "translate"]
