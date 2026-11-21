"""Safe reads and writes for the JSON logs.

logs/session.json is read by the viewer every couple of seconds while the
driver appends to it. On Windows an in-place rewrite can then fail with
"OSError: [Errno 22] Invalid argument" because another process has the file
open — and a rewrite that fails halfway leaves a truncated log. So: write to
a temporary file next to the target, then swap it in, retrying briefly while
a reader holds the old one. Readers retry too, and never mistake "couldn't
read it just now" for "it's empty".
"""
from __future__ import annotations
import json
import os
import random
import time
from pathlib import Path

_ATTEMPTS = 100
_PAUSE_S = 0.15


def read_json(path: Path):
    """Parse a JSON file, retrying while a writer is mid-swap. Raises if the
    file is missing or still unreadable after the retries."""
    last: Exception | None = None
    for _ in range(_ATTEMPTS):
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise
        except (OSError, json.JSONDecodeError) as e:
            last = e
            time.sleep(_PAUSE_S)
    raise RuntimeError(f"could not read {path}: {last}")


def write_json(path: Path, data, trailing_newline: bool = False) -> None:
    """Replace a JSON file atomically."""
    path = Path(path)
    text = json.dumps(data, ensure_ascii=False, indent=2) + ("\n" if trailing_newline else "")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    last: Exception | None = None
    for _ in range(_ATTEMPTS):
        try:
            os.replace(tmp, path)
            return
        except OSError as e:  # a reader has the target open
            last = e
            # Jittered, so we don't keep landing in step with a polling reader.
            time.sleep(random.uniform(0.02, 2 * _PAUSE_S))
    raise RuntimeError(f"could not replace {path} (left the new content in {tmp.name}): {last}")
