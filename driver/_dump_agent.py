"""Dump every session.json entry for one agent — stable-shape command.

Why this exists: when translating at rest, the agent often wants to read the
full journey's thoughts in one pass (register-consistency check, spot the
vlogger voice, see where the trip went). session.json is ~7000 lines, so Read
tool on it is awkward. Using inline `python -c "..."` for this works but each
invocation is a new command shape — the human gets re-prompted for permission
every single time.

This script takes the query via a buffer so the command shape is stable:

  1. Write `logs/_inspect.json` with the Write tool:
        { "agent": "捲" }
     Optional fields:
        { "agent": "捲", "journey": "j1" }          # filter to one journey
        { "agent": "捲", "fields": ["thought","prompt"] }   # pick fields

  2. Run:
        <python> driver/_dump_agent.py

Default `fields` = ["thought"]. Output is plain text, one entry per block:

    --- 捲-j1-001 [t2i] ---
    thought: <text>

Buffer is deleted after a successful read.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

ROOT = Path(__file__).resolve().parent.parent
SESSION = ROOT / "logs" / "session.json"
BUFFER = ROOT / "logs" / "_inspect.json"


def main() -> int:
    if not BUFFER.exists():
        print(f"no buffer at {BUFFER}", file=sys.stderr)
        print(__doc__, file=sys.stderr)
        return 1
    try:
        spec = json.loads(BUFFER.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"{BUFFER} is not valid JSON: {e}", file=sys.stderr)
        return 1

    agent = spec.get("agent", "").strip()
    if not agent:
        print(f"{BUFFER} needs an 'agent' field", file=sys.stderr)
        return 1
    journey = spec.get("journey", "").strip() or None
    fields = spec.get("fields") or ["thought"]
    if not isinstance(fields, list):
        print("'fields' must be a list", file=sys.stderr)
        return 1

    if not SESSION.exists():
        print(f"session.json not found at {SESSION}", file=sys.stderr)
        return 1
    data = json.loads(SESSION.read_text(encoding="utf-8"))

    hits = 0
    for entry in data:
        if entry.get("agent") != agent:
            continue
        if journey and entry.get("journey") != journey:
            continue
        hits += 1
        print(f"--- {entry.get('id','?')} [{entry.get('kind','?')}] ---")
        for fld in fields:
            val = entry.get(fld, "")
            if val is None:
                val = ""
            print(f"{fld}: {val}")
        print()

    if hits == 0:
        print(f"(no entries matched agent={agent!r}"
              + (f", journey={journey!r}" if journey else "")
              + ")", file=sys.stderr)

    try:
        BUFFER.unlink()
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
