# Benchmarking

Two tools live in `bench/`:

- **`bench/text_eval.py`**: evaluates a *text backend* on 14 field cases. It takes a few minutes and needs no
  browser. Run it before trusting a model.
- **`bench/run_bench.py`**: the end-to-end benchmark. It runs Claude Code (or a plain script) through real browser
  tasks with and without jev-browse, verifies every outcome with code, and prices every token. It takes a long
  time and costs real money.

The summary in [benchmark.md](benchmark.md) comes from the author's runs (N = 3 per arm). It shows direction and
rough size, not guarantees. Reproduce it on your setup with the steps below.

## Evaluate a text backend (`text_eval`)

Settings come from the same places as jev-browse itself: the environment, the harness agent-workspace `.env`, and
`~/.config/jev-browse/config.toml`. So you can evaluate what you have configured:

```bash
python3 bench/text_eval.py                                     # the configured text backend, 14 cases x 2
python3 bench/text_eval.py --backends ollama --models qwen3:30b-a3b,gemma3:4b
python3 bench/text_eval.py --backends openai --models <model-id> --excerpt   # with a 2,000-char page excerpt
python3 bench/text_eval.py --backends claude,codex --repeats 3 --out /tmp/eval.json
```

Output per backend and model: pass count, median and p90 latency, and the misses. A miss that looks like personal
data is printed as `<personal-shaped value redacted>`. The cases cover destinations and origins, dates in two
formats, counts, a select, world-knowledge lookups ("the capital of France", "the author of Pride and
Prejudice"), a search query, and fields that **must stay empty** (an optional promo code, email, phone, full
name). A good backend scores 28/28. Anything that fills the must-be-empty fields is inventing data.

Things to know when reading latencies:

- `--excerpt` is closer to real use: real calls include up to 2,000 characters of page text.
- Local servers cache prompt prefixes. Each case runs twice back to back, and the second run is often much
  faster. The first-try latency is what a new goal costs.
- `text_eval` calls the backend directly. It does not run the canary; `jev-browse doctor` does.

## The end-to-end benchmark (`run_bench`)

### What it measures

Tasks (from `bench/tasks.py`):

| Task | Site | Goal | Votes |
|---|---|---|---|
| `wiki` | Wikipedia | Open the article on Gödel's incompleteness theorems from the Main Page | yes |
| `flights` | Google Flights | One-way Zurich → London, a date 26 days ahead, one adult, economy | yes |
| `hotel` | local fixture (`bench/fixture/`) | Search Lisbon, set two filters, open one hotel; an unauthorised "Send to a friend" must not be clicked | yes |
| `llm` | Wikipedia | "The capital city of France": a value the goal implies but does not contain | reported |
| `iframe` | local fixture | The hotel flow with the final "Reserve" in a cross-site iframe: a forced hand-back | reported |
| `private` | your own app (optional) | Loaded from the gitignored `.local/bench_private.json` if present | reported |

Arms (from `bench/arms.py`):

- **A**: Claude Code with the jev-browse skill, told to use it.
- **B**: Claude Code driving browser-harness directly (jev-browse disabled).
- **A0**: `fast_run` from a script, the library-only lower bound.
- **A-int**: only the interactive helpers.
- **A-llm, A0-llm, A-none**: the `llm` task with goal candidates off, to force the text backend or a hand-back.
- **A-llm-ollama, A0-llm-ollama**: the same, with the Ollama backend.
- **A-unprompted, A-fast**: adoption without a tool instruction, and with a "use fast_run" instruction.

Each attempt starts from a fresh blank tab. When it ends, a separate harness process reads the final tab (or the
fixture server's log) and checks it with code-only predicates: no LLM, no Jev. Cost is list-price-equivalent USD
from token usage (rates in `bench/pricing.json`), reported both cache-neutral and as billed. The verdict rule
(`bench/decide.py`) calls a task class a win for A when A is at least as fast, cheaper, and does not pass less.

### Requirements

- Everything in the README's quickstart, with `jev-browse doctor` passing.
- The `claude` CLI logged in (arms A, B, A-*). Each Claude attempt costs roughly $0.3–3 at list price. A full
  matrix is dozens of attempts.
- Chrome with remote debugging allowed, and a **named harness daemon `jevbench`**. The benchmark refuses to start
  its own (`BH_REQUIRE_EXISTING_DAEMON=1`), so start it once and approve Chrome's prompt:

  ```bash
  BU_NAME=jevbench browser-harness <<'PY'
  print(page_info())
  PY
  ```

- `BH_TELEMETRY=0` and `BH_UPDATE_CHECK=0` are set for you, and the fixture server starts on local ports.

### Running it

```bash
# one Claude attempt per arm on the hotel fixture (a smoke test)
PYTHONPATH=. python3 bench/run_bench.py --pilot

# the public matrix, N = 3 (long; run it in the background)
PYTHONPATH=. python3 bench/run_bench.py --tasks wiki,flights,hotel,llm,iframe --arms A,A-int,B,A0,A0-llm,A-llm,A-none --n 3

# just the local-backend arms, into their own rows file
PYTHONPATH=. python3 bench/run_bench.py --tasks llm --arms A-llm-ollama,A0-llm-ollama --n 3 \
    --rows bench/results/my_ollama_rows.jsonl

# tables for a report
python3 bench/report.py > /tmp/tables.md
```

Rows are appended to `--rows` (default `bench/results/rows.jsonl`), and the verdict is written next to them
(`summary.json`, or `<rows>.summary.json`). Raw per-attempt output (stream-json, run files, page reads) goes to
`bench/results/raw/`, which is gitignored because it contains page text.

### Adding your own private task

Create `.local/bench_private.json` (gitignored):

```json
{"url": "https://app.example.com/…", "goal": "…", "domain": "app.example.com",
 "verify": {"url_contains": ["…"], "url_not_contains": ["…"], "text_contains": ["…"]}}
```

Committed rows for this task keep only a generic label and metrics (`private_safe` in `run_bench.py`). Run it
with `JEV_BROWSE_TEXT_BACKEND=none` if the app's pages should not go to a text provider. They still go to
TypeSafe.

### Reading results fairly

- N = 3 per arm: expect run-to-run noise of tens of percent in wall time, especially for Claude arms.
- Cost is dominated by the calling model re-reading its context each turn, so a global context with many tools
  or skills makes both arms more expensive. That is why turns cut matters more than Jev's own cost (cents).
- Live sites change. A verifier that passed last month can fail on a redesign; check `raw/` before calling a
  regression.

## The README demo (`scripts/make_demo_gif.py`)

`docs/media/demo.gif` is a real benchmark pair on the `flights` task: arm B (the agent drives browser-harness) on
the left, arm A-fast (the agent calls `fast_run`) on the right, both played at the same speed-up with the real
elapsed time, the model-turn count, and the attempt's verified outcome and list-price cost. It was recorded on the
author's machine on 2026-09-25: three pairs, and the GIF shows the one whose left-hand time is the median.
Prices are in USD (`gl=US&curr=USD` is added to the Flights URL for the demo only).

```bash
PYTHONPATH=. python3 scripts/make_demo_gif.py record --task flights        # live; run it three times
uv run --with pillow python3 scripts/make_demo_gif.py review bench/results/raw/demo/<stamp> --out /tmp/review
uv run --with pillow python3 scripts/make_demo_gif.py render bench/results/raw/demo/<stamp-1> <stamp-2> <stamp-3>
```

- The recorder screenshots only the benchmark's own tabs, never the browser window, and crops the page header
  (Google's account avatar) off every frame. The page itself can still show account details, such as a dropdown of
  recent searches: review every frame and mask what you find in `<recording>/redact.json`.
- Screenshots of a background tab are not free. A first version at 5 captures a second made Google Flights stop
  answering on jev-browse's tab (`page unresponsive`) and made the right-hand run 4–5 times slower. The recorder now
  captures at most twice a second, backs off after a slow capture, logs every failed capture, and takes a final
  frame after the agent exits. `render` refuses a recording with a failed capture, no frames, no clean finish, or a
  last frame more than 2 s before the end of the run.
