# otherwake — setup on a new machine

This project runs an autonomous video-generation loop: a vision-capable model (Claude Code,
a local Ollama model, or an online API) decides each next segment and a local ComfyUI renders
it. Each 8-second clip's last frame becomes the starting point of the next. Everything is
logged to `logs/session.json` and shown in the web viewer at `www/index.html`.

## Requirements

- **Windows** (the launcher and path handling assume Windows; the Python is portable)
- **Python 3.10+** — the one embedded in ComfyUI portable works
- **ComfyUI portable build**, recent enough to include the native LTX 2.3 nodes, with these
  files under `ComfyUI/models/`:
  - `checkpoints/ltx-2.3-22b-dev-fp8.safetensors`
  - `loras/ltx-2.3-22b-distilled-lora-384.safetensors`
  - `loras/gemma-3-12b-it-abliterated_lora_rank64_bf16.safetensors`
  - `text_encoders/gemma_3_12B_it_fp4_mixed.safetensors`
  - `latent_upscale_models/ltx-2.3-spatial-upscaler-x2-1.1.safetensors`
  - for text-to-image ([workflows/image_z_image_turbo_t2i.json](workflows/image_z_image_turbo_t2i.json)):
    `diffusion_models/z_image_turbo_bf16.safetensors`, `text_encoders/qwen_3_4b.safetensors`,
    `vae/ae.safetensors`

  About 68 GB in total. You can leave this to the setup step: it lists the files that are
  missing and offers to download them (`python driver/setup.py --download-models` does it
  without asking). Downloads resume if interrupted and are checked against a known SHA-256.
  File names must match the workflow JSONs; if yours differ, edit the JSON or rename the file.
- **ffmpeg** on PATH (or set `ffmpeg_path` in `config.json`)
- **A model to be the agent** — any one of: Claude Code, Ollama with a vision model pulled, or
  an Anthropic / Gemini / OpenAI API key

The folder layout is described in the main [README](README.md#repository-layout).

## Move to a new machine — quick steps

1. **Copy the whole project folder** to the new machine. Keep everything under one root.
2. **Install ComfyUI portable** somewhere. Copy the models listed above if you already have
   them; otherwise the next step can download them.
3. **Double-click `start.bat`** in the project root. That's the whole setup procedure.
   It will:
     - find Python on your machine (or tell you to install it)
     - search common locations for your ComfyUI portable install (with several installs
       on one machine, it picks the one that holds the model files listed above)
     - list any of those model files that are missing, and offer to download them
     - auto-detect ffmpeg
     - write `config.json` for you (no prompts — just picks the first ComfyUI it finds)
     - check `input/`, `output/`, ffmpeg, workflows, folder permissions
     - launch ComfyUI in its own window if it isn't already running
     - launch the viewer (`driver/serve.py`) on port 8787
     - open `http://127.0.0.1:8787/www/` in your default browser

   If you'd rather create config.json by hand, copy `config.example.json` to `config.json`
   and edit `comfy_root` to match your install. `project_root: null` means "use this config
   file's parent folder" — no need to set it unless you split the project root from the
   config location.
4. **(Claude Code path only) Set up the permission allow-list — one-time.** Copy
   `.claude/settings.example.json` → `.claude/settings.json` and replace the `<COMFY>` and
   `<PROJECT_ROOT>` placeholders with the actual paths on your machine. These rules tell
   Claude Code it can run the stable-shape scripts (`_gen.py`, `patch_thought.py`,
   `_rename.py`, `_translate_thoughts.py`, `_verify_translations.py`, `_dump_agent.py`)
   without prompting for permission every tick. Without this file you'll get an "Allow
   Claude to run..." prompt on the first segment and lose end-to-end autonomy. (If you
   only use the web UI — Ollama / Anthropic / Gemini / OpenAI — you can skip this step.)

5. **Wake an agent.** From the viewer that just opened, expand the Run panel (top-right
   ▶ Run), pick a provider + model + optional scope, click **Wake**. Or, for a Claude
   Code-driven journey: open the project folder in Claude Code and say「開始」(or "start").
   `CLAUDE.md` auto-loads and Claude wakes up as an agent.

## Sanity check

The friendliest way: **double-click `start.bat`**. The setup-check runs first and prints
a green ✓ for every part of the pipeline. A red ✗ on any line tells you exactly what's
missing and how to fix it; the launcher won't continue if there's a blocker.

If you only want the check (no ComfyUI launch, no viewer):

```
python driver/setup.py             # check + auto-create config.json if missing
python driver/setup.py --no-fix    # check only, never write
```

If you'd rather use the terminal directly:

```bash
cd "<project root>"
<comfy>/python_embeded/python.exe driver/tolive.py
```
Should print `ComfyUI: <version>` and `project_root: <path>`. If it errors, config paths are wrong.

## Log model

Each entry in `session.json` has:
- `id` — `<agent>-<journey>-<nnn>`, e.g. `墨-j1-003`
- `agent`, `journey` — which agent and which of its journeys the segment belongs to
- `kind` — `t2i` | `t2v` | `i2v`
- `prompt` — the text sent to the image / video model
- `thought` — the agent's first-person note for this segment (English), with optional
  `thought_zh` / `thought_ja` translations
- `image` / `video` / `poster` / `source` — relative paths
- `seed`, `workflow`, `duration_s`, `timestamp`
- `status` — `ok` | `failed`
- `failure_reason` — if failed, why (human-written)
- `replaces` — if this entry is a redo of an earlier failed one, its id
- `tags` — free-form list, used for viewer filtering

The viewer treats `replaces` as a decision tree: the failed parent is shown with strike
styling and linked to its replacement.

## Common gotchas

- **HTTP Range support matters.** Videos will stall at 2 s without it. `driver/serve.py`
  adds it. `python -m http.server` does *not* — don't use that.
- **Style shifts don't work via prompt alone in t2v.** The reference frame dominates.
  To change aesthetic (photoreal → ink wash, etc.), do a one-shot t2i in the target
  style first, then feed that as the next segment's reference.
- **LTX 2.3 is bad at chained human actions within one 8-second clip.** "The old man
  slowly turns his head and walks to the window" rarely executes. A single concrete
  moment almost always does.
- **Character consistency is not preserved between segments.** If that matters, you
  need IP-Adapter / PuLID / a LoRA — not covered here. This project leans into drift
  as a feature.
- **Use forward slashes in `config.json` paths** (`C:/ComfyUI_windows_portable`). A single
  backslash is an escape character in JSON and makes the file fail to parse.
