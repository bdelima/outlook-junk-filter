"""Reads /state/logs/decisions.jsonl and its rotated files. Opens nothing else.

Treat every value as untrusted: senders and subjects come from phishing mail. Files are read
one at a time (at most 10 MB each), so up to 100 MB of log is never held in memory at once.
"""

from __future__ import annotations

import heapq
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

NAME = "decisions.jsonl"
ROTATED = 9
PER_PAGE = 100


@dataclass
class Stats:
    skipped: int = 0  # malformed lines


def log_files(log_dir: Path) -> list:
    """The log files, newest first: decisions.jsonl, then decisions.jsonl.1 ... .9."""
    log_dir = Path(log_dir)
    files = [log_dir / NAME] + [log_dir / f"{NAME}.{i}" for i in range(1, ROTATED + 1)]
    return [f for f in files if f.is_file()]


def iter_records(log_dir: Path, stats: Stats | None = None, newest_first: bool = True):
    """Yield records, newest first by default. Malformed and half-written lines are skipped and counted."""
    files = log_files(log_dir)
    if not newest_first:
        files.reverse()
    for path in files:
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                lines = handle.read().splitlines()
        except OSError:
            continue
        if newest_first:
            lines.reverse()
        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                if stats is not None:
                    stats.skipped += 1
                continue
            if not isinstance(record, dict):
                if stats is not None:
                    stats.skipped += 1
                continue
            yield record


@dataclass
class Filters:
    decision: str = ""
    date_from: str = ""  # YYYY-MM-DD
    date_to: str = ""
    score_min: float | None = None
    score_max: float | None = None
    q: str = ""

    def matches(self, record: dict) -> bool:
        if self.decision and record.get("decision") != self.decision:
            return False
        day = str(record.get("ts", ""))[:10]
        if self.date_from and day < self.date_from:
            return False
        if self.date_to and day > self.date_to:
            return False
        score = record.get("score")
        if self.score_min is not None or self.score_max is not None:
            if not isinstance(score, (int, float)):
                return False
            if self.score_min is not None and score < self.score_min:
                return False
            if self.score_max is not None and score > self.score_max:
                return False
        if self.q:
            needle = self.q.casefold()
            if needle not in str(record.get("from_addr", "")).casefold() and needle not in str(
                record.get("subject", "")
            ).casefold():
                return False
        return True


def _score_key(record: dict) -> float:
    score = record.get("score")
    return float(score) if isinstance(score, (int, float)) else float("-inf")


SORTS = ("time", "sender", "subject", "score", "scl", "decision")


def _value(record: dict, sort: str):
    """The value a column sorts by, or None when the record has none."""
    if sort == "score":
        value = record.get("score")
    elif sort == "scl":
        value = record.get("ms_scl")
    elif sort == "sender":
        value = str(record.get("from_addr") or "").casefold() or None
    elif sort == "subject":
        value = str(record.get("subject") or "").casefold() or None
    else:
        value = str(record.get("decision") or "") or None
    if sort in ("score", "scl"):
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    return value


@dataclass
class Page:
    rows: list = field(default_factory=list)
    total: int = 0
    skipped: int = 0


def query(log_dir: Path, filters: Filters, sort: str = "time", order: str = "desc", page: int = 1) -> Page:
    """One page of matching records. Sorting by time follows the file order; by score keeps a bounded heap."""
    stats = Stats()
    page = max(1, page)
    start, end = (page - 1) * PER_PAGE, page * PER_PAGE
    # a descending time sort is the natural newest-first order
    newest_first = not (sort == "time" and order == "asc")
    matched = (r for r in iter_records(log_dir, stats, newest_first) if filters.matches(r))
    if sort in SORTS and sort != "time":
        total = 0

        def counted():
            nonlocal total
            for record in matched:
                total += 1
                yield record

        empty = "" if sort not in ("score", "scl") else 0.0
        if order == "desc":  # records without a value sort last
            ranked = heapq.nlargest(
                end, counted(), key=lambda r: (_value(r, sort) is not None, _value(r, sort) if _value(r, sort) is not None else empty)
            )
        else:
            ranked = heapq.nsmallest(
                end, counted(), key=lambda r: (_value(r, sort) is None, _value(r, sort) if _value(r, sort) is not None else empty)
            )
        return Page(ranked[start:end], total, stats.skipped)
    rows, total = [], 0
    for index, record in enumerate(matched):
        total += 1
        if start <= index < end:
            rows.append(record)
    return Page(rows, total, stats.skipped)


def all_matching(log_dir: Path, filters: Filters):
    """Every matching record, newest first (for the CSV export)."""
    return (r for r in iter_records(log_dir) if filters.matches(r))


def find(log_dir: Path, message_id: str, ts: str) -> dict | None:
    """The record for one message at one time (a message can have several records)."""
    for record in iter_records(log_dir):
        if record.get("message_id") == message_id and record.get("ts") == ts:
            return record
    return None


def find_many(log_dir: Path, keys) -> dict:
    """The records for several (message_id, ts) pairs in one pass over the log; a pair that is not logged is absent."""
    wanted = set(keys)
    found = {}
    for record in iter_records(log_dir):
        key = (record.get("message_id"), record.get("ts"))
        if key in wanted and key not in found:
            found[key] = record
            if len(found) == len(wanted):
                break
    return found


def header(log_dir: Path, now: datetime | None = None) -> dict:
    """Records loaded, oldest and newest timestamps, and the age of the last record in seconds."""
    stats = Stats()
    count, oldest, newest = 0, "", ""
    for record in iter_records(log_dir, stats):
        ts = str(record.get("ts", ""))
        count += 1
        if not newest or ts > newest:
            newest = ts
        if not oldest or (ts and ts < oldest):
            oldest = ts
    age = None
    if newest:
        try:
            last = datetime.strptime(newest, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            age = int(((now or datetime.now(timezone.utc)) - last).total_seconds())
        except ValueError:
            age = None
    return {"records": count, "oldest": oldest, "newest": newest, "age_seconds": age, "skipped": stats.skipped}


def summary(log_dir: Path) -> dict:
    """Counts per decision per day, and a whole-number score histogram."""
    stats = Stats()
    per_day: dict = {}
    hist: dict = {}
    for record in iter_records(log_dir, stats):
        decision = str(record.get("decision", ""))
        day = str(record.get("ts", ""))[:10]
        per_day.setdefault(day, {}).setdefault(decision, 0)
        per_day[day][decision] += 1
        score = record.get("score")
        if isinstance(score, (int, float)):
            bucket = int(score // 1)
            hist[bucket] = hist.get(bucket, 0) + 1
    return {
        "per_scl": _per_scl(log_dir),
        "per_day": dict(sorted(per_day.items(), reverse=True)),
        "histogram": dict(sorted(hist.items())),
        "skipped": stats.skipped,
    }


def _per_scl(log_dir: Path) -> dict:
    """Microsoft SCL -> (messages, average score, highest score), one (latest) record per message; unknown last."""
    latest: dict = {}
    for record in iter_records(log_dir):  # newest first: the first record seen for a message is the latest
        score = record.get("score")
        if isinstance(score, (int, float)) and record.get("message_id") not in latest:
            latest[record.get("message_id")] = (record.get("ms_scl"), float(score))
    groups: dict = {}
    for scl, score in latest.values():
        groups.setdefault(scl, []).append(score)
    order = sorted((k for k in groups if k is not None), reverse=True) + ([None] if None in groups else [])
    return {
        ("none" if k is None else k): (len(groups[k]), sum(groups[k]) / len(groups[k]), max(groups[k])) for k in order
    }


def what_if(log_dir: Path, quarantine_at: float | None, delete_at: float | None) -> dict:
    """How the logged messages would be sorted by a pair of thresholds. Saves nothing.

    Uses the score each message had when it was scored and its logged allowlist hit, one
    (latest) record per message.
    """
    latest: dict = {}
    for record in iter_records(log_dir):  # newest first: the first record seen is the latest
        score = record.get("score")
        if not isinstance(score, (int, float)):
            continue
        key = record.get("message_id")
        if key not in latest:
            latest[key] = (float(score), bool(record.get("keep_rule_hit")))
    counts = {"kept": 0, "quarantined": 0, "deleted": 0, "allowlisted": 0}
    for score, allowlisted in latest.values():
        if allowlisted:
            counts["allowlisted"] += 1
            counts["kept"] += 1
        elif delete_at is not None and score >= delete_at:
            counts["deleted"] += 1
        elif quarantine_at is not None and score >= quarantine_at:
            counts["quarantined"] += 1
        else:
            counts["kept"] += 1
    counts["total"] = len(latest)
    return counts
