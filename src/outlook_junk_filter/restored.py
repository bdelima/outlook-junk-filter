"""Messages the user restored from the review page. The job keeps them (reason ``restored``).

One process holds both the job and the page, so a lock serializes the writes; the file is
written atomically so a crash never leaves it half-written.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

KEEP_DAYS = 365


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class RestoredList:
    def __init__(self, state_dir: Path, clock=lambda: datetime.now(timezone.utc)):
        self._path = Path(state_dir) / "restored.json"
        self._clock = clock
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def contains(self, internet_message_id: str) -> bool:
        if not internet_message_id:
            return False
        with self._lock:
            return internet_message_id in self._load()

    def add(self, internet_message_id: str) -> None:
        if not internet_message_id:
            return
        with self._lock:
            data = self._load()
            data[internet_message_id] = _iso(self._clock())
            cutoff = _iso(self._clock() - timedelta(days=KEEP_DAYS))
            data = {key: ts for key, ts in data.items() if ts >= cutoff}
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self._path)
