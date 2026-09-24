"""Render benchmark tables and the verdict from a rows file written by bench/run_bench.py.

python3 bench/report.py [bench/results/rows.jsonl] > /tmp/tables.md
"""

import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench import decide as DECIDE  # noqa: E402

ORDER = ["A", "A-int", "B", "A0", "A0-llm", "A-llm", "A-none", "A-unprompted"]
TASKS = ["wiki", "flights", "hotel", "llm", "iframe", "private"]


def load(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def fmt(x, nd=2):
    return "—" if x is None else f"{x:.{nd}f}"


def med(vals):
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def per_task_table(rows):
    out = ["| Task | Arm | n | Passed | Wall s median (range) | Cost $ median (cache-neutral) | as billed | "
           "Claude / Jev / text $ | Turns | Jev calls | LLM calls (ms) | jev_used | Hand-back reasons |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for task in TASKS:
        for arm in ORDER:
            rs = [r for r in rows if r["task"] == task and r["arm"] == arm and not r.get("infra_error")]
            if not rs:
                continue
            walls = [r["wall_s"] for r in rs]
            parts = [r.get("cost_parts") or {} for r in rs]
            split = "/".join(fmt(med([p.get(k) for p in parts]), 4) for k in ("calling_claude", "jev", "text_backend"))
            reasons = sorted({x for r in rs for x in (r.get("reasons") or []) if x and x != "claimed_done"})
            used = [r.get("jev_used") for r in rs if r.get("jev_used") is not None]
            out.append(f"| {task} | {arm} | {len(rs)} | {sum(bool(r['passed']) for r in rs)}/{len(rs)} | "
                       f"{fmt(med(walls), 1)} ({fmt(min(walls), 1)}–{fmt(max(walls), 1)}) | "
                       f"{fmt(med([r['cost'] for r in rs]), 4)} | {fmt(med([r.get('cost_as_billed') for r in rs]), 4)} | "
                       f"{split} | {fmt(med([r.get('turns') for r in rs]), 0)} | "
                       f"{fmt(med([r.get('jev_calls') for r in rs]), 0)} | "
                       f"{fmt(med([r.get('llm_calls') for r in rs]), 0)} ({fmt(med([r.get('llm_ms_total') for r in rs]), 0)}) | "
                       f"{(str(sum(bool(u) for u in used)) + '/' + str(len(used))) if used else '—'} | "
                       f"{', '.join(reasons) or '—'} |")
    return "\n".join(out)


def verdict_table(summary):
    out = ["| Class (task) | Verdict | A wall / B wall (band) | A cost / B cost | A / B passes | Hinges |",
           "|---|---|---|---|---|---|"]
    for task, v in summary["classes"].items():
        if v.get("win") is None:
            out.append(f"| {v.get('class', task)} ({task}) | no data | | | | |")
            continue
        out.append(f"| {v['class']} ({task}) | {'**win**' if v['win'] else 'lose'} "
                   f"(fast {'✓' if v['fast'] else '✗'}, cheaper {'✓' if v['cheaper'] else '✗'}, "
                   f"no pass drop {'✓' if v['no_pass_drop'] else '✗'}) | {fmt(v['a_wall_median'], 1)} / "
                   f"{fmt(v['b_wall_median'], 1)} ({int(v['band'] * 100)}%) | {fmt(v['a_cost_median'], 4)} / "
                   f"{fmt(v['b_cost_median'], 4)} | {v['a_passes']}/{v['n_a']} / {v['b_passes']}/{v['n_b']} | "
                   f"{'yes' if v.get('hinges') else 'no'} |")
    return "\n".join(out)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    path = Path(argv[0]) if argv else ROOT / "bench" / "results" / "rows.jsonl"
    if not path.exists():
        raise SystemExit(f"no benchmark rows at {path}: run bench/run_bench.py first (see docs/benchmarking.md)")
    rows = load(path)
    summary = DECIDE.decide(rows)
    print(per_task_table(rows))
    print()
    print(verdict_table(summary))
    print()
    print(json.dumps({k: summary[k] for k in ("a_int", "reported", "losses", "opt_in", "routing")}, indent=1,
                     default=str))


if __name__ == "__main__":
    main()
