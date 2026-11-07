"""One run for one account: walk the Junk delta, score, decide, act or only record.

Decision order: allowlist, scoring failure, delete threshold, quarantine threshold, keep.
After the Junk pass, quarantined messages past the retention period are deleted.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

from .graph import GraphError, NetworkError
from .logs import SUBJECT_MAX, histogram
from .scoring import ScoreError
from .state import MAX_ATTEMPTS

PROGRESS_EVERY = 25
JUNK = "junkemail"
# Graph's well-known folder names. A quarantine folder must not be one of these, by name or by id.
WELL_KNOWN = ("junkemail", "inbox", "deleteditems", "sentitems", "drafts", "outbox", "archive")


def _from_addr(message: dict) -> str:
    return ((message.get("from") or {}).get("emailAddress") or {}).get("address") or ""


class AccountRunner:
    def __init__(self, account, cfg, graph, state, decisions, log, score_fn, touch=lambda: None, now=None, restored=None):
        self.account = account
        self.cfg = cfg
        self.graph = graph
        self.state = state
        self.decisions = decisions
        self.log = log
        self.score_fn = score_fn
        self.touch = touch
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.restored = restored  # messages restored from the review page: always kept
        self._qid: str | None = None  # the quarantine folder's id once resolved

    # ------------------------------------------------------------------ records

    def _record(
        self,
        message,
        mode,
        decision,
        reason,
        result=None,
        keep_rule_hit=False,
        action_error="",
        current_message_id=None,
        folder=None,
    ):
        """Write the per-message record.

        ``current_message_id`` and ``folder`` say where the message is now: Graph gives a moved message a
        new id, so the review page needs them to find it later.
        """
        self.decisions.write(
            {
                "ts": self.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "account": self.account,
                "message_id": message["id"],
                "internet_message_id": message.get("internetMessageId") or "",
                "received": message.get("receivedDateTime") or "",
                "from_addr": _from_addr(message),
                "subject": (message.get("subject") or "")[:SUBJECT_MAX],
                "score": result.score if result else None,
                "rules": result.rules if result else [],
                "keep_rule_hit": keep_rule_hit,
                "relays_untrusted": result.relays_untrusted if result else "",
                "first_untrusted_ip": result.first_untrusted_ip if result else "",
                "auth_results_present": result.auth_results_present if result else False,
                "mode": mode,
                "decision": decision,
                "reason": reason,
                "action_error": action_error,
                "current_message_id": message["id"] if current_message_id is None else current_message_id,
                "folder": JUNK if folder is None else folder,
            }
        )

    def _fail(self, message, mode, reason, counts, result=None, action_error=""):
        """Keep the message and put it on the retry list; give up after MAX_ATTEMPTS."""
        attempts = self.state.add_retry(message, reason)
        if attempts >= MAX_ATTEMPTS:
            self.log.error("%s: gave up on a message after %d attempts (%s); it stays in Junk", self.account, attempts, reason)
            self._record(message, mode, "keep", f"gave up after {attempts} attempts: {reason}", result, action_error=action_error)
            self.state.mark_handled(message)
            counts["keep"] += 1
        else:
            self.log.warning("%s: %s (attempt %d of %d); kept and will retry", self.account, reason, attempts, MAX_ATTEMPTS)
            self._record(message, mode, "error", reason, result, action_error=action_error)
            counts["error"] += 1

    # ------------------------------------------------------------------ quarantine folder

    def _check_quarantine_folder(self, junk_name: str) -> str | None:
        """Why the configured quarantine folder cannot be used, or None if it can."""
        wanted = self.cfg.quarantine_folder.casefold()
        if wanted in WELL_KNOWN or wanted == (junk_name or "").casefold():
            return f"OJF_QUARANTINE_FOLDER={self.cfg.quarantine_folder!r} is a system folder (or Junk itself)"
        folder = self.graph.find_folder(self.cfg.quarantine_folder)
        if folder is None:
            return None  # created on first use
        for name in WELL_KNOWN:
            if self.graph.well_known_folder_id(name) == folder["id"]:
                return f"OJF_QUARANTINE_FOLDER={self.cfg.quarantine_folder!r} resolves to the {name} folder"
        self._qid = folder["id"]
        return None

    def _quarantine_folder_id(self) -> str:
        if self._qid is None:
            folder = self.graph.create_folder(self.cfg.quarantine_folder)
            self._qid = folder["id"]
            self.log.info("%s: created the %r folder", self.account, self.cfg.quarantine_folder)
        return self._qid

    # ------------------------------------------------------------------ one message

    def _delete(self, message_id: str) -> tuple:
        """Delete as OJF_DELETE_MODE says. Returns (decision, new message id, folder)."""
        if self.cfg.delete_mode == "permanent":
            self.graph.permanent_delete(message_id)
            return "permanently-deleted", "", ""
        new_id = self.graph.move_to_deleted_items(message_id)
        return "deleted", new_id or message_id, "deleteditems"

    def _process(self, message, mode, counts, scores) -> None:
        # 0. a message restored from the review page is kept and not rescored
        if self.restored is not None and self.restored.contains(message.get("internetMessageId") or ""):
            self._record(message, mode, "keep", "restored")
            self.state.mark_handled(message)
            counts["keep"] += 1
            return
        try:
            raw = self.graph.raw(message["id"])
        except NetworkError:
            raise
        except GraphError as exc:
            if exc.status == 404:
                self.log.info("%s: a message left Junk before it was fetched; skipping it", self.account)
                self.state.retry.pop(message["id"], None)
                return
            self._fail(message, mode, f"fetch failed: {exc}", counts)
            return
        try:
            result = self.score_fn(raw)
        except ScoreError as exc:
            self._fail(message, mode, f"scoring failed: {exc}", counts)
            return

        scores.append(result.score)
        keep_rule_hit = any(self.cfg.keep_pattern.search(rule) for rule in result.rules)

        # 1. allowlist
        if keep_rule_hit:
            self._record(message, mode, "keep", "allowlisted", result, keep_rule_hit=True)
            self.state.mark_handled(message)
            counts["keep"] += 1
            return

        delete_at = self.cfg.delete_threshold
        quarantine_at = self.cfg.quarantine_threshold if self.cfg.quarantine_enabled else None

        # 3a. delete threshold
        if delete_at is not None and result.score >= delete_at:
            if mode != "act":
                self._record(message, mode, "would-delete", "score at or above the delete threshold (report-only)", result)
                self.state.mark_handled(message)
                counts["would-delete"] += 1
                return
            try:
                decision, new_id, folder = self._delete(message["id"])
            except NetworkError:
                raise
            except GraphError as exc:
                self.log.error("%s: could not delete a message: %s", self.account, exc)
                self._fail(message, mode, "delete failed", counts, result, action_error=str(exc))
                return
            self._record(message, mode, decision, "score at or above the delete threshold", result,
                         current_message_id=new_id, folder=folder)
            self.state.mark_handled(message)
            counts[decision] += 1
            return

        # 3b. quarantine threshold
        if quarantine_at is not None and result.score >= quarantine_at:
            if mode != "act":
                self._record(message, mode, "would-quarantine", "score at or above the quarantine threshold (report-only)", result)
                self.state.mark_handled(message)
                counts["would-quarantine"] += 1
                return
            try:
                new_id = self.graph.move(message["id"], self._quarantine_folder_id())
            except NetworkError:
                raise
            except GraphError as exc:
                self.log.error("%s: could not quarantine a message: %s", self.account, exc)
                self._fail(message, mode, "quarantine failed", counts, result, action_error=str(exc))
                return
            self._record(message, mode, "quarantined", "score at or above the quarantine threshold", result,
                         current_message_id=new_id or message["id"], folder=self.cfg.quarantine_folder)
            self.state.mark_handled(message)
            counts["quarantined"] += 1
            return

        # 4. keep
        if delete_at is None and quarantine_at is None:
            reason = "no threshold set"
        else:
            reason = "below threshold"
        self._record(message, mode, "keep", reason, result)
        self.state.mark_handled(message)
        counts["keep"] += 1

    # ------------------------------------------------------------------ quarantine expiry

    def _expire_quarantine(self, mode, counts) -> None:
        """Delete quarantined messages past the retention period. Age counts from the received time."""
        if mode != "act" or not self.cfg.quarantine_enabled:
            return
        folder_id = self._qid
        if folder_id is None:
            return  # nothing has ever been quarantined here
        cutoff = (self.now() - timedelta(days=self.cfg.quarantine_retention_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for message in self.graph.old_messages(folder_id, cutoff):
            try:
                decision, new_id, folder = self._delete(message["id"])
            except NetworkError:
                raise
            except GraphError as exc:
                self.log.error("%s: could not delete an expired quarantined message: %s", self.account, exc)
                self._record(message, mode, "error", "quarantine expiry failed", action_error=str(exc),
                             current_message_id=message["id"], folder=self.cfg.quarantine_folder)
                counts["error"] += 1
                continue
            self._record(message, mode, decision, f"quarantine retention of {self.cfg.quarantine_retention_days} days passed",
                         current_message_id=new_id, folder=folder)
            counts[decision] += 1

    # ------------------------------------------------------------------ one run

    def run(self) -> dict:
        mode = self.cfg.effective_mode(self.account)
        for problem in self.cfg.run_problems(self.account):
            self.log.error("%s: ERROR %s", self.account, problem)

        folder = self.graph.folder()
        folder_count = folder.get("totalItemCount")

        if mode == "act" and self.cfg.quarantine_enabled:
            problem = self._check_quarantine_folder(folder.get("displayName") or "")
            if problem:
                self.log.error("%s: ERROR %s; behaving as report-only", self.account, problem)
                mode = "report-only"

        self.log.info(
            "%s: Junk folder is %r and holds %s messages (mode %s)",
            self.account,
            folder.get("displayName"),
            folder_count,
            mode,
        )

        messages, new_link = self.graph.delta(self.state.delta_link)

        queue, seen = [], set()
        for entry in list(self.state.retry.values()):
            queue.append(entry["message"])
            seen.add(entry["message"]["id"])
        for message in messages:
            if message["id"] in seen or self.state.is_handled(message):
                continue
            seen.add(message["id"])
            queue.append(message)

        counts: Counter = Counter()
        scores: list = []
        total = len(queue)
        done = 0
        try:
            for message in queue:
                self._process(message, mode, counts, scores)
                done += 1
                if done % PROGRESS_EVERY == 0 or done == total:
                    self.log.info("%s: %d done, %d left", self.account, done, total - done)
                    self.state.save()
                    self.touch()
            # every message is now handled or on the retry list: safe to move the delta link on
            self.state.delta_link = new_link
        finally:
            self.state.save()

        self._expire_quarantine(mode, counts)

        self.log.info(
            "%s: run summary: %s; Junk folder count %s; score histogram %s",
            self.account,
            dict(counts) or "nothing new",
            folder_count,
            histogram(scores),
        )
        return {"counts": dict(counts), "folder_count": folder_count, "histogram": histogram(scores)}
