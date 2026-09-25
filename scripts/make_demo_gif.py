"""Re-create docs/media/demo.gif: one benchmark task done by the same coding agent twice, side by side.

Left: arm B (Claude Code drives browser-harness directly). Right: arm A-fast (Claude Code calls fast_run). Both are
real benchmark attempts (bench/run_bench.py's `attempt`, so the same prompt, code-only verification and list-price
costing as docs/benchmark.md). While each runs, a separate harness process screenshots the benchmark's own tabs
(the orchestrator's fresh tab, or the tab jev-browse opened), never the browser window: no tab strip, bookmarks or
other tabs. Page content still goes through `render`'s review: look at the contact sheet before publishing.

  # 1. record (live: two Claude Code attempts at list price, TypeSafe requests, a real Chrome; see
  #    docs/benchmarking.md for the `jevbench` daemon it needs). Output goes to gitignored bench/results/raw/demo/.
  PYTHONPATH=. python3 scripts/make_demo_gif.py record --task wiki

  # 2. render (offline; needs ffmpeg). Given several recordings, it uses the pair whose left-hand time is the
  #    median (the published GIF: three pairs). Refuses if any attempt failed verification.
  uv run --with pillow python3 scripts/make_demo_gif.py render bench/results/raw/demo/<stamp-1> <stamp-2> <stamp-3> \
      --gif docs/media/demo.gif --contact-sheet /tmp/demo-sheet.png
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ARMS = (("B", "Agent alone", "drives browser-harness step by step"),
        ("A-fast", "Agent + jev-browse", "calls fast_run once"))
TASK_TITLES = {"wiki": "Open a Wikipedia article from the Main Page",
               "flights": "Google Flights one-way search",
               "hotel": "Search, filter and open a listing (local fixture)"}
# Google Flights: US region and dollars, so prices read the same for everyone (the verifier only reads `tfs`).
URL_PARAMS = {"flights": "&gl=US&curr=USD"}
# CSS px cut from the top of every frame. Google's header carries the signed-in account's avatar, so it never shows.
CROP_TOP = {"flights": 72}
CAPTURE_SCALE = 0.5  # screenshots at half the viewport's CSS size
FRAME_PERIOD = 0.2   # at most 5 captures per second

# Runs inside `browser-harness` on the jevbench daemon. Captures whichever tab the attempt is using: the newest tab
# jev-browse (or its new_tab recorder) registered since the recording began, else the orchestrator's fresh tab.
RECORDER = r'''
import base64, json, time
from pathlib import Path
out, stop, prep, t0 = Path(OUT), Path(STOP), PREP, T0
frames = out / "frames"
frames.mkdir(parents=True, exist_ok=True)
log = open(out / "frames.jsonl", "a")
sessions, n = {}, 0
while not stop.exists():
    tick = time.time()
    try:
        pages = {t["targetId"]: t for t in cdp("Target.getTargets")["targetInfos"] if t.get("type") == "page"}
        tid, newest = prep, -1.0
        for reg in REGS:
            try:
                entries = json.loads(Path(reg).read_text()).get("targets", {})
            except (OSError, ValueError):
                continue
            for cand, entry in entries.items():
                created = entry.get("created_at", 0)
                if cand in pages and created >= t0 - 1 and created > newest:
                    tid, newest = cand, created
        if tid not in pages:
            time.sleep(PERIOD)
            continue
        if tid not in sessions:
            sessions[tid] = cdp("Target.attachToTarget", targetId=tid, flatten=True)["sessionId"]
        sid = sessions[tid]
        vp = cdp("Page.getLayoutMetrics", session_id=sid)["cssVisualViewport"]
        shot = cdp("Page.captureScreenshot", session_id=sid, format="jpeg", quality=80,
                   clip={"x": vp["pageX"], "y": vp["pageY"], "width": vp["clientWidth"],
                         "height": vp["clientHeight"], "scale": SCALE})
        name = f"{n:05d}.jpg"
        (frames / name).write_bytes(base64.b64decode(shot["data"]))
        log.write(json.dumps({"t": tick, "file": name, "url": pages[tid].get("url", ""),
                              "css_width": vp["clientWidth"], "css_height": vp["clientHeight"]}) + "\n")
        log.flush()
        n += 1
    except Exception as exc:
        log.write(json.dumps({"t": tick, "error": str(exc)[:200]}) + "\n")
        log.flush()
    time.sleep(max(0.0, PERIOD - (time.time() - tick)))
for sid in sessions.values():
    try:
        cdp("Target.detachFromTarget", sessionId=sid)
    except Exception:
        pass
print("RECORDER_DONE", n)
'''


# --------------------------------------------------------------------------------------------------------- record
class Recorder:
    def __init__(self, rb, prep, out_dir):
        self.rb, self.prep, self.out = rb, prep, Path(out_dir)
        self.stop_file = self.out / "STOP"
        self.proc = None

    def start(self):
        from jev_browse.tab import Registry
        self.out.mkdir(parents=True, exist_ok=True)
        self.stop_file.unlink(missing_ok=True)
        regs = [str(Registry(self.rb.runtime_dir(), "jevbench", kind).path) for kind in ("owned", "created")]
        head = (f"OUT={str(self.out)!r}\nSTOP={str(self.stop_file)!r}\nPREP={self.prep!r}\nT0={time.time()!r}\n"
                f"REGS={regs!r}\nSCALE={CAPTURE_SCALE!r}\nPERIOD={FRAME_PERIOD!r}\n")
        self.proc = subprocess.Popen(["browser-harness"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, env=self.rb.base_env(),
                                     cwd=str(self.rb.harness_tmp()))
        self.proc.stdin.write(head + RECORDER)
        self.proc.stdin.close()
        deadline = time.time() + 20
        while time.time() < deadline and not any((self.out / "frames").glob("*.jpg")):
            if self.proc.poll() is not None:
                raise RuntimeError("recorder exited early: " + self.proc.stderr.read()[-600:])
            time.sleep(0.1)

    def stop(self):
        self.stop_file.touch()
        try:
            out, err = self.proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            out, err = self.proc.communicate()
        self.stop_file.unlink(missing_ok=True)
        if "RECORDER_DONE" not in (out or ""):
            print("recorder did not finish cleanly:", (err or "")[-600:], file=sys.stderr)


def run_claude_timed(rb, arms, arm, task, attempt_id, model, on_start):
    """bench/run_bench.py's run_claude, with a wall-clock time on every stream-json line (for the turn counter)."""
    cwd = rb.harness_tmp() / "jevbench-cwd" / attempt_id
    if cwd.exists():
        shutil.rmtree(cwd)
    cwd.mkdir(parents=True)
    if arm in arms.A_FAMILY:
        (cwd / ".claude" / "skills").mkdir(parents=True)
        (cwd / ".claude" / "skills" / "jev-browse").symlink_to(ROOT / "skill", target_is_directory=True)
    env = rb.base_env(arms.env_for(arm, task.key))
    on_start()
    t0, wall0 = time.monotonic(), time.time()
    proc = subprocess.Popen(arms.claude_argv(model), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, cwd=str(cwd), env=env, start_new_session=True)
    lines, errs = [], []
    pumps = [threading.Thread(target=lambda: [lines.append((time.time() - wall0, ln)) for ln in proc.stdout]),
             threading.Thread(target=lambda: errs.extend(proc.stderr))]
    for p in pumps:
        p.start()
    proc.stdin.write(arms.prompt(arm, task))
    proc.stdin.close()
    timed_out = False
    try:
        proc.wait(timeout=rb.CLAUDE_TIMEOUT)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(proc.pid, 9)
        proc.wait()
    for p in pumps:
        p.join()
    wall = time.monotonic() - t0
    out = "".join(ln for _, ln in lines)
    return {"wall_s": wall, "stream": out, "stderr": "".join(errs)[-2000:], "timed_out": timed_out,
            "audit": arms.audit(out, arm), "line_times": lines, "wall0": wall0}


def stream_timeline(line_times):
    """Relative times of each new assistant message (a model turn) and the model id from the init event."""
    turns, seen, model = [], set(), None
    for t, line in line_times:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "system" and ev.get("subtype") == "init":
            model = ev.get("model")
        elif ev.get("type") == "assistant":
            mid = (ev.get("message") or {}).get("id")
            if mid and mid not in seen:
                seen.add(mid)
                turns.append(round(t, 3))
    return turns, model


def record(args):
    from bench import arms as ARMS_MOD
    from bench import run_bench as RB
    from bench.tasks import load_tasks

    task = load_tasks()[args.task]
    task.url += URL_PARAMS.get(task.key, "")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = Path(args.out) / f"{stamp}-{task.key}"
    raw = RB.RAW
    raw.mkdir(parents=True, exist_ok=True)
    servers = RB.ensure_fixture_server() if task.key in ("hotel", "iframe") else None
    summary = {"task": task.key, "title": TASK_TITLES.get(task.key, task.goal), "url": task.url,
               "recorded": time.strftime("%Y-%m-%d"), "arms": {}}
    orig = RB.run_claude
    try:
        for arm, _, _ in ARMS:
            arm_dir = out / arm
            before = set(RB.OWNED)
            state = {}

            def patched(arm_, task_, attempt_id, model_, arm_dir=arm_dir, before=before, state=state):
                prep = next(iter(RB.OWNED - before))  # attempt() just created it with Target.createTarget
                rec = Recorder(RB, prep, arm_dir)
                try:
                    run = run_claude_timed(RB, ARMS_MOD, arm_, task_, attempt_id, model_, on_start=rec.start)
                finally:
                    rec.stop()
                state["run"] = run
                return run

            RB.run_claude = patched
            row = RB.attempt(arm, task, 0, args.model, raw)
            run = state["run"]
            turns, model = stream_timeline(run["line_times"])
            (arm_dir / "turns.json").write_text(json.dumps(turns))
            (arm_dir / "row.json").write_text(json.dumps(row, indent=1))
            (arm_dir / "meta.json").write_text(json.dumps({"t0": run["wall0"], "model": model}))
            summary["arms"][arm] = {k: row.get(k) for k in ("passed", "wall_s", "cost", "turns", "attempt_id")}
            print(json.dumps({"arm": arm, **summary["arms"][arm]}), flush=True)
    finally:
        RB.run_claude = orig
        for s in servers or ():
            s.shutdown()
    (out / "recording.json").write_text(json.dumps(summary, indent=1))
    print(f"recording: {out}")


# --------------------------------------------------------------------------------------------------------- render
W, GUTTER, TOP, HEAD, FOOT, BOTTOM = 960, 16, 44, 46, 50, 30
PANE_W = (W - 3 * GUTTER) // 2
BG, FG, MUTED, OK, SLOW = (18, 20, 24), (236, 238, 242), (150, 156, 168), (74, 201, 120), (240, 180, 90)


def speed_for(longest, budget=32.0):
    return next((s for s in (1, 1.5, 2, 2.5, 3, 4, 5, 6) if longest / s <= budget), 8)


def fmt_speed(s):
    return f"{s:g}×"


def load_arm(rec_dir, arm, crop_w, crop_h, crop_top=0):
    from PIL import Image

    d = Path(rec_dir) / arm
    row = json.loads((d / "row.json").read_text())
    meta = json.loads((d / "meta.json").read_text())
    turns = json.loads((d / "turns.json").read_text())
    frames = []
    for line in (d / "frames.jsonl").read_text().splitlines():
        f = json.loads(line)
        if "file" in f:
            frames.append((f["t"] - meta["t0"], d / "frames" / f["file"], f))
    pane_h = round(PANE_W * crop_h / crop_w)
    cache = {}

    def image_at(t):
        best = None
        for ft, path, info in frames:
            if ft <= t:
                best = (path, info)
            else:
                break
        if best is None:
            best = (frames[0][1], frames[0][2])
        path, info = best
        if path not in cache:
            img = Image.open(path).convert("RGB")
            s = img.width / info["css_width"]  # pixels per CSS px
            cw, ch = min(crop_w, info["css_width"]), min(crop_h, info["css_height"] - crop_top)
            x0 = (info["css_width"] - cw) / 2
            box = tuple(round(v * s) for v in (x0, crop_top, x0 + cw, crop_top + ch))
            cache[path] = img.crop(box).resize((PANE_W, round(PANE_W * ch / cw)), Image.LANCZOS)
        return cache[path], info.get("url", "")

    return {"row": row, "meta": meta, "turns": turns, "frames": frames, "image_at": image_at, "pane_h": pane_h}


def pick_median(recs):
    """[(path, recording.json)] -> the pair whose left-hand (agent alone) time is the median (the lower middle for an
    even count). Every pair counts, so a failed attempt is an error rather than quietly dropped from the pick."""
    for r, summary in recs:
        failed = [arm for arm, v in summary["arms"].items() if not v.get("passed")]
        if failed:
            raise ValueError(f"{Path(r).name}: {', '.join(failed)} did not pass verification; record a fresh set")
    ordered = sorted(recs, key=lambda rs: rs[1]["arms"]["B"]["wall_s"])
    return ordered[(len(ordered) - 1) // 2]


def render(args):
    from PIL import Image, ImageDraw, ImageFont

    recs = [(Path(r), json.loads((Path(r) / "recording.json").read_text())) for r in args.recording]
    try:
        rec, summary = pick_median(recs)
    except ValueError as exc:
        sys.exit(str(exc))
    print(f"using {rec.name} (median left-hand time of {len(recs)} recorded pairs)")
    crop_top = CROP_TOP.get(summary["task"], 0) if args.crop_top is None else args.crop_top
    arms = {arm: load_arm(rec, arm, args.crop_width, args.crop_height, crop_top) for arm, _, _ in ARMS}
    longest = max(a["row"]["wall_s"] for a in arms.values())
    speed = args.speed or speed_for(longest)
    fps, hold = args.fps, args.hold
    pane_h = arms["B"]["pane_h"]
    height = TOP + HEAD + pane_h + FOOT + BOTTOM
    font = {k: ImageFont.load_default(size=v) for k, v in (("title", 17), ("label", 17), ("small", 13),
                                                            ("timer", 26), ("note", 12))}
    model = arms["B"]["meta"].get("model") or "Claude"
    tmp = rec / "render"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    n_frames = int((longest / speed + hold) * fps)
    sheet = []
    for k in range(n_frames):
        t = k / fps * speed
        img = Image.new("RGB", (W, height), BG)
        d = ImageDraw.Draw(img)
        d.text((GUTTER, 13), f"Same task, same agent: {summary['title']}", fill=FG, font=font["title"])
        speed_label = f"{fmt_speed(speed)} speed" if speed != 1 else "real time"
        d.text((W - GUTTER, 15), speed_label, fill=SLOW, font=font["small"], anchor="ra")
        for i, (arm, label, sub) in enumerate(ARMS):
            a = arms[arm]
            x = GUTTER + i * (PANE_W + GUTTER)
            y = TOP
            d.text((x, y + 4), label, fill=FG, font=font["label"])
            d.text((x, y + 25), sub, fill=MUTED, font=font["small"])
            wall = a["row"]["wall_s"]
            done = t >= wall
            frame, url = a["image_at"](min(t, wall))
            img.paste(frame, (x, y + HEAD))
            if url.startswith("about:blank") or not url:
                d.rectangle((x, y + HEAD, x + PANE_W - 1, y + HEAD + frame.height - 1), fill=(38, 41, 48))
                d.text((x + PANE_W // 2, y + HEAD + frame.height // 2), "blank tab: the agent is working",
                       fill=MUTED, font=font["small"], anchor="mm")
            fy = y + HEAD + pane_h + 8
            turns = sum(1 for tt in a["turns"] if tt <= min(t, wall))
            if done:
                d.text((x, fy), f"done in {wall:.1f} s", fill=OK, font=font["timer"])
                d.text((x + PANE_W, fy + 2), f"${a['row']['cost']:.2f} list price", fill=OK, font=font["label"],
                       anchor="ra")
                d.text((x + PANE_W, fy + 24), f"{turns} model turns · verified", fill=MUTED, font=font["small"],
                       anchor="ra")
            else:
                d.text((x, fy), f"{t:5.1f} s", fill=FG, font=font["timer"])
                d.text((x + PANE_W, fy + 6), f"model turns: {turns}", fill=MUTED, font=font["label"], anchor="ra")
        pick = f" (median of {len(recs)} recorded pairs)" if len(recs) > 1 else ""
        d.text((GUTTER, height - BOTTOM + 8),
               f"Real benchmark run{pick}, {summary['recorded']} · Claude Code ({model}) · {speed_label} · "
               "cost = all model tokens at list price", fill=MUTED, font=font["note"])
        path = tmp / f"{k:05d}.png"
        img.save(path)
        if k in (0, n_frames // 4, n_frames // 2, 3 * n_frames // 4, n_frames - 1):
            sheet.append(img)
    out = Path(args.gif)
    out.parent.mkdir(parents=True, exist_ok=True)
    palette = tmp / "palette.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-framerate", str(fps), "-i", str(tmp / "%05d.png"),
                    "-vf", "palettegen=max_colors=128:stats_mode=diff", str(palette)], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-framerate", str(fps), "-i", str(tmp / "%05d.png"),
                    "-i", str(palette), "-lavfi", "paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle",
                    "-loop", "0", str(out)], check=True)
    if args.contact_sheet:
        cs = Image.new("RGB", (W, height * len(sheet)), BG)
        for i, s in enumerate(sheet):
            cs.paste(s, (0, i * height))
        cs.save(args.contact_sheet)
    shutil.rmtree(tmp)
    size = out.stat().st_size
    print(json.dumps({"gif": str(out), "bytes": size, "seconds": round(n_frames / fps, 1), "speed": speed,
                      **{arm: {k: arms[arm]["row"][k] for k in ("wall_s", "cost", "turns")} for arm in arms}}))
    if size > 5 * 1024 * 1024:
        print("warning: over 5 MB; lower --fps or --crop-width", file=sys.stderr)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record", help="run arms B and A-fast on one task and capture their tabs (live)")
    r.add_argument("--task", default="wiki", choices=sorted(TASK_TITLES))
    r.add_argument("--model", default="opus")
    r.add_argument("--out", default=str(ROOT / "bench" / "results" / "raw" / "demo"))
    g = sub.add_parser("render", help="compose the side-by-side GIF from a recording (offline)")
    g.add_argument("recording", nargs="+", help="one or more recording dirs; with several, the median pair is used")
    g.add_argument("--gif", default=str(ROOT / "docs" / "media" / "demo.gif"))
    g.add_argument("--contact-sheet", help="also write a PNG of five frames, to review before publishing")
    g.add_argument("--speed", type=float, help="playback speed for both panes (default: fit in about 32 s)")
    g.add_argument("--fps", type=int, default=8)
    g.add_argument("--hold", type=float, default=4.0, help="seconds to hold the final frame")
    g.add_argument("--crop-width", type=int, default=1500, help="CSS px, centred; page top is always kept")
    g.add_argument("--crop-height", type=int, default=900)
    g.add_argument("--crop-top", type=int, help="CSS px cut from the top (default: per task; flights drops the header)")
    args = ap.parse_args(argv)
    (record if args.cmd == "record" else render)(args)


if __name__ == "__main__":
    main()
