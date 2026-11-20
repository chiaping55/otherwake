"""OpenAI provider — direct HTTP, no SDK dependency.

Uses the v1/chat/completions endpoint with vision-capable GPT models.
Image is sent as a data: URI (the OpenAI vision format), JSON-mode
requested via response_format: {type: json_object}. Note that JSON-mode
requires the system or user message to mention JSON somewhere — our
SYSTEM_TICK already does ("Return ONLY valid JSON").

API key lives in `cfg["driver"]["api_keys"]["openai"]` — set via the
Run panel's 🔑 Set key button or by editing config.json directly.

OpenAI has no free tier — the API is prepaid. To check the web → API → LTX
chain without paying, use Gemini (free key at aistudio.google.com).

Model list: once a key is set, the dropdown is filled from the live
`GET /v1/models` listing, filtered down to chat models. To pin a specific
set instead, add to config.json:
    "driver": {
      ...
      "openai_models": ["gpt-6.2-sol", "gpt-6-luna"]
    }
"""
from __future__ import annotations
import json
import re
import time
from urllib import error, request

from .base import (ChatRequest, ChatResult, Provider, ProviderError,
                   ONLINE_MIN_OUTPUT_TOKENS, http_get_json, is_snapshot_id,
                   live_models)


# Shown before a key is set, or if the live listing fails. IDs as of
# November 2026.
DEFAULT_MODELS = [
    {"id": "gpt-6.2-sol",   "label": "GPT-6.2 Sol (balanced default)",  "vision": True},
    {"id": "gpt-6.2-astra", "label": "GPT-6.2 Astra (most capable)",    "vision": True},
    {"id": "gpt-6-luna",    "label": "GPT-6 Luna (cheapest, fastest)",  "vision": True},
]

# /v1/models returns everything the key can touch: embeddings, speech, image
# generation, realtime. Keep the chat families, drop the rest.
_CHAT_ID_RE = re.compile(r"^(gpt-|o\d|chat-latest)")
_NOT_CHAT = ("audio", "realtime", "tts", "transcribe", "image", "embedding",
             "search", "instruct", "moderation", "codex", "whisper", "dall-e")
_MAX_LISTED = 15


def _fetch_models(api_key: str) -> list[dict]:
    data = http_get_json(
        "https://api.openai.com/v1/models",
        {"Authorization": f"Bearer {api_key}"},
    )
    rows = []
    for m in data.get("data") or []:
        mid = m.get("id") or ""
        if not _CHAT_ID_RE.match(mid) or is_snapshot_id(mid):
            continue
        if any(word in mid for word in _NOT_CHAT):
            continue
        rows.append((m.get("created") or 0, mid))
    rows.sort(reverse=True)  # newest first
    return [{"id": mid, "label": mid, "vision": True} for _, mid in rows[:_MAX_LISTED]]


class OpenAIProvider(Provider):
    name = "openai"
    requires_api_key = True

    def list_models(self, cfg: dict) -> list[dict]:
        driver = cfg.get("driver", {})
        custom = driver.get("openai_models")
        if isinstance(custom, list) and custom:
            return [{"id": m, "label": m, "vision": True}
                    for m in custom if isinstance(m, str) and m.strip()]
        api_key = (driver.get("api_keys", {}).get("openai") or "").strip()
        live = live_models(self.name, api_key, lambda: _fetch_models(api_key))
        return live or [dict(m) for m in DEFAULT_MODELS]

    def chat(self, cfg: dict, model: str, req: ChatRequest, extra: dict | None = None) -> ChatResult:
        api_key = (cfg.get("driver", {}).get("api_keys", {}).get("openai") or "").strip()
        if not api_key:
            raise ProviderError(
                "openai api key not set. Get one at https://platform.openai.com/api-keys "
                "(prepaid — no free tier) and add it via the Run panel's "
                "🔑 Set key button or in config.json → driver → api_keys → openai."
            )

        url = "https://api.openai.com/v1/chat/completions"

        # User content is an array of "text" + "image_url" blocks.
        # Image is a data: URI with base64 PNG.
        user_content: list[dict] = [{"type": "text", "text": req.user}]
        if req.image_b64:
            user_content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/png;base64,{req.image_b64}",
                },
            })

        body: dict = {
            "model": model,
            "messages": [
                {"role": "system", "content": req.system},
                {"role": "user", "content": user_content},
            ],
            # `max_completion_tokens` replaced `max_tokens` on this endpoint,
            # and it also has to cover the model's reasoning tokens.
            "max_completion_tokens": max(int(req.max_tokens), ONLINE_MIN_OUTPUT_TOKENS),
        }
        # The GPT-5 / GPT-6 / o-series reasoning models reject a custom
        # temperature; only the older GPT-4 family still takes one.
        if model.startswith("gpt-4"):
            body["temperature"] = float(req.temperature)
        if req.json_mode:
            body["response_format"] = {"type": "json_object"}

        data = json.dumps(body).encode("utf-8")
        http_req = request.Request(url, data=data, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        })

        start = time.time()
        try:
            with request.urlopen(http_req, timeout=600) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except error.HTTPError as e:
            try:
                err_body = e.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                err_body = ""
            raise ProviderError(f"openai HTTP {e.code}: {err_body[:300]}") from e
        except error.URLError as e:
            raise ProviderError(f"openai request failed: {e}") from e
        except (json.JSONDecodeError, KeyError) as e:
            raise ProviderError(f"openai returned malformed response: {e}") from e
        latency = time.time() - start

        # Response shape: {"choices":[{"message":{"content":"..."}, ...}], ...}
        try:
            choices = result.get("choices") or []
            if not choices:
                raise ProviderError(f"openai returned no choices: {str(result)[:300]}")
            msg = choices[0].get("message") or {}
            raw_text = (msg.get("content") or "").strip()
        except (TypeError, AttributeError) as e:
            raise ProviderError(f"openai unexpected content shape: {str(result)[:300]}") from e
        if not raw_text:
            raise ProviderError(f"openai returned empty content: {str(result)[:300]}")

        return ChatResult(
            raw_text=raw_text,
            model=model,
            provider=self.name,
            latency_s=latency,
        )
