"""What has already been done from the review page, read back from /state/logs/actions.jsonl.

Teach and restore are recorded against the message's id in Outlook; an allowlist entry is recorded as the address
or ``*@domain`` that was added, so any row from that sender shows it.
"""

from __future__ import annotations

import json
from pathlib import Path

NAME = "actions.jsonl"


class History:
    def __init__(self, entries=()):
        self.teach: dict = {}  # message id -> (label, ts)
        self.restore: dict = {}  # message id -> (destination, ts)
        self.allowed: dict = {}  # "address" or "*@domain" -> ts
        for entry in entries:
            action, message_id, target, ts = (entry.get(k) or "" for k in ("action", "message_id", "target", "ts"))
            if action.startswith("teach-"):
                self.teach[message_id] = (target, ts)
            elif action == "restore":
                self.restore[message_id] = (target, ts)
            elif action == "allowlist":
                self.allowed[target.lower()] = ts

    def allow_status(self, record: dict) -> dict:
        """When the sender's address and domain were allowlisted (None when not)."""
        address = str(record.get("from_addr") or "").strip().lower()
        domain = "*@" + address.split("@", 1)[1] if "@" in address else ""
        return {"address": self.allowed.get(address) if address else None, "domain": self.allowed.get(domain) if domain else None}

    def chips(self, record: dict) -> list:
        """(label, kind, ts) for each action done on this row's message or sender."""
        out = []
        message_id = record.get("current_message_id") or ""
        if message_id in self.teach:
            label, ts = self.teach[message_id]
            out.append(("Spam" if label == "spam" else "Not spam", "spam" if label == "spam" else "ham", ts))
        if message_id in self.restore:
            destination, ts = self.restore[message_id]
            out.append((f"Restored · {destination.capitalize()}", "restore", ts))
        status = self.allow_status(record)
        if status["address"]:
            out.append(("Address", "allow", status["address"]))
        if status["domain"]:
            out.append(("Domain", "allow", status["domain"]))
        return out


def load(log_dir: Path) -> History:
    path = Path(log_dir) / NAME
    entries = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return History()
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return History(entries)
