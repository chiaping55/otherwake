"""otherwake setup helper — finds your ComfyUI install, writes config.json,
and verifies everything is in place.

Called by `driver/_launch.py` (which is what `start.bat` invokes) to do the
config + checks step before the launcher starts ComfyUI and the viewer. Can
also be run standalone if you just want to verify the setup without
launching anything.

CLI options:
    python driver/setup.py             # auto-detect + interactive prompt before writing
    python driver/setup.py --auto-fix  # write config.json automatically (no prompt)
    python driver/setup.py --no-fix    # only check, never write anything
    python driver/setup.py --download-models   # fetch missing model files without asking
"""
from __future__ import annotations
import argparse
import json
import os
import shutil
import string
import sys
from pathlib import Path
from urllib import error, request

sys.path.insert(0, str(Path(__file__).resolve().parent))
import model_files  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

ROOT = Path(__file__).resolve().parent.parent
CFG_PATH = ROOT / "config.json"

# ANSI color helpers — only used when stdout is a real terminal that supports it.
USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if USE_COLOR else s
GREEN = lambda s: _c("32", s)
RED   = lambda s: _c("31", s)
YEL   = lambda s: _c("33", s)
BOLD  = lambda s: _c("1",  s)
DIM   = lambda s: _c("2",  s)

OK_MARK = GREEN("✓")
NO_MARK = RED("✗")
WN_MARK = YEL("!")


def say(mark: str, msg: str, fix: str | None = None) -> None:
    print(f"  {mark}  {msg}")
    if fix:
        for ln in fix.splitlines():
            print(f"      {DIM(ln)}")


# ---------------------------------------------------------------------------
# ComfyUI auto-detection
# ---------------------------------------------------------------------------

def is_comfy_root(p: Path) -> bool:
    """Return True if p looks like the root of a ComfyUI portable install."""
    if not p.is_dir():
        return False
    return (
        (p / "python_embeded" / "python.exe").is_file()
        or (p / "python_embeded" / "bin" / "python").is_file()
        or (p / "python_embeded" / "python").is_file()
    )


def candidate_search_dirs() -> list[Path]:
    """Common parent folders to scan for a ComfyUI install."""
    home = Path.home()
    dirs = [
        home / "Documents",
        home / "Desktop",
        home / "Downloads",
        home,
    ]
    # all drive letters on Windows
    if os.name == "nt":
        for letter in string.ascii_uppercase:
            d = Path(f"{letter}:\\")
            if d.is_dir():
                dirs.append(d)
                dirs.append(d / "AI")
                dirs.append(d / "ComfyUI")
                dirs.append(d / "stable-diffusion")
                dirs.append(d / "tools")
        dirs.append(Path("C:/Program Files"))
        dirs.append(Path("C:/Program Files (x86)"))
    return dirs


def find_comfyui_roots() -> list[Path]:
    """Scan likely places for ComfyUI portable installs. Returns absolute paths,
    deduplicated. Order: roughly nearest to the user first (Documents > drives)."""
    seen: set[Path] = set()
    found: list[Path] = []
    for parent in candidate_search_dirs():
        if not parent.is_dir():
            continue
        try:
            children = list(parent.iterdir())
        except (OSError, PermissionError):
            continue
        for entry in children:
            if not entry.is_dir():
                continue
            name = entry.name.lower()
            if "comfy" not in name:
                continue
            try:
                key = entry.resolve()
            except OSError:
                continue
            if key in seen:
                continue
            seen.add(key)
            if is_comfy_root(entry):
                found.append(entry)
    return found


REQUIRED_WORKFLOWS = (
    "workflows/image_z_image_turbo_t2i.json",
    "workflows/video_ltx2.3_t2v.json",
)
_MODEL_EXTS = (".safetensors", ".gguf", ".ckpt", ".pth", ".pt", ".sft")


def required_model_files() -> list[str]:
    """Model file names the bundled workflows load, read from the workflow
    JSONs themselves so this list can't drift from them."""
    names: set[str] = set()

    def walk(node) -> None:
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, str) and node.lower().endswith(_MODEL_EXTS):
            names.add(Path(node.replace("\\", "/")).name)

    for rel in REQUIRED_WORKFLOWS:
        try:
            walk(json.loads((ROOT / rel).read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(names)


def installed_model_files(comfy_root: Path) -> set[str]:
    """Names of every model file under this install's models/ folder."""
    found: set[str] = set()
    for models_dir in (comfy_root / "ComfyUI" / "models", comfy_root / "models"):
        if not models_dir.is_dir():
            continue
        for _dirpath, _dirnames, filenames in os.walk(models_dir, followlinks=True):
            found.update(f for f in filenames if f.lower().endswith(_MODEL_EXTS))
    return found


def missing_model_files(comfy_root: Path) -> list[str]:
    have = installed_model_files(comfy_root)
    return [n for n in required_model_files() if n not in have]


def find_ffmpeg(comfy_root: Path | None) -> str | None:
    """Look for ffmpeg in comfy_root, then on PATH. Returns absolute path or None."""
    if comfy_root:
        for cand in (
            comfy_root / "python_embeded" / "ffmpeg.exe",
            comfy_root / "python_embeded" / "ffmpeg",
        ):
            if cand.is_file():
                return str(cand).replace("\\", "/")
    on_path = shutil.which("ffmpeg")
    if on_path:
        return on_path.replace("\\", "/")
    return None


# ---------------------------------------------------------------------------
# config.json creation
# ---------------------------------------------------------------------------

def offer_to_create_config(auto_fix: bool, no_fix: bool) -> dict | None:
    """If config.json doesn't exist, try to build one from auto-detection.
    Returns the freshly-loaded config dict on success, None on failure."""
    if no_fix:
        say(NO_MARK, "config.json missing", "(--no-fix specified, not creating)")
        return None

    print()
    print(BOLD("config.json doesn't exist yet — let's build one."))
    roots = find_comfyui_roots()

    if not roots:
        say(NO_MARK, "couldn't auto-detect a ComfyUI install",
            "Looked in: Documents, Desktop, Downloads, C:/, D:/, etc.\n"
            "Install ComfyUI portable and re-run, or manually create config.json:\n"
            "  copy config.example.json to config.json\n"
            "  edit comfy_root to your ComfyUI folder")
        return None

    chosen: Path
    if len(roots) == 1:
        chosen = roots[0]
        say(OK_MARK, f"found ComfyUI at: {chosen}")
    else:
        # Several installs on one machine is common (one per project). Put the
        # one that actually holds this project's models first, so the default
        # pick is the install that can run the workflows.
        total = len(required_model_files())
        missing = {r: len(missing_model_files(r)) for r in roots}
        roots.sort(key=lambda r: missing[r])
        print(f"  {WN_MARK}  found multiple ComfyUI installs:")
        for i, r in enumerate(roots, 1):
            print(f"      [{i}] {r}  ({total - missing[r]}/{total} required model files)")
        if auto_fix:
            chosen = roots[0]
            print(f"      auto-fix: picking [{1}] {chosen}")
        else:
            try:
                ans = input("      pick a number [1]: ").strip() or "1"
                idx = int(ans) - 1
                chosen = roots[idx] if 0 <= idx < len(roots) else roots[0]
            except (ValueError, EOFError, KeyboardInterrupt):
                chosen = roots[0]
            print(f"      using: {chosen}")

    ffmpeg_path = find_ffmpeg(chosen) or ""
    cfg = {
        "comfy_url": "http://127.0.0.1:8188",
        "comfy_root": str(chosen).replace("\\", "/"),
        "ffmpeg_path": ffmpeg_path,
        "project_root": None,
    }
    if not ffmpeg_path:
        say(WN_MARK, "ffmpeg not found inside ComfyUI portable or on PATH",
            "Will leave ffmpeg_path empty in config.json.\n"
            "Most ComfyUI portable builds bundle ffmpeg.exe under python_embeded/.\n"
            "If yours doesn't, install ffmpeg separately and edit config.json.")

    print()
    print(BOLD("about to write:"))
    for k, v in cfg.items():
        print(f"      {k}: {v if v is not None else 'null'}")
    print()
    if not auto_fix:
        try:
            ans = input("write this to config.json? [Y/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        if ans and ans not in ("y", "yes"):
            say(NO_MARK, "config.json not written. you can edit config.example.json by hand.")
            return None

    CFG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    say(OK_MARK, f"wrote {CFG_PATH}")
    return cfg


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def check_python() -> bool:
    v = sys.version_info
    if (v.major, v.minor) >= (3, 10):
        say(OK_MARK, f"Python {v.major}.{v.minor}.{v.micro}")
        return True
    say(NO_MARK, f"Python {v.major}.{v.minor}.{v.micro}",
        "Need Python 3.10 or newer. Install from python.org and re-run.")
    return False


def check_config(auto_fix: bool, no_fix: bool) -> dict | None:
    if not CFG_PATH.exists():
        say(NO_MARK, "config.json not found")
        return offer_to_create_config(auto_fix, no_fix)
    try:
        cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        say(NO_MARK, "config.json is not valid JSON",
            f"parser said: {e}\nFix the syntax (matching braces, commas) and re-run.")
        return None
    say(OK_MARK, "config.json parses")
    return cfg


def check_comfy_root(cfg: dict) -> Path | None:
    root = cfg.get("comfy_root")
    if not root:
        say(NO_MARK, "config.json has no 'comfy_root'",
            "Set it to your ComfyUI install folder (e.g. D:/ComfyUI_windows_portable).")
        return None
    p = Path(root)
    if not p.is_dir():
        say(NO_MARK, f"comfy_root does not exist on disk: {p}",
            "Check the path in config.json — typo, wrong drive letter, etc.")
        return None
    say(OK_MARK, f"ComfyUI install: {p}")
    return p


def check_comfy_subdirs(comfy_root: Path) -> bool:
    ok = True
    for name in ("input", "output"):
        candidates = [comfy_root / "ComfyUI" / name, comfy_root / name]
        found = next((c for c in candidates if c.is_dir()), None)
        if found:
            say(OK_MARK, f"ComfyUI {name}/ exists")
        else:
            say(NO_MARK, f"ComfyUI {name}/ folder not found",
                f"Looked at:\n" +
                "\n".join(f"  - {c}" for c in candidates) + "\n"
                "Is comfy_root pointing at the actual portable folder? (Often ends in '_windows_portable'.)")
            ok = False
    return ok


def check_ffmpeg(cfg: dict, comfy_root: Path | None) -> bool:
    cfg_path = cfg.get("ffmpeg_path") or ""
    p = Path(cfg_path) if cfg_path else None
    if p and p.is_absolute() and p.is_file():
        say(OK_MARK, f"ffmpeg: {p}")
        return True
    if comfy_root:
        for cand in (
            comfy_root / "python_embeded" / "ffmpeg.exe",
            comfy_root / "python_embeded" / "ffmpeg",
        ):
            if cand.is_file():
                say(OK_MARK, f"ffmpeg (auto-found): {cand}")
                return True
    on_path = shutil.which(cfg_path) if cfg_path else shutil.which("ffmpeg")
    if on_path:
        say(OK_MARK, f"ffmpeg on PATH: {on_path}")
        return True
    say(NO_MARK, "ffmpeg not found",
        "Looked at: ffmpeg_path in config.json, ComfyUI's python_embeded/, system PATH.\n"
        "Either install ffmpeg and add to PATH, or set ffmpeg_path in config.json to an absolute file path.")
    return False


def models_root_of(comfy_root: Path) -> Path:
    nested = comfy_root / "ComfyUI" / "models"
    return nested if nested.is_dir() or (comfy_root / "ComfyUI").is_dir() else comfy_root / "models"


def check_models(comfy_root: Path, download: str) -> None:
    """Report model files the workflows need but this install lacks, and
    offer to download them.

    `download` is "yes" (download without asking), "ask" (prompt when run
    from a terminal) or "no". Missing files are a warning, not a blocker:
    models can also live outside models/ via ComfyUI's extra_model_paths.yaml,
    which this check doesn't read.
    """
    needed = required_model_files()
    missing = missing_model_files(comfy_root)
    if not missing:
        say(OK_MARK, f"all {len(needed)} model files the workflows need are installed")
        return

    known = [model_files.MODEL_FILES[n] for n in missing if n in model_files.MODEL_FILES]
    unknown = [n for n in missing if n not in model_files.MODEL_FILES]
    total = sum(m.size for m in known)
    lines = [f"  - {m.name}  ({model_files.gb(m.size)})  → models/{m.folder}/" for m in known]
    lines += [f"  - {n}  (no download source on record — find it yourself)" for n in unknown]
    say(WN_MARK, f"{len(missing)} of {len(needed)} model files not found under {comfy_root}",
        "Generation will fail until these are in place:\n" + "\n".join(lines) + "\n"
        "If they live elsewhere via extra_model_paths.yaml, or comfy_root points at the\n"
        "wrong install, ignore this or fix comfy_root in config.json.")
    if not known or download == "no":
        return

    root = models_root_of(comfy_root)
    free = model_files.free_bytes(root)
    pages = sorted({m.page for m in known})
    print()
    print(f"      These can be downloaded from Hugging Face ({model_files.gb(total)} in total,")
    print(f"      {model_files.gb(free)} free on that drive). No account needed. Each file is")
    print("      published under its own licence — see:")
    for p in pages:
        print(f"        {p}")
    if free < total + 2 * 10**9:
        say(NO_MARK, "not enough free disk space for the download — free some up and re-run")
        return
    if download == "ask":
        if not sys.stdin.isatty():
            print(f"      To download them, run:  python driver/setup.py --download-models")
            return
        try:
            ans = input(f"      download {len(known)} file(s), {model_files.gb(total)}, now? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if ans not in ("y", "yes"):
            print("      skipped. Run  python driver/setup.py --download-models  whenever you're ready.")
            return

    for i, m in enumerate(known, 1):
        print(f"      [{i}/{len(known)}] {m.name}")
        def report(line: str) -> None:
            print(f"\r          {line:<70}", end="", flush=True)
        try:
            dest = model_files.download(m, root, report)
        except model_files.DownloadError as e:
            print()
            say(NO_MARK, str(e))
            return
        except KeyboardInterrupt:
            print()
            say(WN_MARK, "download interrupted — run again to resume where it stopped")
            return
        print(f"\r          {'done, checksum ok → ' + str(dest):<70}")
    still = missing_model_files(comfy_root)
    if still:
        say(WN_MARK, f"still missing: {', '.join(still)}")
    else:
        say(OK_MARK, f"all {len(needed)} model files the workflows need are installed")
        print("      (if ComfyUI is already running, restart it so it sees the new files)")


def check_workflows() -> bool:
    ok = True
    for r in REQUIRED_WORKFLOWS:
        if (ROOT / r).is_file():
            say(OK_MARK, f"workflow present: {r}")
        else:
            say(NO_MARK, f"missing: {r}",
                "Re-pull the repo or copy the workflows/ folder from a working install.")
            ok = False
    return ok


def check_dirs_writable() -> bool:
    ok = True
    for name in ("logs", "image", "video"):
        d = ROOT / name
        try:
            d.mkdir(exist_ok=True)
            test = d / ".setup_write_test"
            test.write_text("ok")
            test.unlink()
            say(OK_MARK, f"{name}/ writable")
        except OSError as e:
            say(NO_MARK, f"{name}/ not writable: {e}",
                "Check folder permissions or move the project somewhere your user can write to.")
            ok = False
    return ok


def check_comfy_api(cfg: dict) -> bool:
    url_base = (cfg.get("comfy_url") or "").rstrip("/")
    if not url_base:
        say(WN_MARK, "comfy_url not set — skipping live API check.")
        return True
    url = url_base + "/system_stats"
    try:
        with request.urlopen(url, timeout=5) as r:
            data = json.loads(r.read())
        version = data.get("system", {}).get("comfyui_version", "?")
        say(OK_MARK, f"ComfyUI server reachable at {url_base} (version {version})")
        return True
    except error.URLError as e:
        say(WN_MARK, f"ComfyUI not reachable at {url_base} ({e.reason})",
            "Start ComfyUI (run_nvidia_gpu.bat or equivalent) and re-run this check.\n"
            "Not strictly required for setup, only for actually running the pipeline.")
        return False
    except Exception as e:  # noqa: BLE001
        say(WN_MARK, f"ComfyUI API responded weirdly: {e}")
        return False


def check_ollama(cfg: dict) -> None:
    url = (cfg.get("ollama_url") or "http://127.0.0.1:11434").rstrip("/")
    try:
        with request.urlopen(url + "/api/tags", timeout=3) as r:
            data = json.loads(r.read())
        models = [m["name"] for m in data.get("models", [])]
        vision_hints = ("vl", "vision", "llava", "minicpm-v", "gemma3", "gemma4",
                        "qwen3.8", "pixtral", "molmo")
        vision = [m for m in models if any(h in m.lower() for h in vision_hints)]
        if vision:
            say(OK_MARK, f"Ollama running at {url} — vision models: {', '.join(vision)}")
        else:
            say(WN_MARK, f"Ollama running at {url}, but no vision model pulled yet",
                "Run:  ollama pull gemma3:12b   (recommended baseline)\n"
                f"(Currently installed: {', '.join(models[:5]) or 'none'})")
    except error.URLError:
        say(WN_MARK, f"Ollama not running at {url}",
            "Only needed for the offline (Ollama) provider.\n"
            "Start it with:  ollama serve   (on Windows the installer already runs it as a service)")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="otherwake setup helper.")
    parser.add_argument("--auto-fix", action="store_true",
                        help="if config.json is missing, write it automatically without asking")
    parser.add_argument("--no-fix", action="store_true",
                        help="never write config.json or download anything (check only)")
    parser.add_argument("--download-models", action="store_true",
                        help="download any missing model files without asking")
    args = parser.parse_args()
    download = "no" if args.no_fix else ("yes" if args.download_models else "ask")

    print()
    print(BOLD("otherwake setup — checking your environment"))
    print(BOLD("=" * 60))
    print()
    print(BOLD("required:"))
    required_ok = True
    required_ok &= check_python()
    cfg = check_config(args.auto_fix, args.no_fix)
    if cfg is None:
        print()
        print(RED("config.json is missing or invalid — fix that first, then re-run."))
        return 1
    comfy_root = check_comfy_root(cfg)
    if comfy_root:
        required_ok &= check_comfy_subdirs(comfy_root)
        required_ok &= check_ffmpeg(cfg, comfy_root)
        check_models(comfy_root, download)
    else:
        required_ok = False
    required_ok &= check_workflows()
    required_ok &= check_dirs_writable()

    print()
    print(BOLD("live services (start them before running the pipeline):"))
    check_comfy_api(cfg)
    check_ollama(cfg)

    print()
    print(BOLD("=" * 60))
    if required_ok:
        print(GREEN(BOLD("READY.")) + " required setup looks good.")
        print(DIM("  next: double-click start.bat (or run python driver/_launch.py) to launch the viewer."))
        print(DIM("        ComfyUI will auto-start if it isn't already; the browser opens to http://127.0.0.1:8787/www/."))
        return 0
    print(RED(BOLD("NOT READY.")) + " fix the ✗ items above and re-run.")
    print(DIM("  ! items are warnings (live services not running) — those don't block setup, just block actually running."))
    return 1


if __name__ == "__main__":
    sys.exit(main())
