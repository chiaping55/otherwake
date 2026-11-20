"""Google Gemini provider — direct HTTP, no SDK dependency.

Uses the v1beta generateContent endpoint with vision-capable Gemini models.
Image is sent as base64 inline_data, JSON-mode requested via
responseMimeType: application/json.

Free tier: generate a key at https://aistudio.google.com/ — no payment
needed. Daily quota generous enough for our ~1 tick / 4-8 min cadence.
This is the recommended path for verifying the whole web → API → LTX
chain at zero cost.

API key lives in `cfg["driver"]["api_keys"]["gemini"]` — set it via the
Run panel's 🔑 Set key button or by editing config.json directly.

Model list: once a key is set, the dropdown is filled from the live
`GET /v1beta/models` listing (models that support generateContent). To pin
a specific set instead, add to config.json:
    "driver": {
      ...
      "gemini_models": ["gemini-4-hydrogen", "gemini-3.8-flash"]
    }
"""
from __future__ import annotations
import json
import time
from urllib import error, request

from .base import (ChatRequest, ChatResult, Provider, ProviderError,
                   ONLINE_MIN_OUTPUT_TOKENS, http_get_json, is_snapshot_id,
                   live_models)


# Shown before a key is set, or if the live listing fails. IDs as of
# November 2026 (the 2.0 family is shut down and 2.5 is deprecated).
DEFAULT_MODELS = [
    {"id": "gemini-4-hydrogen",     "label": "Gemini 4 Hydrogen (recommended default)", "vision": True},
    {"id": "gemini-3.8-flash",      "label": "Gemini 3.8 Flash (fast)",                 "vision": True},
    {"id": "gemini-3.5-flash-lite", "label": "Gemini 3.5 Flash-Lite (cheapest)",        "vision": True},
    {"id": "gemini-4-carbon",       "label": "Gemini 4 Carbon (strongest reasoning)",   "vision": True},
]

# The listing also carries embedding, image, speech and robotics models.
_NOT_CHAT = ("embedding", "image", "tts", "audio", "live", "robotics",
             "computer-use", "aqa")
_MAX_LISTED = 15


def _fetch_models(api_key: str) -> list[dict]:
    data = http_get_json(
        "https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
        {"x-goog-api-key": api_key},
    )
    out = []
    for m in data.get("models") or []:
        mid = (m.get("name") or "").removeprefix("models/")
        if not mid.startswith("gemini-") or is_snapshot_id(mid):
            continue
        if "generateContent" not in (m.get("supportedGenerationMethods") or []):
            continue
        if any(word in mid for word in _NOT_CHAT):
            continue
        out.append({"id": mid, "label": m.get("displayName") or mid, "vision": True})
    out.sort(key=lambda m: m["id"], reverse=True)  # highest version first
    return out[:_MAX_LISTED]


class GeminiProvider(Provider):
    name = "gemini"
    requires_api_key = True

    def list_models(self, cfg: dict) -> list[dict]:
        driver = cfg.get("driver", {})
        custom = driver.get("gemini_models")
        if isinstance(custom, list) and custom:
            return [{"id": m, "label": m, "vision": True}
                    for m in custom if isinstance(m, str) and m.strip()]
        api_key = (driver.get("api_keys", {}).get("gemini") or "").strip()
        live = live_models(self.name, api_key, lambda: _fetch_models(api_key))
        return live or [dict(m) for m in DEFAULT_MODELS]

    def chat(self, cfg: dict, model: str, req: ChatRequest, extra: dict | None = None) -> ChatResult:
        api_key = (cfg.get("driver", {}).get("api_keys", {}).get("gemini") or "").strip()
        if not api_key:
            raise ProviderError(
                "gemini api key not set. Get a free one at https://aistudio.google.com/ "
                "and add it via the Run panel's 🔑 Set key button (or in "
                "config.json → driver → api_keys → gemini)."
            )

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

        # Build content parts. Image (if any) goes first, then user text.
        parts: list[dict] = []
        if req.image_b64:
            parts.append({
                "inline_data": {
                    "mime_type": "image/png",
                    "data": req.image_b64,
                },
            })
        parts.append({"text": req.user})

        gen_config: dict = {
            "temperature": float(req.temperature),
            # Thinking tokens count against this cap too.
            "maxOutputTokens": max(int(req.max_tokens), ONLINE_MIN_OUTPUT_TOKENS),
        }
        if req.json_mode:
            gen_config["responseMimeType"] = "application/json"

        body = {
            "system_instruction": {"parts": [{"text": req.system}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": gen_config,
        }
        data = json.dumps(body).encode("utf-8")
        http_req = request.Request(url, data=data, headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
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
            raise ProviderError(f"gemini HTTP {e.code}: {err_body[:300]}") from e
        except error.URLError as e:
            raise ProviderError(f"gemini request failed: {e}") from e
        except (json.JSONDecodeError, KeyError) as e:
            raise ProviderError(f"gemini returned malformed response: {e}") from e
        latency = time.time() - start

        # Response shape: {"candidates":[{"content":{"parts":[{"text":"..."}, ...]}, ...}], ...}
        # Concatenate all text parts in the first candidate.
        try:
            candidates = result.get("candidates") or []
            if not candidates:
                # Often this means safety-blocking — surface the prompt feedback if present.
                pf = result.get("promptFeedback") or {}
                raise ProviderError(f"gemini returned no candidates (promptFeedback={pf})")
            cand0 = candidates[0]
            content_parts = (cand0.get("content") or {}).get("parts") or []
            text_parts = [p.get("text", "") for p in content_parts if isinstance(p, dict) and "text" in p]
            raw_text = "".join(text_parts).strip()
        except (TypeError, AttributeError) as e:
            raise ProviderError(f"gemini unexpected content shape: {str(result)[:300]}") from e
        if not raw_text:
            raise ProviderError(f"gemini returned no text: {str(result)[:300]}")

        return ChatResult(
            raw_text=raw_text,
            model=model,
            provider=self.name,
            latency_s=latency,
        )
