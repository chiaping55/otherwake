"""Export an agent's journey as a single shareable .mp4.

Produces, in `exports/<agent>[-<journey>]/`:
  - final.mp4            concatenated video of every successful segment
                         (t2i stills are held for a few seconds)
  - final.en.srt         subtitles with the English `thought` of each segment
  - final.zh.srt         Traditional Chinese subtitles (thought_zh)
  - final.ja.srt         Japanese subtitles (thought_ja)
  - description.txt      origin, chapter timestamps (YouTube-friendly), prompts

Two presets:

  --preset youtube  (default)
    Stays close to the original yt_export.py: external .srt files, full
    thoughts, no intro card, no burned subs. Upload final.mp4 to YouTube,
    paste description.txt in the description box, and upload the three
    .srt files via Subtitles. Chapters in description become clickable.

  --preset social
    For Twitter / IG / TikTok / LINE — generates a self-contained mp4 with
    an intro card (agent name + scope + journey summary) and burned-in
    subtitles in the chosen --burn language. Long thoughts are chunked
    into ~90-char subtitle blocks distributed across each segment's
    duration so the screen never fills with text.

Usage:
    python driver/export.py --agent 潮
    python driver/export.py --agent 潮 --journey j1
    python driver/export.py --agent 潮 --preset social --burn en
    python driver/export.py --agent 潮 --preset social --burn zh --intro
"""
from __future__ import annotations
import argparse
import json
import re
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SESSION = ROOT / "logs" / "session.json"
AGENTS = ROOT / "logs" / "agents.json"

sys.path.insert(0, str(ROOT / "driver"))
try:
    from tolive import FFMPEG  # type: ignore
except Exception:
    FFMPEG = "ffmpeg"


def _derive_ffprobe(ffmpeg_path: str) -> str:
    p = Path(ffmpeg_path)
    if p.is_absolute() and p.exists():
        probe = p.parent / p.name.replace("ffmpeg", "ffprobe", 1)
        if probe.exists():
            return str(probe)
    return "ffprobe"


FFPROBE = _derive_ffprobe(FFMPEG)

# Burned-subtitle defaults. Tuned for 1280x720 reference but ffmpeg's
# subtitles filter scales with resolution. Outline + shadow give legibility
# against any background. CJK fonts are picked per language.
BURN_FONT_BY_LANG = {
    "en": "DejaVu Sans, Arial, Helvetica",
    "zh": "Microsoft JhengHei, Microsoft YaHei, Noto Sans CJK TC, sans-serif",
    "ja": "Yu Gothic, Meiryo, Noto Sans CJK JP, sans-serif",
}
BURN_MAX_CHARS_PER_CHUNK = 90  # roughly two readable lines wrapped at 42 cols
BURN_MIN_CHUNK_DURATION = 1.2  # never display a chunk for less than this many seconds
INTRO_DURATION = 3.5
INTRO_FONT_BY_LANG = BURN_FONT_BY_LANG


def run(cmd: list[str]) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(cmd)}\nstderr:\n{r.stderr}")
    return r.stdout


def ffprobe_duration(path: Path) -> float:
    out = run([
        FFPROBE,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ])
    return float(out.strip())


def ffprobe_size(path: Path) -> tuple[int, int]:
    out = run([
        FFPROBE,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=p=0",
        str(path),
    ])
    w, h = [int(x) for x in out.strip().split(",")]
    return w, h


def fmt_ts(seconds: float) -> str:
    """SRT timestamp: HH:MM:SS,mmm"""
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def fmt_chapter_ts(seconds: float) -> str:
    """YouTube chapter timestamp: M:SS or H:MM:SS"""
    s = int(round(seconds))
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def still_to_clip(image: Path, out: Path, duration: float, width: int, height: int) -> None:
    run([
        FFMPEG, "-y", "-loglevel", "error",
        "-loop", "1",
        "-t", str(duration),
        "-i", str(image),
        "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,format=yuv420p",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-r", "24",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(out),
    ])


def reencode_video(src: Path, out: Path, width: int, height: int) -> None:
    run([
        FFMPEG, "-y", "-loglevel", "error",
        "-i", str(src),
        "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,format=yuv420p",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-r", "24",
        "-pix_fmt", "yuv420p",
        "-an",
        "-movflags", "+faststart",
        str(out),
    ])


def _find_intro_font(text: str, size: int):
    """Pick a font file that can render the given text. Prefers CJK-capable
    fonts when the text contains CJK chars, falls back to plain Latin.
    Returns a Pillow ImageFont or raises ImportError if Pillow is missing."""
    from PIL import ImageFont  # type: ignore

    # Detect whether we need CJK coverage.
    needs_cjk = any(
        0x3000 <= ord(c) <= 0x9FFF or 0xAC00 <= ord(c) <= 0xD7AF
        or 0xFF00 <= ord(c) <= 0xFFEF
        for c in text
    )
    candidates: list[str] = []
    if needs_cjk:
        candidates += [
            r"C:\Windows\Fonts\msjh.ttc",                               # JhengHei (TC)
            r"C:\Windows\Fonts\msyh.ttc",                               # YaHei (SC)
            r"C:\Windows\Fonts\YuGothM.ttc",                            # Yu Gothic Medium (JP)
            r"C:\Windows\Fonts\meiryo.ttc",                             # Meiryo (JP)
            "/System/Library/Fonts/PingFang.ttc",                       # macOS CJK
            "/System/Library/Fonts/Hiragino Sans GB.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",   # Linux Noto
        ]
    candidates += [
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size)
            except (OSError, IOError):
                continue
    return ImageFont.load_default()


def make_intro_clip(out: Path, agent_id: str, display: str, scope: str,
                    summary: str, lang: str, width: int, height: int,
                    duration: float = INTRO_DURATION) -> None:
    """Render a black-background title card with agent name + scope, then
    convert the still to a duration-second clip.

    Uses Pillow for the text render — much more reliable than ffmpeg's
    drawtext filter on Windows (which requires fontconfig and complains
    about CJK font resolution). If Pillow is unavailable we fall back to
    a plain black intro (no text — better than crashing the whole export).
    """
    name_text = (display or agent_id).strip()
    line2 = f"scope: {scope.strip()}" if scope.strip() else ""

    # Render PNG with Pillow.
    png_path = out.with_suffix(".png")
    try:
        from PIL import Image, ImageDraw  # type: ignore
        name_size = max(40, height // 14)
        sub_size = max(22, height // 30)
        img = Image.new("RGB", (width, height), color="black")
        draw = ImageDraw.Draw(img)
        name_font = _find_intro_font(name_text, name_size)
        nbox = draw.textbbox((0, 0), name_text, font=name_font)
        nw, nh = nbox[2] - nbox[0], nbox[3] - nbox[1]
        ny = (height - nh) // 2 - sub_size
        draw.text(((width - nw) // 2 - nbox[0], ny - nbox[1]),
                  name_text, fill="white", font=name_font)
        if line2:
            sub_font = _find_intro_font(line2, sub_size)
            sbox = draw.textbbox((0, 0), line2, font=sub_font)
            sw, sh = sbox[2] - sbox[0], sbox[3] - sbox[1]
            sy = ny + nh + max(20, sub_size // 2)
            draw.text(((width - sw) // 2 - sbox[0], sy - sbox[1]),
                      line2, fill=(180, 180, 180), font=sub_font)
        img.save(png_path)
    except ImportError:
        # No Pillow — make a plain black PNG with ffmpeg as fallback. The
        # text won't appear; user gets a blank intro frame instead of a
        # crash. Better than nothing for the rare environments that don't
        # ship Pillow.
        print("[export] Pillow not available — intro card will be blank.")
        run([
            FFMPEG, "-y", "-loglevel", "error",
            "-f", "lavfi",
            "-i", f"color=c=black:s={width}x{height}:d=0.1",
            "-frames:v", "1",
            str(png_path),
        ])

    # Convert the PNG to a duration-second clip via the existing helper.
    still_to_clip(png_path, out, duration, width, height)
    png_path.unlink(missing_ok=True)


def concat_mp4s(clips: list[Path], out: Path) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as lf:
        for c in clips:
            lf.write(f"file '{c.as_posix()}'\n")
        list_path = lf.name
    try:
        run([
            FFMPEG, "-y", "-loglevel", "error",
            "-f", "concat", "-safe", "0",
            "-i", list_path,
            "-c", "copy",
            "-movflags", "+faststart",
            str(out),
        ])
    finally:
        Path(list_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Subtitle building — both external (full thought per segment) and burned
# (chunked across segment duration so screen doesn't fill with text)
# ---------------------------------------------------------------------------

def write_chunked_srt(segments: list[tuple[float, float, str]], out: Path) -> None:
    """Chunked subtitles — used for both burned-in and external SRTs.

    YouTube's player doesn't auto-shrink long cues or hold for them — a
    90-word thought rendered as a single cue just fills the screen and
    sits there until the segment ends. Same chunking we apply for burn
    is what works for YouTube too: split each thought into ~90-char
    blocks at sentence/clause boundaries, time-distributed across the
    segment's duration with a 1.2s minimum dwell."""
    lines = []
    counter = 0
    for (start, end, text) in segments:
        if not text:
            continue
        seg_dur = max(0.1, end - start)
        chunks = chunk_thought(text)
        if not chunks:
            continue
        per = seg_dur / len(chunks)
        if per < BURN_MIN_CHUNK_DURATION:
            n = max(1, int(seg_dur / BURN_MIN_CHUNK_DURATION))
            chunks = chunks[:n]
            per = seg_dur / len(chunks)
        for i, chunk in enumerate(chunks):
            counter += 1
            t0 = start + i * per
            t1 = t0 + per
            wrapped = "\n".join(textwrap.wrap(chunk, width=42)) or chunk
            lines.append(f"{counter}\n{fmt_ts(t0)} --> {fmt_ts(t1)}\n{wrapped}\n")
    out.write_text("\n".join(lines), encoding="utf-8")


def chunk_thought(text: str, max_chars: int = BURN_MAX_CHARS_PER_CHUNK) -> list[str]:
    """Split a long thought into subtitle-friendly chunks at sentence /
    clause boundaries. Returns a list of strings, each ≤ max_chars where
    possible (a single un-breakable run will exceed). Boundaries (in
    decreasing priority): . ! ? 。 ！ ？  | , ; ， ；  | space."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    # Split into sentences first; if a sentence is too long, split on commas.
    primary = re.split(r"(?<=[\.\!\?。！？])\s*", text)
    primary = [p.strip() for p in primary if p.strip()]

    chunks: list[str] = []
    buf = ""
    for sent in primary:
        # If sentence alone fits in a chunk and adding to buf still fits, append.
        candidate = (buf + " " + sent).strip() if buf else sent
        if len(candidate) <= max_chars:
            buf = candidate
            continue
        # Otherwise flush buf and start fresh.
        if buf:
            chunks.append(buf)
            buf = ""
        if len(sent) <= max_chars:
            buf = sent
            continue
        # Sentence is too long on its own — split on , ; ， ； 、
        sub = re.split(r"(?<=[\,;，；、])\s*", sent)
        sub_buf = ""
        for piece in sub:
            piece = piece.strip()
            if not piece:
                continue
            cand = (sub_buf + " " + piece).strip() if sub_buf else piece
            if len(cand) <= max_chars:
                sub_buf = cand
            else:
                if sub_buf:
                    chunks.append(sub_buf)
                if len(piece) <= max_chars:
                    sub_buf = piece
                else:
                    # Last resort: hard-wrap on whitespace.
                    chunks.extend(textwrap.wrap(piece, width=max_chars) or [piece])
                    sub_buf = ""
        if sub_buf:
            buf = sub_buf
    if buf:
        chunks.append(buf)
    return chunks


# Burn SRT and external SRT now use identical chunking — both feed into
# players (YouTube / libass) that just render whatever cues are in the file
# at their stated timestamps.
write_burn_srt = write_chunked_srt


def burn_subtitles(input_mp4: Path, srt: Path, output_mp4: Path,
                   font: str, height: int) -> None:
    """Single-pass burn: take concatenated mp4, overlay subtitles. Uses
    libass via ffmpeg's subtitles filter — needs libavfilter built with
    --enable-libass (true on every official ffmpeg distribution)."""
    font_size = max(20, height // 28)
    margin = max(30, height // 18)
    # The subtitles filter takes path with forward slashes on all platforms.
    srt_arg = str(srt).replace("\\", "/").replace(":", "\\:")
    style = (
        f"FontName={font.split(',')[0].strip()},"
        f"FontSize={font_size},"
        f"PrimaryColour=&H00FFFFFF,"
        f"OutlineColour=&H80000000,"
        f"BorderStyle=1,"
        f"Outline=2,"
        f"Shadow=1,"
        f"Alignment=2,"   # bottom-center
        f"MarginV={margin}"
    )
    run([
        FFMPEG, "-y", "-loglevel", "error",
        "-i", str(input_mp4),
        "-vf", f"subtitles='{srt_arg}':force_style='{style}'",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-r", "24",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(output_mp4),
    ])


# ---------------------------------------------------------------------------
# Description (YouTube-friendly chapter list)
# ---------------------------------------------------------------------------

def write_description(agent: dict, journey: dict | None, entries: list[dict],
                      timeline: list[tuple[float, float]], out: Path,
                      include_prompts: bool = False,
                      intro_offset: float = 0.0) -> None:
    lines: list[str] = []
    display = agent.get("display") or agent.get("id", "")
    lines.append(display)
    lines.append("")
    summary = (journey or {}).get("summary") or agent.get("origin") or ""
    if summary:
        lines.append(summary)
        lines.append("")
    if (journey or {}).get("scope"):
        lines.append(f"scope: {journey['scope']}")
        lines.append("")
    lines.append("─────────────")
    lines.append("CHAPTERS")
    lines.append("─────────────")
    if intro_offset > 0:
        lines.append(f"{fmt_chapter_ts(0.0)} (intro)")
    for (start, _), e in zip(timeline, entries):
        lines.append(f"{fmt_chapter_ts(start)} {e['id']}")
    lines.append("")
    if include_prompts:
        lines.append("─────────────")
        lines.append("PROMPTS")
        lines.append("─────────────")
        for e in entries:
            lines.append(f"[{e['id']}] ({e['kind']})")
            lines.append(e.get("prompt", ""))
            lines.append("")
    lines.append("─────────────")
    if include_prompts:
        lines.append("Made with otherwake — https://github.com/chiaping55/otherwake")
    else:
        lines.append("Made with otherwake. Tool + full prompts for every segment:")
        lines.append("https://github.com/chiaping55/otherwake")
    body = "\n".join(lines)
    out.write_text(body, encoding="utf-8")
    if len(body) > 5000:
        print(f"[export] WARNING: description.txt is {len(body)} chars — YouTube's limit is 5000.")
        print(f"[export]   Re-run without --include-prompts, or shorten manually.")
    else:
        print(f"[export] description: {len(body)} / 5000 chars")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Export an agent's journey as a single mp4.")
    parser.add_argument("--agent", required=True, help="agent id in logs/agents.json")
    parser.add_argument("--journey", help="single journey id (e.g. j1). Default: all journeys for this agent.")
    parser.add_argument("--out", help="output folder (default: exports/{agent}[-{journey}])")
    parser.add_argument("--still-hold", type=float, default=8.0,
                        help="seconds to hold each t2i still image (default 8 — matches a t2v clip's "
                             "8s length so the playback rhythm stays even, and gives subtitles time "
                             "to be read)")
    parser.add_argument("--width", type=int, default=0,
                        help="output width (default: auto-detect from first t2v)")
    parser.add_argument("--height", type=int, default=0,
                        help="output height (default: auto-detect from first t2v)")
    parser.add_argument("--preset", choices=("youtube", "social"), default="youtube",
                        help="youtube = external .srt files (default); social = burned subs + intro card")
    parser.add_argument("--burn", choices=("en", "zh", "ja"),
                        help="burn subtitles in this language. Defaults: 'en' for --preset social, off for youtube.")
    parser.add_argument("--intro", action="store_true",
                        help="prepend a title card with agent name and scope. Default off; auto-on under --preset social.")
    parser.add_argument("--no-srt", action="store_true",
                        help="skip writing external .srt files (smaller output dir; default on under --preset social)")
    parser.add_argument("--include-prompts", action="store_true",
                        help="include every segment's prompt in description.txt")
    args = parser.parse_args()

    # Preset defaults
    if args.preset == "social":
        if args.burn is None:
            args.burn = "en"
        args.intro = True if not args.intro else args.intro
        args.no_srt = True if not args.no_srt else args.no_srt

    session = json.loads(SESSION.read_text(encoding="utf-8"))
    agents = json.loads(AGENTS.read_text(encoding="utf-8"))
    agent = next((a for a in agents["agents"] if a["id"] == args.agent), None)
    if not agent:
        print(f"agent '{args.agent}' not found in agents.json", file=sys.stderr)
        return 1

    # Filter entries: by agent, optionally by journey, status=ok, drop replaced.
    entries = [e for e in session if e.get("agent") == args.agent and e.get("status") == "ok"]
    if args.journey:
        entries = [e for e in entries if e.get("journey") == args.journey]
    replaced = {e["replaces"] for e in entries if e.get("replaces")}
    entries = [e for e in entries if e["id"] not in replaced]
    if not entries:
        which = f"{args.agent}/{args.journey}" if args.journey else args.agent
        print(f"no successful segments for {which}", file=sys.stderr)
        return 1

    # Pick the journey object for description. If --journey given, use that;
    # otherwise pick the most recent rested journey (or the only one).
    journey: dict | None = None
    js = agent.get("journeys") or []
    if args.journey:
        journey = next((j for j in js if j.get("id") == args.journey), None)
    elif len(js) == 1:
        journey = js[0]
    elif js:
        rested = [j for j in js if j.get("rested_at")]
        journey = rested[-1] if rested else js[-1]

    out_dir = Path(args.out) if args.out else (
        ROOT / "exports" / (
            f"{args.agent}-{args.journey}" if args.journey else args.agent
        )
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    # Detect reference resolution
    ref_video = next((ROOT / e["video"] for e in entries if e.get("video")), None)
    if args.width and args.height:
        width, height = args.width, args.height
    elif ref_video and ref_video.exists():
        width, height = ffprobe_size(ref_video)
        print(f"[export] reference resolution: {width}x{height} (from {ref_video.name})")
    else:
        width, height = 1280, 720
        print(f"[export] no t2v reference; defaulting to {width}x{height}")

    # Build clip list
    work_dir = out_dir / "_work"
    work_dir.mkdir(exist_ok=True)
    clips: list[Path] = []
    durations: list[float] = []

    intro_dur = 0.0
    if args.intro:
        intro_clip = work_dir / "_intro.mp4"
        print(f"[export] rendering intro card...")
        make_intro_clip(
            intro_clip,
            agent_id=args.agent,
            display=agent.get("display") or args.agent,
            scope=(journey or {}).get("scope") or "",
            summary=(journey or {}).get("summary") or "",
            lang=args.burn or "en",
            width=width, height=height,
        )
        clips.append(intro_clip)
        intro_dur = ffprobe_duration(intro_clip)
        durations.append(intro_dur)

    for e in entries:
        out_clip = work_dir / f"{e['id']}.mp4"
        if e["kind"] == "t2v" and e.get("video"):
            src = ROOT / e["video"]
            print(f"[export] re-encoding {e['id']}...")
            reencode_video(src, out_clip, width, height)
        elif e["kind"] == "t2i" and e.get("image"):
            src = ROOT / e["image"]
            print(f"[export] rendering still {e['id']}...")
            still_to_clip(src, out_clip, args.still_hold, width, height)
        else:
            print(f"[export] skipping {e['id']} (no media)")
            continue
        clips.append(out_clip)
        durations.append(ffprobe_duration(out_clip))

    # Timeline for entries (after intro offset)
    timeline: list[tuple[float, float]] = []
    t = intro_dur
    for d in durations[1:] if args.intro else durations:
        timeline.append((t, t + d))
        t += d
    total = t
    print(f"[export] total length: {fmt_chapter_ts(total)} ({total:.1f}s)")

    # Concat
    final_mp4 = out_dir / "final.mp4"
    print(f"[export] concatenating into {final_mp4.name}...")
    concat_mp4s(clips, final_mp4)

    # External SRTs (skip if --no-srt)
    if not args.no_srt:
        for lang, key in (("en", "thought"), ("zh", "thought_zh"), ("ja", "thought_ja")):
            srt = out_dir / f"final.{lang}.srt"
            segs = [(s, e, entry.get(key, "") or "") for (s, e), entry in zip(timeline, entries)]
            write_chunked_srt(segs, srt)
            print(f"[export] wrote {srt.name}")

    # Burned subs (chunk thoughts, single ffmpeg pass over final.mp4)
    if args.burn:
        key = {"en": "thought", "zh": "thought_zh", "ja": "thought_ja"}[args.burn]
        burn_srt = work_dir / f"_burn.{args.burn}.srt"
        segs = [(s, e, entry.get(key, "") or "") for (s, e), entry in zip(timeline, entries)]
        # If we have an intro clip, pad the timeline shift is already reflected
        # in `timeline` (segments start at intro_dur). The intro card itself
        # does NOT get burned subtitles — it has its own drawtext overlays.
        write_burn_srt(segs, burn_srt)
        burned_mp4 = out_dir / f"final.burn-{args.burn}.mp4"
        font = BURN_FONT_BY_LANG.get(args.burn, BURN_FONT_BY_LANG["en"])
        print(f"[export] burning {args.burn} subtitles into {burned_mp4.name}...")
        burn_subtitles(final_mp4, burn_srt, burned_mp4, font, height)
        print(f"[export] wrote {burned_mp4.name}")

    # Description
    desc = out_dir / "description.txt"
    write_description(
        agent, journey, entries, timeline, desc,
        include_prompts=args.include_prompts,
        intro_offset=intro_dur,
    )
    print(f"[export] wrote {desc.name}")

    # Cleanup work dir
    for p in work_dir.glob("*.mp4"):
        p.unlink(missing_ok=True)
    for p in work_dir.glob("*.srt"):
        p.unlink(missing_ok=True)
    try:
        work_dir.rmdir()
    except OSError:
        pass

    print()
    print(f"[export] done. Output in: {out_dir}")
    if args.preset == "social" or args.burn:
        print(f"  → for sharing: final.burn-{args.burn or 'en'}.mp4")
    else:
        print(f"  → for YouTube: upload final.mp4, paste description.txt, upload .srt files under Subtitles")
    return 0


if __name__ == "__main__":
    sys.exit(main())
