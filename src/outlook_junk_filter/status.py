"""A small status file the job keeps current (/state/status.json) for the review page and the stats endpoint."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

NAME = "status.json"
_lock = threading.Lock()


def read_status(state_dir: Path) -> dict:
    try:
        data = json.loads((Path(state_dir) / NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_status(state_dir: Path, **fields) -> None:
    """Merge fields into the status file. A failure to write it never matters to the job."""
    with _lock:
        data = read_status(state_dir)
        data.update({k: v for k, v in fields.items() if v is not None})
        path = Path(state_dir) / NAME
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass
