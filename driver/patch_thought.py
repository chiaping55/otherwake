"""Patch an existing session.json entry's `thought` field in place.

Preferred usage (stable command shape — one "Always allow" covers every patch
across every session):
    1. Write `logs/_patch.json` with the Write tool:
       { "seg_id": "a8-j1-003", "thought": "New thought text here." }
    2. Run with no args: `python driver/patch_thought.py`

The script deletes `_patch.json` after a successful patch so the buffer doesn't
get replayed by accident.

Legacy CLI forms are still supported (avoid in ticks — they vary in shape and
re-trigger permission prompts):
    python driver/patch_thought.py <seg_id> <new_thought_text>
    python driver/patch_thought.py <seg_id> - < file.txt
"""
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
PATCH_BUFFER = ROOT / "logs" / "_patch.json"


def patch(seg_id: str, new_thought: str) -> int:
    if not SESSION.exists():
        print(f"session.json not found at {SESSION}", file=sys.stderr)
        return 1
    data = json.loads(SESSION.read_text(encoding="utf-8"))
    hits = 0
    for entry in data:
        if entry.get("id") == seg_id:
            entry["thought"] = new_thought
            hits += 1
    if hits == 0:
        print(f"no entry with id {seg_id!r} in session.json", file=sys.stderr)
        return 2
    SESSION.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"patched {hits} entry/entries for {seg_id}")
    return 0


def main() -> int:
    if len(sys.argv) == 1:
        if not PATCH_BUFFER.exists():
            print(f"no args and no buffer at {PATCH_BUFFER}", file=sys.stderr)
            print(__doc__, file=sys.stderr)
            return 1
        try:
            spec = json.loads(PATCH_BUFFER.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"{PATCH_BUFFER} is not valid JSON: {e}", file=sys.stderr)
            return 1
        seg_id = spec.get("seg_id", "").strip()
        new_thought = spec.get("thought", "")
        if not seg_id:
            print(f"{PATCH_BUFFER} missing `seg_id`", file=sys.stderr)
            return 1
        if not new_thought.strip():
            print(f"{PATCH_BUFFER} has empty `thought`", file=sys.stderr)
            return 1
        rc = patch(seg_id, new_thought)
        if rc == 0:
            try:
                PATCH_BUFFER.unlink()
            except OSError:
                pass
        return rc

    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        return 1
    seg_id = sys.argv[1]
    arg = sys.argv[2]
    if arg == "-":
        new_thought = sys.stdin.read().rstrip("\n")
    else:
        new_thought = arg
    if not new_thought.strip():
        print("empty thought text", file=sys.stderr)
        return 1
    return patch(seg_id, new_thought)


if __name__ == "__main__":
    sys.exit(main())
