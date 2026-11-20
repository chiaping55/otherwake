"""Autonomous otherwake driver — CLI shim around driver/agent_core.py.

Historical entry point. The actual driver logic lives in agent_core.py and
the model layer in providers/. This file is just argparse plumbing so existing
scripts and shell history keep working.

Defaults to the Ollama provider (hence the file name) — pass --provider
anthropic / gemini / openai to drive the same loop from the terminal with an
online model.

Usage:
    python driver/ollama_agent.py --model gemma3:12b
    python driver/ollama_agent.py --model gemma3:12b --scope "Taiwan"
    python driver/ollama_agent.py --model gemma3:12b --no-translate
    python driver/ollama_agent.py --max-segments 20
    python driver/ollama_agent.py --wake-only
    python driver/ollama_agent.py --new-journey <agent-id>
    python driver/ollama_agent.py --rest
    python driver/ollama_agent.py --rest --summary "..." --name "門"
    python driver/ollama_agent.py --translate-journey 墨:j1
"""
from __future__ import annotations
import argparse
import json
import sys

# Make `import providers` etc. work when invoked as a script
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import agent_core


def main() -> int:
    parser = argparse.ArgumentParser(description="Run otherwake with a vision-capable LLM.")
    parser.add_argument("--provider", default="ollama", help="Provider name: ollama (default), anthropic, gemini, openai.")
    parser.add_argument("--model", default="gemma3:12b", help="Model name within the provider (default: gemma3:12b)")
    parser.add_argument("--num-gpu", type=int, default=None, help="Ollama: -1 = all on GPU, 0 = CPU only. Default: provider's own default.")
    parser.add_argument("--max-segments", type=int, default=0, help="Stop after this many successful segments. 0 (default) = unlimited.")
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds between ticks")
    parser.add_argument("--wake-only", action="store_true", help="Wake a new agent and exit")
    parser.add_argument("--new-journey", metavar="AGENT_ID", default=None, help="Start a new journey for an existing agent and exit")
    parser.add_argument("--rest", action="store_true", help="Rest the currently active journey and exit")
    parser.add_argument("--summary", default="", help="Summary text to write when using --rest")
    parser.add_argument("--name", default="", help="One-character name to assign when using --rest")
    parser.add_argument("--scope", default="", help="Optional soft location/theme box (e.g. 'Taiwan'). Stored on the journey.")
    parser.add_argument("--translate", default="", metavar="LANGS",
                        help="Comma-separated languages to translate at rest (zh, ja). Default: none. Example: --translate zh,ja")
    parser.add_argument("--translate-provider", default=None,
                        help="Provider for the translation pass. Default: same as --provider. Useful when the trip "
                             "model is too big to also handle translation (e.g. qwen3-vl:30b drives + gemma3:27b "
                             "translates).")
    parser.add_argument("--translate-model", default=None,
                        help="Model for the translation pass. Default: same as --model. Pair with --translate-provider.")
    parser.add_argument("--no-translate", action="store_true",
                        help="Deprecated alias — translation is now off by default. Kept for backwards compat.")
    parser.add_argument("--translate-journey", metavar="AGENT_ID:JOURNEY_ID", default=None,
                        help="Translate one rested journey. Combine with --translate LANGS to pick languages. Idempotent.")
    args = parser.parse_args()

    cfg = agent_core.load_config()
    agents_data = json.loads(agent_core.AGENTS.read_text(encoding="utf-8"))
    # Parse --translate value: comma-separated, only zh / ja accepted.
    translate_langs = [s.strip() for s in (args.translate or "").split(",") if s.strip()]
    translate_langs = [l for l in translate_langs if l in ("zh", "ja")]

    if args.translate_journey:
        if ":" not in args.translate_journey:
            print("[agent] --translate-journey needs AGENT_ID:JOURNEY_ID (e.g. 墨:j1)")
            return 1
        if not translate_langs:
            print("[agent] --translate-journey needs --translate LANGS (e.g. --translate zh,ja)")
            return 1
        agent_id, journey_id = args.translate_journey.split(":", 1)
        agent_core.translate_journey_via_provider(
            cfg, args.provider, args.model, agents_data,
            agent_id.strip(), journey_id.strip(), translate_langs,
        )
        return 0

    if args.rest:
        summary = args.summary
        name = args.name
        closure = ""
        # If the caller didn't supply summary/name, ask the same provider/model
        # to write them — matches the web Rest button's behaviour and the
        # Claude Code rest ceremony. CLI users can still pass --summary/--name
        # explicitly to override (or to skip the extra API call).
        if not summary and not name:
            agent, journey = agent_core.active_agent_and_journey(agents_data)
            if agent and journey:
                import json as _json
                from pathlib import Path as _Path
                session_path = _Path(__file__).resolve().parent.parent / "logs" / "session.json"
                try:
                    session = _json.loads(session_path.read_text(encoding="utf-8"))
                except (OSError, _json.JSONDecodeError):
                    session = []
                summary, name, closure = agent_core.generate_rest_summary_and_name(
                    cfg, args.provider, args.model, agent, journey, session,
                )
                if summary:
                    print(f"[agent] generated summary: {summary}")
                if name:
                    print(f"[agent] generated name: {name}")
                if closure:
                    print(f"[agent] generated closure: {closure}")
        agent_core.rest_active_journey(
            agents_data, summary, name, closure=closure,
            cfg=cfg, provider_name=args.provider, model=args.model,
            translate_langs=translate_langs,
            translate_provider=args.translate_provider,
            translate_model=args.translate_model,
        )
        return 0

    if args.wake_only:
        if agent_core.active_agent_and_journey(agents_data)[0]:
            print("[agent] an agent is already mid-journey; use --rest first")
            return 1
        agent_core.wake_new_agent(cfg, args.provider, args.model, agents_data, scope=args.scope)
        return 0

    if args.new_journey:
        try:
            agent_core.start_journey_for(
                agents_data, args.new_journey,
                scope=args.scope, provider_name=args.provider, model=args.model,
            )
        except ValueError as e:
            print(f"[agent] {e}")
            return 1
        return 0

    agent_core.run_loop(
        cfg, args.provider, args.model,
        num_gpu=args.num_gpu,
        scope=args.scope,
        translate_langs=translate_langs,
        translate_provider=args.translate_provider,
        translate_model=args.translate_model,
        max_segments=args.max_segments,
        delay=args.delay,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
