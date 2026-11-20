"""ComfyUI API client + segment runner for otherwake's autonomous video pipeline.
(The file keeps its original name — #ToLive was the project's working title.)
Portable — all paths come from config.json next to the project root.
"""
import json, os, shutil, subprocess, time, uuid, datetime
from pathlib import Path
import urllib.request

# ---- config (load once) ----
_CFG_PATH = Path(__file__).resolve().parent.parent / "config.json"
_CFG = json.loads(_CFG_PATH.read_text(encoding="utf-8"))
COMFY = _CFG["comfy_url"].rstrip("/")
COMFY_ROOT = Path(_CFG["comfy_root"])
ROOT = Path(_CFG["project_root"]) if _CFG.get("project_root") else _CFG_PATH.parent
WF = ROOT / "workflows"
IMG_DIR = ROOT / "image"
VID_DIR = ROOT / "video"
LOG_DIR = ROOT / "logs"
STATUS_FILE = LOG_DIR / "status.json"


def _find_comfy_subdir(name: str) -> Path:
    """Locate ComfyUI's input/ or output/ under comfy_root. Supports both:
      - portable:   {comfy_root}/ComfyUI/{name}/
      - standalone: {comfy_root}/{name}/          (pip/manual install pointed at repo root)
      - inside:     {comfy_root}={...}/ComfyUI already — {comfy_root}/{name}/
    """
    candidates = [
        COMFY_ROOT / "ComfyUI" / name,
        COMFY_ROOT / name,
    ]
    for c in candidates:
        if c.is_dir():
            return c
    # fall back to the portable-style path so later errors are legible
    return candidates[0]


def _resolve_ffmpeg(cfg_value: str) -> str:
    """Pick an ffmpeg executable. Order:
      1. cfg_value if it's an absolute path that exists
      2. embedded ffmpeg in ComfyUI portable (Windows / Linux)
      3. cfg_value as-is (assume it's 'ffmpeg' on PATH)
    """
    p = Path(cfg_value) if cfg_value else None
    if p and p.is_absolute() and p.exists():
        return str(p)
    for candidate in (
        COMFY_ROOT / "python_embeded" / "ffmpeg.exe",
        COMFY_ROOT / "python_embeded" / "ffmpeg",
        COMFY_ROOT / "ffmpeg.exe",
        COMFY_ROOT / "ffmpeg",
    ):
        if candidate.exists():
            return str(candidate)
    return cfg_value or "ffmpeg"


COMFY_INPUT = _find_comfy_subdir("input")
COMFY_OUTPUT = _find_comfy_subdir("output")
FFMPEG = _resolve_ffmpeg(_CFG.get("ffmpeg_path", ""))

CLIENT_ID = str(uuid.uuid4())


# ---- live status (viewer polls this) ----
def set_status(state: str, **kv) -> None:
    """state in: idle | t2i | t2v | i2v | extracting | error"""
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {"state": state, "updated_at": datetime.datetime.now().isoformat(timespec="seconds"), **kv}
    STATUS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---- raw ComfyUI ----
def _req(path, data=None, method=None):
    url = COMFY + path
    if data is not None and isinstance(data, dict):
        data = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method=method or "POST")
    else:
        req = urllib.request.Request(url, method=method or "GET")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def upload_image(local_path: Path, name: str | None = None) -> str:
    name = name or local_path.name
    dst = COMFY_INPUT / name
    shutil.copy(local_path, dst)
    return name


def queue(workflow: dict) -> str:
    r = _req("/prompt", {"prompt": workflow, "client_id": CLIENT_ID})
    return r["prompt_id"]


def wait(prompt_id: str, timeout=1800) -> dict:
    start = time.time()
    while True:
        h = _req(f"/history/{prompt_id}")
        if prompt_id in h:
            return h[prompt_id]
        if time.time() - start > timeout:
            raise TimeoutError(f"prompt {prompt_id} timed out")
        time.sleep(2)


def collect_outputs(history: dict) -> list[dict]:
    out = []
    for nid, nout in history.get("outputs", {}).items():
        for key in ("images", "gifs", "videos"):
            for f in nout.get(key, []) or []:
                out.append({"node": nid, "kind": key, **f})
    return out


def download_output(entry: dict, dest: Path) -> Path:
    src = COMFY_OUTPUT / (entry.get("subfolder") or "") / entry["filename"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, dest)
    return dest


def load_workflow(name: str) -> dict:
    if not name.endswith(".json"):
        name += ".json"
    return json.loads((WF / name).read_text(encoding="utf-8"))


# ---- workflows ----
def t2i(prompt: str, seed: int, out_path: Path) -> Path:
    wf = load_workflow("image_z_image_turbo_t2i")
    wf["45"]["inputs"]["text"] = prompt
    wf["44"]["inputs"]["seed"] = seed
    wf["9"]["inputs"]["filename_prefix"] = f"tolive/{out_path.stem}"
    pid = queue(wf)
    hist = wait(pid)
    outs = [o for o in collect_outputs(hist) if o["kind"] == "images" and o["filename"].lower().endswith((".png", ".jpg", ".jpeg"))]
    if not outs:
        raise RuntimeError(f"no image output: {hist}")
    return download_output(outs[0], out_path)


# Per-workflow node patch table. The video workflows in workflows/ all use
# different node IDs and different input field names for the same logical
# slot (prompt, image, filename, seed). Rather than auto-detecting by
# class_type — which fails when a workflow routes prompts through primitive
# string nodes — we keep an explicit map.
#
# To wire up a NEW video workflow:
#   1. Open the .json, find the nodes you'd edit by hand in ComfyUI
#   2. Note the numeric id (or "id:slot" form for primitive routers) and
#      the field name (e.g. "value" vs "text")
#   3. Add an entry below. "seed" is optional — omit if you want the
#      workflow's hardcoded seed to win
_VIDEO_WF_NODES: dict[str, dict[str, tuple[str, str]]] = {
    "video_ltx2.3_t2v": {
        "prompt":   ("267:266", "value"),
        "image":    ("269",     "image"),
        "filename": ("75",      "filename_prefix"),
    },
    "video_ltx2_3_i2v": {
        "prompt":   ("267:266", "value"),
        "image":    ("269",     "image"),
        "filename": ("75",      "filename_prefix"),
    },
    "video_ltx2_3_flf2v": {
        "prompt":   ("267:266", "value"),
        "image":    ("269",     "image"),
        "filename": ("75",      "filename_prefix"),
    },
}


def _run_video_wf(wf_name: str, prompt: str, image_name: str, seed: int, out_path: Path) -> Path:
    wf = load_workflow(wf_name)
    nodes = _VIDEO_WF_NODES.get(wf_name)
    if not nodes:
        raise ValueError(
            f"unknown video workflow {wf_name!r}; "
            f"add a _VIDEO_WF_NODES entry in driver/tolive.py "
            f"(known: {sorted(_VIDEO_WF_NODES)})"
        )
    p_node, p_field = nodes["prompt"]
    i_node, i_field = nodes["image"]
    f_node, f_field = nodes["filename"]
    wf[p_node]["inputs"][p_field] = prompt
    wf[i_node]["inputs"][i_field] = image_name
    wf[f_node]["inputs"][f_field] = f"tolive/{out_path.stem}"
    if "seed" in nodes:
        s_node, s_field = nodes["seed"]
        wf[s_node]["inputs"][s_field] = seed
    pid = queue(wf)
    hist = wait(pid, timeout=1800)
    outs = collect_outputs(hist)
    vids = [o for o in outs if o["filename"].lower().endswith((".mp4", ".webm", ".mov", ".mkv", ".webp", ".gif"))]
    if not vids:
        raise RuntimeError(f"no video output: {outs}")
    return download_output(vids[0], out_path)


def extract_last_frame(video: Path, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [FFMPEG, "-y", "-sseof", "-0.2", "-i", str(video), "-vframes", "1", "-q:v", "2", str(out)],
        check=True, capture_output=True,
    )
    return out


# ---- high level helpers ----
def run_t2i_segment(seg_id: str, prompt: str, seed: int = 42, thought: str | None = None,
                    source: Path | None = None, replaces: str | None = None, tags: list[str] | None = None) -> dict:
    import log as logmod
    set_status("t2i", seg_id=seg_id)
    t0 = time.time()
    img = t2i(prompt, seed=seed, out_path=IMG_DIR / f"{seg_id}.png")
    dt = time.time() - t0
    logmod.append(
        id=seg_id, kind="t2i", prompt=prompt, image=img, duration_s=dt, seed=seed,
        workflow="image_z_image_turbo_t2i", thought=thought, source=source,
        status="ok", replaces=replaces, tags=tags or [],
    )
    set_status("idle")
    return {"image": img, "duration_s": dt}


def run_t2v_segment(seg_id: str, reference_image: Path, prompt: str, seed: int = 42,
                    thought: str | None = None, workflow: str = "video_ltx2.3_t2v",
                    replaces: str | None = None, tags: list[str] | None = None) -> dict:
    import log as logmod
    set_status("t2v", seg_id=seg_id, ref=str(reference_image.name))
    t0 = time.time()
    uploaded = upload_image(reference_image, f"tolive_{seg_id}_ref.png")
    video = _run_video_wf(workflow, prompt, uploaded, seed, VID_DIR / f"{seg_id}.mp4")
    dt = time.time() - t0
    set_status("extracting", seg_id=seg_id)
    last = extract_last_frame(video, IMG_DIR / f"{seg_id}_last.png")
    logmod.append(
        id=seg_id, kind="t2v", prompt=prompt, video=video, poster=reference_image,
        source=reference_image, duration_s=dt, seed=seed, workflow=workflow,
        thought=thought, status="ok", replaces=replaces, tags=tags or [],
    )
    set_status("idle")
    return {"video": video, "last_frame": last, "duration_s": dt, "input": reference_image}


def run_i2v_segment(seg_id: str, input_image: Path, prompt: str, seed: int = 42,
                    thought: str | None = None, replaces: str | None = None, tags: list[str] | None = None) -> dict:
    import log as logmod
    set_status("i2v", seg_id=seg_id)
    t0 = time.time()
    uploaded = upload_image(input_image, f"tolive_{seg_id}_in.png")
    video = _run_video_wf("video_ltx2_3_i2v", prompt, uploaded, seed, VID_DIR / f"{seg_id}.mp4")
    dt = time.time() - t0
    set_status("extracting", seg_id=seg_id)
    last = extract_last_frame(video, IMG_DIR / f"{seg_id}_last.png")
    logmod.append(
        id=seg_id, kind="i2v", prompt=prompt, video=video, poster=input_image,
        source=input_image, duration_s=dt, seed=seed, workflow="video_ltx2_3_i2v",
        thought=thought, status="ok", replaces=replaces, tags=tags or [],
    )
    set_status("idle")
    return {"video": video, "last_frame": last, "duration_s": dt, "input": input_image}


def mark_failed(seg_id: str, failure_reason: str) -> None:
    """Post-hoc mark a segment as failed/abandoned (after you inspect the output)."""
    import log as logmod
    data = logmod._read()
    for e in data:
        if e.get("id") == seg_id:
            e["status"] = "failed"
            e["failure_reason"] = failure_reason
    logmod.LOG.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    print("ComfyUI:", _req("/system_stats")["system"]["comfyui_version"])
    print("project_root:", ROOT)
