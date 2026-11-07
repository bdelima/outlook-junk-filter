"""Scoring with SpamAssassin (the ``spamassassin`` command, not spamd)."""

from __future__ import annotations

import email
import email.policy
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

TIMEOUT_SECONDS = 120
CONFIG_DIR = "/config"


class ScoreError(Exception):
    """The message could not be scored (the caller keeps it and retries)."""


@dataclass
class ScoreResult:
    score: float
    rules: list = field(default_factory=list)
    relays_untrusted: str = ""
    first_untrusted_ip: str = ""
    auth_results_present: bool = False


def _env(state_dir: Path) -> dict:
    """No home directory is available: point HOME and TMPDIR under /state."""
    env = dict(os.environ)
    home = state_dir / "home"
    tmp = state_dir / "tmp"
    for path in (home, tmp, state_dir / "bayes"):
        path.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(home)
    env["TMPDIR"] = str(tmp)
    return env


def _command(extra: list, config_dir: str) -> list:
    return ["spamassassin", "--nocreate-prefs", "--siteconfigpath", config_dir, *extra]


def lint(state_dir: Path, config_dir: str = CONFIG_DIR) -> tuple:
    """Check that SpamAssassin's config loads. Returns (ok, detail)."""
    try:
        proc = subprocess.run(
            _command(["--lint"], config_dir),
            capture_output=True,
            timeout=TIMEOUT_SECONDS,
            env=_env(state_dir),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"could not run spamassassin --lint: {exc}"
    detail = (proc.stderr or b"").decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        return False, f"spamassassin --lint exited {proc.returncode}: {detail[-500:]}"
    return True, detail[-500:]


_SCORE_RE = re.compile(r"score=(-?\d+(?:\.\d+)?)")
_TESTS_RE = re.compile(r"tests=(.*?)(?:\s+[A-Za-z_]+=|$)")
_IP_RE = re.compile(r"ip=([0-9A-Fa-f:.]+)")


def _unfold(value) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()


def parse_output(output: bytes, raw: bytes) -> ScoreResult:
    """Read the score, fired rules and relays from SpamAssassin's output (report_safe 0)."""
    msg = email.message_from_bytes(output, policy=email.policy.compat32)
    status = msg.get("X-Spam-Status")
    if not status:
        raise ScoreError("no X-Spam-Status header in the SpamAssassin output")
    status = _unfold(status)
    score_match = _SCORE_RE.search(status)
    if not score_match:
        raise ScoreError("no score in X-Spam-Status")
    tests_match = _TESTS_RE.search(status)
    rules = []
    if tests_match:
        tests = tests_match.group(1).replace(" ,", ",").replace(", ", ",")
        rules = [t for t in (part.strip() for part in tests.split(",")) if t and t != "none"]
    relays = _unfold(msg.get("X-Spam-RelaysUntrusted", ""))
    ip_match = _IP_RE.search(relays)
    original = email.message_from_bytes(raw, policy=email.policy.compat32)
    return ScoreResult(
        score=float(score_match.group(1)),
        rules=rules,
        relays_untrusted=relays[:1000],
        first_untrusted_ip=ip_match.group(1) if ip_match else "",
        auth_results_present="Authentication-Results" in original,
    )


def score(raw: bytes, state_dir: Path, config_dir: str = CONFIG_DIR) -> ScoreResult:
    try:
        proc = subprocess.run(
            _command([], config_dir),
            input=raw,
            capture_output=True,
            timeout=TIMEOUT_SECONDS,
            env=_env(state_dir),
        )
    except subprocess.TimeoutExpired as exc:
        raise ScoreError(f"spamassassin timed out after {TIMEOUT_SECONDS}s") from exc
    except OSError as exc:
        raise ScoreError(f"could not run spamassassin: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip()
        raise ScoreError(f"spamassassin exited {proc.returncode}: {detail[-300:]}")
    return parse_output(proc.stdout, raw)
