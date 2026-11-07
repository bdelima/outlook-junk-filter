"""Actions the review page can take on one message. Each is recorded in /state/logs/actions.jsonl.

Graph calls go through the job's own client and token cache (Mail.ReadWrite, no new permission).
Nothing here edits SpamAssassin's Bayes database directly: ``sa-learn`` does that, with its own locking.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from ..config import ACCOUNTS
from ..graph import GraphError, NetworkError

ALLOWLIST_FILE = "ojf-allowlist.cf"
ACTIONS_LOG = "actions.jsonl"
LEARN_TIMEOUT = 120

_ADDRESS = re.compile(r"^[a-z0-9._%+'-]{1,64}@(?:[a-z0-9-]{1,63}\.)+[a-z]{2,24}$")
_DOMAIN = re.compile(r"^(?:[a-z0-9-]{1,63}\.)+[a-z]{2,24}$")
RESTORABLE_FROM_EXCLUDED = ("junkemail", "inbox", "")
DESTINATIONS = {"junk": "junkemail", "inbox": "inbox"}


class ActionError(Exception):
    """A refused or failed action; the message is safe to show to the user."""


def can_teach(record: dict) -> bool:
    """A message can be taught while it still exists in Outlook (a permanent delete leaves nothing)."""
    return bool(record.get("current_message_id")) and record.get("folder") not in ("", None)


def can_restore(record: dict) -> bool:
    """Only from the quarantine folder or Deleted Items; not from Junk, the Inbox or a permanent delete."""
    return can_teach(record) and record.get("folder") not in RESTORABLE_FROM_EXCLUDED


class Actions:
    def __init__(self, graph_for, restored, state_dir: Path, config_dir: Path, run=subprocess.run, now=None):
        self._graph_for = graph_for
        self._restored = restored
        self._state = Path(state_dir)
        self._config = Path(config_dir)
        self._run = run
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._allow_lock = threading.Lock()
        self._log_lock = threading.Lock()

    # ------------------------------------------------------------------ the actions log

    def _record(self, action: str, account: str, message_id: str, target: str) -> None:
        line = json.dumps(
            {
                "ts": self._now().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "action": action,
                "account": account,
                "message_id": message_id,
                "target": target,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        path = self._state / "logs" / ACTIONS_LOG
        with self._log_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    @staticmethod
    def _account(account: str) -> str:
        if account not in ACCOUNTS:
            raise ActionError("Unknown account.")
        return account

    # ------------------------------------------------------------------ teach Bayes

    def teach(self, account: str, record: dict, spam: bool) -> str:
        account = self._account(account)
        if not can_teach(record):
            raise ActionError("That message is no longer in Outlook (it was permanently deleted), so it cannot be taught.")
        message_id = record["current_message_id"]
        try:
            raw = self._graph_for(account).raw(message_id)
        except GraphError as exc:
            if exc.status == 404:
                raise ActionError("That message is no longer in Outlook, so it cannot be taught.") from exc
            raise ActionError(f"Outlook refused the request: {exc}") from exc
        except NetworkError as exc:
            raise ActionError("Could not reach Microsoft Graph.") from exc
        env = dict(os.environ)
        home = self._state / "home"
        tmp = self._state / "tmp"
        for path in (home, tmp, self._state / "bayes"):
            path.mkdir(parents=True, exist_ok=True)
        env["HOME"], env["TMPDIR"] = str(home), str(tmp)
        command = ["sa-learn", "--spam" if spam else "--ham", "--single", "--siteconfigpath", str(self._config)]
        try:
            proc = self._run(command, input=raw, capture_output=True, timeout=LEARN_TIMEOUT, env=env)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ActionError("sa-learn could not run.") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or b"").decode("utf-8", "replace").strip()[-200:]
            raise ActionError(f"sa-learn failed: {detail or 'exit ' + str(proc.returncode)}")
        label = "spam" if spam else "not-spam"
        self._record(f"teach-{label}", account, message_id, label)
        return f"Taught Bayes: {label}."

    # ------------------------------------------------------------------ allowlist

    def allowlist(self, account: str, kind: str, from_addr: str) -> str:
        """Add a sender address or its domain as a whitelist_auth line (SPF or DKIM must pass for it to count)."""
        account = self._account(account)
        address = (from_addr or "").strip().lower()
        if not _ADDRESS.match(address):
            raise ActionError("That sender address cannot be allowlisted (it is not a plain address).")
        if kind == "domain":
            entry = "*@" + address.split("@", 1)[1]
            if not _DOMAIN.match(entry[2:]):
                raise ActionError("That domain cannot be allowlisted.")
        elif kind == "address":
            entry = address
        else:
            raise ActionError("Choose the address or the domain.")
        line = f"whitelist_auth {entry}"
        path = self._config / ALLOWLIST_FILE
        with self._allow_lock:
            try:
                existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
            except OSError as exc:
                raise ActionError("Could not read the allowlist file.") from exc
            if line not in existing:
                if not existing:
                    existing = ["# Added from the review page. Each entry counts only when SPF or DKIM passes."]
                existing.append(line)
                tmp = path.with_suffix(".tmp")
                try:
                    tmp.write_text("\n".join(existing) + "\n", encoding="utf-8")
                    os.replace(tmp, path)
                except OSError as exc:
                    raise ActionError("Could not write the allowlist file (is /config writable?).") from exc
        self._record("allowlist", account, "", entry)
        return f"Allowlisted {entry}. It takes effect on the next scoring pass."

    # ------------------------------------------------------------------ restore

    def restore(self, account: str, record: dict, destination: str) -> str:
        account = self._account(account)
        if destination not in DESTINATIONS:
            raise ActionError("Choose Junk or Inbox.")
        if not can_restore(record):
            raise ActionError("Only a message in the quarantine folder or Deleted Items can be restored.")
        message_id = record["current_message_id"]
        try:
            self._graph_for(account).move(message_id, DESTINATIONS[destination])
        except GraphError as exc:
            if exc.status == 404:
                raise ActionError("That message is no longer in Outlook, so it cannot be restored.") from exc
            raise ActionError(f"Outlook refused the request: {exc}") from exc
        except NetworkError as exc:
            raise ActionError("Could not reach Microsoft Graph.") from exc
        # the job must not rescore it and move it straight back
        self._restored.add(record.get("internet_message_id") or "")
        self._record("restore", account, message_id, destination)
        return f"Restored to {destination.capitalize()}. The job will keep it."
