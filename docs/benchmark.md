# Benchmark

**What was compared.** The same browser tasks, done two ways by the same coding agent (Claude Code):

- **with jev-browse:** the agent calls `fast_run` from a browser-harness script;
- **without it:** the agent drives [browser-harness](https://github.com/browser-use/browser-harness) directly,
  step by step.

The tasks used public sites and a local test page shipped in `bench/fixture/`. Every outcome was checked by code
reading the final page, with no model involved. Cost counts every model token at list price (the calling agent,
Jev, and any text backend), with all input tokens priced at the base input rate so prompt-cache luck doesn't move
the verdict.

**Headline.** In the author's runs, an agent using jev-browse was **1.6–3.0× faster and 2.1–5.3× cheaper** than
the same agent driving the browser directly, with the same pass rate. Most of the saving comes from fewer agent turns: the agent writes one
script instead of observing and acting turn by turn. Jev's own cost was a fraction of a cent per task.

**Your numbers will differ.** Your calling model, the size of your agent's context, your network, your hardware,
and the sites' current layouts all move these numbers. N = 3 runs per task and arm shows direction, not precise
effect sizes.

## The author's setup

| | |
|---|---|
| Measured | 2026-09-24, N = 3 runs per task and arm |
| Calling machine | MacBook Pro, Apple M2 Max (12 cores), 64 GB RAM, macOS 15.7 |
| Calling agent | Claude Code 2.1.281, model `claude-opus-5-5`, headless (`claude -p`) |
| Browser | Google Chrome, driven through browser-harness 0.1.13 |
| Jev | `jev-latest` (answered as `jev-1.13.0`) |
| Text backend: Claude | `claude -p --model haiku` (Claude Haiku 4.5) on a Claude subscription |
| Text backend: local | Ollama 0.33.3 on a separate mini PC: AMD Ryzen 7 255, Radeon 780M integrated GPU (ROCm), 58 GB RAM, Linux. Model `qwen3:30b-a3b` (Q4_K_M), thinking off, JSON output, temperature 0, one request at a time (`OLLAMA_NUM_PARALLEL=1`), reached over a LAN/VPN link |

## Results: five ways to run the same tasks

Median of 3 runs: wall time · list-price cost · passed. "Agent" is Claude Code driving the browser; with
jev-browse it calls `fast_run` directly (the [global pointer](global-pointer.md) is in its context). "Script" is
`fast_run` called from a plain Python script, with no agent at all. The text backend in brackets is only called when
a field needs a value that isn't in the goal.

| Task | Agent alone (harness directly) | Agent + jev-browse (Claude Haiku) | Agent + jev-browse (local Ollama) | Script (Claude Haiku) | Script (local Ollama) |
|---|---|---|---|---|---|
| Open a Wikipedia article from the Main Page | 32.9 s · $1.01 · 3/3 | 12.2 s · $0.29 · 3/3 | 15.1 s · $0.43 · 3/3 | 3.9 s · $0.003 · 3/3 | 4.3 s · $0.003 · 3/3 |
| Google Flights one-way search (dates, autocomplete) | 72.8 s · $2.34 · 3/3 | 24.7 s · $0.44 · 3/3 | 24.1 s · $0.45 · 3/3 | 10.9 s · $0.010 · 3/3 | 11.4 s · **$0.007** · 3/3 |
| Search, filter and open a listing (local fixture) | 69.5 s · $2.64 · 3/3 | 42.4 s · $1.23 · 3/3 | 38.8 s · $0.94 · 3/3 | 2.5 s · $0.001 · 1/3 | 3.5 s · $0.002 · 3/3 |
| A value the goal implies but doesn't contain | 29.6 s · $1.00 · 3/3 | 14.0 s · $0.29 · 3/3 | 15.3 s · $0.43 · 3/3 | 6.2 s · $0.004 · 0/3 | 5.2 s · $0.003 · 0/3 |
| Last step inside a cross-site iframe | 97.2 s · $3.19 · 3/3 | 44.8 s · $1.45 · 3/3 | 43.5 s · $1.26 · 3/3 | 2.7 s · $0.001 · 0/3 | 2.7 s · $0.001 · 0/3 |

How to read it:

- **Agent + jev-browse vs agent alone:** 1.6–3.0× faster and 2.1–5.3× cheaper, same pass rate. The saving is
  fewer agent turns (2–10 instead of 7–18).
- **Haiku vs Ollama as the text backend:** only the Flights task needed a generated value. There the text call cost
  $0.002–0.003 with Haiku and **$0** with Ollama, which made the script run **36% cheaper**. On every other task
  neither backend was called, so the two columns run identical code; their differences come from the agent taking a
  different number of turns (for example 3 against 2 on Wikipedia), not from the backend.
- **Script-only failures are hand-backs, by design:** jev-browse stops and returns a typed reason instead of
  guessing, for example a date dialog's "Done" button it isn't confident about, a goal-implied value with candidates
  switched off, or a step inside a cross-site iframe. An agent calling it picks up from there, which is why every
  agent column passes. The listing task's 1/3 against 3/3 is that date-dialog hand-back firing on some runs; no text
  backend was called in either column.

Jev decisions took a median of **290 ms** (p90 672 ms, over 335 decisions). Without any tool instruction in the
prompt, the agent chose jev-browse on its own in 2 of 5 tasks with the global pointer in its context, against 0 of
5 without it.

## Results: text backends

The text backend is only called when a field needs a value that jev-browse cannot take from the caller or pick
from the goal. `bench/text_eval.py`: 14 field cases × 2 runs.

| Backend | Field eval | Median | p90 |
|---|---|---|---|
| Local Ollama, `qwen3:30b-a3b` on the 780M | 28/28 | **0.75 s** | 1.02 s |
| Same, with a 2,000-character page excerpt in the prompt | 28/28 | 1.44 s | 2.2 s |
| Claude Haiku 4.5 via `claude -p` (cold) | 26/28 | 3.86 s | 5.69 s |

Inside real runs, on the task that needs a generated value, with goal candidates switched off so the text backend
must answer (median of 3):

| | Local Ollama | Claude Haiku |
|---|---|---|
| Text call inside a script run | **1.33 s · $0** | 3.2 s · $0.0022 |
| Text call inside an agent-driven run | **1.59 s · $0** | 4.0 s · $0.0024 |
| Whole task, script only | **6.1 s · $0.0034** | 9.4 s · $0.0056 |

A local call costs nothing and is 2–3× faster. When an agent drives, the text call is a small slice of the total,
because the agent's own model calls dominate the cost; the local backend saves the text call's time and cost, and
keeps page text on your own network.

## Reproduce it

Everything needed is in `bench/`: tasks, arms, a local fixture server, code-only verifiers, list-price costing,
and the decision rule. See [benchmarking.md](benchmarking.md) for requirements and commands:

```bash
PYTHONPATH=. python3 bench/run_bench.py --pilot                        # one attempt per arm, local fixture
PYTHONPATH=. python3 bench/run_bench.py --tasks wiki,flights,hotel --n 3
python3 bench/report.py bench/results/rows.jsonl                     # tables and verdict
```

Before trusting a text backend (a local model or any OpenAI-compatible endpoint), evaluate it on your own
hardware with `python3 bench/text_eval.py`. It runs 14 field cases in a few minutes, with no browser.

Results from other setups are welcome. See [CONTRIBUTING.md](../CONTRIBUTING.md#sharing-benchmark-results).
