"""Ollama provider. Wraps the local Ollama HTTP API.

This used to be `call_ollama()` in ollama_agent.py — moved here so the rest of
the project can swap in Anthropic / OpenAI / Gemini through the same interface.
"""
from __future__ import annotations
import json
import shutil
import subprocess
import time
from urllib import error, request

from .base import ChatRequest, ChatResult, Provider, ProviderError


# Vision-capable model name fragments. `ollama list` doesn't tell us if a model
# has vision capability, so we filter heuristically. Keep this conservative —
# missing a model is fine (user just won't see it in the dropdown), but listing
# a non-vision model produces confusing errors at tick time.
#
# Add new entries here as new vision-capable models reach Ollama's library.
# Tip for contributors: use a fragment specific enough that it won't match a
# text-only model. e.g. `gemma3` (vision) instead of bare `gemma` (1 and 2 are
# text-only).
#
# Users can also extend this list per-machine without editing code by adding
# `driver.ollama_vision_hints: ["my_local_thing", ...]` to config.json — those
# entries are appended to the defaults below at list_models() time.
_VISION_HINTS_DEFAULT = (
    "vl",          # qwen2.5vl, qwen3-vl, internvl, cogvlm, etc.
    "vision",      # llama3.2-vision, granite-vision, phi-vision
    "llava",       # llava, llava-llama3, nanollava, llava-phi3
    "minicpm-v",
    "moondream",
    "bakllava",
    "gemma3",      # Gemma 3 added vision; Gemma 1/2 are text-only
    "gemma4",
    "qwen3.8",     # natively multimodal (plain qwen3 is text-only)
    "pixtral",     # Mistral's vision model
    "molmo",       # Allen AI's vision model
)


class OllamaProvider(Provider):
    name = "ollama"
    requires_api_key = False

    def list_models(self, cfg: dict) -> list[dict]:
        """Shell `ollama list` and keep vision-capable rows."""
        if not shutil.which("ollama"):
            return []
        try:
            r = subprocess.run(
                ["ollama", "list"],
                capture_output=True, text=True, timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError):
            return []
        if r.returncode != 0:
            return []

        # Per-machine extras from config.json (driver.ollama_vision_hints) —
        # users can surface a custom or freshly-released vision model without
        # editing this file.
        extras = (cfg.get("driver") or {}).get("ollama_vision_hints") or []
        if isinstance(extras, str):
            extras = [extras]
        hints = tuple(_VISION_HINTS_DEFAULT) + tuple(
            str(h).strip().lower() for h in extras if str(h).strip()
        )

        models = []
        for line in r.stdout.splitlines()[1:]:  # skip header row
            parts = line.split()
            if not parts:
                continue
            model_id = parts[0]
            lower = model_id.lower()
            if any(h in lower for h in hints):
                models.append({
                    "id": model_id,
                    "label": model_id,
                    "vision": True,
                })
        return models

    def chat(self, cfg: dict, model: str, req: ChatRequest, extra: dict | None = None) -> ChatResult:
        url = cfg.get("ollama_url", "http://127.0.0.1:11434").rstrip("/") + "/api/chat"
        messages: list[dict] = [
            {"role": "system", "content": req.system},
            {"role": "user", "content": req.user},
        ]
        if req.image_b64:
            messages[-1]["images"] = [req.image_b64]
        options: dict = {"temperature": req.temperature}
        if extra and "num_gpu" in extra and extra["num_gpu"] is not None:
            options["num_gpu"] = int(extra["num_gpu"])
        # Context window: Ollama defaults to 4096 for many models, which gets
        # blown out by SYSTEM_TICK (~1500 tok) + user prompt (~1500 tok) + image
        # vision tokens (1500-3000 tok depending on resolution) = HTTP 500.
        # Bump to 8192 by default; override via cfg.driver.ollama_num_ctx or
        # extra["num_ctx"].
        num_ctx = (extra or {}).get("num_ctx")
        if num_ctx is None:
            num_ctx = cfg.get("driver", {}).get("ollama_num_ctx", 8192)
        try:
            options["num_ctx"] = int(num_ctx)
        except (TypeError, ValueError):
            options["num_ctx"] = 8192
        # keep_alive controls how long Ollama holds the model in (V)RAM after
        # this response. Default 0 → unload immediately so ComfyUI/LTX can
        # take the GPU back for the t2v that follows. The two models don't
        # fit on a 16-24 GB consumer GPU together (e.g. gemma3:27b ≈ 17 GB +
        # LTX 2.3 ≈ 9 GB + T5 + VAE). Cold-loading Gemma each tick costs
        # ~30-60s but it still beats CPU-only inference by a wide margin
        # for big vision models. Override via cfg.driver.ollama_keep_alive
        # (int seconds, "5m"/"1h"/"0"/"-1" — Ollama's native format) or
        # extra["keep_alive"]. Set "-1" to keep the model loaded forever
        # (useful when you have headroom and don't run ComfyUI on the same
        # GPU, or when running pure-CPU and want to avoid disk reload).
        keep_alive = (extra or {}).get("keep_alive")
        if keep_alive is None:
            keep_alive = cfg.get("driver", {}).get("ollama_keep_alive", 0)
        payload = {
            "model": model,
            "messages": messages,
            "format": "json" if req.json_mode else "text",
            "stream": False,
            "options": options,
            "keep_alive": keep_alive,
        }
        body = json.dumps(payload).encode("utf-8")
        http_req = request.Request(url, data=body, headers={"Content-Type": "application/json"})

        start = time.time()
        try:
            with request.urlopen(http_req, timeout=600) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except error.URLError as e:
            raise ProviderError(f"Ollama request failed at {url}: {e}") from e
        except (json.JSONDecodeError, KeyError) as e:
            raise ProviderError(f"Ollama returned malformed response: {e}") from e
        latency = time.time() - start

        try:
            raw_text = result["message"]["content"]
        except (KeyError, TypeError) as e:
            raise ProviderError(f"Ollama response missing message.content: {result}") from e

        return ChatResult(
            raw_text=raw_text,
            model=model,
            provider=self.name,
            latency_s=latency,
        )
