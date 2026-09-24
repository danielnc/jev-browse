# AGENTS.md

Instructions for coding agents (Codex, Claude Code, and others) working **on** jev-browse. If you only want to
*use* jev-browse, read `install.md` and `skill/SKILL.md` instead.

## What this repo is

A standard-library-only Python package (`jev_browse/`) that browser-harness loads into every harness script,
plus the agent skill (`skill/`), a benchmark (`bench/`), and docs (`docs/`). Architecture: `docs/architecture.md`.

## Commands

```bash
uv sync                          # dev tools only (pytest, ruff); the runtime needs nothing
make check                       # ruff + pytest + node --check snapshot.js; must pass before every commit
make docs-gen                    # after changing a setting in jev_browse/config.py
make check-secrets               # gitleaks over the full history
python3 -m jev_browse doctor     # live check of a real install (network: one TypeSafe request, the canary)
python3 bench/text_eval.py       # live eval of the configured text backend
```

## Rules

- **Runtime is stdlib only.** No new dependencies in `jev_browse/`. Dev and benchmark tooling may use `uv`.
- **Tests are offline.** TypeSafe, the `claude`/`codex` CLIs, HTTP backends, and Chrome are faked (`tests/fakes.py`,
  stub HTTP servers). `tests/conftest.py` isolates tests from the developer's own config file and `JEV_BROWSE_*`
  variables: never rely on them. Write a failing test first for any behaviour change.
- **Settings go through the registry.** Add a `Setting` to `jev_browse/config.py`, read it with `config.get(key)`,
  never `os.environ` directly (except the environment-only names in `config.ENV_ONLY`), then run `make docs-gen`.
  Secrets are environment-only and never read from the config file.
- **Privacy is a feature.** Sensitive fields are never typed, read, or sent. Personal fields never go to a text
  backend. Private URLs (Ollama host, custom endpoints) never appear in errors, traces, or `doctor` output. Keep
  it that way and test it.
- **No personal data in the repo.** No real names, emails, hostnames, IPs, home paths, account names, or private
  app details in code, tests, docs, commit messages, or benchmark rows. Use public sites (Wikipedia, example.com)
  and `127.0.0.1` in examples. Raw benchmark output lives in gitignored `bench/results/raw/`.
- **Measured numbers are labelled.** Anything measured is "on the author's machine" (or yours), with N and date.
  Don't present a single setup's number as a property of the product. Unmeasured features say so.
- **Never weaken a safety gate to make a benchmark pass.** The commit gate, sensitive-field rules, and hand-back
  reasons are the contract with the calling agent.
- **Git:** work on a branch, small commits with conventional prefixes (`feat:`, `fix:`, `docs:`, `test:`,
  `bench:`), and never force-push shared branches. Commit with explicit paths when other work is in progress.

## Layout

| Path | What |
|---|---|
| `jev_browse/run.py` | `fast_run` and the decision loop |
| `jev_browse/actions.py`, `questions.py`, `candidates.py` | Jev questions, answer handling, goal-derived values |
| `jev_browse/tab.py`, `snapshot.js` | owned tabs over CDP; the in-page snapshot |
| `jev_browse/textgen.py` | text backends, canary, grounding gate |
| `jev_browse/typesafe.py` | the TypeSafe client |
| `jev_browse/interactive.py` | `jev_open` / `jev_adopt` / `jev_find` / `jev_click` / `jev_check` / `jev_close` |
| `jev_browse/config.py`, `doctor.py`, `install.py`, `__main__.py` | settings, diagnostics, installer, CLI |
| `skill/` | what the calling agent reads; keep it short and exact |
| `bench/` | end-to-end benchmark and `text_eval` (see `docs/benchmarking.md`) |
