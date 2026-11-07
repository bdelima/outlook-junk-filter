"""One run for one account: walk the Junk delta, score, decide, act or only record."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from .graph import GraphError, NetworkError
from .logs import SUBJECT_MAX, histogram
from .scoring import ScoreError
from .state import MAX_ATTEMPTS

PROGRESS_EVERY = 25


def _from_addr(message: dict) -> str:
    return ((message.get("from") or {}).get("emailAddress") or {}).get("address") or ""


class AccountRunner:
    def __init__(self, account, cfg, graph, state, decisions, log, score_fn, touch=lambda: None):
        self.account = account
        self.cfg = cfg
        self.graph = graph
        self.state = state
        self.decisions = decisions
        self.log = log
        self.score_fn = score_fn
        self.touch = touch

    # ------------------------------------------------------------------ records

    def _record(self, message, mode, decision, reason, result=None, keep_rule_hit=False, action_error=""):
        self.decisions.write(
            {
                "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
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

    # ------------------------------------------------------------------ one message

    def _process(self, message, mode, counts, scores, runstate) -> None:
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
        threshold = self.cfg.delete_threshold

        if keep_rule_hit:
            self._record(message, mode, "keep", "allowlisted", result, keep_rule_hit=True)
            self.state.mark_handled(message)
            counts["keep"] += 1
            return
        if threshold is None or result.score < threshold:
            reason = "below threshold" if threshold is not None else "no threshold set"
            self._record(message, mode, "keep", reason, result)
            self.state.mark_handled(message)
            counts["keep"] += 1
            return

        # the score crossed the threshold
        if mode != "act":
            self._record(message, mode, "would-delete", "score at or above threshold (report-only)", result)
            self.state.mark_handled(message)
            counts["would-delete"] += 1
            return
        cap = self.cfg.max_deletes
        if cap is not None and runstate["deleted"] >= cap:
            if not runstate["cap_warned"]:
                self.log.warning(
                    "%s: reached OJF_MAX_DELETES_PER_RUN=%d; the rest that crossed the threshold wait for the next run",
                    self.account,
                    cap,
                )
                runstate["cap_warned"] = True
            self.state.add_retry(message, "deletion cap reached", count_attempt=False)
            self._record(message, mode, "keep", "deferred: deletion cap reached", result)
            counts["keep"] += 1
            return
        try:
            if self.cfg.delete_mode == "permanent":
                self.graph.permanent_delete(message["id"])
                decision = "permanently-deleted"
            else:
                self.graph.move_to_deleted_items(message["id"])
                decision = "deleted"
        except NetworkError:
            raise
        except GraphError as exc:
            self.log.error("%s: could not delete a message: %s", self.account, exc)
            self._fail(message, mode, "delete failed", counts, result, action_error=str(exc))
            return
        runstate["deleted"] += 1
        self._record(message, mode, decision, "score at or above threshold", result)
        self.state.mark_handled(message)
        counts[decision] += 1

    # ------------------------------------------------------------------ one run

    def run(self) -> dict:
        mode = self.cfg.effective_mode(self.account)
        for problem in self.cfg.run_problems(self.account):
            self.log.error("%s: ERROR %s", self.account, problem)

        folder = self.graph.folder()
        folder_count = folder.get("totalItemCount")
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
        runstate = {"deleted": 0, "cap_warned": False}
        total = len(queue)
        done = 0
        try:
            for message in queue:
                self._process(message, mode, counts, scores, runstate)
                done += 1
                if done % PROGRESS_EVERY == 0 or done == total:
                    self.log.info("%s: %d done, %d left", self.account, done, total - done)
                    self.state.save()
                    self.touch()
            # every message is now handled or on the retry list: safe to move the delta link on
            self.state.delta_link = new_link
        finally:
            self.state.save()

        self.log.info(
            "%s: run summary: %s; Junk folder count %s; score histogram %s",
            self.account,
            dict(counts) or "nothing new",
            folder_count,
            histogram(scores),
        )
        return {"counts": dict(counts), "folder_count": folder_count, "histogram": histogram(scores)}
