"""Container log lines and the per-message JSON log (decisions.jsonl).

Never logs message bodies, attachments or tokens.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path

SUBJECT_MAX = 120
LOG_BYTES = 10 * 1024 * 1024
LOG_BACKUPS = 9  # the current file plus 9 = 10 files of 10 MB


def setup_container_log() -> logging.Logger:
    if hasattr(time, "tzset"):
        time.tzset()
    log = logging.getLogger("ojf")
    log.setLevel(logging.INFO)
    log.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.propagate = False
    return log


class DecisionLog:
    """One JSON object per message, rotated by size."""

    def __init__(self, state_dir: Path):
        path = Path(state_dir) / "logs" / "decisions.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=LOG_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
        )
        self._handler.setFormatter(logging.Formatter("%(message)s"))
        self._logger = logging.getLogger("ojf.decisions")
        self._logger.setLevel(logging.INFO)
        self._logger.handlers.clear()
        self._logger.addHandler(self._handler)
        self._logger.propagate = False

    def write(self, record: dict) -> None:
        self._logger.info(json.dumps(record, ensure_ascii=False, separators=(",", ":")))


def touch_health(state_dir: Path) -> None:
    path = Path(state_dir) / "health"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8"):
        os.utime(path, None)


def histogram(scores: list) -> dict:
    """Score histogram in whole-number buckets, e.g. {"-1": 3, "0": 10, "5": 2}."""
    buckets: dict = {}
    for value in scores:
        key = str(int(value // 1))
        buckets[key] = buckets.get(key, 0) + 1
    return dict(sorted(buckets.items(), key=lambda kv: int(kv[0])))
