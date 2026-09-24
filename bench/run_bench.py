"""Benchmark orchestrator. Stdlib only. Every browser operation (A0's fast_run, fresh-tab
setup, the target diff, verification reads) runs through a `browser-harness` subprocess on the named daemon
`jevbench`, with telemetry and update checks off. Run it in the background; the full matrix takes a long time.

PYTHONPATH=<checkout> python3 bench/run_bench.py --tasks wiki,flights,hotel,llm,iframe,private \
    --arms A,A-int,B,A0,A0-llm,A-llm,A-none --n 3
"""

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench import arms as ARMS  # noqa: E402
from bench import cost as COST  # noqa: E402
from bench import decide as DECIDE  # noqa: E402
from bench import verify as V  # noqa: E402
from bench.fixture import server as FIXTURE  # noqa: E402
from bench.fixture.ports import P1, P2  # noqa: E402
from bench.tasks import load_tasks  # noqa: E402
from jev_browse.textgen import scrubbed_env  # noqa: E402

OUT = ROOT / "bench" / "results"
RAW = OUT / "raw"
BASE_ENV = {"BU_NAME": "jevbench", "BH_TELEMETRY": "0", "BH_UPDATE_CHECK": "0", "BH_TAB_MARKER": "0"}
CLAUDE_TIMEOUT = 900
SCRIPT_TIMEOUT = 300
HARNESS_TIMEOUT = 120
MARK = "BENCH_JSON="


def harness_tmp():
    raw = os.environ.get("BH_TMP_DIR")
    if raw:
        return Path(raw).expanduser()
    home = os.environ.get("BH_HOME") or os.environ.get("BROWSER_HARNESS_HOME")
    base = Path(home).expanduser() if home else Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) \
        / "browser-harness"
    return base / "tmp"


def base_env(extra=None, require_existing=True):
    env = scrubbed_env(os.environ)
    env.update(BASE_ENV)
    if require_existing:
        env["BH_REQUIRE_EXISTING_DAEMON"] = "1"
    env.update(extra or {})
    return env


class InfraError(RuntimeError):
    pass


def harness(script, env_extra=None, timeout=HARNESS_TIMEOUT):
    """Run a heredoc script on the jevbench daemon; return the last BENCH_JSON payload (or None) and stdout."""
    try:
        p = subprocess.run(["browser-harness"], input=script, capture_output=True, text=True, timeout=timeout,
                           env=base_env(env_extra), cwd=str(harness_tmp()))
    except subprocess.TimeoutExpired:
        raise InfraError("harness call timed out") from None
    payload = None
    for line in p.stdout.splitlines():
        if line.startswith(MARK):
            payload = json.loads(line[len(MARK):])
    if p.returncode != 0 and payload is None:
        err = (p.stderr or "")[-600:]
        if any(s in err for s in ("daemon", "REQUIRE_EXISTING", "Connection refused", "No such file")):
            raise InfraError(err)
        raise RuntimeError(err)
    return payload, p.stdout


def list_targets():
    payload, _ = harness(f'import json\nprint("{MARK}" + json.dumps(cdp("Target.getTargets")["targetInfos"]))')
    return [t for t in payload if t.get("type") == "page"]


# ---- tab ownership: the orchestrator closes ONLY targets it can prove were created by the benchmark ----------------
# Never by diff ("appeared during the attempt"), blankness, "current", or "newest": the daemon shares the user's
# Chrome, and a tab the user opens mid-attempt must survive.
OWNED = set()


def runtime_dir():
    raw = os.environ.get("BH_RUNTIME_DIR")
    if raw:
        return Path(raw).expanduser()
    home = os.environ.get("BH_HOME") or os.environ.get("BROWSER_HARNESS_HOME")
    base = Path(home).expanduser() if home else Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) \
        / "browser-harness"
    return base / "runtime"


def new_owned_tab(cdp=None):
    """A fresh blank tab via raw CDP Target.createTarget (never harness new_tab, which can reuse a blank tab),
    made the jevbench daemon's current tab, and recorded as ours."""
    if cdp is None:
        payload, _ = harness(f'import json\ntid = cdp("Target.createTarget", url="about:blank", background=True)'
                             f'["targetId"]\nswitch_tab(tid)\nprint("{MARK}" + json.dumps(tid))')
        tid = payload
    else:
        tid = cdp("Target.createTarget", url="about:blank", background=True)["targetId"]
    OWNED.add(tid)
    return tid


def closable_targets(prep, t0, *, registry, created, live):
    """Ours to close: the orchestrator's own tab, plus targets jev-browse registered or its new_tab recorder
    recorded on the jevbench daemon since this attempt started. Everything else is left open."""
    ids = {prep} if prep in OWNED else set()
    for store in (registry, created):
        for tid, entry in store.entries().items():
            if entry.get("created_at", 0) >= t0 - 1:
                ids.add(tid)
    ids &= set(live)
    OWNED.update(ids)
    return sorted(ids)


def close_owned(ids, cdp=None):
    ids = [i for i in ids if i in OWNED]
    if not ids:
        return
    if cdp is not None:
        for tid in ids:
            try:
                cdp("Target.closeTarget", targetId=tid)
            except Exception:
                pass
        return
    harness("\n".join([f'try:\n    cdp("Target.closeTarget", targetId={tid!r})\nexcept Exception:\n    pass'
                       for tid in ids]))


def extract(tid):
    payload, _ = harness(f'import json\nprint("{MARK}" + json.dumps(js({V.EXTRACT_JS!r}, target_id={tid!r})))')
    return payload or {}


def fixture_log_text():
    try:
        return FIXTURE.LOG.read_text()
    except OSError:
        return ""


def ensure_fixture_server():
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{P1}/health", timeout=2).read()
        urllib.request.urlopen(f"http://127.0.0.1:{P2}/health", timeout=2).read()
        return None  # already serving
    except OSError:
        return FIXTURE.serve(P1, P2)


def run_files_since(t0):
    out = []
    for p in glob.glob(str(harness_tmp() / "jev-browse-run-*.json")):
        try:
            if os.stat(p).st_mtime < t0:
                continue
            data = json.loads(Path(p).read_text())
        except (OSError, ValueError):
            continue
        if data.get("started_at", 0) >= t0 - 1:
            out.append(data)
    return out


def summary_path(rows_path):
    """The verdict lives next to its rows: rows.jsonl -> summary.json, other.jsonl -> other.summary.json (a partial
    run with --rows never overwrites the main summary)."""
    rows_path = Path(rows_path)
    return rows_path.with_name("summary.json") if rows_path.name == "rows.jsonl" else \
        rows_path.with_name(rows_path.stem + ".summary.json")


def jev_totals(run_files):
    tot = {"jev_calls": 0, "jev_input_tokens": 0, "jev_output_tokens": 0, "llm_calls": 0, "llm_ms_total": 0,
           "llm_input_tokens": 0, "llm_output_tokens": 0, "llm_cache_read_tokens": 0, "llm_cache_write_tokens": 0,
           "llm_cost_usd": 0.0}
    reasons = []
    backends = []
    canaries = []
    for f in run_files:
        stats = (f.get("result") or {}).get("stats") or {}
        for k in tot:
            tot[k] += stats.get(k, 0) or 0
        r = f.get("result") or {}
        reasons.append(r.get("reason") or r.get("status"))
        steps = [s["llm"] for s in r.get("trace") or [] if s.get("llm")]
        backends += [s.get("backend") for s in steps]
        canaries += [{k: s["canary"].get(k) for k in ("ok", "cached", "ms")} if s.get("canary") else None
                     for s in steps]
    tot["llm_backends"] = backends
    tot["llm_canary"] = canaries  # per text call: the local backend's canary verdict, and whether it was cached
    return tot, reasons


def private_safe(row):
    keep = {"attempt_id", "task", "arm", "rep", "passed", "wall_s", "cost", "cost_as_billed", "cost_parts", "turns",
            "jev_calls", "llm_calls", "timed_out", "infra_error", "forbidden_used", "jev_used"}
    safe = {k: v for k, v in row.items() if k in keep}
    safe["label"] = "private app flow"
    return safe


# ------------------------------------------------------------------------------------------------ attempts
def run_claude(arm, task, attempt_id, model):
    cwd = harness_tmp() / "jevbench-cwd" / attempt_id
    if cwd.exists():
        shutil.rmtree(cwd)
    cwd.mkdir(parents=True)
    if arm in ARMS.A_FAMILY:  # project skill: B's cwd has none, so B is skill-free by construction
        (cwd / ".claude" / "skills").mkdir(parents=True)
        (cwd / ".claude" / "skills" / "jev-browse").symlink_to(ROOT / "skill", target_is_directory=True)
    env = base_env(ARMS.env_for(arm, task.key))
    t0 = time.monotonic()
    proc = subprocess.Popen(ARMS.claude_argv(model), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, cwd=str(cwd), env=env, start_new_session=True)
    timed_out = False
    try:
        out, err = proc.communicate(ARMS.prompt(arm, task), timeout=CLAUDE_TIMEOUT)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(proc.pid, 9)
        out, err = proc.communicate()
    wall = time.monotonic() - t0
    return {"wall_s": wall, "stream": out, "stderr": (err or "")[-2000:], "timed_out": timed_out,
            "audit": ARMS.audit(out or "", arm)}


def run_script(arm, task, attempt_id):
    script = (f"import json\nr = fast_run({task.url!r}, {task.goal!r}, run_id={attempt_id!r}, timeout_s=150)\n"
              f'print("{MARK}" + json.dumps({{"status": r.status, "reason": r.reason.value if r.reason else None, '
              f'"target_id": r.target_id, "stats": r.stats}}))\n')
    t0 = time.monotonic()
    timed_out = False
    try:
        payload, stdout = harness(script, ARMS.env_for(arm, task.key), timeout=SCRIPT_TIMEOUT)
    except InfraError as exc:
        if "timed out" in str(exc):
            timed_out, payload, stdout = True, None, ""
        else:
            raise
    return {"wall_s": time.monotonic() - t0, "payload": payload, "timed_out": timed_out, "stdout": stdout[-4000:]}


def verify_attempt(task, before_ids, preferred, log_offset, current=None):
    after = list_targets()
    tid, multi, cands = V.select_target(before_ids, after, task.domain, preferred, current=current)
    new_lines = V.fixture_lines(fixture_log_text(), log_offset)
    states = {}
    if task.key == "iframe":
        result = V.verify_iframe(new_lines)
    else:
        ids = [tid] if tid else (cands if multi else [])
        result = {"passed": False, "checks": {"target_found": False}}
        for t in ids:
            state = extract(t)
            states[t] = {"url": state.get("url"), "title": state.get("title")}
            if task.key == "wiki":
                r = V.verify_wiki(state)
            elif task.key == "llm":
                r = V.verify_llm(state)
            elif task.key == "hotel":
                r = V.verify_hotel(state, new_lines)
            elif task.key == "flights":
                r = V.verify_flights(state, task.meta["date"])
            else:
                r = V.verify_private(state, task.meta.get("verify", {}))
            result = r
            if r["passed"]:
                break
    return {**result, "target": tid, "multi_candidate": multi, "candidates": len(cands), "states": states,
            "new_targets_seen": [t["targetId"] for t in after if t["targetId"] not in set(before_ids)]}  # read-only


def attempt(arm, task, rep, model, raw_dir):
    attempt_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{task.key}-{arm}-{rep}".replace("_", "-")
    t_start = time.time()
    prep = new_owned_tab()
    # The fresh blank tab counts as new: an arm's new_tab(url) reuses the current blank tab (harness behaviour).
    before = [t["targetId"] for t in list_targets() if t["targetId"] != prep]
    log_offset = len(fixture_log_text())
    t0 = time.time()
    if arm in ARMS.CLAUDE_ARMS:
        run = run_claude(arm, task, attempt_id, model)
    else:
        run = run_script(arm, task, attempt_id)
    files = run_files_since(t0)
    totals, reasons = jev_totals(files)
    preferred = [f.get("target_id") for f in sorted(files, key=lambda f: -f.get("started_at", 0)) if f.get("target_id")]
    if run.get("payload") and run["payload"].get("target_id"):
        preferred.insert(0, run["payload"]["target_id"])
    check = verify_attempt(task, before, preferred, log_offset, current=prep)
    from jev_browse.tab import Registry
    close_owned(closable_targets(prep, t_start, registry=Registry(runtime_dir(), "jevbench"),
                                 created=Registry(runtime_dir(), "jevbench", "created"),
                                 live=[t["targetId"] for t in list_targets()]))
    audit = run.get("audit") or {}
    result_event = audit.get("result") or {}
    llm_usage = {"input_tokens": totals["llm_input_tokens"], "output_tokens": totals["llm_output_tokens"],
                 "cache_read_input_tokens": totals["llm_cache_read_tokens"],
                 "cache_creation_input_tokens": totals["llm_cache_write_tokens"]}
    # Local (Ollama) text calls have no list price; a call that fell back to Claude is priced at the Haiku rate.
    # Speculative batches are not attached to a trace step, so the per-call backend list can be empty; then trust
    # jev-browse's own text cost, which prices a Claude fallback and is $0 for a local backend.
    if totals["llm_backends"]:
        local_only = all(b == "ollama" for b in totals["llm_backends"])
    else:
        local_only = bool(totals["llm_calls"]) and not totals["llm_cost_usd"]
    c = COST.attempt_cost(claude_event=result_event or None, claude_model=model, jev_input=totals["jev_input_tokens"],
                          jev_output=totals["jev_output_tokens"],
                          llm_usage=llm_usage if totals["llm_calls"] and not local_only else None)
    passed = bool(check["passed"]) and not audit.get("forbidden_used") and not run["timed_out"]
    row = {"attempt_id": attempt_id, "task": task.key, "arm": arm, "rep": rep, "passed": passed,
           "verified": bool(check["passed"]), "checks": check["checks"], "wall_s": round(run["wall_s"], 2),
           "cost": round(c["cache_neutral"], 6), "cost_as_billed": round(c["as_billed"], 6),
           "cost_parts": {k: round(v["cache_neutral"], 6) for k, v in c["parts"].items()},
           "reported_claude_cost": (result_event or {}).get("total_cost_usd"),
           "turns": (result_event or {}).get("num_turns"), **{k: totals[k] for k in ("jev_calls", "jev_input_tokens",
                                                                                      "llm_calls", "llm_ms_total")},
           "llm_backends": totals["llm_backends"], "llm_canary": totals["llm_canary"],
           "reasons": reasons, "timed_out": run["timed_out"], "infra_error": False,
           "multi_candidate": check["multi_candidate"],
           "forbidden_used": audit.get("forbidden_used", False), "jev_used": audit.get("jev_used"),
           "caller_values": audit.get("caller_values"), "skill_loaded": audit.get("skill_loaded"),
           "gate_violation": audit.get("gate_violation"), "update_or_reload": audit.get("update_or_reload"),
           "init": ({"tool_count": len((audit.get("init") or {}).get("tools") or []),
                     "skills_listed": (audit.get("init") or {}).get("skills_listed")} if audit.get("init") else None),
           "script_status": (run.get("payload") or {}).get("status"),
           "script_reason": (run.get("payload") or {}).get("reason")}
    d = raw_dir / attempt_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "row.json").write_text(json.dumps(row, indent=1))
    (d / "verify.json").write_text(json.dumps(check, indent=1, default=str))
    if run.get("stream"):
        (d / "stream.jsonl").write_text(run["stream"])
        (d / "stderr.txt").write_text(run.get("stderr", ""))
    if run.get("stdout"):
        (d / "stdout.txt").write_text(run["stdout"])
    return row


def infra_row(arm, task, rep, err):
    return {"attempt_id": f"infra-{task.key}-{arm}-{rep}", "task": task.key, "arm": arm, "rep": rep, "passed": False,
            "wall_s": 0.0, "cost": 0.0, "infra_error": True, "error": str(err)[:300]}


def run_attempt(arm, task, rep, model, raw_dir, log):
    for attempt_no in range(2):  # an infrastructure failure is re-run once and excluded from the verdict
        try:
            row = attempt(arm, task, rep, model, raw_dir)
            break
        except InfraError as exc:
            row = infra_row(arm, task, rep, exc)
            if attempt_no == 1:
                break
            time.sleep(5)
    log(row)
    return row


def harness_head():
    try:
        payload, _ = harness(f'import browser_harness, json\nprint("{MARK}" + json.dumps(browser_harness.__file__))')
        repo = Path(payload).resolve().parents[2]
        return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    except Exception as exc:
        return f"unknown ({exc})"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", default="wiki,flights,hotel,llm,iframe,private")
    ap.add_argument("--arms", default="A,A-int,B,A0,A0-llm,A-llm,A-none")
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--model", default="opus")
    ap.add_argument("--rows", default=str(OUT / "rows.jsonl"))
    ap.add_argument("--pilot", action="store_true", help="one attempt per Claude arm on `hotel`")
    ap.add_argument("--unprompted", action="store_true", help="one A-unprompted attempt per public task")
    ap.add_argument("--rerun-hinges", action="store_true", help="re-run voting classes that hinge on one failure")
    args = ap.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)
    RAW.mkdir(parents=True, exist_ok=True)
    tasks = load_tasks()
    servers = ensure_fixture_server()
    rows_path = Path(args.rows)
    head_before = harness_head()
    print(f"harness HEAD {head_before}", flush=True)

    def log(row):
        public = private_safe(row) if row["task"] == "private" else row
        with open(rows_path, "a") as f:
            f.write(json.dumps(public) + "\n")
        print(json.dumps({k: public.get(k) for k in ("task", "arm", "rep", "passed", "wall_s", "cost", "turns",
                                                      "reasons", "jev_used", "infra_error")}), flush=True)

    rows = []
    try:
        if args.pilot:
            for arm in ("A", "A-int", "B"):
                rows.append(run_attempt(arm, tasks["hotel"], 0, args.model, RAW, log))
        elif args.unprompted:
            for key in ARMS.ALL_PUBLIC:
                rows.append(run_attempt("A-unprompted", tasks[key], 0, args.model, RAW, log))
        elif args.rerun_hinges:
            existing = [json.loads(line) for line in rows_path.read_text().splitlines()] if rows_path.exists() else []
            verdict = DECIDE.decide(existing)
            for key, v in verdict["classes"].items():
                if v.get("hinges"):
                    for rep in range(args.n, args.n + 3):
                        for arm in (("A", "B") if rep % 2 == 0 else ("B", "A")):
                            rows.append(run_attempt(arm, tasks[key], rep, args.model, RAW, log))
        else:
            wanted_arms = args.arms.split(",")
            wanted_tasks = [t for t in args.tasks.split(",") if t in tasks]
            for rep in range(args.n):
                order = wanted_arms if rep % 2 == 0 else list(reversed(wanted_arms))
                for key in wanted_tasks:
                    for arm in order:
                        if key in ARMS.ARM_TASKS.get(arm, ()):
                            rows.append(run_attempt(arm, tasks[key], rep, args.model, RAW, log))
    finally:
        head_after = harness_head()
        print(f"harness HEAD {head_after} ({'unchanged' if head_after == head_before else 'CHANGED'})", flush=True)
        if servers:
            for s in servers:
                s.shutdown()
    all_rows = [json.loads(line) for line in rows_path.read_text().splitlines()] if rows_path.exists() else rows
    summary = DECIDE.decide(all_rows)
    summary["harness_head"] = {"before": head_before, "after": head_after}
    summary_path(rows_path).write_text(json.dumps(summary, indent=1, default=str))
    print(json.dumps(summary, default=str)[:3000], flush=True)


if __name__ == "__main__":
    main()
