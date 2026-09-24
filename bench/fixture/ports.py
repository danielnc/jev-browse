"""Fixture ports, pinned in one place (env-overridable)."""

import os

P1 = int(os.environ.get("JEV_BENCH_P1", "8765"))  # the page, served as http://localhost:P1
P2 = int(os.environ.get("JEV_BENCH_P2", "8766"))  # the cross-site reserve frame, http://127.0.0.1:P2
