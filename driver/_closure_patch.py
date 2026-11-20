"""Retroactively add a closure sign-off to a rested journey's last thought.

For journeys rested BEFORE the closure feature shipped (commit ed91eab).
Generates a closure line via the same provider/model that drove the trip,
appends it to the last segment's English thought, then translates the
closure into zh / ja and appends to thought_zh / thought_ja if those
fields are populated.

Idempotent on the English thought (won't double-append the same closure).
For zh / ja: appends as long as the existing translation doesn't already
end with the new translated closure.

Usage:
    python driver/_closure_patch.py --agent 雨
    python driver/_closure_patch.py --agent 雨 --journey j1
    python driver/_closure_patch.py --agent 雨 --model gemma3:27b
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "driver"))

import agent_core
from agent_core import (
    generate_rest_summary_and_name,
    _append_closure_to_last_thought,
    _translate_via_provider,
    SESSION,
    AGENTS,
    load_config,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Patch a closure sign-off onto a rested journey's last thought.")
    parser.add_argument("--agent", required=True, help="agent id in logs/agents.json (e.g. 雨)")
    parser.add_argument("--journey", help="journey id (default: most recent rested)")
    parser.add_argument("--provider",
                        help="provider override (default: journey's recorded provider, falls back to 'ollama')")
    parser.add_argument("--model",
                        help="model override (default: journey's recorded model)")
    args = parser.parse_args()

    cfg = load_config()
    agents = json.loads(AGENTS.read_text(encoding="utf-8"))
    agent = next((a for a in agents["agents"] if a["id"] == args.agent), None)
    if not agent:
        print(f"agent {args.agent!r} not found in agents.json", file=sys.stderr)
        return 1

    journeys = agent.get("journeys") or []
    if args.journey:
        journey = next((j for j in journeys if j.get("id") == args.journey), None)
    else:
        rested = [j for j in journeys if j.get("rested_at")]
        journey = rested[-1] if rested else (journeys[-1] if journeys else None)
    if not journey:
        print(f"no journey found for {args.agent}", file=sys.stderr)
        return 1
    journey_id = journey["id"]

    provider = args.provider or journey.get("provider") or "ollama"
    model = args.model or journey.get("model")
    if not model:
        print(f"no model recorded on {args.agent}/{journey_id}; pass --model", file=sys.stderr)
        return 1

    session = json.loads(SESSION.read_text(encoding="utf-8"))
    print(f"[closure-patch] generating closure for {args.agent}/{journey_id} via {provider}:{model} ...")
    _summary, _name, closure = generate_rest_summary_and_name(
        cfg, provider, model, agent, journey, session,
    )
    if not closure:
        print("[closure-patch] model returned empty closure — nothing to do.")
        return 0
    print(f"[closure-patch] closure: {closure!r}")

    # English thought: shared helper takes care of separator + idempotency.
    if _append_closure_to_last_thought(args.agent, journey_id, closure):
        print("[closure-patch] appended closure to English thought.")
    else:
        print("[closure-patch] English thought unchanged (already ends with this closure, or no entries).")

    # zh / ja: only patch if those fields are already populated, so we
    # don't accidentally introduce orphan translations on English-only
    # trips. Use a single _translate_via_provider call to translate the
    # closure into whichever langs the last entry has populated.
    sess = json.loads(SESSION.read_text(encoding="utf-8"))
    last_idx = -1
    for i, e in enumerate(sess):
        if e.get("agent") == args.agent and e.get("journey") == journey_id and e.get("status") == "ok":
            last_idx = i
    if last_idx < 0:
        return 0
    last = sess[last_idx]

    needs_langs: list[str] = []
    if last.get("thought_zh"):
        needs_langs.append("zh")
    if last.get("thought_ja"):
        needs_langs.append("ja")

    if not needs_langs:
        print("[closure-patch] last segment has no zh/ja to update — done.")
        return 0

    print(f"[closure-patch] translating closure into {','.join(needs_langs)} ...")
    result = _translate_via_provider(cfg, provider, model, closure, needs_langs) or {}
    changed = False
    for lang in needs_langs:
        txt = (result.get(lang) or "").strip()
        if not txt:
            print(f"[closure-patch] {lang}: translation came back empty, skipping.")
            continue
        field = f"thought_{lang}"
        current = (last.get(field) or "").rstrip()
        if not current:
            last[field] = txt
        elif current.endswith(txt):
            print(f"[closure-patch] {lang}: already ends with closure, skipping.")
            continue
        else:
            last_char = current[-1]
            sep = " " if last_char in (".", "!", "?", "。", "！", "？") else " — "
            last[field] = f"{current}{sep}{txt}"
        changed = True
        print(f"[closure-patch] {lang}: appended {txt!r}")

    if changed:
        SESSION.write_text(json.dumps(sess, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("[closure-patch] saved session.json.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
