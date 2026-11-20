"""Per-tick generator. Reads spec from a JSON file path passed as argv[1], runs the segment.

Spec format (JSON):
  {
    "kind": "t2v" | "t2i",
    "seg_id": "012_through",
    "ref": "image/011_gate_last.png",   // only for t2v
    "prompt": "...",
    "seed": 110420281,
    "thought": "...",
    "tags": ["photoreal", "..."],
    "replaces": null                     // optional, for t2i reset frames
  }

Always invoked with the same shape of command:
  python driver/_gen.py logs/_next.json
so a single permission rule covers every tick.
"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tolive as tl

spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
kind = spec.get("kind", "t2v")

# Default workflow can be overridden in config.json (`driver.t2v_workflow`)
# or per-tick by including `"workflow": "..."` in the spec. The spec wins so
# you can A/B test mid-trip without restarting anything.
_CONFIG = json.loads((Path(__file__).resolve().parent.parent / "config.json").read_text(encoding="utf-8"))
T2V_WORKFLOW = (
    spec.get("workflow")
    or (_CONFIG.get("driver") or {}).get("t2v_workflow")
    or "video_ltx2.3_t2v"
)

if kind == "t2v":
    r = tl.run_t2v_segment(
        seg_id=spec["seg_id"],
        reference_image=Path(spec["ref"]),
        prompt=spec["prompt"],
        seed=spec["seed"],
        thought=spec.get("thought"),
        tags=spec.get("tags", []),
        replaces=spec.get("replaces"),
        workflow=T2V_WORKFLOW,
    )
    print(f"{spec['seg_id']} (t2v) done in {round(r['duration_s'], 1)}s [{T2V_WORKFLOW}]")
elif kind == "t2i":
    r = tl.run_t2i_segment(
        seg_id=spec["seg_id"],
        prompt=spec["prompt"],
        seed=spec["seed"],
        thought=spec.get("thought"),
        tags=spec.get("tags", []),
        replaces=spec.get("replaces"),
    )
    print(f"{spec['seg_id']} (t2i) done in {round(r['duration_s'], 1)}s")
else:
    raise ValueError(f"unknown kind: {kind}")
