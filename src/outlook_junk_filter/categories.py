"""Outlook categories as commands: apply one to a message and the next pass runs the matching review-page action.

Five action categories (Spam, Not Spam, Allow Sender, Allow Domain, Restore to Junk), a green done marker for each
(the name plus a checkmark), two red ones: Conflict (more than one action category) and Failure (the action failed),
and a gray "Deleted ✓" that the job puts on every message it moves to Deleted Items, so they are easy to pick out there.
Only Junk, Deleted Items and the quarantine folder are looked at. The Inbox is never read or changed, and nothing is deleted.
"""

from __future__ import annotations

from collections import Counter

from .graph import GraphError
from .ui.actions import ActionError

CHECK = "✓"
DELETED = f"Deleted {CHECK}"  # put on a message the job itself moved to Deleted Items
CONFLICT = "Conflict"
FAILURE = "Failure"
ACTIONS = {
    "Spam": "teach-spam",
    "Not Spam": "teach-ham",
    "Allow Sender": "allow-address",
    "Allow Domain": "allow-domain",
    "Restore to Junk": "restore-junk",
}
# Graph takes color presets, not names. Preset0 is red and Preset4 green in Microsoft's table. Peach is not in that
# table (unverified); Preset1 (orange) stands in until the preset Bob's Outlook shows as peach is known.
ACTION_COLOR, DONE_COLOR, PROBLEM_COLOR, DELETED_COLOR = "Preset1", "Preset4", "Preset0", "Preset12"
# marking a message one way retires the opposite teaching marker
CONTRADICTS = {"Spam": "Not Spam", "Not Spam": "Spam"}
WELL_KNOWN = ("junkemail", "inbox", "deleteditems", "sentitems", "drafts", "outbox", "archive")


def marker(name: str) -> str:
    return f"{name} {CHECK}"


def wanted_categories() -> dict:
    """All thirteen category names and their colors."""
    out = {name: ACTION_COLOR for name in ACTIONS}
    out.update({marker(name): DONE_COLOR for name in ACTIONS})
    out.update({CONFLICT: PROBLEM_COLOR, FAILURE: PROBLEM_COLOR, DELETED: DELETED_COLOR})
    return out


def _from_addr(message: dict) -> str:
    return ((message.get("from") or {}).get("emailAddress") or {}).get("address") or ""


class CategoryHooks:
    def __init__(self, cfg, graph, actions, log):
        self.cfg = cfg
        self.graph = graph
        self.actions = actions  # the review page's Actions: same code, same validation, same actions.jsonl
        self.log = log
        self._ready = False
        self._qid: str | None = None

    # ------------------------------------------------------------------ setup

    def ensure_categories(self) -> None:
        """Create any of the thirteen categories that are missing. Names must be unique, so look first. Colors of existing ones are left alone."""
        have = {(item.get("displayName") or "").casefold() for item in self.graph.master_categories()}
        for name, color in wanted_categories().items():
            if name.casefold() not in have:
                self.graph.create_master_category(name, color)
                self.log.info("created the Outlook category %r", name)
        self._ready = True

    # ------------------------------------------------------------------ folders

    def _quarantine_id(self) -> str | None:
        if self._qid is not None:
            return self._qid
        wanted = self.cfg.quarantine_folder
        if wanted.casefold() in WELL_KNOWN:
            return None
        folder = self.graph.find_folder(wanted)
        if folder is None:
            return None
        if any(self.graph.well_known_folder_id(name) == folder["id"] for name in WELL_KNOWN):
            return None  # never the Inbox or another system folder under another name
        self._qid = folder["id"]
        return self._qid

    def _folders(self) -> list:
        """(folder to query, folder name as the review page records it)."""
        out = [("junkemail", "junkemail"), ("deleteditems", "deleteditems")]
        qid = self._quarantine_id()
        if qid:
            out.append((qid, self.cfg.quarantine_folder))
        return out

    # ------------------------------------------------------------------ one pass

    def run(self) -> dict:
        if not self._ready:
            self.ensure_categories()
        counts: Counter = Counter()
        for folder, label in self._folders():
            try:
                messages = self.graph.categorized_messages(folder, ACTIONS)
            except GraphError as exc:
                self.log.error("could not look for categorized messages in %s: %s", label, exc)
                continue
            for message in messages:
                counts[self._handle(message, label)] += 1
        counts.pop("skipped", None)
        if counts:
            self.log.info("category hooks: %s", dict(counts))
        return dict(counts)

    def _set(self, message_id: str, categories: list) -> bool:
        try:
            self.graph.set_categories(message_id, categories)
            return True
        except GraphError as exc:
            self.log.error("could not update a message's categories: %s", exc)
            return False

    def _handle(self, message: dict, folder: str) -> str:
        categories = list(message.get("categories") or [])
        chosen = [name for name in categories if name in ACTIONS]
        if len(chosen) > 1:  # ambiguous: do nothing, and say so once
            if CONFLICT not in categories:
                self._set(message["id"], [*categories, CONFLICT])
                return "conflict"
            return "skipped"
        if not chosen or FAILURE in categories:  # a failed message waits until the person clears Failure
            return "skipped"
        name = chosen[0]
        base = [c for c in categories if c != CONFLICT]  # a leftover Conflict is cleared now that one category is left
        record = {
            "message_id": message["id"],
            "current_message_id": message["id"],
            "internet_message_id": message.get("internetMessageId") or "",
            "from_addr": _from_addr(message),
            "folder": folder,
        }
        try:
            new_id, done = self._perform(name, record)
        except ActionError as exc:
            self.log.warning("category action %r failed: %s", name, exc)
            self._set(message["id"], [*base, FAILURE])
            return "failed"
        drop = {name, CONFLICT, FAILURE, DELETED, marker(name)}  # a restored message is no longer "deleted"
        if name in CONTRADICTS:
            drop.add(marker(CONTRADICTS[name]))
        final = [c for c in base if c not in drop]
        if done:
            final.append(marker(name))
        self._set(new_id, final)
        self.log.info("category action %r done", name)
        return "done"

    def _perform(self, name: str, record: dict) -> tuple:
        """Run the action. Returns (the message's id now, whether to add the done marker)."""
        action = ACTIONS[name]
        if action == "teach-spam":
            self.actions.teach(record, spam=True)
        elif action == "teach-ham":
            self.actions.teach(record, spam=False)
        elif action == "allow-address":
            self.actions.allowlist("address", record["from_addr"], record["message_id"])
        elif action == "allow-domain":
            self.actions.allowlist("domain", record["from_addr"], record["message_id"])
        elif record["folder"] == "junkemail":  # already in Junk: nothing to do but drop the category
            return record["message_id"], False
        else:
            _, new_id = self.actions.restore_moved(record, "junk")
            return new_id, True  # the category change goes on the id the move returned
        return record["message_id"], True
