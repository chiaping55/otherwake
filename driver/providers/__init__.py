"""Provider registry.

Add a new provider in two steps:
  1. Implement a `MyProvider(Provider)` class in driver/providers/myname.py
  2. Add an entry to PROVIDERS below

The agent core looks up `PROVIDERS[provider_name]()` and calls `.chat(...)`.
"""
from __future__ import annotations
from .base import ChatRequest, ChatResult, Provider, ProviderError
from .ollama import OllamaProvider
from .anthropic import AnthropicProvider
from .gemini import GeminiProvider
from .openai import OpenAIProvider


PROVIDERS: dict[str, type[Provider]] = {
    "ollama":    OllamaProvider,
    "anthropic": AnthropicProvider,
    "gemini":    GeminiProvider,
    "openai":    OpenAIProvider,
}


def get_provider(name: str) -> Provider:
    """Instantiate a registered provider by name. Raises KeyError if unknown."""
    if name not in PROVIDERS:
        raise KeyError(f"unknown provider {name!r}; registered: {sorted(PROVIDERS)}")
    return PROVIDERS[name]()


__all__ = [
    "ChatRequest", "ChatResult", "Provider", "ProviderError",
    "OllamaProvider", "AnthropicProvider", "GeminiProvider", "OpenAIProvider",
    "PROVIDERS", "get_provider",
]
