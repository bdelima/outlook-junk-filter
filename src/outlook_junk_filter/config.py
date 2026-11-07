"""Job configuration, read only from environment variables.

Rules (from the build spec): an empty value counts as unset; invalid values never
cause deletion; an unrecognized mode is report-only; a mode of ``act`` with a missing
or invalid OJF_DELETE_MODE or OJF_DELETE_THRESHOLD behaves as report-only.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

ACCOUNTS = ("live", "outlook")
MODES = ("report-only", "act")
DELETE_MODES = ("deleted-items", "permanent")
DEFAULT_INTERVAL = 900
MIN_INTERVAL = 60
DEFAULT_KEEP_PATTERN = r"^USER_IN_.*(WHITE|WELCOME)LIST"


class ConfigError(Exception):
    """OJF_CLIENT_ID is missing (the only reason the process exits)."""


def _get(env, name: str) -> str | None:
    value = env.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


@dataclass
class Config:
    client_id: str
    state_dir: Path = Path("/state")
    config_dir: Path = Path("/config")
    interval: int = DEFAULT_INTERVAL
    modes: dict = field(default_factory=dict)  # account -> report-only | act (as configured, valid)
    delete_mode: str | None = None
    delete_threshold: float | None = None
    max_deletes: int | None = None
    keep_pattern: re.Pattern = field(default_factory=lambda: re.compile(DEFAULT_KEEP_PATTERN))
    version: str = "dev"
    # problems found while parsing; logged at startup and on every run
    problems: list = field(default_factory=list)
    # accounts whose act mode was downgraded because the delete settings are unusable
    act_problems: list = field(default_factory=list)

    def effective_mode(self, account: str) -> str:
        """The mode the account really runs in: act only if everything act needs is valid."""
        if self.modes.get(account) == "act" and not self.act_problems:
            return "act"
        return "report-only"

    def run_problems(self, account: str) -> list:
        """Errors to log on every run for this account."""
        out = list(self.problems)
        if self.modes.get(account) == "act":
            out.extend(self.act_problems)
        return out

    def describe(self) -> dict:
        """Effective settings for the startup log. Never contains secrets."""
        return {
            "client_id": self.client_id,
            "interval_seconds": self.interval,
            "mode_live": self.effective_mode("live"),
            "mode_outlook": self.effective_mode("outlook"),
            "delete_mode": self.delete_mode,
            "delete_threshold": self.delete_threshold,
            "max_deletes_per_run": self.max_deletes,
            "keep_rule_pattern": self.keep_pattern.pattern,
            "version": self.version,
        }


def load(env=None) -> Config:
    env = os.environ if env is None else env
    client_id = _get(env, "OJF_CLIENT_ID")
    if not client_id:
        raise ConfigError("OJF_CLIENT_ID is not set: it is the Application (client) ID of the app registration")

    problems: list = []

    interval = DEFAULT_INTERVAL
    raw = _get(env, "OJF_INTERVAL_SECONDS")
    if raw is not None:
        try:
            interval = int(raw)
        except ValueError:
            problems.append(f"OJF_INTERVAL_SECONDS={raw!r} is not a whole number; using {DEFAULT_INTERVAL}")
            interval = DEFAULT_INTERVAL
        if interval < MIN_INTERVAL:
            problems.append(f"OJF_INTERVAL_SECONDS={interval} is below {MIN_INTERVAL}; using {MIN_INTERVAL}")
            interval = MIN_INTERVAL

    modes = {}
    for account in ACCOUNTS:
        name = f"OJF_MODE_{account.upper()}"
        raw = _get(env, name)
        if raw is None:
            modes[account] = "report-only"
        elif raw.lower() in MODES:
            modes[account] = raw.lower()
        else:
            problems.append(f"{name}={raw!r} is not report-only or act; treating as report-only")
            modes[account] = "report-only"

    delete_mode = None
    raw_delete_mode = _get(env, "OJF_DELETE_MODE")
    delete_mode_problem = None
    if raw_delete_mode is not None:
        if raw_delete_mode.lower() in DELETE_MODES:
            delete_mode = raw_delete_mode.lower()
        else:
            delete_mode_problem = f"OJF_DELETE_MODE={raw_delete_mode!r} is not deleted-items or permanent"
    else:
        delete_mode_problem = "OJF_DELETE_MODE is not set"

    delete_threshold = None
    raw_threshold = _get(env, "OJF_DELETE_THRESHOLD")
    threshold_problem = None
    if raw_threshold is not None:
        try:
            value = float(raw_threshold)
            if math.isfinite(value):
                delete_threshold = value
            else:
                threshold_problem = f"OJF_DELETE_THRESHOLD={raw_threshold!r} is not a finite number"
        except ValueError:
            threshold_problem = f"OJF_DELETE_THRESHOLD={raw_threshold!r} is not a number"
    else:
        threshold_problem = "OJF_DELETE_THRESHOLD is not set"

    act_problems = []
    if delete_mode_problem:
        act_problems.append(f"{delete_mode_problem}; accounts in act mode behave as report-only")
    if threshold_problem:
        act_problems.append(f"{threshold_problem}; accounts in act mode behave as report-only")

    max_deletes = None
    raw = _get(env, "OJF_MAX_DELETES_PER_RUN")
    if raw is not None:
        try:
            max_deletes = int(raw)
            if max_deletes < 0:
                raise ValueError
        except ValueError:
            problems.append(f"OJF_MAX_DELETES_PER_RUN={raw!r} is not a whole number >= 0; ignoring it (no cap)")
            max_deletes = None

    keep_pattern = re.compile(DEFAULT_KEEP_PATTERN)
    raw = _get(env, "OJF_KEEP_RULE_PATTERN")
    if raw is not None:
        try:
            keep_pattern = re.compile(raw)
        except re.error as exc:
            problems.append(f"OJF_KEEP_RULE_PATTERN is not a valid regular expression ({exc}); using the default")

    return Config(
        client_id=client_id,
        interval=interval,
        modes=modes,
        delete_mode=delete_mode,
        delete_threshold=delete_threshold,
        max_deletes=max_deletes,
        keep_pattern=keep_pattern,
        version=_get(env, "APP_VERSION") or "dev",
        problems=problems,
        act_problems=act_problems,
    )
