"""One-click launcher for otherwake.

Invoked by start.bat (or `python driver/_launch.py` directly).

Pipeline:
  1. Run setup.py --auto-fix to ensure config.json exists and points at a
     valid ComfyUI install (auto-detects on first run).
  2. If the ComfyUI HTTP API isn't responding, launch its run_*.bat from
     comfy_root in a new console window and poll until it's reachable.
  3. Start the viewer (driver/serve.py) on port 8787 in this process.
  4. Open the viewer in the default browser as soon as the port answers.

Designed so a brand-new clone can be launched by a non-technical user
double-clicking start.bat — no manual config editing, no separate
ComfyUI / serve.py windows to babysit.
"""
from __future__ import annotations
import json
import os
import runpy
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib import error, request

ROOT = Path(__file__).resolve().parent.parent
CFG_PATH = ROOT / "config.json"
DEFAULT_PORT = 8787
COMFY_BOOT_TIMEOUT_S = 120  # ComfyUI cold-start can take ~30-60s with models warming
VIEWER_OPEN_TIMEOUT_S = 15

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass


def run_setup_auto() -> int:
    """Re-run setup.py --auto-fix on every launch. Cheap (<1s when config is
    already valid) and catches the case where the user reinstalled ComfyUI
    to a new path between launches."""
    setup_py = ROOT / "driver" / "setup.py"
    return subprocess.run(
        [sys.executable, str(setup_py), "--auto-fix"],
        cwd=str(ROOT),
    ).returncode


def load_config() -> dict | None:
    try:
        return json.loads(CFG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"[launcher] cannot read config.json: {e}")
        return None


def is_comfy_alive(url: str, timeout_s: float = 2.0) -> bool:
    """Probe ComfyUI's /system_stats. Quick, cheap, doesn't spam logs."""
    try:
        with request.urlopen(url.rstrip("/") + "/system_stats", timeout=timeout_s) as r:
            r.read()
        return True
    except (error.URLError, OSError):
        return False


def find_comfy_launcher(comfy_root: Path) -> Path | None:
    """Pick the right run_*.bat from a ComfyUI portable root. Order of
    preference: NVIDIA GPU (most common), then CPU fallback."""
    for name in ("run_nvidia_gpu.bat", "run_nvidia.bat", "run_cpu.bat"):
        p = comfy_root / name
        if p.is_file():
            return p
    return None


def start_comfy(comfy_root: Path) -> bool:
    """Spawn ComfyUI in a new console window so the user can see its log
    output (and close it independently). Returns False if the launcher
    can't be found or spawning fails."""
    launcher = find_comfy_launcher(comfy_root)
    if not launcher:
        print(f"[launcher] no run_*.bat in {comfy_root} — start ComfyUI manually.")
        return False
    print(f"[launcher] starting ComfyUI ({launcher.name}) ...")
    creationflags = 0
    if os.name == "nt":
        # CREATE_NEW_CONSOLE keeps ComfyUI's window separate so closing the
        # otherwake viewer window doesn't kill ComfyUI mid-generation.
        creationflags = subprocess.CREATE_NEW_CONSOLE
    try:
        subprocess.Popen(
            [str(launcher)],
            cwd=str(comfy_root),
            shell=False,
            creationflags=creationflags,
        )
        return True
    except OSError as e:
        print(f"[launcher] failed to spawn ComfyUI: {e}")
        return False


def wait_for_comfy(url: str, timeout_s: int = COMFY_BOOT_TIMEOUT_S) -> bool:
    """Poll /system_stats every 2s until it answers or we hit the timeout."""
    print(f"[launcher] waiting for ComfyUI at {url} (up to {timeout_s}s)", end="", flush=True)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if is_comfy_alive(url):
            print(" — up.")
            return True
        time.sleep(2)
        print(".", end="", flush=True)
    print(" — timed out.")
    return False


def is_viewer_already_running(port: int) -> bool:
    """Probe http://127.0.0.1:<port>/api/run/status. If it answers with our
    expected JSON shape, another otherwake serve.py is already listening on
    this port — re-binding will fail and crash the launcher with a
    confusing "ended with code -1" message. Detect this up front so we can
    just hand the user the existing viewer URL instead."""
    try:
        with request.urlopen(
            f"http://127.0.0.1:{port}/api/run/status", timeout=1.5
        ) as r:
            data = json.loads(r.read().decode("utf-8"))
        return "state" in data and "server_time" in data
    except (error.URLError, OSError, json.JSONDecodeError):
        return False


def open_browser_when_ready(url: str) -> None:
    """Background thread: poll the viewer URL, then open it in the default
    browser. Falls back to opening anyway after the timeout so the user
    isn't left with a closed window if the probe is flaky."""
    def _opener() -> None:
        deadline = time.time() + VIEWER_OPEN_TIMEOUT_S
        while time.time() < deadline:
            try:
                with request.urlopen(url, timeout=1) as r:
                    r.read(1)
                webbrowser.open(url)
                return
            except (error.URLError, OSError):
                time.sleep(0.3)
        webbrowser.open(url)
    threading.Thread(target=_opener, daemon=True).start()


def main() -> int:
    print()
    print("=" * 60)
    print(" otherwake — launcher")
    print("=" * 60)

    rc = run_setup_auto()
    if rc != 0:
        print()
        print("[launcher] setup reported a problem — fix the ✗ items above and re-run.")
        return rc

    cfg = load_config()
    if cfg is None:
        return 1

    comfy_url = (cfg.get("comfy_url") or "http://127.0.0.1:8188").rstrip("/")
    comfy_root_str = cfg.get("comfy_root") or ""
    comfy_root = Path(comfy_root_str) if comfy_root_str else None

    if is_comfy_alive(comfy_url):
        print(f"[launcher] ComfyUI already running at {comfy_url}")
    elif comfy_root and comfy_root.is_dir():
        if start_comfy(comfy_root):
            if not wait_for_comfy(comfy_url):
                print("[launcher] ComfyUI didn't respond in 2 minutes — continuing anyway.")
                print("           Check the ComfyUI window for errors. The viewer will")
                print("           open; wake will fail until ComfyUI is ready.")
    else:
        print("[launcher] config.json has no valid comfy_root — start ComfyUI manually.")

    port = int(os.environ.get("OTHERWAKE_PORT") or DEFAULT_PORT)
    url = f"http://127.0.0.1:{port}/www/"

    # Bail out cleanly if a viewer is already running. Otherwise serve.py's
    # bind() crashes with OSError(WinError 10048), runpy lets it propagate,
    # the launcher exits with code -1, and the user sees nothing useful.
    if is_viewer_already_running(port):
        print()
        print(f"[launcher] otherwake viewer is already running on port {port}.")
        print(f"[launcher] opening {url} in your browser — nothing else to do.")
        print()
        webbrowser.open(url)
        return 0

    print()
    print(f"[launcher] viewer URL: {url}")
    print("[launcher] (browser opens automatically; close this window to stop)")
    print()
    open_browser_when_ready(url)

    # Hand control to serve.py — blocks until Ctrl-C / window closed.
    sys.argv = ["serve.py", str(port)]
    runpy.run_path(str(ROOT / "driver" / "serve.py"), run_name="__main__")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[launcher] bye.")
        sys.exit(0)
