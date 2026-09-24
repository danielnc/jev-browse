"""`make clean-traces` helper: delete jev-browse run, trace, and screenshot files in the harness tmp dir.

Never touches the owned-tab registry or the new_tab provenance file, and skips the run file of a live,
unfinished run (its pid is alive and `final` is false).
"""

import json
import os
import sys
from pathlib import Path

PATTERNS = ("jev-browse-run-*.json", "jev-browse-trace-*", "jev-browse-shot-*")


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    return True


def clean(tmp_dir):
    removed = []
    base = Path(tmp_dir)
    if not base.is_dir():
        return removed
    for pattern in PATTERNS:
        for path in base.glob(pattern):
            if path.name.startswith("jev-browse-run-"):
                try:
                    data = json.loads(path.read_text())
                except (OSError, ValueError):
                    data = {}
                if not data.get("final") and _pid_alive(data.get("pid")):
                    continue
            path.unlink(missing_ok=True)
            removed.append(path.name)
    return removed


if __name__ == "__main__":
    for name in clean(sys.argv[1] if len(sys.argv) > 1 else "."):
        print("removed", name)
