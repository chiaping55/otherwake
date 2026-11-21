"""Append-only session log writer for the web viewer. Portable.

Auto-injects `agent` and `journey` into new entries by reading logs/agents.json
and finding the first agent whose journeys[] contains an entry with
rested_at == null. An agent is "awake" iff it has such a journey; there is no
top-level `current` field any more — aliveness is derived from journey state.
"""
import json, datetime, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jsonio  # noqa: E402

# resolve project root via config.json (same as tolive.py)
_CFG_PATH = Path(__file__).resolve().parent.parent / "config.json"
_CFG = json.loads(_CFG_PATH.read_text(encoding="utf-8"))
ROOT = Path(_CFG["project_root"]) if _CFG.get("project_root") else _CFG_PATH.parent
LOG = ROOT / "logs" / "session.json"
AGENTS = ROOT / "logs" / "agents.json"


def _active_agent_and_journey() -> tuple[str | None, str | None]:
    """Return (agent_id, journey_id) for the first agent with a journey whose
    rested_at is null. Returns (None, None) if nobody is awake."""
    if not AGENTS.exists():
        return None, None
    try:
        data = json.loads(AGENTS.read_text(encoding="utf-8"))
    except Exception:
        return None, None
    for agent in data.get("agents", []):
        for journey in agent.get("journeys", []) or []:
            if journey.get("rested_at") is None:
                return agent.get("id"), journey.get("id")
    return None, None


def _read() -> list:
    # A missing log is a fresh start. A log that exists but can't be read is
    # NOT treated as empty — append() would then write back a one-entry file
    # and wipe every earlier journey.
    if not LOG.exists():
        return []
    return jsonio.read_json(LOG)


def _rel(p) -> str | None:
    if p is None:
        return None
    p = Path(p)
    try:
        return p.relative_to(ROOT).as_posix()
    except ValueError:
        return p.as_posix()


def append(**entry) -> None:
    entry.setdefault("timestamp", datetime.datetime.now().isoformat(timespec="seconds"))
    entry.setdefault("status", "ok")
    if "agent" not in entry or "journey" not in entry:
        a, j = _active_agent_and_journey()
        if "agent" not in entry and a:
            entry["agent"] = a
        if "journey" not in entry and j:
            entry["journey"] = j
    for k in ("image", "video", "poster", "source"):
        if k in entry and entry[k] is not None:
            entry[k] = _rel(entry[k])
    # strip None/empty lists to keep json tidy
    entry = {k: v for k, v in entry.items() if v is not None and v != []}
    data = _read()
    data.append(entry)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    jsonio.write_json(LOG, data)
