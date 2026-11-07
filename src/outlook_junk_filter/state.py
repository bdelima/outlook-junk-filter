"""State under /state/delta/state.json.

Holds the delta link, the retry list and the handled internetMessageIds
(entries older than 90 days are dropped).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

HANDLED_DAYS = 90
MAX_ATTEMPTS = 3


def now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class State:
    def __init__(self, state_dir: Path, clock=now):
        self.path = Path(state_dir) / "delta" / "state.json"
        self._clock = clock
        self.delta_link: str | None = None
        self.retry: dict = {}  # graph message id -> {"attempts": n, "message": {...}, "reason": str}
        self.handled: dict = {}  # internetMessageId (or "id:<graph id>") -> iso time
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.delta_link = data.get("delta_link") or None
        self.retry = data.get("retry") or {}
        self.handled = data.get("handled") or {}

    def save(self) -> None:
        cutoff = _iso(self._clock() - timedelta(days=HANDLED_DAYS))
        self.handled = {key: ts for key, ts in self.handled.items() if ts >= cutoff}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"delta_link": self.delta_link, "retry": self.retry, "handled": self.handled}),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    @staticmethod
    def key(message: dict) -> str:
        return message.get("internetMessageId") or f"id:{message['id']}"

    def is_handled(self, message: dict) -> bool:
        return self.key(message) in self.handled

    def mark_handled(self, message: dict) -> None:
        self.handled[self.key(message)] = _iso(self._clock())
        self.retry.pop(message["id"], None)

    def add_retry(self, message: dict, reason: str, count_attempt: bool = True) -> int:
        """Put a message on the retry list. Returns the attempts used so far."""
        entry = self.retry.setdefault(message["id"], {"attempts": 0, "message": message, "reason": reason})
        if count_attempt:
            entry["attempts"] += 1
        entry["reason"] = reason
        entry["message"] = message
        return entry["attempts"]
