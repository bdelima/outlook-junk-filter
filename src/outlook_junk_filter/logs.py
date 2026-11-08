"""Container log lines and the per-message JSON log (decisions.jsonl).

Never logs message bodies, attachments or tokens.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SUBJECT_MAX = 120
LOG_BYTES = 10 * 1024 * 1024
LOG_BACKUPS = 9  # the current file plus 9 = 10 files of 10 MB
TOMBSTONES = "deleted.jsonl"  # rows deleted from the review page: hidden at once, removed for good by the next prune
ACTIONS = "actions.jsonl"
KEEP_FOREVER = ("allowlist",)  # action records worth keeping: a sender's allowlist entry outlives the log
TOMBSTONE_LOCK = threading.Lock()


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_tombstones(log_dir: Path) -> set:
    """The (message_id, ts) pairs hidden by a delete."""
    out = set()
    try:
        lines = (Path(log_dir) / TOMBSTONES).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and "message_id" in entry and "ts" in entry:
            out.add((entry["message_id"], entry["ts"]))
    return out


def add_tombstones(log_dir: Path, pairs) -> None:
    path = Path(log_dir) / TOMBSTONES
    with TOMBSTONE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            for message_id, ts in pairs:
                handle.write(json.dumps({"message_id": message_id, "ts": ts}, separators=(",", ":")) + "\n")


def _rewrite(path: Path, keep) -> int:
    """Rewrite one JSON-lines file keeping the lines ``keep(record)`` accepts. Returns how many lines were dropped."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0
    kept, dropped = [], 0
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            dropped += 1  # a malformed or half-written line
            continue
        if isinstance(record, dict) and keep(record):
            kept.append(line)
        else:
            dropped += 1
    if dropped:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("".join(line + "\n" for line in kept), encoding="utf-8")
        os.replace(tmp, path)
    return dropped


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
        self._path = path
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

    def prune(self, retention_days: int, now: datetime | None = None) -> dict:
        """Drop rows older than ``retention_days`` (0 keeps every age) and rows deleted from the review page.

        Run from the job's own thread between scans, so nothing else is appending. The log file is closed first and
        reopens on the next write, so no row goes to a replaced file.
        """
        log_dir = self._path.parent
        cutoff = _stamp((now or datetime.now(timezone.utc)) - timedelta(days=retention_days)) if retention_days > 0 else ""
        removed = {"old": 0, "deleted": 0, "actions": 0}
        with TOMBSTONE_LOCK:
            hidden = read_tombstones(log_dir)
            self._handler.acquire()
            try:
                if self._handler.stream:
                    self._handler.stream.close()
                    self._handler.stream = None
                for index in range(LOG_BACKUPS + 1):
                    path = self._path if index == 0 else self._path.with_name(f"{self._path.name}.{index}")

                    def keep(record):
                        if (record.get("message_id"), record.get("ts")) in hidden:
                            removed["deleted"] += 1
                            return False
                        if cutoff and str(record.get("ts") or "9") < cutoff:
                            removed["old"] += 1
                            return False
                        return True

                    _rewrite(path, keep)
                    if index and path.exists() and path.stat().st_size == 0:
                        path.unlink()
            finally:
                self._handler.release()
            actions_path = log_dir / ACTIONS
            if cutoff and actions_path.exists():
                removed["actions"] = _rewrite(
                    actions_path,
                    lambda r: r.get("action") in KEEP_FOREVER or str(r.get("ts") or "9") >= cutoff,
                )
            try:
                (log_dir / TOMBSTONES).unlink()
            except OSError:
                pass
        return removed


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
