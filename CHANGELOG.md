# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `jev-browse mcp` (`python -m jev_browse mcp`): an MCP server on stdio for Cursor, Claude Desktop, Codex, and other
  MCP clients. Tools: `fast_run`, `fast_run_status`, `jev_open`, `jev_find`, `jev_click`, `jev_check`, `jev_close`,
  `doctor`. Each call runs through the `browser-harness` CLI with harness telemetry off. Results are concise text
  with a `next:` instruction. A `fast_run` longer than `mcp.wait_s` returns a resumable `run_id`. The MCP SDK is
  the optional `mcp` extra; the harness runtime stays stdlib-only. See `docs/mcp.md`.
- Settings `mcp.harness_command` and `mcp.wait_s`.

## [0.1.0] - 2026-09-24

### Added
- `fast_run(url, goal, ...)`: a browser sub-task on an owned background tab, one TypeSafe Jev request per decision,
  with typed hand-backs (`Reason`), resume on the same tab, a commit gate, evidence, a trace, and a verify-ready
  `JEV_BROWSE_RESULT` line.
- Interactive helpers: `jev_open`, `jev_adopt`, `jev_find`, `jev_click`, `jev_check`, `jev_close`.
- Values from the caller, from goal-derived candidates chosen by Jev, or from a text backend after a miss, with a
  grounding gate. Personal fields are never sent to a text backend. Sensitive fields are never typed, read, or
  sent.
- Text backends: `claude` (subscription CLI, pre-started process), `codex` (subscription CLI, unmeasured),
  `ollama`, `openai` (any OpenAI-compatible endpoint), and `none`. Local and OpenAI-compatible backends run a
  known-answer canary, cached across processes, and a configurable fallback.
- One configuration surface: environment variables (including the browser-harness `.env`) over
  `~/.config/jev-browse/config.toml` over defaults (`docs/configuration.md`).
- `python3 -m jev_browse install | uninstall | doctor | config`, and `bin/jev-browse`. The installer links the
  skill for Claude Code and Codex.
- The `jev-browse` agent skill, an agent-followable `install.md`, and a global-pointer snippet.
- A benchmark (`bench/run_bench.py`) with code-only verification and list-price cost, and a text-backend eval
  (`bench/text_eval.py`).

Ports code from [browser-use/jev-ultrafast](https://github.com/browser-use/jev-ultrafast) (MIT).
