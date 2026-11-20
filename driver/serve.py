"""Range-capable HTTP server + agent control plane.

Three jobs:
  1. Serve files with HTTP Range support so <video> can seek (legacy).
  2. Edit API on POST /api/{delete-agent,reorder-agents,rename-display,
     edit-summary,edit-thought} for the viewer's edit mode (legacy).
  3. Run API on /api/run/{wake,rest,stop,new-journey,status,providers,save-key}
     for browser-driven journeys. The driver loop runs in a daemon thread
     inside this process; closing the browser tab does not stop a journey.

Bound to 127.0.0.1 only — single-user single-machine assumption holds across
all three jobs.
"""
import os, sys, re, json, subprocess, threading, time, traceback
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Ensure `driver/` is importable when this file is run directly.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import agent_core
from providers import PROVIDERS, get_provider, ProviderError

_CFG_PATH = Path(__file__).resolve().parent.parent / "config.json"


def _load_and_migrate_config() -> dict:
    """Load config.json. If the `driver` block doesn't exist (older installs),
    add the defaults and write back. Migration is one-shot, idempotent."""
    cfg = json.loads(_CFG_PATH.read_text(encoding="utf-8"))
    if "driver" not in cfg or not isinstance(cfg["driver"], dict):
        cfg["driver"] = {
            "default_provider": "ollama",
            "default_model": "gemma3:12b",
            "default_num_gpu": -1,
            "ollama_num_ctx": 8192,
            "tick_delay_s": 1.0,
            "fail_limit": 7,
            "api_keys": {"anthropic": "", "openai": "", "gemini": ""},
        }
        _CFG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("[serve] migrated config.json — added `driver` block (defaults).")
    return cfg


_CFG = _load_and_migrate_config()
ROOT = Path(_CFG["project_root"]) if _CFG.get("project_root") else _CFG_PATH.parent

AGENTS_JSON = ROOT / "logs" / "agents.json"
SESSION_JSON = ROOT / "logs" / "session.json"
STATUS_JSON = ROOT / "logs" / "status.json"


# ---------------------------------------------------------------------------
# Edit API helpers
# ---------------------------------------------------------------------------

def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _safe_id(s) -> str:
    """Reject anything that could path-traverse or escape the prefix space.
    Agent ids in this project are short single-grapheme or short word strings;
    no separators, no slashes, no dots."""
    if not isinstance(s, str) or not s:
        raise ValueError("id must be a non-empty string")
    if any(c in s for c in ("/", "\\", "..", "\x00")):
        raise ValueError(f"id contains illegal characters: {s!r}")
    if len(s) > 80:
        raise ValueError("id too long")
    return s


def _delete_agent(agent_id: str) -> dict:
    agent_id = _safe_id(agent_id)
    agents = _read_json(AGENTS_JSON)
    before = len(agents.get("agents", []))
    agents["agents"] = [a for a in agents.get("agents", []) if a.get("id") != agent_id]
    if len(agents["agents"]) == before:
        return {"ok": False, "error": f"no agent with id {agent_id!r}"}

    session = _read_json(SESSION_JSON)
    prefix = f"{agent_id}-"
    session_kept = []
    deleted_seg_ids = []
    referenced_files: set[Path] = set()
    for e in session:
        if e.get("agent") == agent_id or (isinstance(e.get("id"), str) and e["id"].startswith(prefix)):
            deleted_seg_ids.append(e.get("id"))
            # Collect every media file this entry referenced. Covers both naming
            # conventions: Claude-driven (`<name>-j1-NNN.png`) and Ollama-driven
            # (`NNN_slug.png`) — the latter wouldn't match the agent-prefix scan.
            for fld in ("image", "video", "poster", "source"):
                p = e.get(fld)
                if isinstance(p, str) and p:
                    referenced_files.add(ROOT / p)
                    # also try the corresponding `_last.png` for video entries
                    if fld == "video" and p.endswith(".mp4"):
                        last = p[:-4].replace("video/", "image/", 1) + "_last.png"
                        referenced_files.add(ROOT / last)
        else:
            session_kept.append(e)

    files_removed = 0
    # First: delete by entry-referenced paths (works for both naming styles)
    for path in referenced_files:
        if path.is_file():
            try:
                path.unlink()
                files_removed += 1
            except OSError:
                pass
    # Then: also sweep prefix-matched files (catches stragglers from
    # interrupted runs, renamed files, etc.)
    for folder_name in ("image", "video"):
        folder = ROOT / folder_name
        if not folder.exists():
            continue
        for f in list(folder.iterdir()):
            if f.is_file() and f.name.startswith(prefix):
                try:
                    f.unlink()
                    files_removed += 1
                except OSError:
                    pass

    _write_json(AGENTS_JSON, agents)
    _write_json(SESSION_JSON, session_kept)
    return {
        "ok": True,
        "deleted_agent": agent_id,
        "deleted_segments": len(deleted_seg_ids),
        "deleted_files": files_removed,
    }


def _reorder_agents(order: list) -> dict:
    if not isinstance(order, list):
        return {"ok": False, "error": "order must be a list of agent ids"}
    agents = _read_json(AGENTS_JSON)
    by_id = {a["id"]: a for a in agents.get("agents", [])}
    seen = set()
    new_list = []
    for aid in order:
        if not isinstance(aid, str) or aid in seen:
            continue
        if aid in by_id:
            new_list.append(by_id[aid])
            seen.add(aid)
    # any agent the client forgot stays at the end in its original order
    for a in agents.get("agents", []):
        if a["id"] not in seen:
            new_list.append(a)
    agents["agents"] = new_list
    _write_json(AGENTS_JSON, agents)
    return {"ok": True, "order": [a["id"] for a in new_list]}


def _rename_display(agent_id: str, display: str) -> dict:
    agent_id = _safe_id(agent_id)
    if not isinstance(display, str):
        return {"ok": False, "error": "display must be a string"}
    display = display.strip()
    if len(display) > 80:
        return {"ok": False, "error": "display too long"}
    agents = _read_json(AGENTS_JSON)
    target = next((a for a in agents.get("agents", []) if a.get("id") == agent_id), None)
    if target is None:
        return {"ok": False, "error": f"no agent with id {agent_id!r}"}
    target["display"] = display
    _write_json(AGENTS_JSON, agents)
    return {"ok": True, "id": agent_id, "display": display}


def _edit_summary(agent_id: str, journey_id: str, fields: dict) -> dict:
    agent_id = _safe_id(agent_id)
    journey_id = _safe_id(journey_id)
    agents = _read_json(AGENTS_JSON)
    target_agent = next((a for a in agents.get("agents", []) if a.get("id") == agent_id), None)
    if target_agent is None:
        return {"ok": False, "error": f"no agent with id {agent_id!r}"}
    target_journey = next((j for j in (target_agent.get("journeys") or []) if j.get("id") == journey_id), None)
    if target_journey is None:
        return {"ok": False, "error": f"no journey {journey_id!r} on agent {agent_id!r}"}
    touched = []
    for k in ("summary", "summary_zh", "summary_ja"):
        if k in fields and isinstance(fields[k], str):
            target_journey[k] = fields[k]
            touched.append(k)
    if not touched:
        return {"ok": False, "error": "no summary/summary_zh/summary_ja in body"}
    _write_json(AGENTS_JSON, agents)
    return {"ok": True, "fields": touched}


def _edit_thought(seg_id: str, fields: dict) -> dict:
    seg_id = _safe_id(seg_id)
    session = _read_json(SESSION_JSON)
    target = next((e for e in session if e.get("id") == seg_id), None)
    if target is None:
        return {"ok": False, "error": f"no segment with id {seg_id!r}"}
    touched = []
    for k in ("thought", "thought_zh", "thought_ja"):
        if k in fields and isinstance(fields[k], str):
            target[k] = fields[k]
            touched.append(k)
    if not touched:
        return {"ok": False, "error": "no thought/thought_zh/thought_ja in body"}
    _write_json(SESSION_JSON, session)
    return {"ok": True, "fields": touched}


# ---------------------------------------------------------------------------
# Driver control plane (web-driven journeys)
# ---------------------------------------------------------------------------

class DriverState:
    """Singleton holding the running journey's state. One lock guards all
    mutations so the driver thread + HTTP request threads don't race."""
    def __init__(self):
        self.lock = threading.RLock()
        self.thread: threading.Thread | None = None
        self.state = "idle"           # idle | waking | running | resting | stopping | error
        self.phase: str | None = None # thinking | generating | translating | None
        self.run: dict | None = None  # {provider, model, num_gpu, scope, started_at, agent_id, journey_id}
        self.last_seg_id: str | None = None
        self.last_error: str | None = None
        self.stop_requested = False
        self.rest_requested = False
        self.current_subproc = None   # Popen of an in-flight _gen.py
        self.segments_done = 0

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "state": self.state,
                "phase": self.phase,
                "run": dict(self.run) if self.run else None,
                "last_seg_id": self.last_seg_id,
                "last_error": self.last_error,
                "segments_done": self.segments_done,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }


DRIVER = DriverState()


def _write_status_atomic(payload: dict) -> None:
    """Atomic-replace status.json. Retries on Windows file-rename race
    (the viewer's pollStatus fetch holds a short read lock; os.replace
    can fail if the destination is open for read at that instant)."""
    tmp = STATUS_JSON.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    last_err: OSError | None = None
    for attempt in range(6):
        try:
            os.replace(tmp, STATUS_JSON)
            return
        except OSError as e:
            last_err = e
            time.sleep(0.05 * (attempt + 1))  # 50, 100, 150, 200, 250, 300 ms
    # Best-effort: if we still can't replace, leave the .tmp behind and log once.
    print(f"[serve] status write gave up after retries: {last_err}")


def _status_publish() -> None:
    """Compose the full status object (DriverState snapshot + agent context)
    and write to logs/status.json. Called on every state/phase transition."""
    snap = DRIVER.snapshot()
    payload = {
        "state": snap["state"],
        "phase": snap["phase"],
        "updated_at": snap["updated_at"],
        "agent": (snap["run"] or {}).get("agent_id"),
        "journey": (snap["run"] or {}).get("journey_id"),
        "scope": (snap["run"] or {}).get("scope") or "",
        "provider": (snap["run"] or {}).get("provider"),
        "model": (snap["run"] or {}).get("model"),
        "started_at": (snap["run"] or {}).get("started_at"),
        "segments_done": snap["segments_done"],
        "seg_id": snap["last_seg_id"],
        "last_error": snap["last_error"],
    }
    try:
        _write_status_atomic(payload)
    except OSError as e:
        print(f"[serve] status write failed: {e}")


def _status_callback(phase: str, info: dict) -> None:
    """Hook passed to agent_core.run_loop. Updates state + publishes."""
    with DRIVER.lock:
        DRIVER.phase = phase if phase not in ("idle",) else None
        if phase == "thinking":
            DRIVER.state = "running"
            if "agent" in info and DRIVER.run is not None:
                DRIVER.run["agent_id"] = info["agent"]
                DRIVER.run["journey_id"] = info["journey"]
            if "segments_done" in info:
                DRIVER.segments_done = info["segments_done"]
        elif phase == "generating":
            DRIVER.last_seg_id = info.get("seg_id")
        elif phase == "resting":
            DRIVER.state = "resting"
        elif phase == "idle":
            DRIVER.state = "idle"
            DRIVER.run = None
        elif phase == "error":
            DRIVER.last_error = info.get("error") or f"failures={info.get('failures')}"
    _status_publish()


def _on_subproc(proc) -> None:
    """Hook passed to agent_core.run_generator so the driver thread holds the
    Popen handle of the running _gen.py."""
    with DRIVER.lock:
        DRIVER.current_subproc = proc


def _stop_check() -> bool:
    with DRIVER.lock:
        return DRIVER.stop_requested or DRIVER.rest_requested


def _cleanup_orphan_journey() -> None:
    """Remove an active journey that never produced a segment.

    Wake → provider failure (e.g. Ollama context overflow) → Stop leaves a
    placeholder agent in agents.json with `rested_at: null` and zero
    segments in session.json. That state silently blocks every future Wake
    with "another driver is mid-journey" because `active_agent_and_journey`
    still finds it.

    Run this on every driver-thread exit. If the active journey has zero
    segments in session.json, drop it; if that was the agent's only journey,
    drop the whole placeholder agent. Real journeys (1+ segments) are left
    alone so Stop can still mean "pause, resume later"."""
    try:
        agents_data = json.loads(AGENTS_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"[serve] orphan cleanup: agents.json unreadable: {e}")
        return
    agent, journey = agent_core.active_agent_and_journey(agents_data)
    if agent is None or journey is None:
        return  # nothing active — nothing to clean up
    aid = agent.get("id")
    jid = journey.get("id")
    try:
        sess = json.loads(SESSION_JSON.read_text(encoding="utf-8"))
        if not isinstance(sess, list):
            sess = []
    except (OSError, json.JSONDecodeError):
        sess = []
    has_segments = any(
        e.get("agent") == aid and e.get("journey") == jid
        for e in sess
    )
    if has_segments:
        return  # journey produced content — preserve for resume
    journeys = agent.get("journeys", [])
    if len(journeys) <= 1:
        agents_data["agents"] = [a for a in agents_data["agents"] if a.get("id") != aid]
        print(f"[serve] cleaned up orphan placeholder agent {aid!r} (wake produced no segments)")
    else:
        agent["journeys"] = [j for j in journeys if j.get("id") != jid]
        print(f"[serve] cleaned up orphan journey {aid}/{jid} (wake produced no segments)")
    try:
        AGENTS_JSON.write_text(
            json.dumps(agents_data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as e:
        print(f"[serve] orphan cleanup: agents.json write failed: {e}")


def _driver_thread_target(spec: dict) -> None:
    """Body of the daemon thread. spec = {provider, model, num_gpu, scope, translate_langs, translate_provider, translate_model, max_segments}"""
    cfg = _load_and_migrate_config()
    try:
        with DRIVER.lock:
            DRIVER.state = "waking"
            DRIVER.last_error = None
        _status_publish()
        agent_core.run_loop(
            cfg,
            spec["provider"],
            spec["model"],
            num_gpu=spec.get("num_gpu"),
            scope=spec.get("scope", ""),
            translate_langs=spec.get("translate_langs", []),
            translate_provider=spec.get("translate_provider"),
            translate_model=spec.get("translate_model"),
            max_segments=spec.get("max_segments", 0),
            delay=cfg.get("driver", {}).get("tick_delay_s", 1.0),
            fail_limit=cfg.get("driver", {}).get("fail_limit", 7),
            status_cb=_status_callback,
            on_subproc=_on_subproc,
            stop_check=_stop_check,
        )
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        with DRIVER.lock:
            DRIVER.state = "error"
            DRIVER.last_error = f"{type(e).__name__}: {e}"
        _cleanup_orphan_journey()
        _status_publish()
        return

    # If we got here, run_loop returned normally — either the model rested,
    # or stop/rest was requested, or fail_limit was hit. If rest was requested
    # explicitly (not by the model), do the rest now.
    with DRIVER.lock:
        rest_requested = DRIVER.rest_requested
        run = dict(DRIVER.run) if DRIVER.run else None
    rest_done_cleanly = False
    if rest_requested and run is not None:
        try:
            agents_data = json.loads(AGENTS_JSON.read_text(encoding="utf-8"))
            # Web Rest used to leave the journey with empty summary/name and
            # punt to edit mode. Now we ask the same provider/model that drove
            # the trip to write the rest paperwork — summary + one-character
            # name — so the web flow matches Claude Code's rest ceremony.
            # On any failure we fall back to passive rest (the user can still
            # fill summary/name via edit mode), so this can never block the
            # rest itself.
            summary, name, closure = "", "", ""
            agent, journey = agent_core.active_agent_and_journey(agents_data)
            if agent and journey:
                with DRIVER.lock:
                    DRIVER.state = "resting"
                    DRIVER.phase = "summarizing"
                _status_publish()
                try:
                    session = json.loads(SESSION_JSON.read_text(encoding="utf-8"))
                    summary, name, closure = agent_core.generate_rest_summary_and_name(
                        cfg, run["provider"], run["model"], agent, journey, session,
                    )
                except (OSError, json.JSONDecodeError, agent_core.ProviderError) as e:
                    print(f"[serve] rest paperwork errored ({type(e).__name__}: {e}); resting passively")
                    summary, name, closure = "", "", ""
            agent_core.rest_active_journey(
                agents_data, summary, name,
                closure=closure,
                cfg=cfg,
                provider_name=run["provider"], model=run["model"],
                translate_langs=spec.get("translate_langs", []),
                translate_provider=spec.get("translate_provider"),
                translate_model=spec.get("translate_model"),
            )
            rest_done_cleanly = True
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            with DRIVER.lock:
                DRIVER.last_error = f"rest failed: {type(e).__name__}: {e}"

    _cleanup_orphan_journey()
    with DRIVER.lock:
        DRIVER.state = "idle"
        DRIVER.phase = None
        DRIVER.run = None
        DRIVER.stop_requested = False
        DRIVER.rest_requested = False
        DRIVER.thread = None
        # Clear stale tick-error after a successful rest. last_error survives
        # a Stop (so users can see why the loop bailed before resuming) but a
        # rest closes the journey cleanly — leaving the old "timed out"
        # message in the status panel just confuses people.
        if rest_done_cleanly:
            DRIVER.last_error = None
    _status_publish()


def _run_wake(body: dict) -> dict:
    """Start a journey. Refuses if any driver path is already active."""
    with DRIVER.lock:
        if DRIVER.state != "idle" or (DRIVER.thread and DRIVER.thread.is_alive()):
            return {"ok": False, "error": "driver is already busy",
                    "state": DRIVER.state, "run": DRIVER.run}

    # Conflict check against external paths (Claude Code, ollama_agent.py CLI).
    try:
        agents_data = json.loads(AGENTS_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return {"ok": False, "error": f"can't read agents.json: {e}"}
    active_agent, active_journey = agent_core.active_agent_and_journey(agents_data)
    if active_agent is not None and body.get("force") is not True:
        return {
            "ok": False,
            "error": "another driver appears to be mid-journey",
            "active_agent": active_agent.get("id"),
            "active_journey": (active_journey or {}).get("id"),
            "hint": "rest the active journey first, or pass force=true to wake on top of it",
        }

    cfg = _load_and_migrate_config()
    defaults = cfg.get("driver", {})
    provider = (body.get("provider") or defaults.get("default_provider") or "ollama").strip()
    model    = (body.get("model")    or defaults.get("default_model")    or "gemma3:12b").strip()
    if provider not in PROVIDERS:
        return {"ok": False, "error": f"unknown provider {provider!r}",
                "available": sorted(PROVIDERS)}

    # If the provider requires a key, fail-fast with a friendly UI hint
    # instead of letting the daemon thread crash on the first chat() call.
    provider_inst = PROVIDERS[provider]()
    if provider_inst.requires_api_key:
        api_key = (cfg.get("driver", {}).get("api_keys", {}).get(provider) or "").strip()
        if not api_key:
            return {"ok": False,
                    "error": f"{provider} needs an API key — set one in the Run panel first",
                    "needs_key": provider}

    num_gpu = body.get("num_gpu")
    if num_gpu is None:
        num_gpu = defaults.get("default_num_gpu")
    if num_gpu is not None:
        try:
            num_gpu = int(num_gpu)
        except (TypeError, ValueError):
            return {"ok": False, "error": "num_gpu must be an integer or null"}

    scope = (body.get("scope") or "").strip()
    raw_langs = body.get("translate_langs") or []
    if not isinstance(raw_langs, list):
        raw_langs = []
    translate_langs = [str(l).strip() for l in raw_langs if str(l).strip() in ("zh", "ja")]

    # Optional separate translation model — pair big trip drivers with a
    # smaller dedicated translator (e.g. qwen3-vl:30b drives + gemma3:27b
    # translates). Empty / missing → translation uses the trip model.
    translate_provider = (body.get("translate_provider") or "").strip() or None
    translate_model = (body.get("translate_model") or "").strip() or None
    if translate_provider and translate_provider not in PROVIDERS:
        return {"ok": False, "error": f"unknown translate_provider {translate_provider!r}",
                "available": sorted(PROVIDERS)}

    # Normalise wake-command-style inputs: empty / 開始 / start → free-form scope
    if scope.lower() in ("", "開始", "start", "begin", "go", "出發"):
        scope = ""

    spec = {
        "provider": provider,
        "model": model,
        "num_gpu": num_gpu,
        "scope": scope,
        "translate_langs": translate_langs,
        "translate_provider": translate_provider,
        "translate_model": translate_model,
        "max_segments": int(body.get("max_segments") or 0),
    }

    with DRIVER.lock:
        DRIVER.run = {
            "provider": provider, "model": model, "num_gpu": num_gpu, "scope": scope,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "agent_id": (active_agent or {}).get("id"),
            "journey_id": (active_journey or {}).get("id"),
        }
        DRIVER.state = "waking"
        DRIVER.phase = None
        DRIVER.last_error = None
        DRIVER.last_seg_id = None
        DRIVER.segments_done = 0
        DRIVER.stop_requested = False
        DRIVER.rest_requested = False
        DRIVER.thread = threading.Thread(target=_driver_thread_target, args=(spec,), daemon=True)
        DRIVER.thread.start()
    _status_publish()
    return {"ok": True, "started": True, "spec": spec}


def _run_new_journey(body: dict) -> dict:
    """Start a new journey on an existing agent. Same body as wake plus agent_id."""
    agent_id = (body.get("agent_id") or "").strip()
    if not agent_id:
        return {"ok": False, "error": "agent_id required"}
    with DRIVER.lock:
        if DRIVER.state != "idle" or (DRIVER.thread and DRIVER.thread.is_alive()):
            return {"ok": False, "error": "driver is already busy"}

    cfg = _load_and_migrate_config()
    defaults = cfg.get("driver", {})
    provider = (body.get("provider") or defaults.get("default_provider") or "ollama").strip()
    model    = (body.get("model")    or defaults.get("default_model")    or "gemma3:12b").strip()
    scope    = (body.get("scope") or "").strip()
    if scope.lower() in ("開始", "start", "begin", "go", "出發"):
        scope = ""

    try:
        agents_data = json.loads(AGENTS_JSON.read_text(encoding="utf-8"))
        agent_core.start_journey_for(
            agents_data, agent_id,
            scope=scope, provider_name=provider, model=model,
        )
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    except (OSError, json.JSONDecodeError) as e:
        return {"ok": False, "error": f"agents.json read failed: {e}"}

    # Now wake the loop on this newly-started journey.
    return _run_wake({
        "provider": provider, "model": model,
        "num_gpu": body.get("num_gpu"),
        "scope": scope,
        "translate_langs": body.get("translate_langs") or [],
        "translate_provider": body.get("translate_provider"),
        "translate_model": body.get("translate_model"),
        "force": True,  # we just created the active journey, so the conflict check would otherwise refuse
    })


def _run_rest(body: dict) -> dict:
    """Graceful rest. Two paths:

      a) Daemon thread is alive (normal mid-trip rest) — set rest_requested
         and the daemon picks it up after the current tick finishes.

      b) Daemon is idle but agents.json still has an active journey — i.e.
         a previous Stop left it paused, and the user has now decided to
         close the trip after all. Spawn a one-shot worker thread that
         runs the same rest paperwork (summary + name + optional
         translation) and updates DriverState so the UI sees the standard
         resting → summarizing → idle progression.

    Without (b) the user is stuck: the UI's Rest button activates the
    daemon-only path, so once the daemon exits there's no way to close
    a paused journey except CLI rest or force-resuming first.
    """
    with DRIVER.lock:
        if DRIVER.thread and DRIVER.thread.is_alive():
            DRIVER.rest_requested = True
            return {"ok": True, "scheduled": True, "via": "daemon"}

    # Daemon idle — check for an orphan active journey to close out.
    try:
        agents_data = json.loads(AGENTS_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return {"ok": False, "error": f"agents.json read failed: {e}"}
    agent, journey = agent_core.active_agent_and_journey(agents_data)
    if not agent or not journey:
        return {"ok": False, "error": "no active journey to rest"}

    cfg = _load_and_migrate_config()
    defaults = cfg.get("driver", {})
    # Prefer body overrides; fall back to whatever the journey was driven
    # with originally; finally to global defaults.
    provider = (body.get("provider") or journey.get("provider")
                or defaults.get("default_provider", "ollama"))
    model = (body.get("model") or journey.get("model")
             or defaults.get("default_model", "gemma3:12b"))
    raw_langs = body.get("translate_langs") or []
    if not isinstance(raw_langs, list):
        raw_langs = []
    translate_langs = [str(l).strip() for l in raw_langs if str(l).strip() in ("zh", "ja")]
    translate_provider = (body.get("translate_provider") or "").strip() or None
    translate_model = (body.get("translate_model") or "").strip() or None

    def _orphan_rest_worker() -> None:
        with DRIVER.lock:
            DRIVER.state = "resting"
            DRIVER.phase = "summarizing"
            DRIVER.run = {
                "provider": provider, "model": model,
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "agent_id": agent["id"], "journey_id": journey["id"],
                "scope": journey.get("scope") or "",
                "num_gpu": None,
            }
            DRIVER.last_error = None
        _status_publish()
        summary, name, closure = "", "", ""
        try:
            session = json.loads(SESSION_JSON.read_text(encoding="utf-8"))
            summary, name, closure = agent_core.generate_rest_summary_and_name(
                cfg, provider, model, agent, journey, session,
            )
        except (OSError, json.JSONDecodeError, agent_core.ProviderError) as e:
            print(f"[serve] orphan rest paperwork errored ({type(e).__name__}: {e}); resting passively")
        try:
            # Re-read agents.json in case it changed during paperwork.
            agents_data2 = json.loads(AGENTS_JSON.read_text(encoding="utf-8"))
            agent_core.rest_active_journey(
                agents_data2, summary, name, closure=closure,
                cfg=cfg, provider_name=provider, model=model,
                translate_langs=translate_langs,
                translate_provider=translate_provider,
                translate_model=translate_model,
            )
            rest_ok = True
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            with DRIVER.lock:
                DRIVER.last_error = f"rest failed: {type(e).__name__}: {e}"
            rest_ok = False
        with DRIVER.lock:
            DRIVER.state = "idle"
            DRIVER.phase = None
            DRIVER.run = None
            if rest_ok:
                DRIVER.last_error = None
        _status_publish()

    threading.Thread(target=_orphan_rest_worker, daemon=True).start()
    return {"ok": True, "scheduled": True, "via": "orphan"}


def _run_stop(body: dict) -> dict:
    """Stop the loop gracefully: the segment in flight finishes, then the
    driver thread exits and leaves the journey active for a later resume."""
    with DRIVER.lock:
        if DRIVER.state == "idle" and not (DRIVER.thread and DRIVER.thread.is_alive()):
            return {"ok": False, "error": "no journey running"}
        DRIVER.stop_requested = True
    return {"ok": True, "scheduled": True, "hard": False}


def _run_status() -> dict:
    """One-shot status read; the file at logs/status.json is the canonical
    source for the polling viewer."""
    snap = DRIVER.snapshot()
    snap["server_time"] = datetime.now().isoformat(timespec="seconds")
    return snap


def _run_providers() -> dict:
    """Catalog of providers + their available models + auth status."""
    cfg = _load_and_migrate_config()
    api_keys = cfg.get("driver", {}).get("api_keys", {})
    out = []
    for name, cls in PROVIDERS.items():
        inst = cls()
        try:
            models = inst.list_models(cfg)
        except Exception as e:  # noqa: BLE001
            models = []
            out.append({"name": name, "error": f"{type(e).__name__}: {e}",
                        "models": [], "available": False,
                        "requires_api_key": inst.requires_api_key})
            continue
        if inst.requires_api_key:
            available = bool(api_keys.get(name))
        else:
            available = True
        out.append({
            "name": name,
            "requires_api_key": inst.requires_api_key,
            "available": available,
            "models": models,
        })
    return {"ok": True, "providers": out}


def _run_save_key(body: dict) -> dict:
    """Persist an API key for an online provider."""
    provider = (body.get("provider") or "").strip()
    key = body.get("api_key") or ""
    if not provider or provider not in PROVIDERS:
        return {"ok": False, "error": f"unknown provider {provider!r}"}
    cfg = _load_and_migrate_config()
    cfg.setdefault("driver", {}).setdefault("api_keys", {})[provider] = key
    _CFG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"ok": True, "provider": provider, "set": bool(key)}


# ---------------------------------------------------------------------------
# Export (driver/export.py wrapper)
# ---------------------------------------------------------------------------

# Track in-flight export jobs so the UI can poll status. One job per agent at
# a time is enough — exports are infrequent and ffmpeg saturates the CPU.
_EXPORT_JOBS: dict[str, dict] = {}
_EXPORT_LOCK = threading.RLock()


def _run_export(body: dict) -> dict:
    """Spawn driver/export.py in a background thread and return immediately
    with a job id the UI can poll. Body fields:
      agent_id   (required)  — agent in agents.json
      journey_id (optional)  — single journey to export
      preset     ("youtube" | "social", default "youtube")
      burn       ("en" | "zh" | "ja" | None) — burn subtitles in this lang
      intro      (bool)      — prepend title card
    """
    agent_id = (body.get("agent_id") or "").strip()
    if not agent_id:
        return {"ok": False, "error": "agent_id required"}
    journey_id = (body.get("journey_id") or "").strip()
    preset = (body.get("preset") or "youtube").strip().lower()
    if preset not in ("youtube", "social"):
        return {"ok": False, "error": f"unknown preset {preset!r}"}
    burn = (body.get("burn") or "").strip().lower() or None
    if burn and burn not in ("en", "zh", "ja"):
        return {"ok": False, "error": f"unknown burn lang {burn!r}"}
    intro = bool(body.get("intro"))

    job_id = f"{agent_id}{('-' + journey_id) if journey_id else ''}"

    with _EXPORT_LOCK:
        existing = _EXPORT_JOBS.get(job_id)
        if existing and existing.get("state") == "running":
            return {"ok": False, "error": f"export already running for {job_id}",
                    "job_id": job_id}
        _EXPORT_JOBS[job_id] = {
            "state": "running",
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "agent_id": agent_id,
            "journey_id": journey_id,
            "preset": preset,
            "burn": burn,
            "intro": intro,
            "out_dir": None,
            "error": None,
            "log": "",
        }

    cfg = _load_and_migrate_config()
    comfy_root = (cfg.get("comfy_root") or "").rstrip("/").rstrip("\\")
    # Prefer ComfyUI's embedded python so ffmpeg path resolves the same way
    # the rest of the project uses.
    if comfy_root:
        py_candidates = [
            Path(comfy_root) / "python_embeded" / "python.exe",
            Path(comfy_root) / "python_embeded" / "bin" / "python",
        ]
        py = next((str(p) for p in py_candidates if p.is_file()), sys.executable)
    else:
        py = sys.executable

    cmd = [py, str(ROOT / "driver" / "export.py"), "--agent", agent_id]
    if journey_id:
        cmd += ["--journey", journey_id]
    cmd += ["--preset", preset]
    if burn:
        cmd += ["--burn", burn]
    if intro and preset != "social":
        # social already implies intro; only need the flag for youtube preset.
        cmd += ["--intro"]

    def _worker() -> None:
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", cwd=str(ROOT),
            )
            log_lines: list[str] = []
            for line in proc.stdout or []:
                log_lines.append(line.rstrip())
                # Keep tail for UI polling — full log can grow long.
                if len(log_lines) > 200:
                    log_lines = log_lines[-200:]
                with _EXPORT_LOCK:
                    _EXPORT_JOBS[job_id]["log"] = "\n".join(log_lines)
            proc.wait()
            ok = proc.returncode == 0
            out_dir = ROOT / "exports" / job_id
            with _EXPORT_LOCK:
                _EXPORT_JOBS[job_id].update({
                    "state": "done" if ok else "error",
                    "ended_at": datetime.now().isoformat(timespec="seconds"),
                    "out_dir": str(out_dir) if ok and out_dir.exists() else None,
                    "error": None if ok else f"export.py exit code {proc.returncode}",
                })
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            with _EXPORT_LOCK:
                _EXPORT_JOBS[job_id].update({
                    "state": "error",
                    "ended_at": datetime.now().isoformat(timespec="seconds"),
                    "error": f"{type(e).__name__}: {e}",
                })

    threading.Thread(target=_worker, daemon=True).start()
    return {"ok": True, "scheduled": True, "job_id": job_id}


def _run_export_status(job_id: str) -> dict:
    with _EXPORT_LOCK:
        job = _EXPORT_JOBS.get(job_id)
        if not job:
            return {"ok": False, "error": f"no job {job_id!r}"}
        return {"ok": True, "job": dict(job)}


_ROUTES = {
    "/api/delete-agent":     lambda body: _delete_agent(body.get("agent_id", "")),
    "/api/reorder-agents":   lambda body: _reorder_agents(body.get("order", [])),
    "/api/rename-display":   lambda body: _rename_display(body.get("agent_id", ""), body.get("display", "")),
    "/api/edit-summary":     lambda body: _edit_summary(body.get("agent_id", ""), body.get("journey_id", ""), body),
    "/api/edit-thought":     lambda body: _edit_thought(body.get("seg_id", ""), body),
    "/api/run/wake":         _run_wake,
    "/api/run/new-journey":  _run_new_journey,
    "/api/run/rest":         _run_rest,
    "/api/run/stop":         _run_stop,
    "/api/run/save-key":     _run_save_key,
    "/api/export":           lambda body: _run_export(body),
}

_GET_ROUTES = {
    "/api/run/status":       lambda: _run_status(),
    "/api/run/providers":    lambda: _run_providers(),
    "/api/export/status":    lambda qs=None: _run_export_status((qs or {}).get("job_id", "")),
}


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class RangeHandler(SimpleHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        # Dispatch /api/run/* GET routes before falling through to file serving.
        if "?" in self.path:
            api_path, qs_raw = self.path.split("?", 1)
        else:
            api_path, qs_raw = self.path, ""
        if api_path in _GET_ROUTES:
            return self._handle_api_get(api_path, qs_raw)

        path = self.translate_path(self.path)
        if not os.path.isfile(path):
            return super().do_GET()
        rng = self.headers.get("Range")
        if not rng:
            return super().do_GET()
        m = re.match(r"bytes=(\d+)-(\d*)", rng)
        if not m:
            return super().do_GET()
        size = os.path.getsize(path)
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else size - 1
        end = min(end, size - 1)
        length = end - start + 1
        ctype = self.guess_type(path)
        try:
            with open(path, "rb") as f:
                f.seek(start)
                self.send_response(206)
                self.send_header("Content-Type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Content-Length", str(length))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                remaining = length
                while remaining > 0:
                    buf = f.read(min(64 * 1024, remaining))
                    if not buf:
                        break
                    self.wfile.write(buf)
                    remaining -= len(buf)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # ConnectionAbortedError = Windows WinError 10053 (browser tab
            # closed mid-stream during status poll, video range-fetch, etc.)
            pass

    def _handle_api_get(self, path: str, qs_raw: str = "") -> None:
        client_host = self.client_address[0]
        if client_host not in ("127.0.0.1", "::1"):
            self.send_error(403, "API restricted to localhost")
            return
        # Parse query string into a flat dict for handlers that take one.
        qs: dict[str, str] = {}
        if qs_raw:
            from urllib.parse import parse_qs as _parse_qs
            for k, vs in _parse_qs(qs_raw, keep_blank_values=True).items():
                qs[k] = vs[0] if vs else ""
        handler = _GET_ROUTES[path]
        try:
            # Call with qs if the handler accepts it; otherwise call no-arg.
            try:
                result = handler(qs)
            except TypeError:
                result = handler()
            status = 200
        except Exception as e:  # noqa: BLE001
            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            status = 500
        payload = json.dumps(result, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # ConnectionAbortedError = Windows WinError 10053 (browser tab
            # closed mid-stream during status poll, video range-fetch, etc.)
            pass

    def do_POST(self):  # noqa: N802
        # Only accept from localhost (defence in depth — server already binds 127.0.0.1).
        client_host = self.client_address[0]
        if client_host not in ("127.0.0.1", "::1"):
            self.send_error(403, "edit API restricted to localhost")
            return

        path = self.path.split("?", 1)[0]
        handler = _ROUTES.get(path)
        if handler is None:
            self.send_error(404, "no such route")
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            self.send_error(400, f"invalid JSON body: {e}")
            return
        if not isinstance(body, dict):
            self.send_error(400, "body must be a JSON object")
            return

        try:
            result = handler(body)
            status = 200 if result.get("ok") else 400
        except Exception as e:  # noqa: BLE001
            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            status = 500

        payload = json.dumps(result, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # ConnectionAbortedError = Windows WinError 10053 (browser tab
            # closed mid-stream during status poll, video range-fetch, etc.)
            pass

    def end_headers(self):
        self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def log_message(self, fmt, *args):
        return


if __name__ == "__main__":
    os.chdir(ROOT)
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8787
    srv = ThreadingHTTPServer(("127.0.0.1", port), RangeHandler)
    print(f"serving {ROOT} at http://127.0.0.1:{port}/www/")
    print(f"edit API: POST http://127.0.0.1:{port}/api/{{delete-agent|reorder-agents|rename-display|edit-summary|edit-thought}}")
    print(f"run  API: POST http://127.0.0.1:{port}/api/run/{{wake|rest|stop|new-journey|save-key}}")
    print(f"          GET  http://127.0.0.1:{port}/api/run/{{status|providers}}")
    srv.serve_forever()
