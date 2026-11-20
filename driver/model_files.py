"""Where the model files the bundled workflows load come from, and a
resumable downloader for them.

Every entry is pinned to a specific revision of its Hugging Face repository
and carries the file's size and SHA-256, so a download either produces the
exact file the workflows were tested with or fails loudly. None of the
repositories is gated: no account or token is needed.

Stdlib only, like the rest of the driver.
"""
from __future__ import annotations
import hashlib
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib import error, request

_CHUNK = 4 * 1024 * 1024
_RETRIES = 6
_USER_AGENT = "otherwake-setup"


@dataclass(frozen=True)
class ModelFile:
    name: str      # file name the workflow JSON refers to
    folder: str    # subfolder of ComfyUI/models/
    repo: str      # Hugging Face repository
    rev: str       # pinned commit
    path: str      # path inside the repository
    size: int      # bytes
    sha256: str

    @property
    def url(self) -> str:
        return f"https://huggingface.co/{self.repo}/resolve/{self.rev}/{self.path}"

    @property
    def page(self) -> str:
        return f"https://huggingface.co/{self.repo}"


_LTX = "c1521179de480464f366a98a9d9b2acc4cc097b9"
_LTX_FP8 = "91eda216f820e5ecb3d17dc9fe3b7cdaf56b587d"
_LTX2_COMFY = "ccde4ba417d7900669fd56dd292a883cee11ff37"
_ZIMAGE = "6fc90a3b1b653e935a0d175e260736de25b84df5"

MODEL_FILES: dict[str, ModelFile] = {m.name: m for m in (
    # --- video: workflows/video_ltx2.3_t2v.json (LTX-2 Community License) ---
    ModelFile("ltx-2.3-22b-dev-fp8.safetensors", "checkpoints",
              "Lightricks/LTX-2.3-fp8", _LTX_FP8, "ltx-2.3-22b-dev-fp8.safetensors",
              29145431166, "28606c5b5a06ce56f896d4dfcb20f212739e07a68fbe48e53638188449d26450"),
    ModelFile("ltx-2.3-22b-distilled-lora-384.safetensors", "loras",
              "Lightricks/LTX-2.3", _LTX, "ltx-2.3-22b-distilled-lora-384.safetensors",
              7605507256, "2943ab994f3c9d88052e5a2a34cca14e4a2dfc36b1d8c407931d52d5c25dd72b"),
    ModelFile("ltx-2.3-spatial-upscaler-x2-1.1.safetensors", "latent_upscale_models",
              "Lightricks/LTX-2.3", _LTX, "ltx-2.3-spatial-upscaler-x2-1.1.safetensors",
              995743560, "5f416311fa8172b65af67530758964708d29a317b830d689a51143b7f91913ed"),
    ModelFile("gemma_3_12B_it_fp4_mixed.safetensors", "text_encoders",
              "Comfy-Org/ltx-2", _LTX2_COMFY, "split_files/text_encoders/gemma_3_12B_it_fp4_mixed.safetensors",
              9447702218, "aaca463d11e6d8d2a4bdb0d6299214c15ef78a3f73e0ef8113d5a9d0219b3f6d"),
    ModelFile("gemma-3-12b-it-abliterated_lora_rank64_bf16.safetensors", "loras",
              "Comfy-Org/ltx-2", _LTX2_COMFY, "split_files/loras/gemma-3-12b-it-abliterated_lora_rank64_bf16.safetensors",
              628203616, "87bcabeac9bec9f374232b5122d6511c2b2112d479e50176149e944b3712eb4a"),
    # --- image: workflows/image_z_image_turbo_t2i.json (Apache-2.0) ---
    ModelFile("z_image_turbo_bf16.safetensors", "diffusion_models",
              "Comfy-Org/z_image_turbo", _ZIMAGE, "split_files/diffusion_models/z_image_turbo_bf16.safetensors",
              12309866400, "2407613050b809ffdff18a4ac99af83ea6b95443ecebdf80e064a79c825574a6"),
    ModelFile("qwen_3_4b.safetensors", "text_encoders",
              "Comfy-Org/z_image_turbo", _ZIMAGE, "split_files/text_encoders/qwen_3_4b.safetensors",
              8044982048, "6c671498573ac2f7a5501502ccce8d2b08ea6ca2f661c458e708f36b36edfc5a"),
    ModelFile("ae.safetensors", "vae",
              "Comfy-Org/z_image_turbo", _ZIMAGE, "split_files/vae/ae.safetensors",
              335304388, "afc8e28272cd15db3919bacdb6918ce9c1ed22e96cb12c4d5ed0fba823529e38"),
)}


class DownloadError(RuntimeError):
    pass


def gb(n: int) -> str:
    return f"{n / 1e9:.1f} GB"


def free_bytes(folder: Path) -> int:
    probe = folder
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def _hash_existing(part: Path, hasher, limit: int) -> int:
    """Feed an existing partial file into the hasher; returns bytes read."""
    done = 0
    with part.open("rb") as f:
        while done < limit:
            chunk = f.read(min(_CHUNK, limit - done))
            if not chunk:
                break
            hasher.update(chunk)
            done += len(chunk)
    return done


def download(mf: ModelFile, models_root: Path,
             report: Callable[[str], None] | None = None) -> Path:
    """Download one model file into models_root/<folder>/, resuming a partial
    download if one is there, and verify size and SHA-256 before it gets its
    final name. Returns the final path. Raises DownloadError on failure."""
    dest_dir = models_root / mf.folder
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / mf.name
    part = dest_dir / (mf.name + ".part")
    report = report or (lambda _line: None)

    last_err: Exception | None = None
    for attempt in range(1, _RETRIES + 1):
        have = part.stat().st_size if part.exists() else 0
        if have > mf.size:
            part.unlink()
            have = 0
        hasher = hashlib.sha256()
        if have:
            report(f"resuming at {gb(have)} — re-reading the partial file")
            have = _hash_existing(part, hasher, have)

        headers = {"User-Agent": _USER_AGENT}
        if have:
            headers["Range"] = f"bytes={have}-"
        try:
            if have < mf.size:
                with request.urlopen(request.Request(mf.url, headers=headers), timeout=60) as resp:
                    if have and resp.status != 206:
                        # Server ignored the Range header: start over.
                        have = 0
                        hasher = hashlib.sha256()
                    mode = "ab" if have else "wb"
                    started = last_report = time.time()
                    got = 0
                    with part.open(mode) as out:
                        while True:
                            chunk = resp.read(_CHUNK)
                            if not chunk:
                                break
                            out.write(chunk)
                            hasher.update(chunk)
                            have += len(chunk)
                            got += len(chunk)
                            now = time.time()
                            if now - last_report >= 1.0:
                                last_report = now
                                rate = got / max(now - started, 1e-6)
                                left = (mf.size - have) / max(rate, 1.0)
                                report(f"{gb(have)} / {gb(mf.size)}  {have * 100 // mf.size}%  "
                                       f"{rate / 1e6:.0f} MB/s  ~{int(left // 60)} min left")
            if have != mf.size:
                raise DownloadError(f"connection closed at {have} of {mf.size} bytes")
        except (error.URLError, OSError, DownloadError) as e:
            last_err = e
            if isinstance(e, error.HTTPError) and e.code in (401, 403, 404):
                raise DownloadError(f"{mf.name}: server answered HTTP {e.code} for {mf.url}") from e
            report(f"interrupted ({e}); retry {attempt}/{_RETRIES}")
            time.sleep(min(5 * attempt, 30))
            continue

        if hasher.hexdigest() != mf.sha256:
            part.unlink(missing_ok=True)
            raise DownloadError(f"{mf.name}: checksum mismatch — the partial file was removed, run again")
        part.replace(dest)
        return dest

    raise DownloadError(f"{mf.name}: gave up after {_RETRIES} attempts ({last_err}). "
                        f"Run again to resume from where it stopped.")
