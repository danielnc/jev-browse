# jev-browse

**Fast, typed browser sub-tasks for coding agents.** jev-browse adds a few helpers to
[browser-harness](https://github.com/browser-use/browser-harness) so that Claude Code or Codex can hand off a
whole website sub-task in one call: "search Lisbon, set these two filters, open Casa Flora". Each step is decided by
[TypeSafe](https://docs.typesafe.ai) Jev, a small model that answers typed questions in a few hundred milliseconds. The
agent then gets the outcome, or a typed reason why it stopped.

```python
# inside a browser-harness script
r = fast_run("https://en.wikipedia.org/wiki/Main_Page",
             "Open the Wikipedia article on Gödel's incompleteness theorems", run_id="demo-1")
print(r.status, r.reason, r.url)      # claimed_done None https://en.wikipedia.org/wiki/G%C3%B6del%27s_...
jev_close(r.target_id)
```

**Why.** When a coding agent drives a browser itself, each click costs a model turn that re-reads its whole
context. A five-field form becomes fifteen turns. `fast_run` does the clicking and typing in one call, with one Jev
request per decision, and hands back to the agent only when it must: a value it doesn't know, a risky click, a
frame or upload it can't handle, or anything sensitive. In the author's runs that made form and navigation tasks
**1.6–3.0× faster and 2.1–5.3× cheaper** than the agent driving browser-harness alone, with the same pass rate
([details](#benchmark)).

It is a port of [browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast) (MIT), turned into
helpers any agent can call from a browser-harness script.

## Quickstart

You need Chrome, [browser-harness](https://github.com/browser-use/browser-harness) connected to it, Python 3.11+,
and a **TypeSafe API key** from [console.typesafe.ai/keys](https://console.typesafe.ai/keys).

### With your coding agent (recommended)

Paste this into Claude Code or Codex:

```text
Install jev-browse from https://github.com/danielnc/jev-browse by following its install.md: clone it, run its
installer, store my TypeSafe API key in the browser-harness agent-workspace .env (ask me for it; never print it),
and run `python3 -m jev_browse doctor` until it passes. Ask me which text backend I want (default: my Claude
subscription if the claude CLI is installed) and whether to add the jev-browse pointer to my global agent
instructions.
```

### By hand (about a minute)

```bash
git clone https://github.com/danielnc/jev-browse ~/jev-browse && cd ~/jev-browse
python3 -m jev_browse install          # adds the helpers to browser-harness and links the skill
ENV=~/.config/browser-harness/agent-workspace/.env
printf 'TYPESAFE_API_KEY=%s\n' '<your key>' >> "$ENV" && chmod 600 "$ENV"
python3 -m jev_browse doctor           # checks everything and prints what is active
browser-harness <<'PY'
r = fast_run("https://en.wikipedia.org/wiki/Main_Page", "Open the Wikipedia article about the Eiffel Tower",
             run_id="hello-1")
print(r.status, r.url)
jev_close(r.target_id)
PY
```

Then add the [global pointer](#tell-your-agent-about-it) to your agent's instructions. Without it, agents
rarely think to use jev-browse on their own.

## How it works

```text
 your agent (Claude Code / Codex)
   │  writes one browser-harness script:  r = fast_run(url, goal, values={...})
   ▼
 browser-harness  ──CDP──►  Chrome: a new background tab owned by jev-browse
   │                           │
   │   ┌───────────────────────┘
   │   ▼
   │  loop:  snapshot the page (snapshot.js: visible text, controls, fields; sensitive values never read)
   │         → one TypeSafe Jev request: next operation? which target? which value?  ──►  api.typesafe.ai
   │         → value missing from the goal?  ask the text backend once (optional)   ──►  Claude / Codex /
   │         → safety gates: commit verbs, sensitive fields, frames, uploads, hosts        Ollama / any
   │         → click / type / select / scroll via CDP                                     OpenAI-compatible
   │  until DONE, BLOCKED, or a typed hand-back (confirm_required, in_frame, text_value_unavailable, …)
   ▼
 RunResult(status, reason, url, evidence, trace)  +  a JEV_BROWSE_RESULT={...} line to verify in the same script
```

- **One decision = one small Jev request** (typically a few hundred milliseconds). No screenshots and no large
  model in the loop.
- **Values** come from your `values=`, from the goal itself (Jev picks among candidates taken from the goal), or,
  only after a miss, from a text backend. A grounding gate checks the backend's answer before anything is typed.
- **It never guesses past its limits.** Too many options, frames, shadow DOM, canvas, uploads, missing values,
  sensitive fields, or a click that would send, pay, delete, or book all hand back with a `Reason`. The tab stays
  open for the agent to finish or resume.

More: [docs/architecture.md](docs/architecture.md).

## When to use it (and when not)

| Use `fast_run` for | Use browser-harness directly for |
|---|---|
| Multi-step forms and searches (fill, pick dates, filter, open a result) | Anything visual: charts, layouts, images, "does this look right" |
| Link navigation to a known destination | Iframes, shadow DOM, canvas, file uploads, drag and drop |
| "Find and click the X" steps (`jev_find` / `jev_click`) | Sensitive pages (banking, health, credentials) |
| Sub-tasks where you only need the outcome | Pages whose text you should not send to TypeSafe |

When `fast_run` hands back, the agent continues with the harness on the same tab, or resumes after supplying
what was missing: `fast_run(None, goal, target_id=r.target_id, values={...})`.

## Configuration

Zero config beyond `TYPESAFE_API_KEY`. Everything else is an environment variable (the harness `.env` counts) or
an entry in `~/.config/jev-browse/config.toml`. The environment wins. The settings you are most likely to change:

| Setting (`config.toml`) | Environment | Default |
|---|---|---|
| `text.backend` | `JEV_BROWSE_TEXT_BACKEND` | `auto`: `claude` if its CLI is installed, else `none` |
| `text.fallback` | `JEV_BROWSE_TEXT_FALLBACK` | `auto`: who answers when a local backend fails |
| `ollama.url`, `ollama.model` | `JEV_BROWSE_OLLAMA_URL`, `_MODEL` | unset, `qwen3:30b-a3b` |
| `openai.base_url`, `openai.model`, `openai.api_key_env` | `JEV_BROWSE_OPENAI_*` | unset |
| `jev.model` | `JEV_BROWSE_JEV_MODEL` | `jev-latest` |
| `run.max_actions`, `run.timeout_s` | `JEV_BROWSE_MAX_ACTIONS`, `JEV_BROWSE_TIMEOUT_S` | 30, 90 s |
| `safety.allowed_hosts` | `JEV_BROWSE_ALLOWED_HOSTS` | all hosts |

> **Privacy note on `text.fallback`.** With a local (`ollama`) or OpenAI-compatible backend, the default
> `text.fallback = "auto"` means: if that backend fails its known-answer check, is unreachable, or returns invalid
> output twice, **the `claude` CLI answers instead (when it is installed), so the goal, field labels, and page
> excerpt go to Anthropic.** If you chose a local model to keep page text on your machine, set
> `text.fallback = "none"` (`JEV_BROWSE_TEXT_FALLBACK=none`): the miss then hands back to your agent. `doctor`
> prints which fallback is active.

All settings, and common setups (privacy mode, local model, OpenRouter/Groq/Cerebras/Gemini):
[docs/configuration.md](docs/configuration.md). `python3 -m jev_browse config` shows what is active and where each
value came from.

## Text backends

A text backend is asked only when a field's value is implied but not stated ("the capital of France" → `Paris`).

| Backend | Text goes to | Cost | Trade-off |
|---|---|---|---|
| `claude` (default if installed) | Anthropic, via your Claude subscription | subscription only; API keys are stripped | a few seconds per miss (CLI start-up, mostly hidden by a pre-started process) |
| `codex` | OpenAI, via your ChatGPT subscription | subscription only | a few seconds of CLI start-up per miss; **not yet benchmarked** |
| `ollama` | your own server (and the fallback's provider if it fails; see `text.fallback`) | free per token | as fast as your hardware; guarded by a known-answer canary |
| `openai` | any OpenAI-compatible endpoint (and the fallback's provider if it fails) | provider prices | unmeasured; bring your own model |
| `none` | nowhere | free | misses hand back to the agent (privacy mode) |

Personal fields (name, email, phone, address) and sensitive fields are **never** sent to a text backend. Honest
numbers and set-up notes: [docs/backends.md](docs/backends.md).

## Safety and privacy

- **What leaves your machine.** On every decision, visible page text, element labels, non-personal field values,
  URL, and title go to **TypeSafe** (`api.typesafe.ai`). After a miss, the goal, field labels, and up to 2,000
  characters of page text go to your **text backend**, or to its fallback when a local backend fails (see
  `text.fallback` above). Use it only on pages you are comfortable sending there. Use
  `text_backend="none"` (or `text.backend = "none"`) for sensitive sites.
- **Sensitive fields** (passwords, one-time codes, card numbers, CVV, IBAN, national IDs) are never typed, never
  read out of the page, and never sent. Personal fields are reported only as filled or empty.
- **Commit gate.** Clicks that send, pay, delete, book, or confirm need the goal's explicit authorisation, or your
  `confirm=[...]` in a *later* script. Otherwise the run hands back `confirm_required`. This is a heuristic (verbs
  plus structural signals such as a confirmation inside a dialog): it lowers the risk of an unwanted click in your
  signed-in browser. It does not remove it.
- **Owned tabs.** jev-browse works only in background tabs it created (or that your `new_tab()` created and you
  adopted). It never touches, focuses, or closes your other tabs.
- **Hosts.** `safety.allowed_hosts` restricts every page jev-browse opens, adopts, or observes.
- **No telemetry in jev-browse.** browser-harness has its own telemetry, which sends script text and helper-call
  arguments to PostHog when enabled. The installer and `doctor` warn if it is on. Opt out with
  `browser-harness telemetry disable`.
- **Local data.** Run files, traces, and screenshots can contain page text and typed values. They stay in the
  harness tmp dir and other gitignored paths, and `make clean-traces` removes them.

## Tell your agent about it

A skill that never triggers delivers nothing. With only the skill installed, the calling agent used jev-browse in
**0 of 5** unprompted tasks in the author's runs. After a short pointer in the global instructions it used it in
**2 of 5**, and the explicit `fast_run` call shape saved a turn per task. Paste this into `~/.claude/CLAUDE.md`,
`~/.codex/AGENTS.md`, or your project's agent file:

```markdown
## Browser tasks: jev-browse fast path
For a multi-step website sub-task (search, fill, filter, open a result) where you only need the outcome, call
jev-browse from a browser-harness script. You don't need to load its skill first:

    r = fast_run(url, goal, values={...known field values...}, run_id="<unique>")
    print(r.status, r.reason, r.detail, r.target_id)

- `claimed_done` is not proof: check the printed JEV_BROWSE_RESULT line, or `js("...", target_id=r.target_id)`,
  in the same script, then `jev_close(r.target_id)`.
- On a hand-back (`r.reason`), load the jev-browse skill for what to do next. Resume on the same tab with
  `fast_run(None, <same goal>, target_id=r.target_id, values={...})`; never re-run from the URL.
- Never act on `confirm_required` in the same script: decide first (ask me if my request does not clearly cover
  it), then resume with `confirm=[...]`.
- Use browser-harness directly for visual judgement, frames, uploads, and sensitive pages. Page text goes to
  TypeSafe; pass `text_backend="none"` on sensitive sites.
- Close only tab ids that jev-browse returned to you.
```

The same snippet is in [docs/global-pointer.md](docs/global-pointer.md).

## Use it from any MCP client

Cursor, Claude Desktop, Codex, Windsurf, and other MCP clients can use jev-browse through its MCP server, which runs
on stdio. Each tool call goes through browser-harness exactly as a script would, so ownership, config, and
hand-backs are unchanged:

```bash
uv run --directory /path/to/jev-browse --extra mcp python -m jev_browse mcp
```

Tools: `fast_run`, `fast_run_status`, `jev_open`, `jev_find`, `jev_click`, `jev_check`, `jev_close`, `doctor`. A
`fast_run` that outlasts the client's tool timeout returns a `run_id` to resume with `fast_run_status`. Config
snippets for Claude Desktop, Cursor, and Codex are in [docs/mcp.md](docs/mcp.md).

## Limitations

- No iframes, shadow DOM, canvas or visual understanding, file uploads, or pop-up tabs: these hand back.
- Pages with very many options or very large state hand back (`too_many_options`, `state_too_large`).
- Decisions are text-only. A page whose meaning is visual will confuse it, and it will usually say so
  (`visual_only`, `low_confidence`).
- Every decision is a metered TypeSafe request: a few tenths of a cent per task at the time of writing.
- The commit gate and the grounding gate are heuristics. Review what a run did (`r.trace`) on anything that
  matters.
- The Codex and OpenAI-compatible backends are unmeasured.
- Tested with browser-harness 0.1.13 on macOS with Chrome. Other platforms should work but have not been
  benchmarked.

## Benchmark

jev-browse was compared with the same coding agent driving browser-harness directly, on public-site navigation,
form, and filter tasks, with every outcome verified by code. In the author's runs it was **1.6–3.0× faster and
2.1–5.3× cheaper**, with the same pass rate (N = 3 each), mostly because the agent needs far fewer turns. Your
numbers will differ. Summary: [docs/benchmark.md](docs/benchmark.md). Reproduce it, or evaluate your own text
backend, with `bench/` ([docs/benchmarking.md](docs/benchmarking.md)).

## Documentation

- [install.md](install.md): step-by-step install, written for an agent to follow
- [docs/architecture.md](docs/architecture.md): the decision loop, modules, and safety model
- [docs/configuration.md](docs/configuration.md): every setting
- [docs/backends.md](docs/backends.md): text backends and their trade-offs
- [docs/benchmarking.md](docs/benchmarking.md): running the benchmark and `text_eval`
- [docs/mcp.md](docs/mcp.md): the MCP server and client configuration
- [skill/SKILL.md](skill/SKILL.md) and [skill/reference.md](skill/reference.md): what the agent reads

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Development is offline by default: `uv sync && make check` runs the
linter and more than 400 tests with TypeSafe, the CLIs, and Chrome mocked.

## License

MIT, see [LICENSE](LICENSE). jev-browse ports code from
[browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast) (MIT, Copyright (c) 2026 Browser Use);
see [NOTICE](NOTICE). Ported files carry a header comment naming the upstream file.
