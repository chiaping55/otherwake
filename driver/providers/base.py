"""Provider abstraction for otherwake's agent driver.

Every concrete provider (Ollama, Anthropic, OpenAI, Gemini) implements the same
small interface: take a system prompt + user prompt + optional image, return a
JSON string. The agent core (driver/agent_core.py) doesn't care which provider
it is — it just calls `provider.chat(...)` and parses the result.

This is the file to read first when adding a new provider.
"""
from __future__ import annotations
import json
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from urllib import request

# Floor applied to ChatRequest.max_tokens by the online providers. Current
# frontier models reason before they answer, and those reasoning tokens are
# billed against the same output cap — the driver's 1024 default (sized for
# local models) would be spent before the JSON reply started.
ONLINE_MIN_OUTPUT_TOKENS = 16000

# Dated snapshot suffixes (-2026-03-01, -20260301). The live listings return
# both the alias and its snapshots; the dropdown only wants the alias.
_SNAPSHOT_RE = re.compile(r"-(\d{4}-\d{2}-\d{2}|\d{8})$")

_MODEL_CACHE: dict[tuple[str, int], tuple[float, list[dict]]] = {}
_MODEL_CACHE_TTL_S = 600.0


def is_snapshot_id(model_id: str) -> bool:
    return bool(_SNAPSHOT_RE.search(model_id))


def http_get_json(url: str, headers: dict, timeout: float = 8.0) -> dict:
    req = request.Request(url, headers=headers)
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def live_models(provider: str, api_key: str, fetch) -> list[dict] | None:
    """Ask the provider's own API which models this key can use.

    Hard-coded model lists go stale within months, so the online providers
    list live whenever a key is set and only fall back to their built-in
    defaults when there is no key or the listing call fails. `fetch` returns
    the model list; results are cached per (provider, key) for ten minutes
    because the viewer polls the catalog. Returns None on any failure.
    """
    if not api_key:
        return None
    cache_key = (provider, hash(api_key))
    hit = _MODEL_CACHE.get(cache_key)
    if hit and time.time() - hit[0] < _MODEL_CACHE_TTL_S:
        return [dict(m) for m in hit[1]]
    try:
        models = fetch()
    except Exception:  # noqa: BLE001 - listing is best-effort; fall back
        return None
    if not models:
        return None
    _MODEL_CACHE[cache_key] = (time.time(), models)
    return [dict(m) for m in models]


@dataclass
class ChatRequest:
    """One chat call's input. `image_b64` is base64-encoded PNG bytes (no data
    URI prefix) — providers wrap it into their own image-message format."""
    system: str
    user: str
    image_b64: str | None = None
    temperature: float = 0.85
    json_mode: bool = True
    max_tokens: int = 1024


@dataclass
class ChatResult:
    raw_text: str          # JSON string; the caller runs parse_json_lenient on it
    model: str
    provider: str
    latency_s: float


class ProviderError(RuntimeError):
    """Raised by Provider.chat when the call fails (network, auth, parse, etc).
    The driver loop catches this and applies its retry/backoff policy."""


class Provider(ABC):
    """Base class. Subclasses set `name` and `requires_api_key` as class attrs."""
    name: str = "base"
    requires_api_key: bool = False

    @abstractmethod
    def list_models(self, cfg: dict) -> list[dict]:
        """Return [{"id": "gemma3:12b", "label": "gemma3:12b", "vision": True}, ...].

        For Ollama: shells `ollama list` and filters vision-capable.
        For online providers, first match wins: the user's own list in
        config.json, the provider's live model listing (needs an API key),
        then a short built-in fallback.
        """
        ...

    @abstractmethod
    def chat(self, cfg: dict, model: str, req: ChatRequest, extra: dict | None = None) -> ChatResult:
        """One non-streaming call. Returns ChatResult.raw_text (JSON string).

        `extra` carries provider-specific knobs that don't fit the generic ChatRequest:
          - ollama: {"num_gpu": 0 | -1}     (0 = CPU only, -1 = all-GPU, omit = default)
          - anthropic / openai / gemini: ignored

        Raises ProviderError on transport, auth, or parse failure.
        """
        ...
