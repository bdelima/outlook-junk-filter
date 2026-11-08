"""Counts for the Homepage widget (GET /api/stats). Counts only: no senders, subjects or message ids."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import healthcheck
from ..status import read_status
from . import logreader

KEPT = ("keep",)
QUARANTINED = ("quarantined", "would-quarantine")
DELETED = ("deleted", "permanently-deleted", "would-delete")


def _parse(ts):
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def _tally(records) -> dict:
    out = {"scored": 0, "kept": 0, "quarantined": 0, "deleted": 0, "errors": 0, "ai_rated": 0}
    for record in records:
        out["scored"] += 1
        decision = record.get("decision")
        if decision in KEPT:
            out["kept"] += 1
        elif decision in QUARANTINED:
            out["quarantined"] += 1
        elif decision in DELETED:
            out["deleted"] += 1
        elif decision == "error":
            out["errors"] += 1
        ai = record.get("ai_score")
        if isinstance(ai, (int, float)) and not isinstance(ai, bool):
            out["ai_rated"] += 1
    return out


def compute(log_dir: Path, now: datetime | None = None) -> dict:
    """Latest record per message inside each window; "today" is the container's local day."""
    now = (now or datetime.now(timezone.utc)).astimezone()
    start_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = now - timedelta(days=7)
    today, week = {}, {}
    total = 0
    for record in logreader.iter_records(log_dir):  # newest first, so the first sight of a message is its latest row
        total += 1
        moment = _parse(record.get("ts"))
        if moment is None:
            continue
        moment = moment.astimezone()
        key = record.get("message_id")
        if moment >= week_ago and key not in week:
            week[key] = record
        if moment >= start_today and key not in today:
            today[key] = record
    state_dir = Path(log_dir).parent
    status = read_status(state_dir)
    scan = _parse(status.get("last_scan")) if status.get("last_scan") else None
    return {
        "app": "SpamWatch",
        "version": status.get("version"),
        "account": status.get("account") or None,
        "mode": status.get("mode"),
        "healthy": healthcheck.healthy(state_dir / "health"),
        "last_scan_seconds_ago": max(0, int((now - scan).total_seconds())) if scan else None,
        "today": _tally(today.values()),
        "last_7_days": _tally(week.values()),
        "log_records": total,
    }
