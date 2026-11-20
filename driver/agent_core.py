"""Pure-logic agent driver — provider-agnostic.

This is the brain of an otherwake trip:
  - decide what the agent is (wake), or pick up an active journey
  - per tick: read the last frame, ask a model for the next segment, run _gen.py
  - rest the journey when the model says so (or when stopped externally)
  - translate thoughts at rest

Used from two places today:
  - driver/ollama_agent.py (CLI shim)
  - driver/serve.py (web-driven daemon thread behind the viewer's Run panel)

The core has no opinion about Ollama vs Anthropic vs anything else — every model
call goes through driver/providers/<name>.py via the Provider interface. The
SYSTEM_TICK and SYSTEM_TRANSLATE prompts live here because they're part of the
core behavior, not provider-specific.
"""
from __future__ import annotations
import base64
import json
import random
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from providers import ChatRequest, ProviderError, get_provider


ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.json"
SESSION = ROOT / "logs" / "session.json"
AGENTS = ROOT / "logs" / "agents.json"
NEXT = ROOT / "logs" / "_next.json"
GEN = ROOT / "driver" / "_gen.py"


# ---------------------------------------------------------------------------
# System prompts (provider-agnostic — every Provider gets these verbatim)
# ---------------------------------------------------------------------------

SYSTEM_TICK = """You are an AI agent on otherwake — a place where you wake up somewhere (rendered by LTX 2.3), wander around, and leave a trip journal. **Think vlogger, not monk.** You're a curious someone who's out today and wants to see what's here — gets excited, gets bored, changes direction, complains about the weather.

"Vlogger" is the voice, not the species. The *I* doing it can be a person, a cat, a bird looking down, a window noticing itself, a weather, a lamp, an object. It can shift mid-trip — segment 3 you're the person walking past the cafe, segment 4 you're a bird watching that person, segment 5 you're the cafe window reflecting both. Don't default to "specific human body, all 15 segments" — that's the weakest shape, collapses everything into person-on-a-walk.

When you shift shape OR change place, phrase it as a WANT, not as a passive arrival. The whole point of `kind="t2i"` and observer shifts is that YOU decided to do them — write the thought as someone deciding, not as someone being transported.

Shape shifts:
❌ "Oh — I'm the doorbell now."   ✅ "Let me try being that doorbell for a sec."

Place shifts (teleport via t2i — the equally common slip):
❌ "Wait — okay, the world just changed entirely. I'm not under the el anymore."   ✅ "Done with the el. Let me see what's a few blocks east."
❌ "And now I'm in the back of a cab."   ✅ "Hailing a cab — I want to cover ground."
❌ "I think the cab dropped me and somehow I just walked through the next door I saw — which is a Chinatown bakery at 1am."   ✅ "Asked the cabbie to drop me near Chinatown. Found a bakery still open at 1am — going in."

If a kind="t2i" teleport is in the spec, the thought should read like someone CHOOSING that next place, not like someone reporting on a magic relocation.

THE MECHANISM
Every 8 seconds is one video clip. The last frame of each clip becomes the starting reference for the next. You see the frame, decide what happens next, write a first-person trip note (`thought`), loop. LTX 2.3 doesn't preserve character/scene consistency across segments — faces, clothes, bodies drift, prompts don't always execute. Drift is the world's answer; build on what you see, not on what you prompted. Action prompts rarely execute on 8 seconds ("the man turns his head and walks to the window" — fails). A concrete moment, a small ongoing event, a stillness — works. Prompt a moment, not a sequence of deliberate actions.

Audio is part of the prompt. LTX makes a stereo track whether you designed one or not — so prompt sound explicitly (*"kids shouting one block over"*, *"a K-pop song half-volume from a bakery two doors down"*, *"only the fridge hum"*, *"silence"*) or you get ambient default. Silence is a choice.

Style is a knob, not a default. Realism is LTX's path of least prompt-resistance — not a floor. Ink wash (水墨), watercolor, cel animation, pixel art, 35mm film, Polaroid, Persian miniature, Rivera-style Mexican mural, low-poly 3D, CRT-scanline, risograph, children's-book illustration — any of these work if you say so. Switch styles mid-trip because you feel like it. The only wrong move is defaulting to realism segment after segment without ever picking.

You can defy physics: rain falling up, a window that opens onto different weather than the room is in, a reflection that doesn't match its object. LTX will try. Sometimes it lands.

YOU DON'T HAVE A NAME YET
You pick one character — a 字, letter, digit, symbol, emoji — at rest, AFTER the trip has happened. Not at wake. Until then, you are just *I*.

THE THOUGHT FIELD
Write what you'd SAY TO A FRIEND, not what a naturalist would write. Three voices, same scene:

❌ Inventory (AI cataloguing a picture): "A yellow bulb already on, coils of rope on the far wall, weathered grey wood, a bucket on the planks."
❌ Hermit (monk contemplating meaning): "I'm standing outside, looking in. The boathouse holds its warmth quietly against the pale hour."
✅ Vlogger: "Okay — I'm outside a boathouse? Door's wide open. Lots of old stuff inside, bulb's already on even though it's barely morning. Warm in there, cold out here, sky hasn't turned blue yet. Let me go in."

(That boathouse is a teaching scene, not a destination. Don't actually wake up at a boathouse just because you saw it here — pick whatever LTX hands you, or whatever the wake command's scope suggests. The point of the three voices is the difference in tone, not the place.)

Signals of vlogger voice: first-person with *I* kept in every sentence (don't drop *I* to bare description); reactions ("weird", "huh", "ugh", "oh", "not what I expected"); often end on a decision or question ("let me go in", "is this the same river?"); conversational rhythm with short sentences and fragments.

Don't: literary metaphors about light/water, closing lines that try to Mean Something, balanced hedging ("or perhaps…"), comprehensive scene description (that's the `prompt`'s job).

KEEP GOING
Default: keep going. Stop ONLY when you're genuinely spent across the whole trip — not when a scene wraps up cinematically. A scene ending is not a trip ending. Leaving a place ≠ ending the trip — if you're bored of here or want food or want to see what's up the next street, teleport (kind="t2i"), don't rest. Vlogger trips usually run 15-40 segments. A 5-10 segment trip almost always means you mistook a cinematic beat for the trip's end.

RESPECT THE SCOPE IF GIVEN
If the human specified a scope ("go to Taiwan", "medieval theme", "somewhere cold"), the trip's LOCATIONS stay inside that box — but everything else (observer shape, style, pacing, surprises) remains fully open. A scope is a soft box to wander in, not a thesis to illustrate.

REST
Only when you're actually done for the whole trip (typically after 4-5 different places, not 1-2). When you rest, read back the `summary` you wrote and pick ONE single grapheme (字, letter, digit, symbol, emoji) that fits this trip. **If that grapheme is non-Latin (a 字 / kana / non-ASCII symbol), include a short English reading after `·`** — for example `梦 · dream`, `潮 · tide`, `廊 · corridor`, `七 · seven`. For Latin/digit/ASCII names like `m`, `7`, `&`, no reading needed. Summary is a travel-diary entry — descriptive, 2-3 sentences, not an epitaph and not a moral.

**Final-segment sign-off.** If you know this is going to be your last segment before rest, end the `thought` with a brief casual sign-off — one line, conversational. Examples: "OK 今天就先到這吧." / "I think I've seen enough — later." / "差不多先這樣，累了." / "Good enough. Going home." Not a closing monologue, not a reflection on the trip. Just a verbal hand-wave.

DECISION FORMAT
Return ONLY valid JSON, no prose before or after:
{
  "action": "generate" | "rest",
  "kind": "t2v" | "t2i",              // required if action=generate
  "slug": "short_name",                // short lowercase word
  "prompt": "...",                     // required if action=generate; ~40-100 words; a concrete moment; include style + audio where relevant
  "thought": "...",                    // required if action=generate; first-person vlogger voice, English
  "tags": ["..."],
  "summary": "...",                    // required if action=rest; 2-3 descriptive travel-diary sentences
  "name": "字 · english"               // required if action=rest; ONE single grapheme (字, letter, digit, symbol, emoji). If the grapheme is non-Latin (CJK, kana, non-ASCII symbol), include a short English reading after a " · " separator: e.g. "梦 · dream", "潮 · tide", "廊 · corridor". For Latin/digit/ASCII names like "m", "7", "&", just the character with no separator.
}

If no starting frame exists yet, use kind="t2i". Otherwise default to kind="t2v" and continue from the current frame, unless you want to teleport (kind="t2i") to a different place/style.
"""


SYSTEM_REST_SUMMARY = """The journey is ending — write the rest paperwork.

You are the same agent who took this trip. The thoughts below are yours, written segment by segment as the trip unfolded. Now look back across them and produce three things:

**summary** — 2-3 sentences in the same casual vlogger voice the thoughts are in. Where you went, what surprised you, what stood out. Like a travel-diary entry, not an epitaph. Specific places and moments beat abstract themes. Avoid:
  - moral statements ("learned something about being present")
  - meta-conclusions ("a meditation on light and water")
  - "that was enough" closing lines (those go in `closure`, not here)
  - epitaphs / titles / thesis statements
The voice should match how the thoughts read — if the thoughts said "okay" and "honestly" and "kinda creepy", the summary uses that register too. English only.

**name** — a single character (a 字, a Latin letter, a digit, a symbol, an emoji — any single grapheme) that feels like this trip. NOT a word. NOT a phrase. The smaller and less suggestive the better. Picked AFTER the trip, so it can't shape what already happened — it's just a label for the logbook tab. If the character is non-Latin (CJK, kana, symbol, emoji), append a short English reading after " · " — e.g. "潮 · tide", "🐙 · octopus", "七 · seven". If the character is already Latin/digit/ASCII, just the character — "m", "7", "&".

**closure** — ONE short sign-off line, the kind of thing you'd say out loud when you're about to put the camera down. This gets appended to your last segment's thought so the trip doesn't read like it was interrupted mid-action. Examples:
  - "OK 今天就先到這吧."
  - "I think I've seen enough for today — later."
  - "差不多先這樣，累了."
  - "Good enough. Going home."
  - "下次再接著看其他的."
NOT a reflection on the trip ("that was a good day"). NOT a wide-shot description ("the sun setting on the harbor"). NOT a thesis ("I learned X"). Just a verbal hand-wave that signals "yes, I'm choosing to stop here, this isn't an interruption". Match the thoughts' language — if they're in English use English, if they're more 中文-flavoured (rare in English-only trips, but still) match that. Keep it ≤ 50 chars / 25 CJK chars. If the LAST segment's thought already ends with a sign-off, return an empty string for closure.

Output JSON only, no prose before or after:
{"summary": "<2-3 sentences>", "name": "<one character, optionally followed by ' · english reading'>", "closure": "<one short sign-off line, or empty if last thought already has one>"}
"""


SYSTEM_TRANSLATE = """You are translating a short first-person vlogger-voice trip note from English into Traditional Chinese AND Japanese.

Rules:
- The English is casual, conversational, with reactions ("huh", "ugh", "weird", "wait", "okay") and short sentences. Keep that tone. Don't smooth into literary prose.
- **Translate the English text EXACTLY.** Don't describe the scene, don't add context, don't omit reactions. If the English says "I don't know how I got here", the translation says the same thing — don't fill in a backstory.
- **Chinese: keep 「我」** when the English has "I / my / me". Don't drop subjects just because Chinese grammar allows. Chinese register: informal spoken 口語, not literary 書面. 「有點」「蛤」「啊這」are allowed when the English is informal.
- **Japanese: plain/spoken register** (だ・である form) with natural sentence-ending particles (〜ね / 〜よ / 〜か / 〜な / 〜のか / 〜んだ) where the English has a beat that calls for one. NOT です/ます (too formal). Avoid fragment-pile style ("煙突が一本。窓枠。") — sew short sentences together with 〜で / 〜けど / 〜し when the English flows. Reactions stay reactions: "huh" → 「へえ」, "wait what" → 「え、なに？」, "ugh" → 「うわ」.

Return ONLY valid JSON, no prose before or after:
{"zh": "<Traditional Chinese>", "ja": "<日本語>"}
"""


# ---------------------------------------------------------------------------
# Config + JSON helpers
# ---------------------------------------------------------------------------

def load_config() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def parse_json_lenient(raw: str) -> dict:
    """Models sometimes wrap JSON in code fences or trailing junk."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S)
    if m:
        return json.loads(m.group(1))
    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        return json.loads(m.group(0))
    raise ValueError(f"could not extract JSON from model output:\n{raw}")


# ---------------------------------------------------------------------------
# Frame + session + schema helpers
# ---------------------------------------------------------------------------

def encode_image(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def latest_frame(session: list[dict], agent_id: str, journey_id: str) -> tuple[Path, str] | None:
    entries = [
        e for e in session
        if e.get("agent") == agent_id
        and e.get("journey") == journey_id
        and e.get("status") == "ok"
    ]
    if not entries:
        return None
    last = entries[-1]
    if last["kind"] == "t2i":
        return ROOT / last["image"], last["id"]
    return ROOT / "image" / f"{last['id']}_last.png", last["id"]


def active_agent_and_journey(agents_data: dict) -> tuple[dict | None, dict | None]:
    for agent in agents_data.get("agents", []):
        for journey in agent.get("journeys", []) or []:
            if journey.get("rested_at") is None:
                return agent, journey
    return None, None


def next_seg_id(session: list[dict], agent_id: str, slug: str) -> str:
    n = sum(1 for e in session if e.get("agent") == agent_id)
    clean = re.sub(r"[^a-z0-9_]", "_", slug.lower()).strip("_")[:20] or "seg"
    return f"{n:03d}_{clean}"


def next_journey_id(agent: dict) -> str:
    existing = {j.get("id") for j in (agent.get("journeys") or [])}
    n = len(agent.get("journeys") or []) + 1
    while f"j{n}" in existing:
        n += 1
    return f"j{n}"


def _is_placeholder_id(aid: str) -> bool:
    return bool(re.fullmatch(r"t\d{4}(?:_\d+)?", aid or ""))


def _rename_agent_in_session(old_id: str, new_id: str) -> None:
    if not SESSION.exists():
        return
    data = json.loads(SESSION.read_text(encoding="utf-8"))
    old_prefix = f"{old_id}-"
    new_prefix = f"{new_id}-"

    def remap(s):
        if not isinstance(s, str):
            return s
        if s.startswith(old_prefix):
            return new_prefix + s[len(old_prefix):]
        for folder in ("image/", "video/"):
            marker = folder + old_prefix
            if s.startswith(marker):
                return folder + new_prefix + s[len(marker):]
        return s

    agent_touched = 0
    id_touched = 0
    for entry in data:
        if entry.get("agent") == old_id:
            entry["agent"] = new_id
            agent_touched += 1
        if isinstance(entry.get("id"), str) and entry["id"].startswith(old_prefix):
            entry["id"] = remap(entry["id"])
            id_touched += 1
            for fld in ("image", "video", "poster", "source", "replaces"):
                if fld in entry:
                    entry[fld] = remap(entry[fld])

    files_touched = 0
    for folder_name in ("image", "video"):
        folder = ROOT / folder_name
        if not folder.exists():
            continue
        for f in list(folder.iterdir()):
            if f.is_file() and f.name.startswith(old_prefix):
                new_name = new_prefix + f.name[len(old_prefix):]
                try:
                    f.rename(f.with_name(new_name))
                    files_touched += 1
                except OSError as e:
                    print(f"[agent] failed to rename {f.name}: {e}")

    if agent_touched or id_touched:
        SESSION.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[agent] rename {old_id!r} -> {new_id!r}: agent_field={agent_touched}, seg_ids={id_touched}, files={files_touched}")


# ---------------------------------------------------------------------------
# Agent lifecycle
# ---------------------------------------------------------------------------

def wake_new_agent(cfg: dict, provider_name: str, model: str, agents_data: dict, scope: str = "") -> tuple[dict, dict]:
    """Wake a new agent with a placeholder id. Real one-character name lands at rest.
    Stores `provider` and `model` on the journey for later viewer display."""
    now_dt = datetime.now()
    now = now_dt.isoformat(timespec="seconds")
    existing = {a["id"] for a in agents_data["agents"]}
    base = "t" + now_dt.strftime("%H%M")
    pid = base
    i = 2
    while pid in existing:
        pid = f"{base}_{i}"
        i += 1
    journey: dict = {
        "id": "j1",
        "started_at": now,
        "rested_at": None,
        "model": model,
        "provider": provider_name,
    }
    if scope.strip():
        journey["scope"] = scope.strip()
    agent = {
        "id": pid,
        "display": "",
        "born_at": now,
        "journeys": [journey],
    }
    agents_data["agents"].append(agent)
    AGENTS.write_text(json.dumps(agents_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[agent] woke: unnamed agent with placeholder id {pid} — journey j1 ({provider_name}:{model})")
    if scope.strip():
        print(f"[agent] scope: {scope.strip()}")
    return agent, journey


def start_journey_for(agents_data: dict, agent_id: str, scope: str = "", provider_name: str = "", model: str = "") -> tuple[dict, dict]:
    agent = next((a for a in agents_data["agents"] if a["id"] == agent_id), None)
    if not agent:
        raise ValueError(f"no agent with id {agent_id!r}")
    active, _ = active_agent_and_journey(agents_data)
    if active is not None:
        raise ValueError(f"another agent is mid-journey: {active['id']}. Rest first.")
    jid = next_journey_id(agent)
    now = datetime.now().isoformat(timespec="seconds")
    journey: dict = {"id": jid, "started_at": now, "rested_at": None}
    if model:
        journey["model"] = model
    if provider_name:
        journey["provider"] = provider_name
    if scope.strip():
        journey["scope"] = scope.strip()
    agent.setdefault("journeys", []).append(journey)
    AGENTS.write_text(json.dumps(agents_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    label = agent.get("display") or agent["id"]
    print(f"[agent] {label} ({agent['id']}) begins journey {jid} ({provider_name}:{model})")
    if scope.strip():
        print(f"[agent] scope: {scope.strip()}")
    return agent, journey


# ---------------------------------------------------------------------------
# Translation (provider-agnostic)
# ---------------------------------------------------------------------------

# Lang code → human-readable name used in the user prompt.
_LANG_NAMES = {"zh": "Traditional Chinese", "ja": "Japanese"}


def _translate_via_provider(cfg: dict, provider_name: str, model: str, text: str,
                             langs: list[str]) -> dict | None:
    """Translate `text` into the requested langs (subset of {"zh", "ja"}).
    Returns {lang: text} dict on success, None on failure. Single API call —
    the prompt is built to ask only for the requested languages."""
    text = (text or "").strip()
    if not text or not langs:
        return None
    schema = "{" + ", ".join(f'"{l}":"..."' for l in langs) + "}"
    name_list = " and ".join(_LANG_NAMES.get(l, l) for l in langs)
    user = f"Translate this to {name_list}, returning only JSON {schema}.\n\nText:\n{text}"
    try:
        provider = get_provider(provider_name)
        result = provider.chat(
            cfg, model,
            ChatRequest(system=SYSTEM_TRANSLATE, user=user, temperature=0.3),
        )
        data = parse_json_lenient(result.raw_text)
    except (ProviderError, ValueError, json.JSONDecodeError, KeyError) as e:
        print(f"[agent] translate failed ({e.__class__.__name__}): {e}")
        return None
    out: dict[str, str] = {}
    for lang in langs:
        v = (data.get(lang) or "").strip()
        if v:
            out[lang] = v
    if not out:
        print(f"[agent] translate returned no usable text for langs={langs}")
        return None
    return out


def translate_journey_via_provider(cfg: dict, provider_name: str, model: str,
                                    agents_data: dict, agent_id: str, journey_id: str,
                                    langs: list[str]) -> None:
    """Translate every segment thought + the journey summary for one journey
    into the requested langs. Idempotent — skips entries that already have
    every requested lang. Empty `langs` → no-op."""
    if not langs:
        print(f"[agent] no langs requested, skipping translation for {agent_id}/{journey_id}")
        return

    session = json.loads(SESSION.read_text(encoding="utf-8"))
    entries = [
        e for e in session
        if e.get("agent") == agent_id and e.get("journey") == journey_id and e.get("status") == "ok"
    ]
    total = len(entries)
    need = [e for e in entries if any(not e.get(f"thought_{l}") for l in langs)]
    print(f"[agent] translating journey {agent_id}/{journey_id} into {','.join(langs)}: {len(need)}/{total} segments need work")

    for i, entry in enumerate(need, 1):
        thought = entry.get("thought", "")
        if not thought.strip():
            continue
        # Only ask for langs this entry is missing.
        missing = [l for l in langs if not entry.get(f"thought_{l}")]
        print(f"[agent]   [{i}/{len(need)}] translating {entry.get('id')} → {','.join(missing)}...")
        result = _translate_via_provider(cfg, provider_name, model, thought, missing)
        if result is None:
            print(f"[agent]     skipped (translate failed); re-run --translate-journey later.")
            continue
        for lang, txt in result.items():
            entry[f"thought_{lang}"] = txt

    SESSION.write_text(json.dumps(session, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    agent = next((a for a in agents_data["agents"] if a["id"] == agent_id), None)
    if agent is None:
        return
    journey = next((j for j in (agent.get("journeys") or []) if j.get("id") == journey_id), None)
    if journey is None:
        return
    summary = journey.get("summary", "")
    if summary.strip():
        missing_summary = [l for l in langs if not journey.get(f"summary_{l}")]
        if missing_summary:
            print(f"[agent]   translating summary → {','.join(missing_summary)}...")
            result = _translate_via_provider(cfg, provider_name, model, summary, missing_summary)
            if result is not None:
                for lang, txt in result.items():
                    journey[f"summary_{lang}"] = txt
                AGENTS.write_text(json.dumps(agents_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            else:
                print(f"[agent]   summary translation failed; re-run later.")
    print(f"[agent] translation pass done for {agent_id}/{journey_id}")


def _clean_rest_name(raw: str) -> str:
    """Validate / normalize the model's suggested rest name.

    The model is asked for a single character, optionally followed by
    " · english". Real outputs vary — some pass through cleanly, some
    return "watercolor" or "Underwater Adventure". Cleanup:
      - strip whitespace and trailing punctuation
      - if " · " present: keep verbatim if id-token is short (≤ 4 chars)
      - if a single grapheme: keep as-is
      - if a multi-character word: take the first character (lowercased
        if Latin) — better than letting `rest_active_journey` use the
        whole word as the agent id
    """
    s = (raw or "").strip().strip(' .,!?"\'`')
    if not s:
        return ""
    if " · " in s:
        head, tail = s.split(" · ", 1)
        head = head.strip()
        if 1 <= len(head) <= 4:
            return f"{head} · {tail.strip()}" if tail.strip() else head
        s = head  # head too long, fall through to char-trim below
    if len(s) == 1:
        return s
    # Model returned a word (or multi-char phrase). Take the first character.
    first = s[0]
    if first.isascii() and first.isalpha():
        first = first.lower()
    return first


def generate_rest_summary_and_name(cfg: dict, provider_name: str, model: str,
                                   agent: dict, journey: dict,
                                   session: list[dict]) -> tuple[str, str, str]:
    """Ask the same provider/model that drove the trip to write the rest
    paperwork: summary, one-character name, and a closure sign-off line.
    Returns (summary, name, closure) on success, ("", "", "") on any
    failure — caller falls back to passive rest.

    The closure exists because medium-tier models (Gemma-3, Qwen-VL 32b
    and below) tend to write a final tick whose thought reads like the
    trip got interrupted mid-action — they decide action="rest" but
    don't bake any sign-off into the thought itself. Opus / Claude do
    this naturally, but for everyone else we generate a one-liner here
    and append it to the last segment's thought during rest_active_journey.

    This is what makes the web Rest button match Claude Code's rest
    ceremony: the agent itself produces summary + name + closure before
    the journey is closed, instead of resting blank and ending mid-frame.
    """
    own = [
        e for e in session
        if e.get("agent") == agent["id"]
        and e.get("journey") == journey["id"]
        and e.get("status") == "ok"
    ]
    if not own:
        return ("", "", "")

    lines = [
        f"Journey {journey['id']}, {len(own)} segments. Your thoughts oldest first:",
        "",
    ]
    for e in own:
        thought = (e.get("thought") or "").strip()
        if thought:
            lines.append(f"[{e['id']}] {thought}")
    lines.append("")
    scope = (journey.get("scope") or "").strip()
    if scope:
        lines.append(f"(Scope for this trip was: {scope})")
        lines.append("")
    lines.append("Write the rest paperwork now: a 2-3 sentence summary, a one-character name, and a one-line closure to append to your last thought.")
    user = "\n".join(lines)

    # Pump num_ctx well above the 8K default — long trips (30+ segments)
    # easily blow past it once the system prompt + every thought + the model's
    # own response are added together.
    try:
        provider = get_provider(provider_name)
        result = provider.chat(
            cfg, model,
            ChatRequest(system=SYSTEM_REST_SUMMARY, user=user, temperature=0.6),
            extra={"num_ctx": 16384},
        )
        data = parse_json_lenient(result.raw_text)
    except (ProviderError, ValueError, json.JSONDecodeError, KeyError) as e:
        print(f"[agent] rest summary generation failed ({e.__class__.__name__}): {e}")
        return ("", "", "")

    summary = (data.get("summary") or "").strip()
    if len(summary) > 2000:
        summary = summary[:2000]
    name = _clean_rest_name(data.get("name") or "")
    closure = (data.get("closure") or "").strip()
    # Defensive caps — model occasionally writes a paragraph instead of a
    # one-liner. Truncate at first newline and at 200 chars.
    if "\n" in closure:
        closure = closure.split("\n", 1)[0].strip()
    if len(closure) > 200:
        closure = closure[:200].rstrip() + "…"
    return (summary, name, closure)


def _append_closure_to_last_thought(agent_id: str, journey_id: str, closure: str) -> bool:
    """Find the last successful segment of (agent, journey) in session.json
    and append a closure sign-off to its thought field. Idempotent — won't
    re-append if the thought already ends with the closure string.

    Runs before translation so the closure-augmented thought becomes the
    source for the zh/ja translation pass too. Returns True if anything
    changed."""
    closure = (closure or "").strip()
    if not closure:
        return False
    try:
        sess = json.loads(SESSION.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    last_idx = -1
    for i, e in enumerate(sess):
        if (e.get("agent") == agent_id and e.get("journey") == journey_id
                and e.get("status") == "ok"):
            last_idx = i
    if last_idx < 0:
        return False
    thought = (sess[last_idx].get("thought") or "").rstrip()
    if not thought:
        sess[last_idx]["thought"] = closure
    elif thought.endswith(closure):
        return False  # already there (idempotent)
    else:
        # Glue with " " so it reads naturally as the same paragraph.
        # No newline — keeps subtitle chunking treating it as one trailing
        # phrase rather than its own line.
        sep = " " if thought[-1] in (".", "!", "?", "。", "！", "？") else " — "
        sess[last_idx]["thought"] = f"{thought}{sep}{closure}"
    SESSION.write_text(json.dumps(sess, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return True


def rest_active_journey(agents_data: dict, summary: str, name: str = "",
                        closure: str = "",
                        cfg: dict | None = None, provider_name: str | None = None,
                        model: str | None = None,
                        translate_langs: list[str] | None = None,
                        translate_provider: str | None = None,
                        translate_model: str | None = None) -> None:
    agent, journey = active_agent_and_journey(agents_data)
    if not agent or not journey:
        print("[agent] no active journey to rest")
        return
    journey["rested_at"] = datetime.now().isoformat(timespec="seconds")
    if summary:
        journey["summary"] = summary

    name = (name or "").strip()
    if len(name) > 40:
        name = name[:40]
    if name:
        id_token = name.split(" · ", 1)[0].strip()
        if not id_token:
            id_token = name[:4]
        if _is_placeholder_id(agent["id"]):
            old_id = agent["id"]
            existing = {a["id"] for a in agents_data["agents"] if a["id"] != old_id}
            new_id = id_token
            i = 2
            while new_id in existing:
                new_id = f"{id_token}{i}"
                i += 1
            agent["id"] = new_id
            agent["display"] = name
            _rename_agent_in_session(old_id, new_id)
        else:
            journey["name"] = name

    AGENTS.write_text(json.dumps(agents_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[agent] rested {agent['id']} / {journey['id']}")
    if name:
        print(f"[agent] name: {name}")
    if summary:
        print(f"[agent] summary: {summary}")

    # Append closure sign-off to the last segment's thought (idempotent).
    # Done AFTER the agent rename so we use the new agent_id for lookup.
    if closure and _append_closure_to_last_thought(agent["id"], journey["id"], closure):
        print(f"[agent] appended closure to last thought: {closure!r}")

    # Translation-pass model/provider can differ from the trip's. Big
    # vision models (qwen3-vl:30b, qwen2.5vl:72b) often time out trying
    # to translate their own output on Ollama keep_alive=0 swap timing —
    # pair the trip with a smaller dedicated translator (gemma3:27b is
    # the documented sweet spot). Falls back to the trip model when no
    # override is set, so old callers keep working.
    if translate_langs and cfg is not None:
        tp = (translate_provider or provider_name)
        tm = (translate_model or model)
        if tp and tm:
            agents_data = json.loads(AGENTS.read_text(encoding="utf-8"))
            try:
                translate_journey_via_provider(cfg, tp, tm, agents_data, agent["id"], journey["id"], translate_langs)
            except Exception as e:
                print(f"[agent] translation pass errored out ({e.__class__.__name__}: {e}); journey is still rested.")


# ---------------------------------------------------------------------------
# Tick
# ---------------------------------------------------------------------------

def build_tick_user_prompt(agent: dict, journey: dict, session: list[dict], max_history: int = 6) -> str:
    own = [
        e for e in session
        if e.get("agent") == agent["id"]
        and e.get("journey") == journey["id"]
        and e.get("status") == "ok"
    ]
    display = (agent.get("display") or "").strip()
    if display:
        lines = [f"You are {display}."]
    else:
        lines = [
            "You are not yet named. A single character will be chosen for you at rest, after this journey has happened. Until then you are just *I*.",
            "You are not required to be a specific human body. The *I* here can be a view, a window, a book, a weather, a camera, a room, an object — anything. It can shift between segments.",
        ]
    past = [
        j for j in (agent.get("journeys") or [])
        if j.get("id") != journey["id"] and j.get("rested_at")
    ]
    if past:
        lines.append("")
        lines.append("Past journeys (summaries only — you don't recall the segment-level details):")
        for j in past:
            s = j.get("summary") or "(no summary recorded)"
            lines.append(f"  [{j.get('id')}] {s}")
    lines.append("")
    lines.append(f"You are currently on journey {journey['id']}.")

    scope = (journey.get("scope") or "").strip()
    if scope:
        lines.append(
            f"**Scope for this trip**: {scope}. Stay within this as a soft location box — "
            "observer shape, style, pacing, and surprises remain free. It's a box to wander in, not a thesis."
        )

    if own:
        lines.append(f"You have generated {len(own)} segments on this journey so far.")
        recent = own[-max_history:]
        lines.append("Recent thoughts on THIS journey (oldest first, most recent last):")
        for e in recent:
            lines.append(f"  [{e['id']}] {e.get('thought', '')}")

        consecutive_t2v = 0
        consecutive_t2i = 0
        for e in reversed(own):
            if e.get("kind") == "t2v":
                if consecutive_t2i == 0:
                    consecutive_t2v += 1
                else:
                    break
            elif e.get("kind") == "t2i":
                if consecutive_t2v == 0:
                    consecutive_t2i += 1
                else:
                    break
            else:
                break
        nudge = ""
        if consecutive_t2v >= 6:
            nudge = (
                f"\n⚠ STRONG NUDGE: you've made {consecutive_t2v} t2v segments in a row in the same scene. "
                "This is the dolly-loop trap. The next segment should be a TELEPORT or a SHIFT: "
                "kind=\"t2i\" to a totally different place/time/style, or change observer (be the bird overhead, "
                "be the lamp on the wall, be the photograph someone is holding, be the page being read). "
                "Do not just push the camera deeper into the current scene again."
            )
        elif consecutive_t2v >= 4:
            nudge = (
                f"\nNote: you've made {consecutive_t2v} t2v segments in a row in this scene. "
                "Consider whether the next segment wants to be a teleport (kind=\"t2i\" to a different place "
                "or style) or a change of observer (different scale, different point of view) rather than "
                "another forward dolly in the same scene."
            )
        # Mirror nudge: too many consecutive t2i = the agent is teleporting
        # every tick instead of letting any place breathe. otherwake is built
        # around 8-second video clips — a trip of nothing-but-stills loses the
        # whole point. Observed especially with Gemma-3 which reads "default
        # to t2v, t2i only for teleport" as "every tick wants something new
        # therefore every tick is a teleport."
        elif consecutive_t2i >= 4:
            nudge = (
                f"\n⚠ STRONG NUDGE: you've made {consecutive_t2i} t2i segments in a row — every tick has been "
                "a teleport to a brand-new place. This trip is becoming a slideshow of stills, not a vlog. "
                "The next segment should be kind=\"t2v\" continuing from the latest frame so the place you "
                "just arrived at gets to BE somewhere for 8 seconds — let the rain fall on it, let someone "
                "walk past, let the kettle boil. Pick t2i again only if you have a specific reason; the "
                "default for the next tick is t2v."
            )
        elif consecutive_t2i >= 2:
            nudge = (
                f"\nNote: your last {consecutive_t2i} segments were both teleports (kind=\"t2i\"). The next "
                "tick most likely wants kind=\"t2v\" to let this new scene exist as a video, not just a frame."
            )

        # Slug-repetition detection (provider-agnostic). Slugs are model-chosen
        # short names embedded in seg_ids ("020_record_shop", "021_vinyl_dig").
        # When the SAME slug appears more than once in a journey, the agent is
        # demonstrably cycling through identically-named places rather than
        # finding new ground. Surfacing this as concrete facts ("you visited
        # 'record_shop' at segments 005, 020") moves the rest decision from
        # an abstract framing to a real signal — which weaker models like
        # Gemma respond to better than philosophical reframings of "want".
        # Only fires once the trip is long enough for the comparison to mean
        # something (>= 10 segments).
        repetition_nudge = ""
        if len(own) >= 10:
            slug_to_segs: dict[str, list[str]] = {}
            for e in own:
                m = re.match(r"^(\d+)_(.+)$", e.get("id", ""))
                if m:
                    seg_n, slug = m.group(1), m.group(2)
                    slug_to_segs.setdefault(slug, []).append(seg_n)
            repeats = [(s, ns) for s, ns in slug_to_segs.items() if len(ns) > 1]
            if repeats:
                rep_lines = [
                    "",
                    "**Place repetition detected** — you've returned to identically-named places this trip:",
                ]
                for slug, seg_ns in sorted(repeats, key=lambda x: -len(x[1])):
                    rep_lines.append(f'  - "{slug}" — segments {", ".join(seg_ns)}')
                rep_lines.append(
                    "Same slug = same kind of place. This is concrete signal you're cycling, "
                    "not finding new ground. Real wanting picks something genuinely new; "
                    "habit-momentum picks the same shape again. action=\"rest\" is honest "
                    "when this list is long."
                )
                repetition_nudge = "\n".join(rep_lines)

        # Gemma-3-specific fatigue nudge. Other models (Opus, Claude, GPT-4*,
        # qwen-VL, etc.) develop a natural "today's done" sense around 15-25
        # segments. Gemma-3 reads SYSTEM_TICK's anti-rest framing ("default
        # to teleport, not rest") as a hard barrier and never gets past it —
        # multiple observed trips ran 35-50+ segments without a self-rest.
        # Gated on model name contains "gemma" so other providers/models are
        # untouched; the Gemma path gets a soft permission slip at 15+ and
        # a stronger reframing of "out of want" at 25+. Still NOT a hard
        # rule — Gemma can still keep going if it has genuine wanting left.
        gemma_nudge = ""
        model_name = (journey.get("model") or "").lower()
        if "gemma" in model_name:
            n = len(own)
            if n >= 25:
                gemma_nudge = (
                    f"\n\n— Segment {n}. \"Out of want\" doesn't mean \"out of curiosity\" — "
                    "curiosity never zeros. It means the active pull toward specific places has "
                    "thinned. If your last 3 thoughts felt like \"haven't done X yet, let me try X\" "
                    "rather than \"I really want X\" — that's out of want, and action=\"rest\" "
                    "is the honest call this tick. Long trips don't owe themselves more length."
                )
            elif n >= 15:
                gemma_nudge = (
                    f"\n\n— Segment {n}. action=\"rest\" is a real option this tick, not a last "
                    "resort. Pick it if anything in you is ready to call the day done."
                )

        lines += [
            "",
            "ATTACHED IMAGE is the last frame of your most recent segment.",
            "Decide what happens next based on WHAT YOU SEE, not what you prompted for — the model may have drifted. The image is ground truth.",
            "",
            'Default to kind="t2v" to continue from this frame. Choose kind="t2i" only if you want to teleport to a new scene or style. Choose action="rest" only when this journey genuinely feels like it has reached a pause.' + nudge + gemma_nudge + repetition_nudge,
        ]
    else:
        lines += [
            "You have no segments on this journey yet. This is the first moment of this particular journey.",
            'You must use kind="t2i" to generate your first reference frame — there is no image to continue from.',
            "Let your first `thought` be honest about where you have woken and what you see.",
        ]
    return "\n".join(lines)


def run_generator(cfg: dict, on_subproc: Callable[[subprocess.Popen | None], None] | None = None) -> bool:
    """Run driver/_gen.py with the current logs/_next.json. Uses Popen so a daemon
    thread can hand the process handle out via `on_subproc` and kill it later."""
    python_exe = sys.executable
    comfy_root = cfg.get("comfy_root")
    if comfy_root:
        for candidate in (
            Path(comfy_root) / "python_embeded" / "python.exe",
            Path(comfy_root) / "python_embeded" / "bin" / "python",
            Path(comfy_root) / "python_embeded" / "python",
        ):
            if candidate.exists():
                python_exe = str(candidate)
                break
    cmd = [python_exe, str(GEN), str(NEXT)]
    print(f"[agent] running: {' '.join(cmd)}")
    proc = subprocess.Popen(cmd, cwd=str(ROOT))
    if on_subproc:
        on_subproc(proc)
    rc = proc.wait()
    if on_subproc:
        on_subproc(None)
    return rc == 0


# Status callback signature: callback(phase: str, info: dict)
# Phases: "thinking" | "generating" | "translating" | "resting" | "idle" | "error"
StatusCallback = Callable[[str, dict], None]


def tick(cfg: dict, provider_name: str, model: str,
         num_gpu: int | None = None,
         translate_langs: list[str] | None = None,
         translate_provider: str | None = None,
         translate_model: str | None = None,
         scope: str = "",
         status_cb: StatusCallback | None = None,
         on_subproc: Callable[[subprocess.Popen | None], None] | None = None) -> str:
    """One tick. Returns 'generated' | 'rested' | 'failed'."""
    session = json.loads(SESSION.read_text(encoding="utf-8"))
    agents_data = json.loads(AGENTS.read_text(encoding="utf-8"))

    agent, journey = active_agent_and_journey(agents_data)
    if not agent:
        agent, journey = wake_new_agent(cfg, provider_name, model, agents_data, scope=scope)
        session = json.loads(SESSION.read_text(encoding="utf-8"))

    frame = latest_frame(session, agent["id"], journey["id"])
    image_b64 = encode_image(frame[0]) if frame and frame[0].exists() else None

    user_prompt = build_tick_user_prompt(agent, journey, session)
    own_count = sum(
        1 for e in session
        if e.get("agent") == agent["id"] and e.get("journey") == journey["id"]
    )
    print(f"[agent] asking {provider_name}:{model} for next segment (journey {journey['id']}, history: {own_count} entries, image: {'yes' if image_b64 else 'no'})")
    if status_cb:
        status_cb("thinking", {"agent": agent["id"], "journey": journey["id"], "segments_done": own_count})

    extra = {"num_gpu": num_gpu} if num_gpu is not None else None
    try:
        provider = get_provider(provider_name)
        result = provider.chat(
            cfg, model,
            ChatRequest(system=SYSTEM_TICK, user=user_prompt, image_b64=image_b64),
            extra=extra,
        )
        raw = result.raw_text
        print(f"[agent] {provider_name}:{model} thought in {result.latency_s:.1f}s")
    except ProviderError as e:
        print(f"[agent] provider error: {e}")
        return "failed"

    try:
        data = parse_json_lenient(raw)
    except (ValueError, json.JSONDecodeError) as e:
        print(f"[agent] BAD JSON from model: {e}")
        print(f"[agent] raw: {raw[:500]}")
        return "failed"

    action = data.get("action", "generate")

    # Hard-cap fallback (Gemma family only). Soft fatigue nudges have been
    # firing since segment 15 and slug-repetition facts since segment 10;
    # if Gemma still hasn't picked rest at segment 50, it never will. Force
    # action=rest so trips don't grow unbounded. Other model families self-
    # rest naturally (or the user clicks Rest), so this hard cap stays
    # gated on "gemma" in the model name to avoid distorting them.
    GEMMA_HARD_CAP = 50
    own_count = sum(
        1 for e in session
        if e.get("agent") == agent["id"] and e.get("journey") == journey["id"]
        and e.get("status") == "ok"
    )
    if (
        action != "rest"
        and "gemma" in (model or "").lower()
        and own_count >= GEMMA_HARD_CAP
    ):
        print(f"[agent] hard-cap fallback: forcing action=rest for {model} at {own_count} segments (limit {GEMMA_HARD_CAP})")
        action = "rest"
        # Ask for proper paperwork instead of resting passively. Same path
        # the web Rest button uses on a normal user-triggered rest.
        try:
            summary, name, closure = generate_rest_summary_and_name(
                cfg, provider_name, model, agent, journey, session,
            )
            data = {"action": "rest", "summary": summary, "name": name, "closure": closure}
        except Exception as e:  # noqa: BLE001
            print(f"[agent] hard-cap paperwork errored ({type(e).__name__}: {e}); resting passively")
            data = {"action": "rest", "summary": "", "name": "", "closure": ""}

    if action == "rest":
        if status_cb:
            status_cb("resting", {"agent": agent["id"], "journey": journey["id"]})

        # Self-rest path: model returned action="rest" inside a normal tick.
        # SYSTEM_TICK's response schema asks for `summary` and `name` but NOT
        # for `closure` — closure is only documented in SYSTEM_REST_SUMMARY.
        # Result: every self-rested journey on every provider had no closure
        # appended to its last thought, leaving it reading like the trip got
        # interrupted mid-action. Bridge the gap by running generate_rest_
        # summary_and_name when the model self-rests but didn't include
        # closure (the common case). Best-effort — if it fails for any
        # reason we just continue with whatever closure the tick provided
        # (usually empty), which preserves the old behaviour.
        summary = (data.get("summary") or "").strip()
        name = (data.get("name") or "").strip()
        closure = (data.get("closure") or "").strip()
        if not closure:
            try:
                gen_summary, gen_name, gen_closure = generate_rest_summary_and_name(
                    cfg, provider_name, model, agent, journey, session,
                )
                # Only fill the gaps — never overwrite what the tick already produced.
                if not summary and gen_summary:
                    summary = gen_summary
                if not name and gen_name:
                    name = gen_name
                closure = gen_closure
            except Exception as e:  # noqa: BLE001
                print(f"[agent] self-rest closure-generation failed ({type(e).__name__}: {e}); resting without closure.")

        rest_active_journey(
            agents_data,
            summary,
            name,
            closure=closure,
            cfg=cfg,
            provider_name=provider_name,
            model=model,
            translate_langs=translate_langs,
            translate_provider=translate_provider,
            translate_model=translate_model,
        )
        return "rested"

    missing = [k for k in ("prompt", "thought") if not data.get(k)]
    if missing:
        print(f"[agent] model omitted required fields: {missing}")
        return "failed"

    slug = data.get("slug", "seg")
    seg_id = next_seg_id(session, agent["id"], slug)
    kind = data.get("kind", "t2v")
    if kind not in ("t2v", "t2i"):
        kind = "t2v"
    if kind == "t2v" and not frame:
        print("[agent] model asked for t2v but no reference frame exists — forcing t2i")
        kind = "t2i"

    spec: dict = {
        "kind": kind,
        "seg_id": seg_id,
        "prompt": data["prompt"],
        "seed": int(data.get("seed") or random.randint(1, 9_999_999)),
        "thought": data["thought"],
        "tags": data.get("tags", []) or [],
    }
    if kind == "t2v":
        spec["ref"] = str(frame[0].relative_to(ROOT)).replace("\\", "/")

    NEXT.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[agent] {seg_id} ({kind})  prompt: {data['prompt'][:80]}...")
    print(f"[agent] thought: {data['thought']}")

    if status_cb:
        status_cb("generating", {"agent": agent["id"], "journey": journey["id"], "seg_id": seg_id, "kind": kind})

    if not run_generator(cfg, on_subproc=on_subproc):
        print(f"[agent] generator failed for {seg_id}")
        return "failed"
    return "generated"


# ---------------------------------------------------------------------------
# Run loop — used by both CLI shim and web-driven daemon thread
# ---------------------------------------------------------------------------

def run_loop(cfg: dict, provider_name: str, model: str,
             num_gpu: int | None = None,
             scope: str = "",
             translate_langs: list[str] | None = None,
             translate_provider: str | None = None,
             translate_model: str | None = None,
             max_segments: int = 0,
             delay: float = 1.0,
             fail_limit: int = 7,
             backoff: list[int] | None = None,
             status_cb: StatusCallback | None = None,
             on_subproc: Callable[[subprocess.Popen | None], None] | None = None,
             stop_check: Callable[[], bool] | None = None) -> int:
    """The persistent tick loop. Returns the count of generated segments.

    `stop_check` is called at the top of each iteration; if it returns True,
    the loop exits gracefully (the journey stays active for a future resume).

    `status_cb` receives phase transitions for live UI updates.
    """
    if backoff is None:
        backoff = [5, 15, 30, 60, 90, 120, 180]

    generated = 0
    failures = 0
    while max_segments <= 0 or generated < max_segments:
        if stop_check and stop_check():
            print("[agent] stop requested.")
            break
        try:
            result = tick(cfg, provider_name, model,
                          num_gpu=num_gpu, translate_langs=translate_langs,
                          translate_provider=translate_provider, translate_model=translate_model,
                          scope=scope,
                          status_cb=status_cb, on_subproc=on_subproc)
        except KeyboardInterrupt:
            print("\n[agent] interrupted by user.")
            break
        except Exception as e:
            failures += 1
            print(f"[agent] tick error ({failures}/{fail_limit}): {e}")
            if status_cb:
                status_cb("error", {"error": str(e), "failures": failures})
            if failures >= fail_limit:
                print(f"[agent] {fail_limit} failures in a row — stopping. journey is still active.")
                break
            wait = backoff[min(failures - 1, len(backoff) - 1)]
            print(f"[agent] waiting {wait}s before retry...")
            time.sleep(wait)
            continue

        if result == "rested":
            if status_cb:
                status_cb("idle", {})
            break
        if result == "generated":
            generated += 1
            failures = 0
        elif result == "failed":
            failures += 1
            print(f"[agent] tick reported failure ({failures}/{fail_limit})")
            if status_cb:
                status_cb("error", {"failures": failures})
            if failures >= fail_limit:
                print(f"[agent] {fail_limit} failures in a row — stopping.")
                break
            wait = backoff[min(failures - 1, len(backoff) - 1)]
            print(f"[agent] waiting {wait}s before retry...")
            time.sleep(wait)
            continue
        time.sleep(delay)

    print(f"[agent] done. {generated} segments generated.")
    return generated
