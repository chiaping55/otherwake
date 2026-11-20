"""List every untranslated session.json segment and every rested journey
missing a summary translation. Stable-shape command — one "Always allow"
covers this forever.

Usage: <python> driver/_verify_translations.py

Run it after editing `driver/_translate_thoughts.py` + executing the translator
to confirm coverage before announcing rest. Prints nothing reassuring (no
"all ok" wall) when there's nothing missing — silence means coverage is
complete.
"""
from __future__ import annotations
import json
import sys
from collections import defaultdict

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SESSION = ROOT / "logs" / "session.json"
AGENTS = ROOT / "logs" / "agents.json"


def main() -> int:
    if not SESSION.exists() or not AGENTS.exists():
        print("session.json or agents.json not found", file=sys.stderr)
        return 1

    session = json.loads(SESSION.read_text(encoding="utf-8"))
    agents_data = json.loads(AGENTS.read_text(encoding="utf-8"))

    by_group: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for e in session:
        ag = e.get("agent")
        jr = e.get("journey")
        if ag and jr:
            by_group[(ag, jr)].append(e)

    total_missing_thoughts = 0
    total_missing_summaries = 0
    lines: list[str] = []

    for agent in agents_data.get("agents", []):
        for journey in agent.get("journeys", []):
            key = (agent["id"], journey["id"])
            entries = by_group.get(key, [])

            thought_missing = [
                e for e in entries
                if not e.get("thought_zh") or not e.get("thought_ja")
            ]

            summary_needed = bool(journey.get("rested_at")) and bool(journey.get("summary"))
            summary_missing_langs = []
            if summary_needed:
                if not journey.get("summary_zh"):
                    summary_missing_langs.append("zh")
                if not journey.get("summary_ja"):
                    summary_missing_langs.append("ja")

            if not thought_missing and not summary_missing_langs:
                continue

            lines.append(f"\n{agent['id']} / {journey['id']}  ({len(entries)} segments)")
            if thought_missing:
                total_missing_thoughts += len(thought_missing)
                lines.append(f"  thoughts missing ({len(thought_missing)}):")
                for e in thought_missing:
                    miss = []
                    if not e.get("thought_zh"):
                        miss.append("zh")
                    if not e.get("thought_ja"):
                        miss.append("ja")
                    lines.append(f"    {e['id']}: missing {','.join(miss)}")
            if summary_missing_langs:
                total_missing_summaries += len(summary_missing_langs)
                lines.append(f"  summary missing: {','.join(summary_missing_langs)}")

    if not lines:
        print("ok — all thoughts and all rested-journey summaries have zh + ja")
        return 0

    for l in lines:
        print(l)
    print(
        f"\ntotal: {total_missing_thoughts} thought translations missing, "
        f"{total_missing_summaries} summary translations missing"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
