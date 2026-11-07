"""Job configuration, read only from environment variables.

Rules (from the build spec): an empty value counts as unset; invalid values never
cause deletion; an unrecognized mode is report-only; a mode of ``act`` that lacks a valid
delete mode or any usable threshold behaves as report-only.
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
DEFAULT_QUARANTINE_FOLDER = "Quarantined"
DEFAULT_RETENTION_DAYS = 7
DEFAULT_UI_PORT = 60888
_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


class ConfigError(Exception):
    """OJF_CLIENT_ID is missing (the only reason the process exits)."""


def _get(env, name: str) -> str | None:
    value = env.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _threshold(env, name: str):
    """Returns (value, problem). A missing threshold has no problem here; callers decide."""
    raw = _get(env, name)
    if raw is None:
        return None, None
    try:
        value = float(raw)
    except ValueError:
        return None, f"{name}={raw!r} is not a number"
    if not math.isfinite(value):
        return None, f"{name}={raw!r} is not a finite number"
    return value, None


@dataclass
class Config:
    client_id: str
    state_dir: Path = Path("/state")
    config_dir: Path = Path("/config")
    interval: int = DEFAULT_INTERVAL
    modes: dict = field(default_factory=dict)  # account -> report-only | act (as configured, valid)
    delete_mode: str | None = None
    delete_threshold: float | None = None
    keep_pattern: re.Pattern = field(default_factory=lambda: re.compile(DEFAULT_KEEP_PATTERN))
    quarantine_enabled: bool = True
    quarantine_folder: str = DEFAULT_QUARANTINE_FOLDER
    quarantine_threshold: float | None = None
    quarantine_retention_days: int = DEFAULT_RETENTION_DAYS
    ui_port: int = DEFAULT_UI_PORT
    version: str = "dev"
    # problems found while parsing; logged at startup and on every run
    problems: list = field(default_factory=list)
    # why an account configured for act would behave as report-only
    act_problems: list = field(default_factory=list)

    @property
    def quarantine_active(self) -> bool:
        """Quarantine is on and has a threshold to act on."""
        return self.quarantine_enabled and self.quarantine_threshold is not None

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
            "quarantine_enabled": self.quarantine_enabled,
            "quarantine_folder": self.quarantine_folder,
            "quarantine_threshold": self.quarantine_threshold,
            "quarantine_retention_days": self.quarantine_retention_days,
            "ui_port": self.ui_port,
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

    # --- quarantine ---------------------------------------------------------
    quarantine_enabled = True
    raw = _get(env, "OJF_QUARANTINE_ENABLED")
    if raw is not None:
        if raw.lower() in _TRUE:
            quarantine_enabled = True
        elif raw.lower() in _FALSE:
            quarantine_enabled = False
        else:
            problems.append(f"OJF_QUARANTINE_ENABLED={raw!r} is not on or off; using on")

    quarantine_folder = _get(env, "OJF_QUARANTINE_FOLDER") or DEFAULT_QUARANTINE_FOLDER

    retention = DEFAULT_RETENTION_DAYS
    raw = _get(env, "OJF_QUARANTINE_RETENTION_DAYS")
    if raw is not None:
        try:
            retention = int(raw)
            if retention < 0:
                raise ValueError
        except ValueError:
            problems.append(
                f"OJF_QUARANTINE_RETENTION_DAYS={raw!r} is not a whole number >= 0; using {DEFAULT_RETENTION_DAYS}"
            )
            retention = DEFAULT_RETENTION_DAYS

    ui_port = DEFAULT_UI_PORT
    raw = _get(env, "OJF_UI_PORT")
    if raw is not None:
        try:
            ui_port = int(raw)
            if not 1 <= ui_port <= 65535:
                raise ValueError
        except ValueError:
            problems.append(f"OJF_UI_PORT={raw!r} is not a port number; using {DEFAULT_UI_PORT}")
            ui_port = DEFAULT_UI_PORT

    # --- thresholds and delete mode -----------------------------------------
    delete_mode = None
    delete_mode_problem = None
    raw = _get(env, "OJF_DELETE_MODE")
    if raw is None:
        delete_mode_problem = "OJF_DELETE_MODE is not set"
    elif raw.lower() in DELETE_MODES:
        delete_mode = raw.lower()
    else:
        delete_mode_problem = f"OJF_DELETE_MODE={raw!r} is not deleted-items or permanent"

    delete_threshold, delete_threshold_problem = _threshold(env, "OJF_DELETE_THRESHOLD")
    quarantine_threshold, quarantine_threshold_problem = _threshold(env, "OJF_QUARANTINE_THRESHOLD")

    act_problems = []
    if delete_mode_problem:
        act_problems.append(f"{delete_mode_problem}")
    if delete_threshold_problem:
        act_problems.append(delete_threshold_problem)
    if quarantine_enabled and quarantine_threshold_problem:
        act_problems.append(quarantine_threshold_problem)
    usable_quarantine = quarantine_enabled and quarantine_threshold is not None
    if delete_threshold is None and not usable_quarantine and not (delete_threshold_problem or quarantine_threshold_problem):
        act_problems.append("neither OJF_DELETE_THRESHOLD nor OJF_QUARANTINE_THRESHOLD is set")
    if usable_quarantine and delete_threshold is not None and quarantine_threshold >= delete_threshold:
        act_problems.append(
            f"OJF_QUARANTINE_THRESHOLD={quarantine_threshold} must be lower than OJF_DELETE_THRESHOLD={delete_threshold}"
        )
    act_problems = [f"{p}; accounts in act mode behave as report-only" for p in act_problems]

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
        keep_pattern=keep_pattern,
        quarantine_enabled=quarantine_enabled,
        quarantine_folder=quarantine_folder,
        quarantine_threshold=quarantine_threshold,
        quarantine_retention_days=retention,
        ui_port=ui_port,
        version=_get(env, "APP_VERSION") or "dev",
        problems=problems,
        act_problems=act_problems,
    )
