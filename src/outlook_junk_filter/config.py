"""Job configuration, read only from environment variables.

Rules (from the build spec): an empty value counts as unset; invalid values never
cause deletion; an unrecognized mode is report-only; a mode of ``act`` that lacks a valid
delete mode or any usable threshold behaves as report-only.

One instance runs exactly one mailbox, so there is one mode (``OJF_MODE``).
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

MODES = ("report-only", "act")
DELETE_MODES = ("deleted-items", "permanent")
DEFAULT_INTERVAL = 900
MIN_INTERVAL = 60
DEFAULT_KEEP_PATTERN = r"^USER_IN_.*(WHITE|WELCOME)LIST"
DEFAULT_QUARANTINE_FOLDER = "Quarantined"
DEFAULT_RETENTION_DAYS = 7
DEFAULT_UI_PORT = 60888
DEFAULT_AI_MODEL = "OpenVINO/Qwen3-8B-int4-cw-ov"
DEFAULT_AI_POINTS = 4.0
DEFAULT_AI_TIMEOUT = 60
# Microsoft's own spam verdict (SCL, 0-9; 5-6 spam, 7-9 high-confidence spam) -> points added to the score
DEFAULT_SCL_POINTS = {7: 1.5, 8: 3.0, 9: 4.0}
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


def _valid_http_url(raw: str) -> bool:
    """An http(s) address with a host and, if given, a numeric port (catches typos such as host::8000)."""
    if re.search(r"\s", raw):
        return False
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    try:
        _ = parts.port  # raises ValueError for a port that is not a number in range
    except ValueError:
        return False
    return True


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
    mode: str = "report-only"  # report-only | act, as configured (a valid value)
    delete_mode: str | None = None
    delete_threshold: float | None = None
    keep_pattern: re.Pattern = field(default_factory=lambda: re.compile(DEFAULT_KEEP_PATTERN))
    quarantine_enabled: bool = True
    quarantine_folder: str = DEFAULT_QUARANTINE_FOLDER
    quarantine_threshold: float | None = None
    quarantine_retention_days: int = DEFAULT_RETENTION_DAYS
    ui_port: int = DEFAULT_UI_PORT
    ui_csrf: bool = False
    scl_points: dict = field(default_factory=lambda: dict(DEFAULT_SCL_POINTS))  # SCL value -> points; empty = off
    ai_url: str | None = None  # base URL of an OpenAI-compatible chat server; None = AI scoring off
    ai_model: str = DEFAULT_AI_MODEL
    ai_max_points: float = DEFAULT_AI_POINTS
    ai_min_score: float = 0.0  # only messages whose score so far is at or above this go to the AI
    ai_max_score: float | None = None  # ... and below this; None = the delete threshold
    ai_timeout: int = DEFAULT_AI_TIMEOUT
    version: str = "dev"
    # problems found while parsing; logged at startup and on every run
    problems: list = field(default_factory=list)
    # why a mode of act would behave as report-only
    act_problems: list = field(default_factory=list)

    @property
    def quarantine_active(self) -> bool:
        """Quarantine is on and has a threshold to act on."""
        return self.quarantine_enabled and self.quarantine_threshold is not None

    @property
    def ai_upper(self) -> float | None:
        """Messages at or above this score are already decided; None = no upper limit."""
        return self.ai_max_score if self.ai_max_score is not None else self.delete_threshold

    def ai_points(self, ai_score: float) -> float:
        """Points for an AI rating of 0-10: nothing up to 5, then up to ai_max_points at 10."""
        return round(self.ai_max_points * max(0.0, ai_score - 5.0) / 5.0, 3)

    def effective_mode(self) -> str:
        """The mode the job really runs in: act only if everything act needs is valid."""
        if self.mode == "act" and not self.act_problems:
            return "act"
        return "report-only"

    def run_problems(self) -> list:
        """Errors to log on every run."""
        out = list(self.problems)
        if self.mode == "act":
            out.extend(self.act_problems)
        return out

    def describe(self) -> dict:
        """Effective settings for the startup log. Never contains secrets."""
        return {
            "client_id": self.client_id,
            "interval_seconds": self.interval,
            "mode": self.effective_mode(),
            "delete_mode": self.delete_mode,
            "delete_threshold": self.delete_threshold,
            "quarantine_enabled": self.quarantine_enabled,
            "quarantine_folder": self.quarantine_folder,
            "quarantine_threshold": self.quarantine_threshold,
            "quarantine_retention_days": self.quarantine_retention_days,
            "ui_port": self.ui_port,
            "ui_csrf": self.ui_csrf,
            "ms_scl_points": self.scl_points,
            "ai_url": self.ai_url,
            "ai_model": self.ai_model if self.ai_url else None,
            "ai_range": [self.ai_min_score, self.ai_upper] if self.ai_url else None,
            "ai_max_points": self.ai_max_points if self.ai_url else None,
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

    mode = "report-only"
    raw = _get(env, "OJF_MODE")
    if raw is not None:
        if raw.lower() in MODES:
            mode = raw.lower()
        else:
            problems.append(f"OJF_MODE={raw!r} is not report-only or act; treating as report-only")

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

    ui_csrf = False  # off by default: the page is meant for a private LAN, behind a proxy the user controls
    raw = _get(env, "OJF_UI_CSRF")
    if raw is not None:
        if raw.lower() in _TRUE:
            ui_csrf = True
        elif raw.lower() in _FALSE:
            ui_csrf = False
        else:
            problems.append(f"OJF_UI_CSRF={raw!r} is not on or off; using off")

    scl_points = dict(DEFAULT_SCL_POINTS)
    raw = _get(env, "OJF_MS_SCL_POINTS")
    if raw is not None:
        if raw.lower() in ("off", "none", "0"):
            scl_points = {}
        else:
            try:
                parsed = {}
                for part in raw.split(","):
                    level, _, points = part.partition("=")
                    level, points = int(level.strip()), float(points.strip())
                    if not 0 <= level <= 9 or not math.isfinite(points):
                        raise ValueError
                    parsed[level] = points
                scl_points = parsed
            except ValueError:
                problems.append(
                    f"OJF_MS_SCL_POINTS={raw!r} is not like 7=1.5,8=3,9=4 (or off); using the default"
                )

    ai_url = None
    raw = _get(env, "OJF_AI_URL")
    if raw is not None:
        parts = urlsplit(raw)
        if _valid_http_url(raw):
            # a bare host:port means OpenVINO Model Server's OpenAI-style API, served under /v3
            ai_url = raw.rstrip("/") if parts.path.strip("/") else raw.rstrip("/") + "/v3"
        else:
            problems.append(f"OJF_AI_URL={raw!r} is not a valid http:// or https:// address with a host and an optional numeric port, like http://host:8000; AI scoring is off")
    ai_model = _get(env, "OJF_AI_MODEL") or DEFAULT_AI_MODEL
    ai_max_points, ai_min_score, ai_max_score, ai_timeout = DEFAULT_AI_POINTS, 0.0, None, DEFAULT_AI_TIMEOUT
    value, problem = _threshold(env, "OJF_AI_MAX_POINTS")
    if problem:
        problems.append(f"{problem}; using {DEFAULT_AI_POINTS}")
    elif value is not None:
        if value < 0:
            problems.append(f"OJF_AI_MAX_POINTS={value} is negative; using {DEFAULT_AI_POINTS}")
        else:
            ai_max_points = value
    value, problem = _threshold(env, "OJF_AI_MIN_SCORE")
    if problem:
        problems.append(f"{problem}; using 0")
    elif value is not None:
        ai_min_score = value
    value, problem = _threshold(env, "OJF_AI_MAX_SCORE")
    if problem:
        problems.append(f"{problem}; using the delete threshold")
    else:
        ai_max_score = value
    raw = _get(env, "OJF_AI_TIMEOUT_SECONDS")
    if raw is not None:
        try:
            ai_timeout = int(raw)
            if not 1 <= ai_timeout <= 600:
                raise ValueError
        except ValueError:
            problems.append(f"OJF_AI_TIMEOUT_SECONDS={raw!r} is not a whole number from 1 to 600; using {DEFAULT_AI_TIMEOUT}")
            ai_timeout = DEFAULT_AI_TIMEOUT

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
    act_problems = [f"{p}; a mode of act behaves as report-only" for p in act_problems]

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
        mode=mode,
        delete_mode=delete_mode,
        delete_threshold=delete_threshold,
        keep_pattern=keep_pattern,
        quarantine_enabled=quarantine_enabled,
        quarantine_folder=quarantine_folder,
        quarantine_threshold=quarantine_threshold,
        quarantine_retention_days=retention,
        ui_port=ui_port,
        ui_csrf=ui_csrf,
        scl_points=scl_points,
        ai_url=ai_url,
        ai_model=ai_model,
        ai_max_points=ai_max_points,
        ai_min_score=ai_min_score,
        ai_max_score=ai_max_score,
        ai_timeout=ai_timeout,
        version=_get(env, "APP_VERSION") or "dev",
        problems=problems,
        act_problems=act_problems,
    )
