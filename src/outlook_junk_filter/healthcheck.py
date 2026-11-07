"""Container health check: healthy if /state/health was touched within the larger of
three intervals and 30 minutes."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path


def window(env=None) -> int:
    env = os.environ if env is None else env
    try:
        interval = max(60, int((env.get("OJF_INTERVAL_SECONDS") or "900").strip()))
    except ValueError:
        interval = 900
    return max(3 * interval, 30 * 60)


def healthy(path: Path, now: float | None = None, env=None) -> bool:
    try:
        age = (time.time() if now is None else now) - Path(path).stat().st_mtime
    except OSError:
        return False
    return age <= window(env)


if __name__ == "__main__":
    sys.exit(0 if healthy(Path("/state/health")) else 1)
