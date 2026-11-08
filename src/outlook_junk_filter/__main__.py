"""The long-lived process: validate config, then loop over the one mailbox."""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config as config_module
from . import scoring
from .ai import AiClient
from .auth import Auth, AuthError
from .graph import GraphClient, GraphError, NetworkError
from .logs import DecisionLog, setup_container_log, touch_health
from .restored import RestoredList
from .runner import Runner
from .state import State
from .status import write_status
from .ui.actions import Actions
from .ui.server import Context, serve_in_thread

PRUNE_EVERY = 24 * 3600


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(state_dir: Path = Path("/state"), config_dir: str = scoring.CONFIG_DIR, loops: int | None = None) -> int:
    log = setup_container_log()
    try:
        cfg = config_module.load()
    except config_module.ConfigError as exc:
        print(f"outlook-junk-filter: {exc}", file=sys.stderr)
        return 2
    cfg.state_dir = Path(state_dir)

    def touch() -> None:
        touch_health(cfg.state_dir)

    log.info("outlook-junk-filter %s starting; effective settings: %s", cfg.version, cfg.describe())
    for problem in cfg.problems:
        log.error("ERROR %s", problem)

    decisions = DecisionLog(cfg.state_dir)
    auth = Auth(cfg.client_id, cfg.state_dir, log, touch)
    state = State(cfg.state_dir)
    restored = RestoredList(cfg.state_dir)
    graph = GraphClient(auth.access_token, log=log)
    ai = AiClient(cfg.ai_url, cfg.ai_model, cfg.ai_timeout) if cfg.ai_url else None
    if ai is not None:  # say now whether the AI server answers, not in the middle of the first scan
        level, detail = ai.check()
        if level == "error":
            log.error("ERROR AI scoring server %s is not usable: %s; AI scoring is skipped until it answers", cfg.ai_url, detail)
        else:
            getattr(log, level)("AI scoring server %s: %s", cfg.ai_url, detail)

    def score_fn(raw: bytes):
        return scoring.score(raw, cfg.state_dir, config_dir)

    def run_once(lint_ok: bool) -> None:
        try:
            if not lint_ok:
                log.error("ERROR SpamAssassin's config does not load; scoring nothing and keeping every message")
                return
            Runner(cfg, graph, state, decisions, log, score_fn, touch, restored=restored, ai=ai).run()
            write_status(cfg.state_dir, last_scan=_now())
        except NetworkError as exc:
            log.error("network failure, skipping this run: %s", exc)
        except AuthError as exc:
            log.error("ERROR token refresh failed: %s", exc)
        except GraphError as exc:
            log.error("Graph call failed, skipping this run: %s", exc)
        except Exception:  # an unexpected error must never stop the loop
            log.exception("unexpected error, carrying on")
        finally:
            touch()

    try:  # the review page lives in this process; if it cannot start, the job still runs
        actions = Actions(graph, restored, cfg.state_dir, Path(config_dir))
        serve_in_thread(cfg.ui_port, Context(cfg.state_dir / "logs", actions, csrf=cfg.ui_csrf))
        log.info("Review page on port %d", cfg.ui_port)
    except OSError as exc:
        log.error("ERROR the review page could not start on port %d: %s", cfg.ui_port, exc)

    write_status(cfg.state_dir, version=cfg.version, mode=cfg.effective_mode())

    def prune_logs() -> None:
        try:
            gone = decisions.prune(cfg.log_retention_days)
            if any(gone.values()):
                log.info(
                    "Pruned the logs: %d decision row(s) older than %d days, %d deleted from the review page, %d old action(s)",
                    gone["old"], cfg.log_retention_days, gone["deleted"], gone["actions"],
                )
        except Exception:  # pruning must never stop the job
            log.exception("could not prune the logs, carrying on")

    last_prune = None
    count = 0
    while True:
        touch()
        if last_prune is None or time.monotonic() - last_prune >= PRUNE_EVERY:
            prune_logs()
            last_prune = time.monotonic()
        lint_ok, detail = scoring.lint(cfg.state_dir, config_dir)
        if lint_ok:
            log.info("SpamAssassin config loads cleanly")
        else:
            log.error("ERROR SpamAssassin config check failed: %s", detail)

        waited = False
        result = auth.silent()
        if result.token:
            write_status(cfg.state_dir, account=auth.account_name())
            run_once(lint_ok)
        else:
            if result.error:
                log.error("ERROR token refresh failed: %s", result.error)
            if result.needs_signin:
                outcome = auth.sign_in()
                if outcome == "ok":
                    write_status(cfg.state_dir, account=auth.account_name())
                    run_once(lint_ok)
                elif outcome == "expired":
                    waited = True  # waiting for the code already paced this loop

        touch()
        count += 1
        if loops is not None and count >= loops:
            return 0
        if not waited:
            time.sleep(cfg.interval)


if __name__ == "__main__":
    sys.exit(main())
