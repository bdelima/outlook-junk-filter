"""The long-lived process: validate config, then loop over the two accounts."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from . import config as config_module
from . import scoring
from .auth import AccountAuth, AuthError
from .graph import GraphClient, GraphError, NetworkError
from .logs import DecisionLog, setup_container_log, touch_health
from .runner import AccountRunner
from .state import AccountState


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
    auths = {a: AccountAuth(a, cfg.client_id, cfg.state_dir, log, touch) for a in config_module.ACCOUNTS}
    states = {a: AccountState(a, cfg.state_dir) for a in config_module.ACCOUNTS}

    def score_fn(raw: bytes):
        return scoring.score(raw, cfg.state_dir, config_dir)

    def run_account(account: str, lint_ok: bool) -> None:
        try:
            if not lint_ok:
                log.error("%s: ERROR SpamAssassin's config does not load; scoring nothing and keeping every message", account)
                return
            graph = GraphClient(auths[account].access_token, log=log)
            AccountRunner(account, cfg, graph, states[account], decisions, log, score_fn, touch).run()
        except NetworkError as exc:
            log.error("%s: network failure, skipping this account for the run: %s", account, exc)
        except AuthError as exc:
            log.error("ERROR token refresh failed %s: %s", account, exc)
        except GraphError as exc:
            log.error("%s: Graph call failed, skipping this account for the run: %s", account, exc)
        except Exception:  # one account must never stop the loop
            log.exception("%s: unexpected error, carrying on", account)
        finally:
            touch()

    count = 0
    while True:
        touch()
        lint_ok, detail = scoring.lint(cfg.state_dir, config_dir)
        if lint_ok:
            log.info("SpamAssassin config loads cleanly")
        else:
            log.error("ERROR SpamAssassin config check failed: %s", detail)

        ready, pending = [], []
        for account in config_module.ACCOUNTS:
            result = auths[account].silent()
            if result.token:
                ready.append(account)
                continue
            if result.error:
                log.error("ERROR token refresh failed %s: %s", account, result.error)
            if result.needs_signin:
                pending.append(account)

        for account in ready:  # accounts with tokens first, so a pending sign-in never blocks them
            run_account(account, lint_ok)

        waited = False
        for account in pending:
            outcome = auths[account].sign_in()
            if outcome == "ok":
                run_account(account, lint_ok)
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
