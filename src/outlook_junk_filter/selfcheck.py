"""Image tests use this: it runs the job's own SpamAssassin code paths against a config.

    python -m outlook_junk_filter.selfcheck lint
    python -m outlook_junk_filter.selfcheck score FILE.eml
"""

from __future__ import annotations

import sys
from pathlib import Path

from . import scoring

STATE = Path("/state")


def main(argv: list) -> int:
    if argv[:1] == ["lint"]:
        ok, detail = scoring.lint(STATE)
        print("lint ok" if ok else f"lint FAILED: {detail}")
        return 0 if ok else 1
    if len(argv) == 2 and argv[0] == "score":
        try:
            result = scoring.score(Path(argv[1]).read_bytes(), STATE)
        except scoring.ScoreError as exc:
            print(f"score FAILED: {exc}")
            return 1
        print(f"score={result.score} rules={','.join(result.rules)}")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
