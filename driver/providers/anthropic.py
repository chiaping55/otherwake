"""Anthropic provider — direct HTTP, no SDK dependency.

The driver runs on whatever Python is lying around (usually ComfyUI's
embedded interpreter), so every provider here is stdlib-only by design.

Uses the Messages API with vision-capable Claude models. Image is sent as
base64 inline. The SYSTEM_TICK / SYSTEM_TRANSLATE prompts already say
"Return ONLY valid JSON", so no separate JSON-mode flag is needed.

API key lives in `cfg["driver"]["api_keys"]["anthropic"]` — set it via the
Run panel's "Save key" button or by editing config.json directly.

Model list: once a key is set, the dropdown is filled from the live
`GET /v1/models` listing, so it follows new releases on its own. To pin a
specific set instead, add to config.json:
    "driver": {
      ...
      "anthropic_models": ["claude-opus-5-5", "claude-haiku-5-5"]
    }
"""
from __future__ import annotations
import json
import time
from urllib import error, request

from .base import (ChatRequest, ChatResult, Provider, ProviderError,
                   ONLINE_MIN_OUTPUT_TOKENS, http_get_json, is_snapshot_id,
                   live_models)

API_VERSION = "2023-06-01"

# Shown before a key is set, or if the live listing fails.
DEFAULT_MODELS = [
    {"id": "claude-opus-5-5",   "label": "Claude Opus 5.5 (default)",          "vision": True},
    {"id": "claude-sonnet-5-5", "label": "Claude Sonnet 5.5 (faster, cheaper)", "vision": True},
    {"id": "claude-haiku-5-5",  "label": "Claude Haiku 5.5 (fastest, cheapest)", "vision": True},
    {"id": "claude-fable-5-2",  "label": "Claude Fable 5.2 (most capable)",     "vision": True},
]

# Models that can decline a request with stop_reason "refusal" and support
# the server-side fallback, which re-runs the declined request on another
# model inside the same call.
_FALLBACK_PREFIXES = ("claude-fable-5-1", "claude-fable-5-2", "claude-opus-5",
                      "claude-sonnet-5-5")
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


def _fetch_models(api_key: str) -> list[dict]:
    data = http_get_json(
        "https://api.anthropic.com/v1/models?limit=100",
        {"x-api-key": api_key, "anthropic-version": API_VERSION},
    )
    out = []
    for m in data.get("data") or []:
        mid = m.get("id") or ""
        if not mid.startswith("claude-") or is_snapshot_id(mid):
            continue
        out.append({"id": mid, "label": m.get("display_name") or mid, "vision": True})
    return out


class AnthropicProvider(Provider):
    name = "anthropic"
    requires_api_key = True

    def list_models(self, cfg: dict) -> list[dict]:
        driver = cfg.get("driver", {})
        custom = driver.get("anthropic_models")
        if isinstance(custom, list) and custom:
            return [{"id": m, "label": m, "vision": True}
                    for m in custom if isinstance(m, str) and m.strip()]
        api_key = (driver.get("api_keys", {}).get("anthropic") or "").strip()
        live = live_models(self.name, api_key, lambda: _fetch_models(api_key))
        return live or [dict(m) for m in DEFAULT_MODELS]

    def chat(self, cfg: dict, model: str, req: ChatRequest, extra: dict | None = None) -> ChatResult:
        driver = cfg.get("driver", {})
        api_key = (driver.get("api_keys", {}).get("anthropic") or "").strip()
        if not api_key:
            raise ProviderError(
                "anthropic api key not set. Add it in the Run panel or in "
                "config.json → driver → api_keys → anthropic."
            )

        url = "https://api.anthropic.com/v1/messages"

        # Image (if any) goes first, then the user-side text prompt.
        content: list[dict] = []
        if req.image_b64:
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": req.image_b64,
                },
            })
        content.append({"type": "text", "text": req.user})

        # No `temperature`: current Claude models reject sampling parameters
        # with a 400. No `thinking` either — it is adaptive (and on) by
        # default on the current models, and older ones simply run without.
        body: dict = {
            "model": model,
            "max_tokens": max(int(req.max_tokens), ONLINE_MIN_OUTPUT_TOKENS),
            "system": req.system,
            "messages": [{"role": "user", "content": content}],
        }
        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": API_VERSION,
        }
        if driver.get("anthropic_fallbacks", True) and model.startswith(_FALLBACK_PREFIXES):
            body["fallbacks"] = "default"
            headers["anthropic-beta"] = _FALLBACK_BETA

        data = json.dumps(body).encode("utf-8")
        http_req = request.Request(url, data=data, headers=headers)

        start = time.time()
        try:
            with request.urlopen(http_req, timeout=600) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except error.HTTPError as e:
            try:
                err_body = e.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                err_body = ""
            raise ProviderError(f"anthropic HTTP {e.code}: {err_body[:300]}") from e
        except error.URLError as e:
            raise ProviderError(f"anthropic request failed: {e}") from e
        except (json.JSONDecodeError, KeyError) as e:
            raise ProviderError(f"anthropic returned malformed response: {e}") from e
        latency = time.time() - start

        if not isinstance(result, dict):
            raise ProviderError(f"anthropic unexpected response shape: {str(result)[:300]}")

        # A refusal is a normal 200 response, so check before reading content.
        stop_reason = result.get("stop_reason")
        if stop_reason == "refusal":
            category = (result.get("stop_details") or {}).get("category")
            raise ProviderError(f"anthropic declined the request (category={category})")

        # Response shape: {"content":[{"type":"thinking",...},{"type":"text","text":"..."}], ...}
        # Keep the text blocks; thinking and fallback blocks are skipped.
        blocks = result.get("content") or []
        raw_text = "".join(
            b.get("text", "") for b in blocks
            if isinstance(b, dict) and b.get("type") == "text"
        ).strip()
        if not raw_text:
            raise ProviderError(
                f"anthropic returned no text (stop_reason={stop_reason}): {str(result)[:300]}"
            )

        return ChatResult(
            raw_text=raw_text,
            model=model,
            provider=self.name,
            latency_s=latency,
        )
