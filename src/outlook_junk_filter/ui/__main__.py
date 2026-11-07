"""Serve the review page on its own, without the job: ``python -m outlook_junk_filter.ui``.

Viewing works; actions need the job's Graph sign-in and answer that they are unavailable.
The image tests use this to check the page on a read-only root filesystem.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .server import Context, make_server


def main() -> int:
    port = int(os.environ.get("OJF_UI_PORT", "60888"))
    server = make_server(port, Context(Path("/state/logs")))
    print(f"review page on port {port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
