"""Concrete model providers.

Importing this package does not require any third-party SDK: the
OpenAI-compatible provider speaks HTTP directly via httpx, so the same code
targets OpenAI, Azure OpenAI, Ollama, vLLM and llama.cpp's server.
"""

from .openai_compatible import OpenAICompatibleProvider

__all__ = ["OpenAICompatibleProvider"]
