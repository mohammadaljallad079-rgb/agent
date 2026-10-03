"""Context management: assemble the *smallest sufficient* model context."""

from .budget import TokenBudget, estimate_tokens
from .manager import ContextManager, ContextPackage

__all__ = ["ContextManager", "ContextPackage", "TokenBudget", "estimate_tokens"]
